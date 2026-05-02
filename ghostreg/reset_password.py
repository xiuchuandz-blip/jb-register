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

from pathlib import Path

from . import config
from .email_service import (
    _get_api_key, _new_client, _account_cache,
    get_mails, delete_email, extract_verification_code,
    get_yyds_domain_set,
)

logger = logging.getLogger("ghostreg.reset_password")

BASE_URL = "https://account.jetbrains.com"

# 持久化 token 缓存文件（防止 app 重启后 token 丢失）
_TOKEN_CACHE_FILE = Path(__file__).parent.parent / "output" / "email_token_cache.json"


def _load_token_cache() -> dict:
    if _TOKEN_CACHE_FILE.exists():
        try:
            import json as _json
            return _json.loads(_TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_token_cache(cache: dict):
    import json as _json
    _TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_CACHE_FILE.write_text(_json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")


def _remove_from_token_cache(email: str):
    cache = _load_token_cache()
    if email in cache:
        del cache[email]
        _save_token_cache(cache)


# ── YYDS 邮箱重建（指定 localPart + domain，带持久化 token 缓存） ──

def apply_email_exact(local_part: str, domain: str) -> str:
    """
    用指定的 localPart + domain 重新创建 YYDS Mail 临时邮箱。
    若邮箱已存在（409），尝试从持久化缓存恢复 token；
    缓存也在创建成功时保存，确保 app 重启后仍可使用。
    """
    email = f"{local_part}@{domain}"

    # 先尝试从持久化缓存恢复
    token_cache = _load_token_cache()
    if email in token_cache:
        cached = token_cache[email]
        _account_cache[email] = cached
        logger.info(f"从缓存恢复邮箱 token: {email}")
        return email

    api_key = _get_api_key()
    client = _new_client()
    try:
        resp = client.post(
            f"{config.YYDS_API_BASE}/accounts",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={"localPart": local_part, "domain": domain},
        )
        result = resp.json()
    finally:
        client.close()

    if not result.get("success"):
        err_code = result.get("errorCode", "")
        if err_code == "address_already_in_use":
            # 邮箱已存在但无 token 缓存 → 降级使用 API Key 直接读取邮件
            # email_service._make_auth_headers 无 token 时自动走 API Key 路径
            logger.warning(
                f"邮箱 {email} 已被占用且无 token 缓存，将使用 API Key 降级读取邮件。"
            )
            return email
        raise RuntimeError(f"YYDS Mail 创建邮箱失败: {result}")

    account = result["data"]
    entry = {
        "id": account["id"],
        "token": account["token"],
        "domain": domain,
    }
    _account_cache[email] = entry

    # 持久化保存 token，防止 app 重启丢失
    token_cache[email] = entry
    _save_token_cache(token_cache)

    logger.info(f"临时邮箱重建成功: {email}")
    return email


# ── 邮件内容解析 ──

def extract_reset_link(content: str) -> str | None:
    """
    从邮件内容中提取 JetBrains 密码重置链接。
    JetBrains 实际格式: /reset-password/<token> （路径参数，不是查询参数）
    """
    patterns = [
        # 主要格式：路径参数 /reset-password/TOKEN（无查询字符串）
        r'https://account\.jetbrains\.com/reset-password/[A-Za-z0-9_\-]+',
        # 兼容：查询参数格式 /reset-password?token=TOKEN
        r'https://account\.jetbrains\.com/reset-password\?[^\s"\'<>\)]+',
        # 兼容：restore-password 路径
        r'https://account\.jetbrains\.com/restore-password/[A-Za-z0-9_\-]+',
        r'https://account\.jetbrains\.com/restore-password\?[^\s"\'<>\)]+',
        # 兼容：confirm 路径
        r'https://account\.jetbrains\.com/confirm\?[^\s"\'<>\)]+',
    ]
    for pattern in patterns:
        m = re.search(pattern, content, re.IGNORECASE)
        if m:
            link = m.group(0).rstrip('.,;)')
            logger.info(f"找到重置链接: {link[:80]}...")
            return link
    return None


def _mail_id_to_timestamp(mail_id: str) -> float:
    """
    将 YYDS Mail 邮件 ID（格式 YYYYMMDDTHHMMSS-xxxx）解析为 UTC Unix 时间戳。
    YYDS 服务器使用北京时间（UTC+8），需转换为 UTC 再比较。
    解析失败时返回 0（视为最老邮件，会被过滤掉）。
    """
    import datetime
    try:
        dt_part = mail_id.split("-")[0]  # "20260429T024219"
        dt = datetime.datetime.strptime(dt_part, "%Y%m%dT%H%M%S")
        # YYDS ID 时间戳为北京时间（UTC+8），转换为 UTC
        cst = datetime.timezone(datetime.timedelta(hours=8))
        return dt.replace(tzinfo=cst).timestamp()
    except Exception:
        return 0.0


def poll_email_for_reset(email: str, timeout: float = 300, interval: float = 5,
                          since: float = 0.0) -> dict:
    """
    轮询邮件箱，寻找 JetBrains 密码重置邮件。
    since: Unix 时间戳，只处理在此时刻之后到达的邮件（防止匹配历史旧邮件）。
    返回 {"type": "link", "value": url} 或 {"type": "code", "value": "123456"}
    超时则抛出 TimeoutError。
    """
    deadline = time.time() + timeout
    attempt = 0
    seen_ids: set[str] = set()

    logger.info(f"开始轮询重置邮件: {email} (超时={timeout}s, since={since:.0f})")

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

            # 过滤：跳过在重置请求发起之前到达的旧邮件
            if since > 0 and _mail_id_to_timestamp(mid) < since - 5:
                continue

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

    def _browser_headers(self) -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{BASE_URL}/forgot-password",
        }

    # ── Step A：触发忘记密码邮件（表单 POST 方式）──

    def request_forgot_password(self, email: str) -> bool:
        """
        向 JetBrains 提交忘记密码表单，触发发送密码重置邮件。
        真实接口：POST /request-password-reset?_st=<csrf>
        表单字段：username=<email>
        """
        try:
            # Step 1: 获取忘记密码页面，提取 CSRF token 和 form action
            r = self.s.get(
                f"{BASE_URL}/forgot-password",
                headers=self._browser_headers(),
                timeout=20,
            )
            logger.info(f"  GET /forgot-password: HTTP {r.status_code}")
            if r.status_code != 200:
                return False

            # 从 form action 提取完整的提交 URL（含 _st=... CSRF token）
            action_match = re.search(
                r'action="(/request-password-reset\?[^"]+)"', r.text
            )
            if not action_match:
                logger.warning("  未找到 form action，尝试备用方案")
                return self._request_via_session(email)

            action_path = action_match.group(1)
            post_url = f"{BASE_URL}{action_path}"
            logger.info(f"  Form action: {post_url[:80]}...")

            # Step 2: 提交表单（字段名为 username，支持邮箱或用户名）
            r2 = self.s.post(
                post_url,
                data={"username": email},
                headers={
                    **self._browser_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=20,
            )
            logger.info(f"  POST /request-password-reset: HTTP {r2.status_code}")

            if r2.status_code in (200, 302):
                logger.info("  ✓ 忘记密码请求成功，JetBrains 将发送重置邮件")
                return True

            logger.warning(f"  非预期状态码: {r2.status_code}")
            return False

        except Exception as e:
            logger.warning(f"  form 方式失败: {e}，尝试备用方案")
            return self._request_via_session(email)

    def _request_via_session(self, email: str) -> bool:
        """备用方案：通过 session API 触发登录 OTP（用于后续验证）"""
        try:
            r = self.s.get(f"{BASE_URL}/login", headers=self._browser_headers(), timeout=20)
            csrf = self.s.cookies.get("_st-JBA", "")
            hdrs = {"Content-Type": "application/json"}
            if csrf:
                hdrs["X-XSRF-TOKEN"] = csrf

            resp = self.s.post(
                f"{BASE_URL}/api/auth/sessions", headers=hdrs, timeout=20,
            )
            if resp.status_code != 200:
                return False
            self.session_id = resp.json().get("id", "")
            if not self.session_id:
                return False
            logger.info(f"  备用 Session ID: {self.session_id}")

            for suffix in ("email/login",):
                r2 = self.s.post(
                    f"{BASE_URL}/api/auth/sessions/{self.session_id}/{suffix}",
                    headers=hdrs,
                    json={"email": email},
                    timeout=20,
                )
                logger.info(f"  /{suffix}: HTTP {r2.status_code}")
                if r2.status_code in (200, 204):
                    return True
        except Exception as e:
            logger.warning(f"  备用 session 方式失败: {e}")
        return False

    # ── Step B：完成密码重置（通过重置链接） ──

    def complete_reset_via_link(self, reset_link: str, new_password: str) -> bool:
        """
        访问 JetBrains 密码重置链接，解析表单并提交新密码。
        JetBrains 重置链接格式：/reset-password?token=...
        提交接口通常是同页面的 POST，包含 _st CSRF token。
        """
        try:
            # 1. 访问重置链接，加载带表单的页面
            resp = self.s.get(
                reset_link, timeout=30, allow_redirects=True,
                headers=self._browser_headers(),
            )
            final_url = str(resp.url)
            logger.info(f"  访问重置链接: HTTP {resp.status_code}, URL: {final_url[:80]}")

            # 如果被重定向到 /login，说明 token 无效或已过期
            if "/login" in final_url and "reset" not in final_url:
                logger.warning("  重置链接已失效或 token 无效（重定向到 /login）")
                return False

        except Exception as e:
            logger.warning(f"  访问重置链接失败: {e}")
            return False

        html = resp.text

        # 2. 从 HTML 中提取表单 action（含新的 _st CSRF token）
        form_action = None
        # JetBrains 实际 form action: /password-reset?code=TOKEN&_st=CSRF（HTML 编码为 &amp;）
        action_match = re.search(r'action="(/[^"]+)"', html)
        if action_match:
            raw_action = action_match.group(1)
            # 解码 HTML 实体（&amp; → &）
            import html as html_lib
            decoded_action = html_lib.unescape(raw_action)
            form_action = f"{BASE_URL}{decoded_action}"
            logger.info(f"  表单 action: {form_action[:100]}")

        # 如果找不到 form action，用当前 URL 作为 fallback
        if not form_action:
            form_action = final_url
            logger.info(f"  推断表单 action: {form_action[:80]}")

        # 3. 从 HTML 找所有 input 字段名（适配不同版本的表单字段名）
        input_names = re.findall(r'<input[^>]+name="([^"]+)"', html)
        logger.info(f"  表单字段: {input_names}")

        # 构造表单数据
        # JetBrains 实际字段: password + pass2（confirm 字段叫 pass2）
        form_data: dict[str, str] = {}
        for name in input_names:
            nl = name.lower()
            is_pw_field = (
                "password" in nl
                or "pwd" in nl
                or nl.startswith("pass")   # pass2, passwd, etc.
                or nl in ("pass", "pw")
            )
            if is_pw_field:
                form_data[name] = new_password
            else:
                # hidden fields (token, _csrf 等) 从 html 提取 value
                val_match = re.search(
                    rf'<input[^>]+name="{re.escape(name)}"[^>]+value="([^"]*)"', html
                )
                if not val_match:
                    val_match = re.search(
                        rf'<input[^>]+value="([^"]*)"[^>]+name="{re.escape(name)}"', html
                    )
                form_data[name] = val_match.group(1) if val_match else ""

        if not form_data:
            # 页面没有任何 input 字段，说明重置链接已过期或无效（JetBrains 显示错误页而非表单）
            logger.warning("  未找到表单字段（链接可能已过期），拒绝提交以避免误判")
            return False

        logger.info(f"  提交表单字段: {list(form_data.keys())}")

        # 4. POST 提交新密码
        try:
            r2 = self.s.post(
                form_action,
                data=form_data,
                headers={
                    **self._browser_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": final_url,
                },
                timeout=30,
                allow_redirects=True,
            )
            final_url2 = str(r2.url)
            logger.info(f"  POST 新密码: HTTP {r2.status_code}, URL: {final_url2[:80]}")

            # 严格判断成功：URL 跳转到 login/success/profile，或页面无错误关键词
            url_ok = any(k in final_url2 for k in ("login", "success", "profile", "account.jetbrains.com/?"))
            # 检查页面是否包含错误提示（expired/invalid/failed/error）
            error_match = re.search(
                r'(?:error|invalid|expired|failed|no longer valid|link.*not valid)[^<]{0,80}',
                r2.text, re.I
            )
            if error_match:
                logger.warning(f"  页面含错误提示: {error_match.group(0)[:80]}")
                return False
            if r2.status_code in (200, 302) and url_ok:
                logger.info("  ✓ 密码重置表单提交成功（URL 跳转确认）")
                return True
            # HTTP 200 且 URL 未变（仍在 reset 页面），视为失败
            if r2.status_code == 200 and "reset" in final_url2:
                logger.warning("  POST 后仍停留在 reset 页面，视为失败")
                return False
            # 其他 200：页面无错误关键词则允许通过
            if r2.status_code == 200:
                logger.info("  ✓ 密码重置表单提交成功（HTTP 200，无错误）")
                return True
        except Exception as e:
            logger.warning(f"  POST 新密码失败: {e}")

        return False

    # ── Step B2：完成密码重置（通过 OTP 验证码 + session） ──

    def complete_reset_via_otp(self, otp: str, new_password: str) -> bool:
        """用 OTP 验证码完成登录，然后修改密码。"""
        if not self.session_id:
            return False
        json_hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            r = self.s.post(
                f"{BASE_URL}/api/auth/sessions/{self.session_id}/otp/email",
                headers=json_hdrs,
                json={"oneTimePassword": otp},
                timeout=20,
            )
            logger.info(f"  OTP 验证: HTTP {r.status_code}")
            if r.status_code != 200:
                return False

            # 获取认证 token/cookie 后改密
            csrf = self.s.cookies.get("_st-JBA", "")
            if csrf:
                json_hdrs["X-XSRF-TOKEN"] = csrf
            change_candidates = [
                (f"{BASE_URL}/api/auth/sessions/{self.session_id}/password/change",
                 {"newPassword": new_password}),
                (f"{BASE_URL}/api/account/password",
                 {"newPassword": new_password}),
            ]
            for url, body in change_candidates:
                cr = self.s.post(url, headers=json_hdrs, json=body, timeout=20)
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
    poll_timeout: float = 300,
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

    # 预检：该域名是否属于 YYDS 临时邮箱服务
    _EXTERNAL_DOMAINS = {
        "outlook.com", "outlook.cn", "hotmail.com", "hotmail.cn",
        "live.com", "live.cn", "msn.com",
        "gmail.com", "googlemail.com",
        "qq.com", "163.com", "126.com", "sina.com", "sina.cn",
        "yahoo.com", "yahoo.co.jp", "protonmail.com", "icloud.com",
    }
    domain_lower = domain.lower()
    if domain_lower in _EXTERNAL_DOMAINS:
        return {
            "ok": False,
            "error": (
                f"该账号使用真实外部邮箱 (@{domain}) 注册，"
                "自动改密功能仅支持通过 YYDS 临时邮箱注册的账号。"
                "请手动访问 JetBrains 忘记密码页面完成重置。"
            ),
        }

    # 检查 YYDS 当前可用域名池：不在列表 = 旧服务域名或已下线域名
    try:
        yyds_domains = get_yyds_domain_set()
        if yyds_domains and domain_lower not in yyds_domains:
            return {
                "ok": False,
                "error": (
                    f"域名 @{domain} 不属于当前 YYDS Mail 临时邮箱服务。"
                    "该账号可能是通过旧版邮箱服务（如 nimail.cn）或其他平台注册的，"
                    "自动改密无法获取该收件箱，请手动到 JetBrains 忘记密码页面完成重置。"
                ),
            }
    except Exception:
        pass  # 查不到域名列表时不阻断，让后续步骤自然报错

    # Step 1: 重建临时邮箱
    log("重建临时邮箱...", "recreate_email")
    try:
        apply_email_exact(local_part, domain)
    except Exception as e:
        return {"ok": False, "error": f"无法重建临时邮箱 ({email}): {e}"}

    try:
        # Step 2: 触发忘记密码
        log("向 JetBrains 请求发送密码重置邮件...", "request_reset")
        reset_request_time = time.time()  # 记录请求时刻，过滤旧邮件用
        resetter = JBPasswordResetter()
        ok = resetter.request_forgot_password(email)
        if not ok:
            return {"ok": False, "error": "触发 JetBrains 忘记密码失败，可能该邮箱不存在或 API 端点已变更"}

        log("重置邮件已请求，等待邮件到达...", "polling")

        # Step 3: 轮询邮件（只看 reset_request_time 之后到达的新邮件）
        result = poll_email_for_reset(email, timeout=poll_timeout, interval=5,
                                      since=reset_request_time)
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
        # 清理临时邮箱（同时清除持久化 token 缓存）
        try:
            delete_email(email)
        except Exception:
            pass
        try:
            _remove_from_token_cache(email)
        except Exception:
            pass
