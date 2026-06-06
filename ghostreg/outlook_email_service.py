"""
Outlook / Microsoft 邮箱服务
支持三种读邮件方式（自动选最优，逐级回退）：
  1. IMAP XOAUTH2（OAuth 令牌登 IMAP）— 最适合 4 段凭证
  2. Microsoft Graph API             — 次选
  3. IMAP Basic Auth                 — 2 段凭证兜底

凭证格式：
  4 段：email----password----client_id----refresh_token
  2 段：email----password  /  email:password

用于接收 JetBrains 注册 OTP 验证码。
"""
import base64
import email
import email.header
import email.message
import imaplib
import logging
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger("ghostreg.outlook_email")

# ── IMAP 服务器 ──
IMAP_HOST_OUTLOOK = "imap-mail.outlook.com"
IMAP_HOST_OFFICE365 = "outlook.office365.com"
IMAP_PORT = 993
_PERSONAL_DOMAINS = {
    "outlook.com", "hotmail.com", "live.com",
    "outlook.cn", "hotmail.cn", "live.cn", "msn.com",
}

# ── OAuth scopes ──
_SCOPES_IMAP = [
    # 个人账号 IMAP OAuth 标准 scope
    "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    # 企业账号 IMAP scope
    "https://outlook.office365.com/IMAP.AccessAsUser.All offline_access",
    # 有时需要 openid
    "openid https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
]
_SCOPES_GRAPH = [
    "https://graph.microsoft.com/Mail.Read offline_access",
    "https://graph.microsoft.com/.default offline_access",
]

# ── Token 端点 ──
_TOKEN_ENDPOINTS = [
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    "https://login.live.com/oauth20_token.srf",           # MSA legacy
]


# ══════════════════════════════════════════════
#  凭证解析
# ══════════════════════════════════════════════

def parse_credentials_text(text: str) -> list[dict]:
    """
    解析多行凭证：
      4 段：email----password----client_id----refresh_token
      2 段：email----password 或 email:password 或 email password
    """
    results = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) >= 4:
            em, pw = parts[0].strip(), parts[1].strip()
            cid, rt = parts[2].strip(), parts[3].strip()
            if "@" in em and rt:
                results.append({"email": em, "password": pw, "client_id": cid, "refresh_token": rt})
                continue
        if len(parts) == 2:
            em, pw = parts[0].strip(), parts[1].strip()
            if "@" in em and pw:
                results.append({"email": em, "password": pw})
                continue
        if ":" in line and line.count(":") == 1:
            em, pw = line.split(":", 1)
            em, pw = em.strip(), pw.strip()
            if "@" in em and pw:
                results.append({"email": em, "password": pw})
                continue
        if "\t" in line:
            p = line.split("\t", 1)
            em, pw = p[0].strip(), p[1].strip()
            if "@" in em and pw:
                results.append({"email": em, "password": pw})
                continue
        if " " in line:
            p = line.split(None, 1)
            if len(p) == 2:
                em, pw = p[0].strip(), p[1].strip()
                if "@" in em and pw:
                    results.append({"email": em, "password": pw})
    return results


# ══════════════════════════════════════════════
#  OAuth: refresh token → access token
# ══════════════════════════════════════════════

def _try_refresh(client_id: str, refresh_token: str,
                 scope: str, endpoint: str) -> str | None:
    """尝试单个 scope + endpoint 换 access_token，失败返回 None"""
    try:
        resp = httpx.post(
            endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
                "scope": scope,
            },
            timeout=20,
            verify=False,
        )
        if resp.status_code == 200:
            token = resp.json().get("access_token", "")
            if token:
                logger.debug(f"Token refresh OK: {endpoint} scope={scope[:40]}")
                return token
        logger.debug(f"Token refresh {resp.status_code} @ {endpoint}: {resp.text[:120]}")
    except Exception as e:
        logger.debug(f"Token refresh error @ {endpoint}: {e}")
    return None


