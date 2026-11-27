"""
JetBrains 登录邮箱 OTP 自动接码服务。

支持三种自动接码方式（按优先级自动选择）：
  1. Outlook / Microsoft 邮箱（复用 ghostreg.outlook_email_service：XOAUTH2 / Graph API / Basic IMAP）
  2. YYDS Mail 临时邮箱（复用 ghostreg.email_service，支持同名邮箱自动激活）
  3. NiMail（@nimail.cn 自动激活 + 保留原始 @ 拉取正文）

以及第四种人工兜底：
  4. 手动输入验证码回调（供令牌失效的 Outlook 等邮箱人工接码）
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx

from . import config, email_service, outlook_email_service

logger = logging.getLogger("ghostreg.login_otp")

_NIMAIL_BASE = "https://www.nimail.cn"


def _strip_html(html: str) -> str:
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_code(content: str) -> Optional[str]:
    if not content:
        return None
    # 优先用项目现有正则，再结合纯文本清洗防 CSS 颜色干扰
    code = email_service.extract_verification_code(content)
    if code:
        return code
    cleaned = _strip_html(content)
    return outlook_email_service.extract_otp_from_content(cleaned)


def _new_nimail_client() -> httpx.Client:
    try:
        transport = httpx.HTTPTransport(local_address=config.HTTP_LOCAL_ADDRESS)
        return httpx.Client(timeout=20, proxy=None, transport=transport, verify=False)
    except Exception:
        return httpx.Client(timeout=20, verify=False)


def _ensure_yyds_inbox_exact(email_addr: str) -> None:
    """尽量确保 YYDS 同域名邮箱存在（忽略已占用错误）"""
    if "@" not in email_addr:
        return
    api_key = config.YYDS_API_KEY or config.load_api_key()
    if not api_key:
        return
    local_part, domain = email_addr.split("@", 1)
    client = email_service._new_client()
    try:
        resp = client.post(
            f"{config.YYDS_API_BASE}/accounts",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={"localPart": local_part, "domain": domain},
        )
        data = resp.json()
        if data.get("success") and isinstance(data.get("data"), dict):
            acc = data["data"]
            email_service._account_cache[email_addr] = {
                "id": acc.get("id", ""),
                "token": acc.get("token", ""),
                "domain": domain,
            }
    except Exception as e:
        logger.debug(f"[LoginOTP] YYDS 预激活 {email_addr} 跳过: {e}")
    finally:
        client.close()


def _nimail_get_mails(email_addr: str) -> list[dict]:
    client = _new_nimail_client()
    try:
        resp = client.get(f"{_NIMAIL_BASE}/api/getmails", params={"mail": email_addr})
        if resp.status_code != 200:
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data") or data.get("mails") or []
    except Exception as e:
        logger.debug(f"[LoginOTP] NiMail getmails 失败: {e}")
    finally:
        client.close()
    return []


def _nimail_get_raw_html(email_addr: str, mail_id: str) -> str:
    client = _new_nimail_client()
    try:
        # NiMail raw-html 路径需要保留原始 @ 字符，不能 percent-encode
        url = f"{_NIMAIL_BASE}/api/raw-html/{email_addr}/{mail_id}"
        resp = client.get(url)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.debug(f"[LoginOTP] NiMail raw-html 失败: {e}")
    finally:
        client.close()
    return ""


@dataclass
class LoginOtpContext:
    email: str
    provider: str = "manual"  # "outlook" | "yyds" | "nimail" | "manual"
    outlook_cred: Optional[dict] = None
    outlook_snapshot: Optional[dict] = None
    known_ids: set[str] = field(default_factory=set)
    prepare_error: str = ""


def prepare_otp_context(email_addr: str, outlook_cred: Optional[dict] = None) -> LoginOtpContext:
    """
    在触发登录 OTP 之前调用：识别邮箱类型并记录收件箱快照，避免把历史验证码当成新验证码。
    注意：哪怕这里快照失败，也不会直接中断密码登录，只有真正遇到 EMAIL_OTP_REQUIRED 时才使用或降级。
    """
    email_lower = (email_addr or "").strip().lower()

    # 1. Outlook / Microsoft 凭证优先
    if outlook_cred and (
        (outlook_cred.get("refresh_token") and outlook_cred.get("client_id"))
        or outlook_cred.get("password")
    ):
        ctx = LoginOtpContext(email=email_addr, provider="outlook", outlook_cred=outlook_cred)
        try:
            ctx.outlook_snapshot = outlook_email_service.get_snapshot(outlook_cred)
        except Exception as e:
            ctx.prepare_error = f"Outlook 邮箱连接失败: {e}"
            logger.warning(f"[LoginOTP] {email_addr} Outlook 预处理失败: {e}")
        return ctx

    # 2. NiMail 临时邮箱
    if email_lower.endswith("@nimail.cn"):
        ctx = LoginOtpContext(email=email_addr, provider="nimail")
        try:
            client = _new_nimail_client()
            try:
                client.get(f"{_NIMAIL_BASE}/api/applymail", params={"mail": email_addr})
            finally:
                client.close()
            mails = _nimail_get_mails(email_addr)
            ctx.known_ids = {str(m.get("id")) for m in mails if m.get("id") is not None}
        except Exception as e:
            ctx.prepare_error = f"NiMail 初始化失败: {e}"
            logger.warning(f"[LoginOTP] {email_addr} NiMail 快照失败: {e}")
        return ctx

    # 3. YYDS Mail（需要配置了 API Key 且域名在 YYDS 在线域名列表中）
    api_key = config.YYDS_API_KEY or config.load_api_key()
    if api_key and "@" in email_lower:
        domain = email_lower.split("@", 1)[1]
        yyds_domains = email_service.get_yyds_domain_set()
        if domain in yyds_domains:
            ctx = LoginOtpContext(email=email_addr, provider="yyds")
            try:
                _ensure_yyds_inbox_exact(email_addr)
                mails = email_service.get_mails(email_addr)
                ctx.known_ids = {str(m.get("id")) for m in mails if m.get("id") is not None}
            except Exception as e:
                ctx.prepare_error = f"YYDS 初始化失败: {e}"
                logger.warning(f"[LoginOTP] {email_addr} YYDS 快照失败: {e}")
            return ctx

    # 4. 其他邮箱：走手动输入兜底
    return LoginOtpContext(email=email_addr, provider="manual")


def fetch_login_otp(
    ctx: LoginOtpContext,
    timeout: float = 120.0,
    interval: float = 3.0,
    manual_otp_fn: Optional[Callable[[str, str], Optional[str]]] = None,
) -> tuple[str, str]:
    """
    等待并获取 JetBrains 6 位登录验证码。

    Returns:
        (code, method_used)
    Raises:
        RuntimeError / TimeoutError
    """
    if ctx.provider == "outlook" and ctx.outlook_cred and ctx.outlook_snapshot:
        try:
            code = outlook_email_service.poll_otp(
                ctx.outlook_cred,
                ctx.outlook_snapshot,
                timeout=timeout,
                interval=max(3.0, interval),
            )
            return code, f"outlook:{ctx.outlook_snapshot.get('method', '')}"
        except Exception as e:
            if manual_otp_fn:
                logger.warning(f"[LoginOTP] {ctx.email} Outlook 自动接码失败({e})，降级手动输入")
                code = manual_otp_fn(ctx.email, f"Outlook接码失败: {e}")
                if code:
                    return code, "manual"
            raise RuntimeError(f"Outlook 自动接码失败: {e}")

    if ctx.provider == "outlook" and ctx.prepare_error:
        if manual_otp_fn:
            code = manual_otp_fn(ctx.email, ctx.prepare_error)
            if code:
                return code, "manual"
        raise RuntimeError(ctx.prepare_error)

    if ctx.provider == "yyds":
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                mails = email_service.get_mails(ctx.email)
                # 优先检查新到达的邮件
                for mail in reversed(mails):
                    mid = str(mail.get("id") or "")
                    if mid and mid in ctx.known_ids:
                        continue
                    subject = mail.get("subject", "") or ""
                    body = mail.get("content", "") or mail.get("html", "") or mail.get("text", "")
                    code = _extract_code(subject + " " + body)
                    if code:
                        return code, "yyds"
            except Exception as e:
                logger.debug(f"[LoginOTP] YYDS 轮询异常 {ctx.email}: {e}")
            time.sleep(interval)
        if manual_otp_fn:
            code = manual_otp_fn(ctx.email, "YYDS 等待验证码超时")
            if code:
                return code, "manual"
        raise TimeoutError(f"YYDS 等待登录验证码超时 ({timeout}s): {ctx.email}")

    if ctx.provider == "nimail":
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                mails = _nimail_get_mails(ctx.email)
                for mail in mails:
                    mid = str(mail.get("id") or "")
                    if mid and mid in ctx.known_ids:
                        continue
                    subject = str(mail.get("subject") or "")
                    raw = _nimail_get_raw_html(ctx.email, mid) if mid else ""
                    body = raw or str(mail.get("content") or mail.get("body") or mail.get("text") or "")
                    code = _extract_code(subject + " " + body)
                    if code:
                        return code, "nimail"
            except Exception as e:
                logger.debug(f"[LoginOTP] NiMail 轮询异常 {ctx.email}: {e}")
            time.sleep(interval)
        if manual_otp_fn:
            code = manual_otp_fn(ctx.email, "NiMail 等待验证码超时")
            if code:
                return code, "manual"
        raise TimeoutError(f"NiMail 等待登录验证码超时 ({timeout}s): {ctx.email}")

    # 手动输入兜底
    if manual_otp_fn:
        code = manual_otp_fn(ctx.email, "需要邮箱验证码（未配置自动接码凭证）")
        if code:
            return code, "manual"

    raise RuntimeError("需要邮箱验证码，但未提供 Outlook 邮箱令牌（也未命中 YYDS/NiMail）")
