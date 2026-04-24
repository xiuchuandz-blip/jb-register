"""
JetBrains 注册 API 封装 — 纯 HTTP，零浏览器

完整 API 链（从抓包逆向）：
1. GET  /signup                                    → 获取 _st-JBA (CSRF) + JSESSIONID-JBA
2. POST /api/auth/sessions                         → {"id": "uuid"}
3. POST /api/auth/sessions/{id}/email/signup        → 首次 → 429 (需 captcha)
4. POST /api/auth/captcha/verify                   → {"captchaResponse": token} → 204
5. POST /api/auth/sessions/{id}/email/signup        → 重试 → 200 (发验证码)
6. POST /api/auth/sessions/{id}/otp/email          → {"oneTimePassword": "123456"} → 200
7. POST /api/auth/sessions/{id}/password/check     → 密码强度检查 → 200
8. POST /api/auth/sessions/{id}/profile             → 最终注册 → 200 {"authenticated":true}
"""
import logging
import time

from curl_cffi.requests import Session

logger = logging.getLogger("protocol_reg.jb_api")

BASE_URL = "https://account.jetbrains.com"
RECAPTCHA_SITEKEY = "6LdWq6kUAAAAACyMjpY_mPIqVIOz65GD157ItGzC"


class JBRegError(Exception):
    """注册流程错误"""
    pass


class TencentCaptchaError(JBRegError):
    """JetBrains 返回了腾讯验证码类型，当前不支持，需要重试"""
    pass


# 429 退避配置
MAX_429_RETRIES = 6           # 遇到 429 最多重试次数
BACKOFF_BASE = 5              # 初始退避秒数（实测 JB 限频窗口 ~10s）
BACKOFF_MAX = 30              # 最大退避秒数