def refresh_access_token(client_id: str, refresh_token: str,
                         prefer_scope: str = "") -> tuple[str, str]:
    """
    尝试多个 scope + endpoint 组合，返回第一个成功的 (access_token, scope_used)。
    抛出 RuntimeError 如果全部失败。
    """
    scopes_to_try = []
    if prefer_scope:
        scopes_to_try.append(prefer_scope)
    scopes_to_try.extend(_SCOPES_IMAP)
    scopes_to_try.extend(_SCOPES_GRAPH)
    # 也试一次不带 scope（继承原 scope）
    scopes_to_try.append("")

    seen = set()
    for scope in scopes_to_try:
        if scope in seen:
            continue
        seen.add(scope)
        for endpoint in _TOKEN_ENDPOINTS:
            at = _try_refresh(client_id, refresh_token, scope, endpoint)
            if at:
                return at, scope
    raise RuntimeError("所有 token 端点和 scope 均无法换取 access_token，请检查 refresh_token 是否过期")


# ══════════════════════════════════════════════
#  IMAP XOAUTH2（OAuth 令牌登录 IMAP）
# ══════════════════════════════════════════════

def _xoauth2_string(email_addr: str, access_token: str) -> bytes:
    # imaplib.authenticate() 内部会再做一次 base64，所以这里只返回原始字节
    raw = f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01"
    return raw.encode()


def connect_imap_xoauth2(email_addr: str, access_token: str) -> imaplib.IMAP4_SSL:
    """用 XOAUTH2 机制（Bearer token）登录 IMAP"""
    host = _get_imap_host(email_addr)
    logger.info(f"IMAP XOAUTH2 连接: {host}")
    conn = imaplib.IMAP4_SSL(host, IMAP_PORT)
    xoauth = _xoauth2_string(email_addr, access_token)
    status, resp = conn.authenticate("XOAUTH2", lambda x: xoauth)
    if status != "OK":
        raise RuntimeError(f"IMAP XOAUTH2 认证失败: {resp}")
    logger.info(f"IMAP XOAUTH2 登录成功: {email_addr}")
    return conn


# ══════════════════════════════════════════════
#  IMAP 通用操作（Basic Auth + XOAUTH2 均可用）
# ══════════════════════════════════════════════

def _get_imap_host(email_addr: str) -> str:
    domain = email_addr.split("@")[-1].lower() if "@" in email_addr else ""
    return IMAP_HOST_OUTLOOK if domain in _PERSONAL_DOMAINS else IMAP_HOST_OFFICE365


def connect_imap_basic(email_addr: str, password: str) -> imaplib.IMAP4_SSL:
    """基础 IMAP 登录（用户名+密码）"""
    host = _get_imap_host(email_addr)
    logger.info(f"IMAP Basic 连接: {host}")
    conn = imaplib.IMAP4_SSL(host, IMAP_PORT)
    status, resp = conn.login(email_addr, password)
    if status != "OK":
        raise RuntimeError(f"IMAP 登录失败: {resp}")
    return conn


def disconnect_imap(conn: imaplib.IMAP4_SSL):
    try:
        conn.logout()
    except Exception:
        pass


def get_imap_uid_snapshot(conn: imaplib.IMAP4_SSL) -> set[bytes]:
    conn.select("INBOX", readonly=True)
    status, data = conn.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        return set()
    return set(data[0].split())


def get_imap_new_messages(conn: imaplib.IMAP4_SSL, known_uids: set[bytes]) -> list[dict]:
    conn.select("INBOX", readonly=True)
    status, data = conn.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        return []
    new_uids = set(data[0].split()) - known_uids
    results = []
    for uid in new_uids:
        try:
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, bytes):
                continue
            msg = email.message_from_bytes(raw)
            subject = _decode_header(msg.get("Subject", ""))
            content = _extract_imap_body(msg)
            results.append({"subject": subject, "content": content})
        except Exception as e:
            logger.warning(f"读取 IMAP 邮件失败: {e}")
    return results


