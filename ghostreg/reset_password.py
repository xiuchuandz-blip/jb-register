"""
GhostReg — JetBrains 账号邮箱改密模块
流程:
1. 重新创建注册时使用的 YYDS 临时邮箱（同一地址）
2. 触发 JetBrains 忘记密码，发送重置邮件
3. 轮询邮件，提取重置链接（或 OTP 验证码）
4. 访问重置链接，设置新密码
"""
import logging
import re
import time
from urllib.parse import urlparse, parse_qs

import httpx
from curl_cffi.requests import Session as CurlSession

from . import config
from .email_service import (
    _get_api_key, _new_client, _account_cache,
    get_mails, delete_email, extract_verification_code,
)

logger = logging.getLogger("ghostreg.reset_password")

BASE_URL = "https://account.jetbrains.com"


# ── YYDS 邮箱重建（指定 localPart + domain） ──

def apply_email_exact(local_part: str, domain: str) -> str:
    """
    用指定的 localPart + domain 重新创建 YYDS Mail 临时邮箱。
    用于找回密码时重新激活已删除的同一邮箱地址。
    """
    api_key = _get_api_key()
    client = _new_client()
    email = f"{local_part}@{domain}"
    try:
        resp = client.post(
            f"{config.YYDS_API_BASE}/accounts",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={"localPart": local_part, "domain": domain},
        )
        resp.raise_for_status()
        result = resp.json()
    finally:
        client.close()

    if not result.get("success"):
        raise RuntimeError(f"YYDS Mail 创建邮箱失败: {result}")

    account = result["data"]
    _account_cache[email] = {
        "id": account["id"],
        "token": account["token"],
        "domain": domain,
    }
    logger.info(f"临时邮箱重建成功: {email}")
    return email


# ── 邮件内容解析 ──

def extract_reset_link(content: str) -> str | None:
    """从邮件内容中提取 JetBrains 密码重置链接。"""
    patterns = [
        r'https://account\.jetbrains\.com/[^\s"\'<>\)]*(?:reset[_-]?password|restore[_-]?password|confirm|password)[^\s"\'<>\)]*token=[^\s"\'<>\)]+',
        r'https://account\.jetbrains\.com/reset-password\?[^\s"\'<>\)]+',
        r'https://account\.jetbrains\.com/restore-password\?[^\s"\'<>\)]+',
        r'https://account\.jetbrains\.com/confirm\?[^\s"\'<>\)]+',
    ]
    for pattern in patterns:
        m = re.search(pattern, content, re.IGNORECASE)
        if m:
            link = m.group(0).rstrip('.,;)')
            logger.info(f"找到重置链接: {link[:80]}...")
            return link
    return None


def poll_email_for_reset(email: str, timeout: float = 120, interval: float = 5) -> dict:
    """
    轮询邮件箱，寻找 JetBrains 密码重置邮件。
    返回 {"type": "link", "value": url} 或 {"type": "code", "value": "123456"}
    超时则抛出 TimeoutError。
    """
    deadline = time.time() + timeout
    attempt = 0
    seen_ids: set[str] = set()

    logger.info(f"开始轮询重置邮件: {email} (超时={timeout}s)")

    while time.time() < deadline:
        attempt += 1
        try:
            mails = get_mails(email)
        except Exception as e:
            logger.warning(f"第{attempt}次轮询失败: {e}")
            time.sleep(interval)
            continue

        for mail in mails:
            mid = mail.get("id", "")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)

            content = mail.get("content", "") or mail.get("html", "") or mail.get("text", "")
            subject = mail.get("subject", "") or ""
            full_text = subject + " " + content

            # 优先找重置链接
            link = extract_reset_link(full_text)
            if link:
                return {"type": "link", "value": link}

            # 其次找验证码（OTP）
            code = extract_verification_code(full_text)
            if code:
                return {"type": "code", "value": code}

        logger.debug(f"第{attempt}次轮询未收到重置邮件，等待 {interval}s...")
        time.sleep(interval)

    raise TimeoutError(f"等待重置邮件超时 ({timeout}s)")


# ── JetBrains HTTP 改密器 ──

