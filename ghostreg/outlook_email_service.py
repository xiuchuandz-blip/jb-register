"""
Outlook/Microsoft 邮箱 IMAP 服务
支持 outlook.com / hotmail.com / live.com / Office365 邮箱
用于接收 JetBrains 注册验证码（OTP）
"""
import email
import email.header
import email.message
import imaplib
import logging
import re
import time
from typing import Optional

logger = logging.getLogger("ghostreg.outlook_email")

# Outlook.com 个人账号
IMAP_HOST_OUTLOOK = "imap-mail.outlook.com"
# Office 365 企业账号
IMAP_HOST_OFFICE365 = "outlook.office365.com"
IMAP_PORT = 993

_PERSONAL_DOMAINS = {
    "outlook.com", "hotmail.com", "live.com",
    "outlook.cn", "hotmail.cn", "live.cn",
    "msn.com",
}


def _get_imap_host(email_addr: str) -> str:
    """根据邮箱域名选择 IMAP 服务器"""
    domain = email_addr.split("@")[-1].lower() if "@" in email_addr else ""
    if domain in _PERSONAL_DOMAINS:
        return IMAP_HOST_OUTLOOK
    return IMAP_HOST_OFFICE365


def connect(email_addr: str, password: str) -> imaplib.IMAP4_SSL:
    """
    连接到 Outlook IMAP 服务器并登录。
    返回已认证的 IMAP4_SSL 对象。
    抛出 Exception 如果连接或登录失败。
    """
    host = _get_imap_host(email_addr)
    logger.info(f"IMAP 连接: {host}:993 ({email_addr})")
    conn = imaplib.IMAP4_SSL(host, IMAP_PORT)
    status, resp = conn.login(email_addr, password)
    if status != "OK":
        raise RuntimeError(f"IMAP 登录失败: {resp}")
    logger.info(f"IMAP 登录成功: {email_addr}")
    return conn


def disconnect(conn: imaplib.IMAP4_SSL):
    """安全断开 IMAP 连接"""
    try:
        conn.logout()
    except Exception:
        pass


def get_uid_snapshot(conn: imaplib.IMAP4_SSL, folder: str = "INBOX") -> set[bytes]:
    """
    获取指定文件夹所有邮件的 UID 快照。
    在触发 OTP 前调用此函数，之后通过对比找出新邮件。
    """
    conn.select(folder, readonly=True)
    status, data = conn.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        return set()
    uids = data[0].split()
    return set(uids)