def _decode_header(raw) -> str:
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def _extract_imap_body(msg: email.message.Message) -> str:
    html_parts, text_parts = [], []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if "attachment" in (part.get("Content-Disposition") or ""):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            (html_parts if ct == "text/html" else text_parts).append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            (html_parts if msg.get_content_type() == "text/html" else text_parts).append(decoded)
    return "".join(html_parts) or "".join(text_parts)


# ══════════════════════════════════════════════
#  Microsoft Graph API（次选）
# ══════════════════════════════════════════════

_GRAPH_MSG_URL = "https://graph.microsoft.com/v1.0/me/messages"


def _graph_get(url: str, access_token: str, params: dict) -> dict:
    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
        verify=False,
    )
    resp.raise_for_status()
    return resp.json()


def get_graph_msg_ids(access_token: str, top: int = 50) -> set[str]:
    data = _graph_get(_GRAPH_MSG_URL, access_token,
                      {"$top": top, "$select": "id", "$orderby": "receivedDateTime desc"})
    return {m["id"] for m in data.get("value", [])}


def get_graph_new_messages(access_token: str, known_ids: set[str], top: int = 20) -> list[dict]:
    data = _graph_get(_GRAPH_MSG_URL, access_token,
                      {"$top": top, "$select": "id,subject,body",
                       "$orderby": "receivedDateTime desc"})
    result = []
    for m in data.get("value", []):
        if m["id"] not in known_ids:
            body = m.get("body", {}).get("content", "")
            result.append({"id": m["id"], "subject": m.get("subject", ""), "content": body})
    return result


# ══════════════════════════════════════════════
#  统一快照 / 轮询接口（自动选方式）
# ══════════════════════════════════════════════

def get_snapshot(cred: dict) -> dict:
    """
    在触发 OTP 之前调用：获取当前收件箱邮件快照。
    优先级: IMAP XOAUTH2 > Graph API > IMAP Basic
    返回 snapshot dict，传给 poll_otp()。
    """
    if cred.get("refresh_token") and cred.get("client_id"):
        # ── 方式 1：IMAP XOAUTH2 ──
        try:
            at, scope = refresh_access_token(
                cred["client_id"], cred["refresh_token"],
                prefer_scope="https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
            )
            conn = connect_imap_xoauth2(cred["email"], at)
            uids = get_imap_uid_snapshot(conn)
            disconnect_imap(conn)
            logger.info(f"快照方式: IMAP XOAUTH2，已有 {len(uids)} 封邮件")
            return {
                "method": "imap_xoauth2",
                "access_token": at,
                "scope": scope,
                "known_uids": uids,
                "client_id": cred["client_id"],
                "refresh_token": cred["refresh_token"],
                "email": cred["email"],
            }
        except Exception as e:
            logger.warning(f"IMAP XOAUTH2 快照失败，尝试 Graph API: {e}")

        # ── 方式 2：Graph API ──
        try:
            at, scope = refresh_access_token(
                cred["client_id"], cred["refresh_token"],
                prefer_scope="https://graph.microsoft.com/Mail.Read offline_access",
            )
            ids = get_graph_msg_ids(at)
            logger.info(f"快照方式: Graph API，已有 {len(ids)} 封邮件")
            return {
                "method": "graph",
                "access_token": at,
                "scope": scope,
                "known_ids": ids,
                "client_id": cred["client_id"],
                "refresh_token": cred["refresh_token"],
            }
        except Exception as e:
            logger.warning(f"Graph API 快照失败，尝试 IMAP Basic: {e}")

    # ── 方式 3：IMAP Basic Auth ──
    conn = connect_imap_basic(cred["email"], cred["password"])
    uids = get_imap_uid_snapshot(conn)
    disconnect_imap(conn)
    logger.info(f"快照方式: IMAP Basic，已有 {len(uids)} 封邮件")
    return {"method": "imap_basic", "known_uids": uids}


