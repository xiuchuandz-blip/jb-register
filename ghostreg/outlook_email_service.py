"""
Outlook / Microsoft 邮箱服务
支持两种认证方式：
  1. OAuth Refresh Token（推荐）：email----password----client_id----refresh_token
     → 使用 Microsoft Graph API 读取邮件，无需开启 IMAP
  2. Basic IMAP（兜底）：email----password
     → 使用 imaplib 直接连接，需要在 Microsoft 账户里开启 IMAP

用于接收 JetBrains 注册 OTP 验证码。
"""
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

# ── Microsoft OAuth / Graph API 常量 ──
_MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"

# ── IMAP 常量 ──
IMAP_HOST_OUTLOOK = "imap-mail.outlook.com"
IMAP_HOST_OFFICE365 = "outlook.office365.com"
IMAP_PORT = 993
_PERSONAL_DOMAINS = {
    "outlook.com", "hotmail.com", "live.com",
    "outlook.cn", "hotmail.cn", "live.cn", "msn.com",
}


# ══════════════════════════════════════════════
#  凭证解析
# ══════════════════════════════════════════════

def parse_credentials_text(text: str) -> list[dict]:
    """
    解析多行凭证文本，支持两种格式：

    4段（OAuth，推荐）：
      email----password----client_id----refresh_token

    2段（Basic IMAP）：
      email----password
      email:password
      email password

    返回：[{"email", "password", "client_id"?, "refresh_token"?}]
    """
    results = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("----")
        if len(parts) >= 4:
            # 4段 OAuth 格式
            em = parts[0].strip()
            pw = parts[1].strip()
            cid = parts[2].strip()
            rt = parts[3].strip()
            if "@" in em and rt:
                results.append({
                    "email": em,
                    "password": pw,
                    "client_id": cid,
                    "refresh_token": rt,
                })
                continue

        if len(parts) == 2:
            # 2段 email----password
            em, pw = parts[0].strip(), parts[1].strip()
            if "@" in em and pw:
                results.append({"email": em, "password": pw})
                continue

        # 尝试其他分隔符
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
            em, pw = p[0].strip(), (p[1].strip() if len(p) > 1 else "")
            if "@" in em and pw:
                results.append({"email": em, "password": pw})

    return results


# ══════════════════════════════════════════════
#  Microsoft Graph API 方式（OAuth Refresh Token）
# ══════════════════════════════════════════════

def refresh_access_token(client_id: str, refresh_token: str) -> str:
    """
    用 refresh_token 换取新的 access_token。
    自动尝试 common 和 consumers 端点。
    """
    endpoints = [
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    ]
    last_err = None
    for url in endpoints:
        try:
            resp = httpx.post(
                url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                },
                timeout=30,
                verify=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("access_token", "")
                if token:
                    logger.debug(f"Token refresh 成功 via {url}")
                    return token
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"Token refresh 失败: {last_err}")


def get_graph_msg_ids(access_token: str, top: int = 50) -> set[str]:
    """获取 Graph API 邮件 ID 集合（用于快照对比）"""
    resp = httpx.get(
        _GRAPH_MESSAGES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"$top": top, "$select": "id", "$orderby": "receivedDateTime desc"},
        timeout=30,
        verify=False,
    )
    resp.raise_for_status()
    return {m["id"] for m in resp.json().get("value", [])}


def get_graph_new_messages(access_token: str, known_ids: set[str], top: int = 20) -> list[dict]:
    """获取 known_ids 之后新到的邮件（subject + body）"""
    resp = httpx.get(
        _GRAPH_MESSAGES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "$top": top,
            "$select": "id,subject,body",
            "$orderby": "receivedDateTime desc",
        },
        timeout=30,
        verify=False,
    )
    resp.raise_for_status()
    msgs = resp.json().get("value", [])
    new = []
    for m in msgs:
        if m["id"] not in known_ids:
            body = m.get("body", {})
            content = body.get("content", "")
            new.append({"id": m["id"], "subject": m.get("subject", ""), "content": content})
    return new


def get_snapshot_graph(client_id: str, refresh_token: str) -> tuple[str, set[str]]:
    """
    获取当前收件箱邮件 ID 快照。
    返回 (access_token, id_set) — access_token 供后续轮询复用。
    """
    at = refresh_access_token(client_id, refresh_token)
    ids = get_graph_msg_ids(at)
    logger.info(f"Graph API 快照: {len(ids)} 封已有邮件")
    return at, ids


def poll_for_otp_graph(
    client_id: str,
    refresh_token: str,
    known_ids: set[str],
    access_token: str,
    timeout: float = 120.0,
    interval: float = 5.0,
) -> str:
    """
    通过 Graph API 轮询新邮件并提取 JetBrains OTP 验证码。
    access_token 过期时自动用 refresh_token 重新换取。
    """
    deadline = time.time() + timeout
    attempt = 0
    at = access_token

    while time.time() < deadline:
        attempt += 1
        try:
            new_msgs = get_graph_new_messages(at, known_ids)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.info("access_token 过期，重新 refresh...")
                try:
                    at = refresh_access_token(client_id, refresh_token)
                    new_msgs = get_graph_new_messages(at, known_ids)
                except Exception as re2:
                    logger.warning(f"第{attempt}次 token refresh 失败: {re2}")
                    time.sleep(interval)
                    continue
            else:
                logger.warning(f"第{attempt}次 Graph API 错误: {e}")
                time.sleep(interval)
                continue
        except Exception as e:
            logger.warning(f"第{attempt}次 Graph API 异常: {e}")
            time.sleep(interval)
            continue

        for m in new_msgs:
            full = m["subject"] + " " + m["content"]
            code = extract_otp_from_content(full)
            if code:
                logger.info(f"第{attempt}次轮询获取 OTP: {code}")
                return code

        logger.debug(f"第{attempt}次 Graph API 轮询，新邮件数={len(new_msgs)}，未找到 OTP")
        time.sleep(interval)

    raise TimeoutError(f"等待 OTP 超时 ({timeout}s)")