class JBPasswordResetter:
    """纯 HTTP 方式触发 JetBrains 忘记密码 + 完成密码重置。"""

    def __init__(self):
        self.s = CurlSession(impersonate="chrome120")
        self.csrf = ""
        self.session_id = ""

    def _hdrs(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.csrf:
            h["X-XSRF-TOKEN"] = self.csrf
        return h

    def _refresh_csrf(self, path: str = "/forgot-password"):
        """访问页面以获取最新的 CSRF cookie。"""
        try:
            resp = self.s.get(f"{BASE_URL}{path}", timeout=20)
            self.csrf = self.s.cookies.get("_st-JBA", "")
            logger.info(f"  CSRF 刷新: {self.csrf[:20] if self.csrf else '(空)'}...")
        except Exception as e:
            logger.warning(f"  刷新 CSRF 失败: {e}")

    # ── Step A：触发忘记密码邮件 ──

    def request_forgot_password(self, email: str) -> bool:
        """
        向 JetBrains 请求发送密码重置邮件。
        依次尝试多个可能的 API 端点，返回 True 表示触发成功。
        """
        self._refresh_csrf("/forgot-password")

        candidates = [
            ("POST", f"{BASE_URL}/api/auth/restore-password",
             {"email": email}),
            ("POST", f"{BASE_URL}/api/auth/forgot-password",
             {"email": email}),
            ("POST", f"{BASE_URL}/api/account/password/reset-request",
             {"email": email}),
            ("POST", f"{BASE_URL}/api/account/forgot-password",
             {"email": email}),
        ]

        for method, url, body in candidates:
            try:
                resp = self.s.post(url, headers=self._hdrs(), json=body, timeout=20)
                logger.info(f"  {url}: HTTP {resp.status_code}")
                if resp.status_code in (200, 204):
                    logger.info(f"  ✓ 触发成功 → {url}")
                    return True
                if resp.status_code == 404:
                    continue
                if resp.status_code == 400:
                    # 可能是邮箱不存在
                    try:
                        err = resp.json()
                        logger.warning(f"  400 详情: {err}")
                    except Exception:
                        pass
                    return False
            except Exception as e:
                logger.warning(f"  {url} 异常: {e}")
                continue

        # 最后兜底：session 方式
        return self._request_via_session(email)

    def _request_via_session(self, email: str) -> bool:
        """
        通过 auth session API 触发找回密码（备用方案）：
        创建 session → 提交邮箱登录 → JetBrains 发验证码/链接。
        """
        try:
            self._refresh_csrf("/login")
            resp = self.s.post(
                f"{BASE_URL}/api/auth/sessions",
                headers=self._hdrs(), timeout=20,
            )
            if resp.status_code != 200:
                return False
            self.session_id = resp.json().get("id", "")
            if not self.session_id:
                return False
            logger.info(f"  Session ID: {self.session_id}")

            # 提交邮箱（用 login 而不是 signup）
            for endpoint_suffix in ("email/login", "email/restore-password", "email/forgot-password"):
                r = self.s.post(
                    f"{BASE_URL}/api/auth/sessions/{self.session_id}/{endpoint_suffix}",
                    headers=self._hdrs(),
                    json={"email": email},
                    timeout=20,
                )
                logger.info(f"  /{endpoint_suffix}: HTTP {r.status_code}")
                if r.status_code in (200, 204):
                    return True
                if r.status_code == 404:
                    continue
        except Exception as e:
            logger.warning(f"  session 方式失败: {e}")
        return False

    # ── Step B：完成密码重置（通过重置链接） ──

    def complete_reset_via_link(self, reset_link: str, new_password: str) -> bool:
        """访问重置链接并提交新密码。"""
        # 1. 访问重置链接，获取 token
        try:
            resp = self.s.get(reset_link, timeout=30, allow_redirects=True)
            logger.info(f"  访问重置链接: HTTP {resp.status_code}, 最终URL: {str(resp.url)[:80]}")
        except Exception as e:
            logger.warning(f"  访问重置链接失败: {e}")
            return False

        # 从 URL 参数中提取 token
        final_url = str(resp.url)
        parsed = urlparse(final_url)
        qs = parse_qs(parsed.query)
        token = (qs.get("token") or qs.get("reset_token") or [""])[0]

        # 也尝试从原始链接提取
        if not token:
            orig_parsed = urlparse(reset_link)
            orig_qs = parse_qs(orig_parsed.query)
            token = (orig_qs.get("token") or orig_qs.get("reset_token") or [""])[0]

        if not token:
            # 尝试从页面 HTML 中提取 token（hidden input 或 JS 变量）
            html = resp.text
            m = re.search(r'"token"\s*:\s*"([^"]{8,})"', html)
            if m:
                token = m.group(1)

        logger.info(f"  重置 token: {token[:20] if token else '未找到'}...")

        # 2. 提交新密码
        self.csrf = self.s.cookies.get("_st-JBA", self.csrf)

        reset_candidates = [
            (f"{BASE_URL}/api/auth/restore-password/confirm",
             {"token": token, "password": new_password}),
            (f"{BASE_URL}/api/auth/reset-password",
             {"token": token, "password": new_password}),
            (f"{BASE_URL}/api/auth/reset-password",
             {"token": token, "newPassword": new_password}),
            (f"{BASE_URL}/api/account/password/reset",
             {"token": token, "password": new_password}),
        ]

        for url, body in reset_candidates:
            if not token and "token" in body:
                continue
            try:
                r = self.s.post(url, headers=self._hdrs(), json=body, timeout=20)
                logger.info(f"  {url}: HTTP {r.status_code}")
                if r.status_code in (200, 204):
                    logger.info("  ✓ 密码重置成功")
                    return True
                if r.status_code == 404:
                    continue
            except Exception as e:
                logger.warning(f"  {url} 异常: {e}")
                continue

        # 如果无法通过 API 完成，尝试提交 HTML 表单
        return self._complete_via_form(final_url, token, new_password)

    def _complete_via_form(self, page_url: str, token: str, new_password: str) -> bool:
        """备用：提交 HTML 表单形式的密码重置。"""
        try:
            form_data = {
                "_csrf": self.csrf,
                "token": token,
                "password": new_password,
                "confirmPassword": new_password,
            }
            resp = self.s.post(
                page_url, data=form_data, timeout=20,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            logger.info(f"  表单提交: HTTP {resp.status_code}")
            return resp.status_code in (200, 302)
        except Exception as e:
            logger.warning(f"  表单提交失败: {e}")
            return False

    # ── Step B2：完成密码重置（通过 OTP 验证码 + session） ──

    def complete_reset_via_otp(self, otp: str, new_password: str) -> bool:
        """用 OTP 验证码完成登录，然后修改密码。"""
        if not self.session_id:
            return False
        try:
            r = self.s.post(
                f"{BASE_URL}/api/auth/sessions/{self.session_id}/otp/email",
                headers=self._hdrs(),
                json={"oneTimePassword": otp},
                timeout=20,
            )
            logger.info(f"  OTP 验证: HTTP {r.status_code}")
            if r.status_code != 200:
                return False

            # 获取认证 token/cookie 后改密
            self.csrf = self.s.cookies.get("_st-JBA", self.csrf)
            change_candidates = [
                (f"{BASE_URL}/api/auth/sessions/{self.session_id}/password/change",
                 {"newPassword": new_password}),
                (f"{BASE_URL}/api/account/password",
                 {"newPassword": new_password}),
            ]
            for url, body in change_candidates:
                cr = self.s.post(url, headers=self._hdrs(), json=body, timeout=20)
                logger.info(f"  改密 {url}: HTTP {cr.status_code}")
                if cr.status_code in (200, 204):
                    return True
        except Exception as e:
            logger.warning(f"  OTP 改密失败: {e}")
        return False


# ── 主入口 ──

def reset_account_password(
    email: str,
    new_password: str,
    progress_cb=None,
    poll_timeout: float = 120,
) -> dict:
    """
    完整重置流程:
    1. 重新创建同名临时邮箱
    2. 触发 JetBrains 忘记密码
    3. 轮询重置邮件（链接 or OTP）
    4. 完成密码重置
    5. 清理临时邮箱
    返回 {"ok": True/False, "message": "..."} 或 {"ok": False, "error": "..."}
    """

    def log(msg: str, step: str = ""):
        logger.info(f"[{email}] {msg}")
        if progress_cb:
            progress_cb({"email": email, "step": step, "message": msg})

    if "@" not in email:
        return {"ok": False, "error": "邮箱格式无效"}

    local_part, domain = email.split("@", 1)

    # Step 1: 重建临时邮箱
    log("重建临时邮箱...", "recreate_email")
    try:
        apply_email_exact(local_part, domain)
    except Exception as e:
        return {"ok": False, "error": f"无法重建临时邮箱 ({email}): {e}"}

    try:
        # Step 2: 触发忘记密码
        log("向 JetBrains 请求发送密码重置邮件...", "request_reset")
        resetter = JBPasswordResetter()
        ok = resetter.request_forgot_password(email)
        if not ok:
            return {"ok": False, "error": "触发 JetBrains 忘记密码失败，可能该邮箱不存在或 API 端点已变更"}

        log("重置邮件已请求，等待邮件到达...", "polling")

        # Step 3: 轮询邮件
        result = poll_email_for_reset(email, timeout=poll_timeout, interval=5)
        log(f"收到重置邮件 (类型: {result['type']})", "got_mail")

        # Step 4: 完成重置
        log("正在完成密码重置...", "completing")
        if result["type"] == "link":
            success = resetter.complete_reset_via_link(result["value"], new_password)
        elif result["type"] == "code":
            success = resetter.complete_reset_via_otp(result["value"], new_password)
        else:
            return {"ok": False, "error": "邮件内容无法解析（既无重置链接也无验证码）"}

        if success:
            log("密码重置成功！", "done")
            return {"ok": True, "message": f"{email} 密码已成功更改"}
        else:
            return {
                "ok": False,
                "error": "密码重置请求已发送并收到邮件，但最终提交新密码时失败。"
                         "JetBrains API 端点可能已更新，请尝试在浏览器手动完成。"
            }

    except TimeoutError:
        return {"ok": False, "error": f"等待重置邮件超时 ({poll_timeout}s)，请检查邮箱 API Key 是否有效"}
    except Exception as e:
        logger.exception(f"[{email}] 改密流程异常")
        return {"ok": False, "error": f"改密流程异常: {e}"}
    finally:
        # 清理临时邮箱
        try:
            delete_email(email)
        except Exception:
            pass