def poll_otp(cred: dict, snapshot: dict,
             timeout: float = 120.0, interval: float = 5.0) -> str:
    """轮询等待 OTP，根据 snapshot['method'] 自动选择方式"""
    method = snapshot["method"]

    if method == "imap_xoauth2":
        return _poll_imap_xoauth2(cred, snapshot, timeout, interval)
    elif method == "graph":
        return _poll_graph(snapshot, timeout, interval)
    else:
        return _poll_imap_basic(cred, snapshot, timeout, interval)


def _poll_imap_xoauth2(cred: dict, snapshot: dict,
                       timeout: float, interval: float) -> str:
    at = snapshot["access_token"]
    known_uids = snapshot["known_uids"]
    deadline = time.time() + timeout
    attempt = 0
    conn = None
    try:
        conn = connect_imap_xoauth2(snapshot["email"], at)
        while time.time() < deadline:
            attempt += 1
            try:
                msgs = get_imap_new_messages(conn, known_uids)
            except imaplib.IMAP4.abort:
                logger.warning("IMAP XOAUTH2 连接断开，重连中...")
                try:
                    conn.logout()
                except Exception:
                    pass
                try:
                    at, _ = refresh_access_token(
                        snapshot["client_id"], snapshot["refresh_token"],
                        prefer_scope=snapshot.get("scope", ""),
                    )
                    conn = connect_imap_xoauth2(snapshot["email"], at)
                except Exception as e:
                    logger.warning(f"重连失败: {e}")
                time.sleep(interval)
                continue
            except Exception as e:
                logger.warning(f"第{attempt}次 IMAP XOAUTH2 轮询异常: {e}")
                time.sleep(interval)
                continue

            for m in msgs:
                code = extract_otp_from_content(m["subject"] + " " + m["content"])
                if code:
                    logger.info(f"[IMAP XOAUTH2] 第{attempt}次获取 OTP: {code}")
                    return code
            logger.debug(f"[IMAP XOAUTH2] 第{attempt}次，新邮件={len(msgs)}")
            time.sleep(interval)
    finally:
        if conn:
            disconnect_imap(conn)
    raise TimeoutError(f"OTP 等待超时 ({timeout}s) [IMAP XOAUTH2]")


def _poll_graph(snapshot: dict, timeout: float, interval: float) -> str:
    at = snapshot["access_token"]
    known_ids = snapshot["known_ids"]
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            msgs = get_graph_new_messages(at, known_ids)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                try:
                    at, _ = refresh_access_token(
                        snapshot["client_id"], snapshot["refresh_token"],
                        prefer_scope=snapshot.get("scope", ""),
                    )
                    msgs = get_graph_new_messages(at, known_ids)
                except Exception as e2:
                    logger.warning(f"第{attempt}次 Graph token 续期失败: {e2}")
                    time.sleep(interval)
                    continue
            else:
                logger.warning(f"第{attempt}次 Graph 错误: {e}")
                time.sleep(interval)
                continue
        except Exception as e:
            logger.warning(f"第{attempt}次 Graph 异常: {e}")
            time.sleep(interval)
            continue

        for m in msgs:
            code = extract_otp_from_content(m["subject"] + " " + m["content"])
            if code:
                logger.info(f"[Graph] 第{attempt}次获取 OTP: {code}")
                return code
        logger.debug(f"[Graph] 第{attempt}次，新邮件={len(msgs)}")
        time.sleep(interval)
    raise TimeoutError(f"OTP 等待超时 ({timeout}s) [Graph]")


def _poll_imap_basic(cred: dict, snapshot: dict,
                     timeout: float, interval: float) -> str:
    known_uids = snapshot["known_uids"]
    deadline = time.time() + timeout
    attempt = 0
    conn = connect_imap_basic(cred["email"], cred["password"])
    try:
        while time.time() < deadline:
            attempt += 1
            try:
                msgs = get_imap_new_messages(conn, known_uids)
            except imaplib.IMAP4.abort:
                logger.warning("IMAP Basic 连接断开，重连中...")
                try:
                    conn.logout()
                except Exception:
                    pass
                conn = connect_imap_basic(cred["email"], cred["password"])
                time.sleep(interval)
                continue
            except Exception as e:
                logger.warning(f"第{attempt}次 IMAP Basic 轮询异常: {e}")
                time.sleep(interval)
                continue

            for m in msgs:
                code = extract_otp_from_content(m["subject"] + " " + m["content"])
                if code:
                    logger.info(f"[IMAP Basic] 第{attempt}次获取 OTP: {code}")
                    return code
            logger.debug(f"[IMAP Basic] 第{attempt}次，新邮件={len(msgs)}")
            time.sleep(interval)
    finally:
        disconnect_imap(conn)
    raise TimeoutError(f"OTP 等待超时 ({timeout}s) [IMAP Basic]")


