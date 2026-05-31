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
    """JetBrains 返回了腾讯验证码类型,当前不支持,需要重试"""
    pass


class CaptchaTokenInvalidError(JBRegError):
    """
    打码 token 被 JetBrains 拒绝(yescaptcha 偶发吐错 token)。
    上层应保留 sessions_id,只重新打码而不换 IP/邮箱。
    """
    pass


class DisposableEmailError(JBRegError):
    """
    JetBrains 返回 400 DisposableEmail —— 该域名被列为一次性邮箱。
    上层应立即永久拉黑该域名，换一个新邮箱重试。
    """
    def __init__(self, domain: str, message: str = ""):
        super().__init__(message or f"域名 {domain} 被 JetBrains 识别为一次性邮箱")
        self.domain = domain


class SessionsBucketHitError(JBRegError):
    """
    OAuth sessions IP 桶撞墙(429)。
    实测:JB 此处限流分级,撞过的 IP 越等越严(>45s 才恢复,且恢复后再撞会更严)。
    上层应立即换 IP 重试,不要在原 IP 上等。
    
    Attributes:
        step: 撞墙的步骤 ("Step2" / "Step5")
        retry_after_ms: 服务端返回的退避毫秒(可能为 0)
    """
    def __init__(self, step: str, message: str = "", retry_after_ms: int = 0):
        super().__init__(message or f"{step} sessions IP 桶 429")
        self.step = step
        self.retry_after_ms = retry_after_ms


# 429 退避配置
MAX_429_RETRIES = 6           # 遇到 429 最多重试次数
BACKOFF_BASE = 5              # 初始退避秒数(实测 JB 限频窗口 ~10s)
BACKOFF_MAX = 30              # 最大退避秒数


# SOCKS5/代理死亡特征关键字 — 双层代理下命中任一立即换 IP
_SOCKS5_DEAD_KEYWORDS = (
    "SOCKS5",                           # SOCKS5 connection / SOCKS5 authentication
    "connection to proxy closed",
    "Failed to receive SOCKS",
    "BAD_DECRYPT",                      # BoringSSL SSL_read 解密失败(SOCKS5 串流损坏)
    "SSL_read",
    "BoringSSL",
    "SSL routines",
    "(28)",                             # curl timeout
    "(7)",                              # curl couldn't connect
    "(56)",                             # curl recv failure
    "(35)",                             # curl SSL handshake failure
    "Could not resolve host",
    "Connection refused",
    "Connection reset",
    "Read timeout",
)