class JBRegisterer:
    """
    单个 JetBrains 账号协议注册器。
    每个实例 = 一个注册流程（独立 Session + Cookie）。
    所有 API 步骤自带 429 退避重试。
    """

    def __init__(self, session: Session):
        self.s = session
        self.csrf: str = ""
        self.session_id: str = ""

    # ── 通用重试 ──

    def _post_with_retry(self, url: str, label: str, **kwargs):
        """
        带 429 退避重试的 POST。
        遇到 429 等 15/22/33/50/75/90 秒再试。
        """
        for attempt in range(MAX_429_RETRIES + 1):
            try:
                resp = self.s.post(url, **kwargs)
            except Exception as e:
                if attempt < 3:
                    wait = 2 + attempt * 2
                    logger.warning(f"  [{label}] 网络错误，{wait}s 后重试: {e}")
                    time.sleep(wait)
                    continue
                raise JBRegError(f"{label} 网络错误: {e}")

            if resp.status_code != 429:
                return resp

            if attempt >= MAX_429_RETRIES:
                break

            wait = min(BACKOFF_BASE * (1.5 ** attempt), BACKOFF_MAX)
            logger.warning(f"  [{label}] 429 限频，等 {wait:.0f}s 后重试 ({attempt+1}/{MAX_429_RETRIES})...")
            time.sleep(wait)

        raise JBRegError(f"{label} 连续 429 限频，放弃（已重试 {MAX_429_RETRIES} 次）")

    def _get_with_retry(self, url: str, label: str, **kwargs):
        """带 TLS/429 重试的 GET"""
        for attempt in range(4):
            try:
                resp = self.s.get(url, **kwargs)
                if resp.status_code == 429:
                    wait = min(BACKOFF_BASE * (1.5 ** attempt), BACKOFF_MAX)
                    logger.warning(f"  [{label}] 429 限频，等 {wait:.0f}s...")
                    time.sleep(wait)
                    continue
                return resp
            except Exception as e:
                wait = 2 + attempt * 2
                logger.warning(f"  [{label}] 第 {attempt+1} 次失败: {e}，{wait}s 后重试")
                time.sleep(wait)
        raise JBRegError(f"{label} 4 次重试均失败")

    # ── 注册流程 ──

    def _api_headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.csrf:
            h["X-XSRF-TOKEN"] = self.csrf
        return h

    def _init_session(self):
        """Step 1: GET /signup 获取 cookies"""
        logger.info("[Step 1] 获取页面 cookies...")
        resp = self._get_with_retry(f"{BASE_URL}/signup", "Step1")
        if resp.status_code != 200:
            raise JBRegError(f"GET /signup 返回 {resp.status_code}")

        self.csrf = self.s.cookies.get("_st-JBA", "")
        if not self.csrf:
            raise JBRegError("未获取到 _st-JBA cookie")
        logger.info(f"  CSRF: {self.csrf[:20]}...")

    def _create_auth_session(self):
        """Step 2: POST /api/auth/sessions（带 429 重试）"""
        logger.info("[Step 2] 创建 auth session...")
        resp = self._post_with_retry(
            f"{BASE_URL}/api/auth/sessions", "Step2",
            headers=self._api_headers(),
        )
        if resp.status_code != 200:
            raise JBRegError(f"创建 session 失败: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        self.session_id = data.get("id", "")
        if not self.session_id:
            raise JBRegError(f"session 无 id: {data}")

        logger.info(f"  Session ID: {self.session_id}")
        self.s.headers["Referer"] = f"{BASE_URL}/signup?authSessionId={self.session_id}"

    def _submit_email_first(self, email: str):
        """Step 3: 首次提交邮箱（429 = 需要 captcha，这是预期行为，不重试）"""
        logger.info(f"[Step 3] 提交邮箱 {email}（触发 captcha）...")
        try:
            resp = self.s.post(
                f"{BASE_URL}/api/auth/sessions/{self.session_id}/email/signup",
                headers=self._api_headers(),
                json={"email": email},
            )
        except Exception as e:
            raise JBRegError(f"email/signup 网络错误: {e}")

        if resp.status_code == 429:
            # 检查 captchaType — 基于 IP 动态分配 reCAPTCHA 或 Tencent
            try:
                data = resp.json()
                captcha_type = data.get("captchaType", "")
            except Exception:
                captcha_type = ""
            if captcha_type == "Tencent":
                raise TencentCaptchaError("JetBrains 返回了腾讯验证码（基于 IP），跳过此任务")
            logger.info(f"  429 → captcha required, type={captcha_type or 'GoogleV2'}（预期行为）")
            return
        if resp.status_code == 200:
            logger.info("  200 → 直接通过（无需 captcha）")
            return
        raise JBRegError(f"email/signup 首次提交异常: {resp.status_code}")

    def _verify_captcha(self, token: str):
        """Step 4: POST /api/auth/captcha/verify（带 429 重试）"""
        logger.info(f"[Step 4] 提交 captcha token ({len(token)} chars)...")
        resp = self._post_with_retry(
            f"{BASE_URL}/api/auth/captcha/verify", "Step4-captcha",
            headers=self._api_headers(),
            json={"captchaResponse": token},
        )
        if resp.status_code in (200, 204):
            logger.info(f"  {resp.status_code} → captcha 验证通过")
            return
        raise JBRegError(f"captcha/verify 失败: {resp.status_code} {resp.text[:200]}")

    def _submit_email_retry(self, email: str):
        """Step 5: captcha 通过后重新提交邮箱（带 429 重试）"""
        logger.info("[Step 5] 重新提交邮箱（发送 OTP）...")
        resp = self._post_with_retry(
            f"{BASE_URL}/api/auth/sessions/{self.session_id}/email/signup", "Step5-email",
            headers=self._api_headers(),
            json={"email": email},
        )
        if resp.status_code == 200:
            logger.info("  200 → 验证码邮件已发送")
            return
        raise JBRegError(f"email/signup 重试失败: {resp.status_code} {resp.text[:200]}")

    def _submit_otp(self, otp_code: str):
        """Step 6: POST otp/email（带 429 重试）"""
        logger.info(f"[Step 6] 提交 OTP: {otp_code}")
        resp = self._post_with_retry(
            f"{BASE_URL}/api/auth/sessions/{self.session_id}/otp/email", "Step6-otp",
            headers=self._api_headers(),
            json={"oneTimePassword": otp_code},
        )
        if resp.status_code == 200:
            logger.info("  200 → OTP 验证通过")
            return
        raise JBRegError(f"otp/email 失败: {resp.status_code} {resp.text[:200]}")

    def _submit_password(self, password: str, first_name: str, last_name: str, email: str):
        """Step 7: POST password/check（带 429 重试）"""
        logger.info("[Step 7] 密码检查...")
        resp = self._post_with_retry(
            f"{BASE_URL}/api/auth/sessions/{self.session_id}/password/check", "Step7-pw",
            headers=self._api_headers(),
            json={"password": password, "additionalContext": [first_name, last_name, email]},
        )
        if resp.status_code == 200:
            logger.info("  200 → 密码检查通过")
            return
        raise JBRegError(f"password/check 失败: {resp.status_code} {resp.text[:200]}")

    def _complete_signup(self, password: str, first_name: str, last_name: str, email: str):
        """Step 8: POST profile（带 429 重试）"""
        logger.info("[Step 8] 提交 profile 完成注册...")
        resp = self._post_with_retry(
            f"{BASE_URL}/api/auth/sessions/{self.session_id}/profile", "Step8-profile",
            headers=self._api_headers(),
            json={"password": password, "firstName": first_name, "lastName": last_name, "acceptTerms": True},
        )
        if resp.status_code == 200:
            data = resp.json() if resp.text else {}
            state = data.get("state", "")
            authenticated = data.get("authenticated", False)
            if authenticated or state == "REDIRECT_TO_RETURN_URL":
                logger.info(f"  OK 注册成功！state={state}, authenticated={authenticated}")
                return
            logger.warning(f"  200 但状态异常: {data}")
            return
        raise JBRegError(f"profile 提交失败: {resp.status_code} {resp.text[:200]}")