# ══════════════════════════════════════════════
#  连接测试
# ══════════════════════════════════════════════

def test_connection(email_addr: str, password: str = "",
                    client_id: str = "", refresh_token: str = "") -> dict:
    """
    测试连接，按优先级尝试所有方式，返回第一个成功的结果。
    """
    if refresh_token and client_id:
        # 1. 尝试 IMAP XOAUTH2
        try:
            at, scope = refresh_access_token(
                client_id, refresh_token,
                prefer_scope="https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
            )
            conn = connect_imap_xoauth2(email_addr, at)
            conn.select("INBOX", readonly=True)
            disconnect_imap(conn)
            return {
                "ok": True,
                "message": f"IMAP XOAUTH2 连接成功（scope: {scope[:50]}...）",
                "method": "imap_xoauth2",
            }
        except Exception as e1:
            xoauth_err = str(e1)  # Python 3 会在 except 块结束后删除 e1，提前保存
            logger.info(f"IMAP XOAUTH2 测试失败: {xoauth_err}，尝试 Graph API...")

        # 2. 尝试 Graph API
        try:
            at, scope = refresh_access_token(
                client_id, refresh_token,
                prefer_scope="https://graph.microsoft.com/Mail.Read offline_access",
            )
            ids = get_graph_msg_ids(at, top=1)
            return {
                "ok": True,
                "message": f"Graph API 连接成功（收件箱有邮件 {len(ids)} 封可见）",
                "method": "graph",
            }
        except Exception as e2:
            return {
                "ok": False,
                "message": f"IMAP XOAUTH2 失败: {xoauth_err[:80]}；Graph 失败: {str(e2)[:80]}",
                "method": "none",
            }

    # 3. IMAP Basic Auth
    host = _get_imap_host(email_addr)
    try:
        conn = connect_imap_basic(email_addr, password)
        conn.select("INBOX", readonly=True)
        disconnect_imap(conn)
        return {"ok": True, "message": f"IMAP Basic 连接成功 ({host})", "method": "imap_basic"}
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg or "authentication failed" in msg.lower():
            hint = "密码错误，或账号未开启 IMAP / 需要「应用密码」"
        else:
            hint = f"IMAP 错误: {msg}"
        return {"ok": False, "message": hint, "method": "imap_basic"}
    except Exception as e:
        return {"ok": False, "message": f"连接失败: {e}", "method": "imap_basic"}


# ══════════════════════════════════════════════
#  OTP 提取（通用）
# ══════════════════════════════════════════════

def extract_otp_from_content(content: str) -> Optional[str]:
    """从邮件内容提取 JetBrains 6位 OTP 验证码"""
    patterns = [
        r'font-weight:\s*bold[^>]*>\s*(\d{6})\s*<',
        r'font-size:\s*24px[^>]*>\s*(\d{6})\s*<',
        r'(?:verification|confirm|code|验证码)\s*(?:is|:)?\s*(\d{6})',
        r'>\s*(\d{6})\s*<',
    ]
    for pattern in patterns:
        m = re.search(pattern, content, re.IGNORECASE)
        if m:
            return m.group(1)
    clean = re.sub(r'<[^>]+>', ' ', content)
    clean = re.sub(r'#[0-9a-fA-F]{6}', '', clean)
    clean = re.sub(r'\d{7,}', '', clean)
    m = re.search(r'(?<!\d)(\d{6})(?!\d)', clean)
    if m:
        return m.group(1)
    return None