def _decode_header_value(raw) -> str:
    """解码邮件头（可能包含 base64 或 quoted-printable 编码）"""
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            except Exception:
                decoded.append(part.decode("utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded)


def _extract_text_from_message(msg: email.message.Message) -> str:
    """提取邮件的文本内容（优先 HTML，其次纯文本）"""
    html_parts = []
    text_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("utf-8", errors="replace")
            if ct == "text/html":
                html_parts.append(decoded)
            elif ct == "text/plain":
                text_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                decoded = payload.decode("utf-8", errors="replace")
            ct = msg.get_content_type()
            if ct == "text/html":
                html_parts.append(decoded)
            else:
                text_parts.append(decoded)

    return "".join(html_parts) or "".join(text_parts)


def extract_otp_from_content(content: str) -> Optional[str]:
    """
    从邮件内容中提取 JetBrains OTP 验证码（6位数字）。
    与 email_service.py 中的 extract_verification_code 逻辑对齐。
    """
    patterns = [
        r'font-weight:\s*bold[^>]*>\s*(\d{6})\s*<',
        r'font-size:\s*24px[^>]*>\s*(\d{6})\s*<',
        r'(?:verification|confirm|code|验证码)\s*(?:is|:)?\s*(\d{6})',
        r'>\s*(\d{6})\s*<',
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return match.group(1)

    clean = re.sub(r'<[^>]+>', ' ', content)
    clean = re.sub(r'#[0-9a-fA-F]{6}', '', clean)
    clean = re.sub(r'\d{7,}', '', clean)
    match = re.search(r'(?<!\d)(\d{6})(?!\d)', clean)
    if match:
        return match.group(1)
    return None


def fetch_new_messages(
    conn: imaplib.IMAP4_SSL,
    known_uids: set[bytes],
    folder: str = "INBOX",
) -> list[dict]:
    """
    获取 known_uids 之后新到达的邮件列表。
    返回 [{"uid": b"xxx", "subject": "...", "content": "..."}]
    """
    conn.select(folder, readonly=True)
    status, data = conn.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        return []

    all_uids = set(data[0].split())
    new_uids = all_uids - known_uids
    if not new_uids:
        return []

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
            subject = _decode_header_value(msg.get("Subject", ""))
            content = _extract_text_from_message(msg)
            results.append({"uid": uid, "subject": subject, "content": content})
        except Exception as e:
            logger.warning(f"读取邮件 uid={uid} 失败: {e}")

    return results


def poll_for_otp(
    email_addr: str,
    password: str,
    known_uids: set[bytes],
    timeout: float = 120.0,
    interval: float = 5.0,
) -> str:
    """
    轮询 Outlook 邮箱等待新 OTP 邮件并提取验证码。

    Args:
        email_addr: Outlook 邮箱地址
        password: 邮箱密码或应用密码
        known_uids: 触发 OTP 之前的邮件 UID 快照（用于找出新邮件）
        timeout: 最大等待秒数
        interval: 轮询间隔秒数

    Returns:
        6位验证码字符串

    Raises:
        TimeoutError: 超时未收到验证码
        RuntimeError: IMAP 连接或登录失败
    """
    deadline = time.time() + timeout
    attempt = 0
    conn = None
    try:
        conn = connect(email_addr, password)
        while time.time() < deadline:
            attempt += 1
            try:
                msgs = fetch_new_messages(conn, known_uids)
                for m in msgs:
                    full_text = m["subject"] + " " + m["content"]
                    if "jetbrains" in full_text.lower() or "account" in full_text.lower() or re.search(r'\d{6}', full_text):
                        code = extract_otp_from_content(full_text)
                        if code:
                            logger.info(f"[{email_addr}] 第{attempt}次轮询获取 OTP: {code}")
                            return code
                # 没找到 JetBrains 邮件，尝试所有新邮件
                for m in msgs:
                    code = extract_otp_from_content(m["content"])
                    if code:
                        logger.info(f"[{email_addr}] 第{attempt}次轮询获取 OTP（非JB邮件）: {code}")
                        return code
                logger.debug(f"[{email_addr}] 第{attempt}次轮询，新邮件数={len(msgs)}，未找到OTP")
            except imaplib.IMAP4.abort:
                # 连接被服务器断开，重新连接
                logger.warning(f"[{email_addr}] IMAP 连接被断开，重新连接...")
                try:
                    conn.logout()
                except Exception:
                    pass
                conn = connect(email_addr, password)
            except Exception as e:
                logger.warning(f"[{email_addr}] 第{attempt}次轮询异常: {e}")
            time.sleep(interval)
    finally:
        if conn:
            disconnect(conn)

    raise TimeoutError(f"等待 OTP 超时 ({timeout}s): {email_addr}")


def test_connection(email_addr: str, password: str) -> dict:
    """
    测试 IMAP 连接是否可用。
    返回 {"ok": bool, "message": str, "host": str}
    """
    host = _get_imap_host(email_addr)
    try:
        conn = connect(email_addr, password)
        # 尝试选择收件箱
        status, _ = conn.select("INBOX", readonly=True)
        disconnect(conn)
        if status == "OK":
            return {"ok": True, "message": f"连接成功 ({host})", "host": host}
        else:
            return {"ok": False, "message": f"INBOX 选择失败: {status}", "host": host}
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg or "authentication failed" in msg.lower():
            hint = "登录失败，请检查密码是否正确。若启用了二步验证，请使用「应用密码」。"
        elif "UNAVAILABLE" in msg:
            hint = "邮箱服务暂时不可用，请稍后再试。"
        else:
            hint = f"IMAP 错误: {msg}"
        return {"ok": False, "message": hint, "host": host}
    except Exception as e:
        return {"ok": False, "message": f"连接失败: {e}", "host": host}


def parse_credentials_text(text: str) -> list[dict]:
    """
    解析用户粘贴的邮箱凭证文本。
    支持格式：
      email----password
      email:password
      email password
    返回 [{"email": str, "password": str}] 列表，跳过无效行。
    """
    results = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cred = None
        # 优先匹配 ---- 分隔（最常见）
        if "----" in line:
            parts = line.split("----", 1)
            cred = (parts[0].strip(), parts[1].strip())
        elif line.count(":") == 1:
            parts = line.split(":", 1)
            cred = (parts[0].strip(), parts[1].strip())
        elif "\t" in line:
            parts = line.split("\t", 1)
            cred = (parts[0].strip(), parts[1].strip())
        elif " " in line:
            parts = line.split(None, 1)
            cred = (parts[0].strip(), parts[1].strip())
        if cred and "@" in cred[0] and cred[1]:
            results.append({"email": cred[0], "password": cred[1]})
    return results