def _is_socks5_dead(err_msg: str) -> bool:
    """判断异常是否是 SOCKS5/代理死亡(应立即换 IP)"""
    return any(kw in err_msg for kw in _SOCKS5_DEAD_KEYWORDS)


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
                err_msg = str(e)
                # 双层代理 SOCKS5 类失败 → 立即换 IP,不在原 IP 上等
                if _is_socks5_dead(err_msg):
                    logger.warning(f"  [{label}] SOCKS5/代理失败,抛 SessionsBucketHitError 让上层换 IP: {e}")
                    raise SessionsBucketHitError(label, message=f"代理连接失败: {err_msg[:120]}")
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
        """带 TLS/429 重试的 GET。
        SOCKS5 连接失败 → 立即抛 SessionsBucketHitError 让上层换 IP(跟撞 429 同等处理)
        非 SOCKS5 类网络错误 → 短重试 4 次
        """
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
                err_msg = str(e)
                # 双层代理 SOCKS5 类失败 → 立即换 IP,不在原 IP 上等
                # 双层代理 SOCKS5 类失败 → 立即抛让上层换 IP
                if _is_socks5_dead(err_msg):
                    logger.warning(f"  [{label}] SOCKS5/代理失败,抛 SessionsBucketHitError 让上层换 IP: {e}")
                    raise SessionsBucketHitError(label, message=f"代理连接失败: {err_msg[:120]}")
                wait = 2 + attempt * 2
                logger.warning(f"  [{label}] 第 {attempt+1} 次失败: {e}，{wait}s 后重试")
                time.sleep(wait)
        # 4 次重试都失败,网络环境本身可能也有问题。也抛 SessionsBucketHitError 让上层换 IP 重新试
        # (而不是 JBRegError 直接 fail)
        raise SessionsBucketHitError(label, message=f"{label} 4 次重试均失败,可能 SOCKS5 不稳定")

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
        """
        Step 2: POST /api/auth/sessions
        撞 429 → SessionsBucketHitError(上层换 IP 重试,不在原 IP 等)
        实测:此接口 IP 桶 5/30s,>45s 恢复,撞过的 IP 加严限流。
        """
        logger.info("[Step 2] 创建 auth session...")
        try:
            resp = self.s.post(
                f"{BASE_URL}/api/auth/sessions",
                headers=self._api_headers(),
            )
        except Exception as e:
            err_msg = str(e)
            # SOCKS5 死了 → 立即换 IP
            if _is_socks5_dead(err_msg):
                raise SessionsBucketHitError("Step2", message=f"代理连接失败: {err_msg[:120]}")
            raise JBRegError(f"Step2 网络错误: {e}")
        
        if resp.status_code == 429:
            retry_ms = self._read_retry_after_ms(resp)
            logger.warning(f"  [Step2] 撞 sessions IP 桶 429 (retry-after-ms={retry_ms}),抛异常让上层换 IP")
            raise SessionsBucketHitError("Step2", retry_after_ms=retry_ms)
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
            err_msg = str(e)
            if _is_socks5_dead(err_msg):
                raise SessionsBucketHitError("Step3", message=f"代理连接失败: {err_msg[:120]}")
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
        if resp.status_code == 400:
            try:
                err_code = resp.json().get("errorCode", "")
            except Exception:
                err_code = ""
            if "DisposableEmail" in err_code or "DisposableEmail" in resp.text:
                domain = email.split("@", 1)[1] if "@" in email else email
                logger.warning(f"  400 DisposableEmail → 域名 {domain} 被 JB 拒绝")
                raise DisposableEmailError(domain)
        raise JBRegError(f"email/signup 首次提交异常: {resp.status_code} {resp.text[:120]}")

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
        # 400 CaptchaVerificationFailed = 打码平台偶发吐错 token,重新打码即可
        if resp.status_code == 400 and "CaptchaVerificationFailed" in resp.text:
            raise CaptchaTokenInvalidError(f"打码 token 被 JB 拒绝(打码平台偶发,重新打码): {resp.text[:120]}")
        raise JBRegError(f"captcha/verify 失败: {resp.status_code} {resp.text[:200]}")

    def _submit_email_retry(self, email: str):
        """
        Step 5: captcha 通过后重新提交邮箱(发 OTP)。
        撞 429 → SessionsBucketHitError(上层保留 session_id 换 IP 重试,
        不浪费已经打过的 captcha 积分)
        """
        logger.info("[Step 5] 重新提交邮箱(发送 OTP)...")
        try:
            resp = self.s.post(
                f"{BASE_URL}/api/auth/sessions/{self.session_id}/email/signup",
                headers=self._api_headers(),
                json={"email": email},
            )
        except Exception as e:
            err_msg = str(e)
            if _is_socks5_dead(err_msg):
                raise SessionsBucketHitError("Step5", message=f"代理连接失败: {err_msg[:120]}")
            raise JBRegError(f"Step5 网络错误: {e}")
        
        if resp.status_code == 429:
            retry_ms = self._read_retry_after_ms(resp)
            logger.warning(f"  [Step5] 撞 sessions IP 桶 429 (retry-after-ms={retry_ms}),抛异常让上层换 IP(保留 session_id)")
            raise SessionsBucketHitError("Step5", retry_after_ms=retry_ms)
        if resp.status_code == 200:
            logger.info("  200 → 验证码邮件已发送")
            return
        if resp.status_code == 400:
            try:
                err_code = resp.json().get("errorCode", "")
            except Exception:
                err_code = ""
            if "DisposableEmail" in err_code or "DisposableEmail" in resp.text:
                domain = email.split("@", 1)[1] if "@" in email else email
                logger.warning(f"  400 DisposableEmail → 域名 {domain} 被 JB 拒绝")
                raise DisposableEmailError(domain)
        raise JBRegError(f"email/signup 重试失败: {resp.status_code} {resp.text[:200]}")
    
    @staticmethod
    def _read_retry_after_ms(resp) -> int:
        """读 retry-after-ms 头(JB 自定义,毫秒级倒计时)。没有就返回 0"""
        try:
            v = resp.headers.get("retry-after-ms") or resp.headers.get("Retry-After-Ms")
            if v:
                return int(v)
        except Exception:
            pass
        return 0

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