# ══════════════════════════════════════════════
#  IMAP Basic Auth 方式（兜底）
# ══════════════════════════════════════════════

def _get_imap_host(email_addr: str) -> str:
    domain = email_addr.split("@")[-1].lower() if "@" in email_addr else ""
    return IMAP_HOST_OUTLOOK if domain in _PERSONAL_DOMAINS else IMAP_HOST_OFFICE365


def connect_imap(email_addr: str, password: str) -> imaplib.IMAP4_SSL:
    host = _get_imap_host(email_addr)
    logger.info(f"IMAP 连接: {host}:993")
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
            if ct == "text/html":
                html_parts.append(decoded)
            elif ct == "text/plain":
                text_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(decoded)
            else:
                text_parts.append(decoded)
    return "".join(html_parts) or "".join(text_parts)


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


def poll_for_otp_imap(
    email_addr: str,
    password: str,
    known_uids: set[bytes],
    timeout: float = 120.0,
    interval: float = 5.0,
) -> str:
    deadline = time.time() + timeout
    attempt = 0
    conn = connect_imap(email_addr, password)
    try:
        while time.time() < deadline:
            attempt += 1
            try:
                msgs = get_imap_new_messages(conn, known_uids)
            except imaplib.IMAP4.abort:
                logger.warning("IMAP 连接断开，重新连接...")
                try:
                    conn.logout()
                except Exception:
                    pass
                conn = connect_imap(email_addr, password)
                time.sleep(interval)
                continue
            except Exception as e:
                logger.warning(f"第{attempt}次 IMAP 轮询异常: {e}")
                time.sleep(interval)
                continue

            for m in msgs:
                full = m["subject"] + " " + m["content"]
                code = extract_otp_from_content(full)
                if code:
                    logger.info(f"第{attempt}次 IMAP 轮询获取 OTP: {code}")
                    return code
            logger.debug(f"第{attempt}次 IMAP 轮询，新邮件={len(msgs)}，未找到 OTP")
            time.sleep(interval)
    finally:
        disconnect_imap(conn)
    raise TimeoutError(f"等待 OTP 超时 ({timeout}s)")


# ══════════════════════════════════════════════
#  统一接口（供 worker 调用）
# ══════════════════════════════════════════════

def get_snapshot(cred: dict) -> dict:
    """
    获取邮件快照（在触发 OTP 之前调用）。
    根据凭证自动选择 Graph API 或 IMAP。
    返回 snapshot dict，传给 poll_otp()。
    """
    if cred.get("refresh_token") and cred.get("client_id"):
        at, ids = get_snapshot_graph(cred["client_id"], cred["refresh_token"])
        return {"method": "graph", "access_token": at, "known_ids": ids}
    else:
        conn = connect_imap(cred["email"], cred["password"])
        uids = get_imap_uid_snapshot(conn)
        disconnect_imap(conn)
        return {"method": "imap", "known_uids": uids}


def poll_otp(cred: dict, snapshot: dict, timeout: float = 120.0, interval: float = 5.0) -> str:
    """
    轮询等待 OTP（根据 snapshot 自动选择方式）。
    """
    if snapshot["method"] == "graph":
        return poll_for_otp_graph(
            client_id=cred["client_id"],
            refresh_token=cred["refresh_token"],
            known_ids=snapshot["known_ids"],
            access_token=snapshot["access_token"],
            timeout=timeout,
            interval=interval,
        )
    else:
        return poll_for_otp_imap(
            email_addr=cred["email"],
            password=cred["password"],
            known_uids=snapshot["known_uids"],
            timeout=timeout,
            interval=interval,
        )


def test_connection(email_addr: str, password: str,
                    client_id: str = "", refresh_token: str = "") -> dict:
    """
    测试连接是否可用。
    有 refresh_token → 测试 Graph API；否则测试 IMAP。
    """
    if refresh_token and client_id:
        try:
            at = refresh_access_token(client_id, refresh_token)
            ids = get_graph_msg_ids(at, top=1)
            return {"ok": True, "message": f"Graph API 连接成功，收件箱邮件数 ≥ {len(ids)}", "method": "graph"}
        except Exception as e:
            return {"ok": False, "message": f"Graph API 失败: {e}", "method": "graph"}
    else:
        host = _get_imap_host(email_addr)
        try:
            conn = connect_imap(email_addr, password)
            conn.select("INBOX", readonly=True)
            disconnect_imap(conn)
            return {"ok": True, "message": f"IMAP 连接成功 ({host})", "method": "imap"}
        except imaplib.IMAP4.error as e:
            msg = str(e)
            if "AUTHENTICATIONFAILED" in msg or "authentication failed" in msg.lower():
                hint = "登录失败，请检查密码。若开启了二步验证请用「应用密码」。"
            else:
                hint = f"IMAP 错误: {msg}"
            return {"ok": False, "message": hint, "method": "imap"}
        except Exception as e:
            return {"ok": False, "message": f"连接失败: {e}", "method": "imap"}


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
