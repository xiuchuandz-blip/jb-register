"""
GhostReg 半自动注册流程
DrissionPage 控制浏览器完成 7+1 步：
  1. Accept All (Cookie 弹窗)
  2. Continue with email
  3. 填邮箱 → Continue
  4. reCAPTCHA checkbox ("I'm not a robot")
  5. ★ 等待用户手动完成图片验证 ★
  6. 6 位邮箱验证码
  7. First name + Last name + Password → Create account
  8. （可选）自动跳转 tokens 页 → 选国家 → 弹出 Add credit card
"""
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from DrissionPage import Chromium, ChromiumOptions

from . import config
from . import email_service
from . import captcha_solver
from . import captcha_service

logger = logging.getLogger("ghostreg.register")

# ── 指纹混淆 ─────────────────────────────────────────────────────────────────
# 每次新文档加载前由 CDP 注入，覆盖所有常见自动化检测点

_STEALTH_JS = r"""
(function() {
  // 1. 删除 navigator.webdriver
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });

  // 2. 伪造 navigator.plugins（三个真实 Chrome 插件）
  const _fp = [
    { name: 'Chrome PDF Plugin',  filename: 'internal-pdf-viewer',              description: 'Portable Document Format' },
    { name: 'Chrome PDF Viewer',  filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
    { name: 'Native Client',      filename: 'internal-nacl-plugin',             description: '' },
  ];
  const pa = Object.create(PluginArray.prototype);
  _fp.forEach(function(p, i) {
    const pl = Object.create(Plugin.prototype);
    Object.defineProperty(pl, 'name',        { value: p.name,        enumerable: true });
    Object.defineProperty(pl, 'filename',    { value: p.filename,    enumerable: true });
    Object.defineProperty(pl, 'description', { value: p.description, enumerable: true });
    Object.defineProperty(pl, 'length',      { value: 1 });
    pa[i] = pl;
  });
  Object.defineProperty(pa, 'length', { value: _fp.length });
  Object.defineProperty(navigator, 'plugins', { get: () => pa, enumerable: true, configurable: true });

  // 3. 语言 & mimeTypes
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'], configurable: true });
  Object.defineProperty(navigator, 'mimeTypes', { get: () => ({ length: 0 }), configurable: true });

  // 4. chrome.runtime 存在（避免 undefined 检测）
  window.chrome = window.chrome || {};
  window.chrome.runtime = window.chrome.runtime || {
    OnInstalledReason: {}, OnRestartRequiredReason: {},
    PlatformArch: {}, PlatformNaclArch: {}, PlatformOs: {}, RequestUpdateCheckStatus: {}
  };
  window.chrome.app = window.chrome.app || { isInstalled: false };

  // 5. Permissions — 通知权限返回 default（非 denied）
  if (window.Permissions && window.Permissions.prototype) {
    var _origQ = window.Permissions.prototype.query;
    window.Permissions.prototype.query = function(p) {
      if (p && p.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission || 'default', onchange: null });
      }
      return _origQ.apply(this, arguments);
    };
  }

  // 6. Canvas 轻微随机噪声（改变指纹哈希但不破坏渲染）
  var _noise = (Math.random() * 4 | 0) - 2;  // -2 ~ +1
  var _origGID = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function() {
    var d = _origGID.apply(this, arguments);
    for (var i = 0; i < d.data.length; i += 256) {
      if (d.data[i] !== undefined)
        d.data[i] = Math.max(0, Math.min(255, d.data[i] + _noise));
    }
    return d;
  };

  // 7. WebGL 渲染器字符串混淆
  var _origGP = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return _origGP.apply(this, arguments);
  };
  if (window.WebGL2RenderingContext) {
    var _origGP2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris OpenGL Engine';
      return _origGP2.apply(this, arguments);
    };
  }

  // 8. 清理自动化全局属性
  ['__selenium_evaluate','__webdriver_evaluate','__selenium_unwrapped','__webdriver_unwrapped',
   '__driver_evaluate','__driver_unwrapped','__fxdriver_evaluate','__fxdriver_unwrapped',
   'calledSelenium','_selenium','callPhantom','_phantom','phantom','__nightmare',
   'domAutomation','domAutomationController','_Selenium_IDE_Recorder'].forEach(function(k) {
    try { delete window[k]; } catch(e) {}
    Object.defineProperty(window, k, { get: () => undefined, configurable: true });
  });
})();
"""


def _stealth_options(co: "ChromiumOptions"):
    """为 ChromiumOptions 追加所有指纹混淆相关启动参数"""
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--disable-features=IsolateOrigins,site-per-process")
    co.set_argument("--disable-infobars")
    co.set_argument("--password-store=basic")
    co.set_argument("--use-mock-keychain")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    co.set_argument("--lang=en-US")
    co.set_argument("--disable-popup-blocking")
    co.set_argument("--exclude-switches=enable-automation")
    co.set_argument("--disable-automation")
    # 禁止 Google 账户登录/选择弹窗（新 profile 首次启动时会弹 "选择账户" 对话框）
    co.set_argument("--bwsi")                         # Browse Without Sign-In
    co.set_argument("--noerrdialogs")                 # 不弹错误/提示对话框
    co.set_argument("--disable-features=SigninEmailConfirmation")
    co.set_argument("--disable-sync")                 # 禁用 Google Sync
    co.set_argument("--disable-background-networking") # 阻止账户相关后台请求


def _apply_stealth(tab):
    """向 tab 注入指纹混淆 JS（每次新文档加载前自动执行）"""
    try:
        tab.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=_STEALTH_JS)
        logger.debug("[Stealth] 指纹混淆脚本已注入")
    except Exception as e:
        logger.warning(f"[Stealth] CDP 注入失败(忽略): {e}")


# reCAPTCHA 出现标记（线程安全，多窗口并发时各自独立）
_thread_local = threading.local()


def _mark_recaptcha_seen():
    """标记当前线程的任务已看到 reCAPTCHA iframe"""
    _thread_local.recaptcha_seen = True


def _was_recaptcha_seen() -> bool:
    """当前线程的任务是否曾看到过 reCAPTCHA"""
    return getattr(_thread_local, 'recaptcha_seen', False)


def _reset_recaptcha_seen():
    """重置标记（新任务开始时调用）"""
    _thread_local.recaptcha_seen = False


# ── 数据结构 ──

@dataclass
class AccountResult:
    """注册结果"""
    email: str
    password: str
    success: bool
    error: str = ""
    browser: object = None  # 成功时保留浏览器实例引用（Chromium）


@dataclass
class TaskStatus:
    """单个注册任务的实时状态"""
    task_id: int = 0
    step: int = 0          # 当前步骤 1-7, 0=未开始
    step_label: str = ""   # 当前步骤描述
    email: str = ""
    password: str = ""
    success: Optional[bool] = None   # None=进行中, True/False=完成
    error: str = ""


# 状态回调类型: (task_id, status) → void
StatusCallback = Callable[[TaskStatus], None]


def _noop_callback(status: TaskStatus):
    pass


# ── 浏览器创建 ──

# 浏览器临时数据目录（避免占用 C 盘空间，存到项目所在盘）
_PROJECT_DIR = Path(__file__).parent.parent
_BROWSER_DATA_DIR = _PROJECT_DIR / "browser_data"
_BROWSER_DATA_DIR.mkdir(exist_ok=True)

_next_port = 9600  # 起始端口，每个实例递增
_port_lock = threading.Lock()


def _alloc_port() -> int:
    global _next_port
    with _port_lock:
        port = _next_port
        _next_port += 1
    return port


def _cleanup_browser_data(browser_or_path) -> None:
    """清理浏览器用户数据目录。
    接受 Chromium 实例（取其 user_data_path）或直接传 Path/str。
    """
    try:
        if hasattr(browser_or_path, "user_data_path"):
            data_path = Path(browser_or_path.user_data_path)
        else:
            data_path = Path(str(browser_or_path))
        if data_path.exists() and data_path.parent == _BROWSER_DATA_DIR:
            shutil.rmtree(data_path, ignore_errors=True)
            logger.info(f"[Cleanup] 已清理浏览器数据: {data_path.name}")
    except Exception as e:
        logger.warning(f"[Cleanup] 清理浏览器数据失败: {e}")


def cleanup_stale_browser_data() -> int:
    """清理所有没有活跃浏览器的 browser_data 子目录。
    在服务启动时调用。返回清理的目录数量。
    """
    if not _BROWSER_DATA_DIR.exists():
        return 0
    # 获取当前活跃的 debug 端口
    try:
        active = scan_debug_browsers()
        active_ports = {str(b["port"]) for b in active}
    except Exception:
        active_ports = set()
    cleaned = 0
    for sub in _BROWSER_DATA_DIR.iterdir():
        if sub.is_dir() and sub.name not in active_ports:
            try:
                shutil.rmtree(sub, ignore_errors=True)
                cleaned += 1
                logger.info(f"[Cleanup] 启动清理: {sub.name}")
            except Exception as e:
                logger.warning(f"[Cleanup] 清理 {sub.name} 失败: {e}")
    if cleaned:
        logger.info(f"[Cleanup] 共清理 {cleaned} 个过期浏览器数据目录")
    return cleaned


def _find_browser_path(browser_type: str) -> str | None:
    """
    查找浏览器可执行文件路径。
    优先级：settings.json 中的 browser_path → 标准安装位置（Windows / Linux / macOS）
    """
    import os
    import platform
    import shutil

    # 0. 优先使用用户在 UI 中手动配置的路径
    from . import config as _cfg
    custom = _cfg._load_settings().get("browser_path", "").strip()
    if custom and os.path.isfile(custom):
        return custom

    _sys = platform.system()

    if _sys == "Windows":
        candidates = {
            "chrome": [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            ],
            "edge": [
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            ],
            "brave": [
                os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                os.path.expandvars(r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            ],
        }
    elif _sys == "Darwin":  # macOS
        candidates = {
            "chrome": [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ],
            "edge": [
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ],
            "brave": [
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            ],
        }
    else:  # Linux
        candidates = {
            "chrome": [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/snap/bin/chromium",
                "/usr/local/bin/google-chrome",
            ],
            "edge": [
                "/usr/bin/microsoft-edge",
                "/usr/bin/microsoft-edge-stable",
            ],
            "brave": [
                "/usr/bin/brave-browser",
                "/usr/bin/brave",
            ],
        }

    for path in candidates.get(browser_type, []):
        if os.path.isfile(path):
            return path

    # 最后尝试 which / shutil.which（$PATH 中的任意名称）
    which_names = {
        "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
        "edge":   ["microsoft-edge", "microsoft-edge-stable"],
        "brave":  ["brave-browser", "brave"],
    }
    for name in which_names.get(browser_type, []):
        found = shutil.which(name)
        if found:
            return found

    return None


def _create_browser(browser_type: str = "chrome") -> Chromium:
    """创建浏览器实例。browser_type: chrome / edge / brave"""
    port = _alloc_port()
    co = ChromiumOptions()
    co.set_local_port(port)
    co.set_user_data_path(str(_BROWSER_DATA_DIR / str(port)))
    co.incognito()
    _stealth_options(co)

    # 显式查找并设置浏览器路径（所有类型都走这个逻辑，避免 DrissionPage 自动检测失败）
    path = _find_browser_path(browser_type)
    if path:
        co.set_browser_path(path)
        logger.info(f"[Browser] 使用 {browser_type}: {path}")
    else:
        logger.warning(
            f"[Browser] 未找到 {browser_type} 可执行文件。"
            f"请在「设置」→「浏览器路径」中手动填写，或安装 Chrome/Edge/Brave 到默认位置。"
        )

    browser = Chromium(co)
    _apply_stealth(browser.latest_tab)
    return browser


# ═══════════════════════════════════════════════════════════
#  Step 1: Cookie 弹窗
# ═══════════════════════════════════════════════════════════

def _handle_cookie_consent(tab) -> bool:
    logger.info("[Step 1] 处理 Cookie 弹窗...")
    try:
        accept_btn = tab.ele("text:Accept All", timeout=8)
        if accept_btn:
            accept_btn.click()
            logger.info("[Step 1] 已点击 Accept All")
            time.sleep(1)
            return True
    except Exception as e:
        logger.warning(f"[Step 1] 异常（忽略）: {e}")
    logger.info("[Step 1] 未发现 Cookie 弹窗或已处理，继续")
    return True


# ═══════════════════════════════════════════════════════════
#  Step 2: Continue with email
# ═══════════════════════════════════════════════════════════

def _click_continue_with_email(tab) -> bool:
    logger.info("[Step 2] 点击 Continue with email...")
    try:
        tab.wait.doc_loaded(timeout=config.PAGE_TIMEOUT)
    except Exception:
        pass
    try:
        btn = tab.ele("text:Continue with email", timeout=config.PAGE_TIMEOUT)
        if not btn:
            logger.warning("[Step 2] 未找到按钮，可能已在邮箱输入页")
            return True
        btn.click()
        time.sleep(2)
        logger.info("[Step 2] 已点击 Continue with email")
    except Exception as e:
        logger.warning(f"[Step 2] 异常（尝试继续）: {e}")
    return True


# ═══════════════════════════════════════════════════════════
#  Step 3: 填写邮箱
# ═══════════════════════════════════════════════════════════

def _fill_email(tab, email: str) -> bool:
    logger.info(f"[Step 3] 填写邮箱: {email}")
    try:
        tab.wait.doc_loaded(timeout=15)
    except Exception:
        pass

    email_input = None
    for selector in ["@placeholder=Email", "@type=email", "@name=email", "tag:input"]:
        try:
            email_input = tab.ele(selector, timeout=8)
            if email_input:
                break
        except Exception:
            pass

    if not email_input:
        logger.error("[Step 3] 未找到邮箱输入框")
        return False

    try:
        email_input.clear()
        email_input.input(email)
        time.sleep(0.5)
    except Exception as e:
        logger.error(f"[Step 3] 输入邮箱失败: {e}")
        return False

    try:
        continue_btn = tab.ele("text:Continue", timeout=5) or tab.ele("@type=submit", timeout=5)
        if continue_btn:
            continue_btn.click()
            logger.info("[Step 3] 已点击 Continue")
        else:
            email_input.input("\n")
    except Exception:
        pass

    time.sleep(2)
    return True


# ═══════════════════════════════════════════════════════════
#  Step 4: reCAPTCHA checkbox
# ═══════════════════════════════════════════════════════════

def _click_recaptcha_checkbox(tab) -> bool:
    """点击 'I'm not a robot' checkbox，含页面刷新等待和重试"""
    logger.info("[Step 4] 等待页面加载 reCAPTCHA...")

    # 点 Continue 后页面会刷新，必须等刷新完成
    try:
        tab.wait.doc_loaded(timeout=config.PAGE_TIMEOUT)
    except Exception:
        pass
    time.sleep(3)  # 等 reCAPTCHA iframe 异步加载

    # 重试查找 iframe（最多 6 次，每次等 3 秒）
    anchor_frame = None
    for attempt in range(1, 7):
        logger.info(f"[Step 4] 查找 reCAPTCHA iframe（第 {attempt}/6 次）...")
        try:
            anchor_frame = tab.get_frame("@title=reCAPTCHA", timeout=10)
        except Exception:
            pass

        if not anchor_frame:
            try:
                iframes = tab.eles("tag:iframe")
                for ifr in iframes:
                    src = (ifr.attr("src") or "").lower()
                    title = (ifr.attr("title") or "").lower()
                    if ("recaptcha" in src and "anchor" in src) or title == "recaptcha":
                        try:
                            anchor_frame = tab.get_frame(ifr)
                            break
                        except Exception:
                            pass
            except Exception:
                pass

        if anchor_frame:
            _mark_recaptcha_seen()
            break

        # 每次重试前也检查一下是否已经跳过
        if _captcha_is_done(tab):
            logger.info("[Step 4] 等待中检测到页面已跳过 reCAPTCHA")
            return True

        logger.warning(f"[Step 4] 第 {attempt} 次未找到 iframe，等待 3 秒...")
        time.sleep(3)

    if not anchor_frame:
        logger.error("[Step 4] 6 次重试后仍未找到 reCAPTCHA iframe")
        return False

    # 查找并点击 checkbox（iframe 可能加载了但内容还没渲染，重试 3 次）
    checkbox = None
    for cb_try in range(1, 4):
        try:
            checkbox = anchor_frame.ele("#recaptcha-anchor", timeout=10)
        except Exception:
            pass
        if not checkbox:
            try:
                checkbox = anchor_frame.ele(".recaptcha-checkbox", timeout=5)
            except Exception:
                pass
        if checkbox:
            break
        logger.warning(f"[Step 4] checkbox 第 {cb_try} 次未找到，等 3 秒...")
        time.sleep(3)

    if not checkbox:
        logger.error("[Step 4] 多次重试后仍未找到 checkbox")
        return False

    checkbox.click()
    logger.info("[Step 4] 已点击 'I'm not a robot'")
    time.sleep(2)

    if _captcha_is_done(tab):
        logger.info("[Step 4] reCAPTCHA 直接通过（无图片验证）")
        return True

    return True  # checkbox 已点击，图片验证交给用户


# ═══════════════════════════════════════════════════════════
#  Step 5: 等待用户手动完成验证码 (半自动核心)
# ═══════════════════════════════════════════════════════════

def _wait_for_manual_captcha(tab, cancel_flag: Callable[[], bool] | None = None) -> bool:
    """
    等待用户手动完成 reCAPTCHA 图片验证，无时限。
    cancel_flag: 可选的取消检查函数，返回 True 时退出等待。
    """
    if _captcha_is_done(tab):
        logger.info("[Step 5] reCAPTCHA 已通过（无需手动操作）")
        return True

    logger.info("[Step 5] 请在浏览器中手动完成验证码（无时限，慢慢来）...")
    poll_count = 0

    while True:
        poll_count += 1
        if cancel_flag and cancel_flag():
            logger.info("[Step 5] 收到取消信号，停止等待")
            return False
        if _captcha_is_done(tab):
            logger.info(f"[Step 5] 验证码已通过（轮询 {poll_count} 次）")
            return True
        time.sleep(2)


def _captcha_is_done(tab) -> bool:
    """
    检查 reCAPTCHA 验证码是否已通过。
    核心逻辑（移植自 Fork）：
    1) 先检查 dialog[open] 是否还包含 reCAPTCHA iframe → 如果是，验证还没完
    2) dialog 关了之后再检测 OTP/密码/文字等信号
    3) 降级：reCAPTCHA checkbox aria-checked=true
    4) 降级：dialog 关了 + 之前看到过 reCAPTCHA → 验证通过
    """
    try:
        result = tab.run_js("""
            // 如果 dialog[open] 还在且内含 reCAPTCHA，验证还没完成
            const dialog = document.querySelector('dialog[open]');
            if (dialog) {
                const hasRecaptcha = dialog.querySelector('iframe[title*="reCAPTCHA"], iframe[title*="recaptcha"]');
                if (hasRecaptcha) return 'dialog_blocking';
            }

            // 已进入 OTP 页面
            if (document.querySelector('input[name="otp-1"]')) return 'otp';
            const ml = document.querySelectorAll('input[maxlength="1"]');
            if (ml.length >= 4) return 'otp_ml';

            // 已进入注册表单
            if (document.querySelector('input[type="password"]')) return 'password';

            // 页面文字
            const txt = (document.body.textContent || '').toLowerCase();
            if (txt.includes('verification code') || txt.includes('enter the code')
                || txt.includes('confirm your email') || txt.includes('check your email'))
                return 'text_match';

            return '';
        """)

        if result == 'dialog_blocking':
            return False
        if result and result != '':
            logger.info(f"[Check] reCAPTCHA 已通过（信号: {result}）")
            return True
    except Exception:
        pass

    # 降级：reCAPTCHA checkbox aria-checked=true
    try:
        anchor_frame = tab.get_frame("@title=reCAPTCHA", timeout=2)
        if anchor_frame:
            _mark_recaptcha_seen()
            anchor = anchor_frame.ele("#recaptcha-anchor", timeout=1)
            if anchor and anchor.attr("aria-checked") == "true":
                logger.info("[Check] reCAPTCHA checkbox 已勾选")
                return True
    except Exception:
        pass

    # 降级：dialog 关了 + 之前看到过 reCAPTCHA → 验证通过
    if _was_recaptcha_seen():
        try:
            has_dialog = tab.run_js("return !!document.querySelector('dialog[open]')")
            if not has_dialog:
                logger.info("[Check] dialog 已关闭且曾看到 reCAPTCHA，验证通过")
                return True
        except Exception:
            pass

    return False


# ═══════════════════════════════════════════════════════════
#  Step 5-Platform: 打码平台 Token 注入
#  提取 sitekey → 调用打码平台 → 获取 gRecaptchaResponse → JS 注入
# ═══════════════════════════════════════════════════════════

def _extract_sitekey(tab) -> str:
    """从页面中提取 reCAPTCHA sitekey（从 iframe src 的 k= 参数中提取）"""
    try:
        sitekey = tab.run_js("""
            for (const f of document.querySelectorAll('iframe')) {
                const src = f.src || '';
                if (src.includes('recaptcha') && src.includes('/anchor')) {
                    const m = src.match(/[?&]k=([^&]+)/);
                    if (m) return m[1];
                }
            }
            // 降级：查找 data-sitekey 属性
            const el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            return '';
        """)
        if sitekey:
            logger.info(f"[Platform] 提取到 sitekey: {sitekey[:20]}...")
        return sitekey or ""
    except Exception as e:
        logger.warning(f"[Platform] 提取 sitekey 失败: {e}")
        return ""


def _inject_recaptcha_token(tab, token: str) -> bool:
    """
    将打码平台返回的 gRecaptchaResponse token 注入到页面中。
    核心步骤：填 textarea → 触发回调 → 关闭 dialog → 移除 reCAPTCHA iframe
    """
    # 将 token 存到 JS 变量（用 JSON.dumps 安全转义，防止 token 中含反引号/${}）
    import json as _json
    safe_token = _json.dumps(token)  # 输出带双引号的安全 JS 字符串
    try:
        tab.run_js(f'window.__ghostreg_token = {safe_token};')
    except Exception as e:
        logger.warning(f"[Platform] 设置 token 变量失败: {e}")
        return False

    try:
        result = tab.run_js("""
            const token = window.__ghostreg_token;
            let status = [];

            // 1. 填充所有 g-recaptcha-response textarea
            const textareas = document.querySelectorAll('textarea[id*="g-recaptcha-response"]');
            for (const ta of textareas) {
                ta.innerHTML = token;
                ta.value = token;
                ta.style.display = 'block';  // 有些是 hidden
            }
            status.push('filled:' + textareas.length);

            // 2. 触发 reCAPTCHA 回调（精确方法：遍历 clients 找 callback 属性）
            let callbackFired = false;
            try {
                const clients = ___grecaptcha_cfg.clients;
                for (const cid in clients) {
                    const client = clients[cid];
                    // 精确查找：reCAPTCHA 将回调存在嵌套对象的特定路径
                    function findCb(obj, depth) {
                        if (depth > 8 || !obj || typeof obj !== 'object') return null;
                        // 直接检查常见的回调属性名
                        for (const k of ['callback', 'success-callback', 'resolve']) {
                            if (typeof obj[k] === 'function') return obj[k];
                        }
                        for (const k in obj) {
                            if (typeof obj[k] === 'object' && obj[k] !== null) {
                                const found = findCb(obj[k], depth + 1);
                                if (found) return found;
                            }
                        }
                        return null;
                    }
                    const cb = findCb(client, 0);
                    if (cb) {
                        cb(token);
                        callbackFired = true;
                        status.push('callback_ok');
                        break;
                    }
                }
            } catch(e) { status.push('callback_err:' + e.message); }

            // 3. 全局回调降级
            if (!callbackFired) {
                try {
                    if (typeof onRecaptchaSuccess === 'function') {
                        onRecaptchaSuccess(token);
                        callbackFired = true;
                        status.push('global_cb');
                    }
                } catch(e) {}
            }

            // 4. 强制关闭 dialog（reCAPTCHA 弹窗容器）
            const dialogs = document.querySelectorAll('dialog[open]');
            for (const d of dialogs) {
                d.removeAttribute('open');
                d.close && d.close();
            }
            status.push('dialog_closed:' + dialogs.length);

            // 5. 隐藏/移除 reCAPTCHA iframe（防止遮挡后续操作）
            const iframes = document.querySelectorAll('iframe[title*="reCAPTCHA"], iframe[title*="recaptcha"], iframe[src*="recaptcha"]');
            let hidden = 0;
            for (const f of iframes) {
                f.style.display = 'none';
                f.style.visibility = 'hidden';
                f.style.width = '0';
                f.style.height = '0';
                // 将包含 iframe 的遮罩层也隐藏
                let parent = f.parentElement;
                for (let i = 0; i < 5 && parent; i++) {
                    if (parent.style && (parent.style.position === 'fixed' || parent.style.position === 'absolute'
                        || parent.style.zIndex > 1000 || getComputedStyle(parent).position === 'fixed')) {
                        parent.style.display = 'none';
                        break;
                    }
                    parent = parent.parentElement;
                }
                hidden++;
            }
            status.push('iframes_hidden:' + hidden);

            // 6. 移除所有 reCAPTCHA 遮罩层（Google 在 body 直接加的 div）
            const overlays = document.querySelectorAll('div[style*="z-index"][style*="position: fixed"], div[style*="z-index"][style*="position:fixed"]');
            let removed = 0;
            for (const o of overlays) {
                const zi = parseInt(getComputedStyle(o).zIndex);
                if (zi > 1000000) {
                    o.style.display = 'none';
                    removed++;
                }
            }
            status.push('overlays_removed:' + removed);

            return status.join('|');
        """)
        logger.info(f"[Platform] Token 注入结果: {result}")
        return True
    except Exception as e:
        logger.warning(f"[Platform] Token 注入失败: {e}")
        return False


def _cleanup_recaptcha_overlays(tab):
    """清理 reCAPTCHA 残留的 dialog/iframe/遮罩层，防止遮挡后续操作"""
    try:
        tab.run_js("""
            // 关闭所有 dialog
            document.querySelectorAll('dialog[open]').forEach(d => {
                d.removeAttribute('open');
                try { d.close(); } catch(e) {}
            });
            // 隐藏 reCAPTCHA iframe
            document.querySelectorAll('iframe[title*="reCAPTCHA"], iframe[title*="recaptcha"], iframe[src*="recaptcha"]').forEach(f => {
                f.style.display = 'none';
                f.style.width = '0';
                f.style.height = '0';
            });
            // 隐藏 Google reCAPTCHA 遮罩层（z-index > 1000000 的 fixed div）
            document.querySelectorAll('div').forEach(d => {
                const s = getComputedStyle(d);
                if (s.position === 'fixed' && parseInt(s.zIndex) > 1000000) {
                    d.style.display = 'none';
                }
            });
        """)
    except Exception:
        pass


def _solve_with_platform(tab, cancel_flag: Callable[[], bool] | None = None) -> bool:
    """
    使用打码平台解决 reCAPTCHA v2：
    1. 从页面提取 sitekey
    2. 调用打码平台 API 获取 token
    3. 注入 token 到页面
    4. 提交表单
    """
    if not captcha_service.is_enabled():
        logger.warning("[Platform] 打码平台未配置")
        return False

    logger.info("[Platform] 开始打码平台流程...")

    # 提取 sitekey（最多重试 3 次，每次等 1 秒）
    sitekey = ""
    for attempt in range(3):
        sitekey = _extract_sitekey(tab)
        if sitekey:
            break
        logger.warning(f"[Platform] 第 {attempt+1} 次未提取到 sitekey，等 1 秒...")
        time.sleep(1)

    if not sitekey:
        logger.error("[Platform] 无法提取 sitekey")
        return False

    # 获取当前页面 URL
    try:
        page_url = tab.url or config.SIGNUP_URL
    except Exception:
        page_url = config.SIGNUP_URL

    # 调用打码平台（这一步 10-80 秒，无法加速）
    logger.info(f"[Platform] 调用打码平台... (sitekey={sitekey[:20]}...)")
    try:
        token = captcha_service.solve_recaptcha_v2(page_url, sitekey)
    except captcha_service.CaptchaServiceError as e:
        logger.error(f"[Platform] 打码平台失败: {e}")
        return False

    if not token:
        logger.error("[Platform] 打码平台未返回 token")
        return False

    # 注入 token + 关闭 dialog + 清理 iframe
    logger.info(f"[Platform] 获得 token（{len(token)} 字符），注入并清理...")
    if not _inject_recaptcha_token(tab, token):
        logger.error("[Platform] Token 注入失败")
        return False

    time.sleep(0.5)

    # 注入后检测是否已通过（dialog 关闭 + 回调触发可能导致自动跳转）
    if _captcha_is_done(tab) or _has_left_email_page(tab):
        logger.info("[Platform] Token 注入后已通过")
        return True

    # Token 已注入成功，后续 Continue 提交交给外层 Step 5b 统一处理
    # 不在这里重复提交，避免频繁点击重置风控计时器
    logger.info("[Platform] Token 已注入，等待外层提交 Continue")
    return True


# ═══════════════════════════════════════════════════════════
#  Step 5-AI: 纯 CDP 截图 + AI 坐标点击（零 iframe 操作）
#  CDP 截图瞬间完成 → AI 返回坐标 → tab.actions 绝对坐标点击
# ═══════════════════════════════════════════════════════════

def _cdp_screenshot(tab) -> bytes | None:
    """CDP 截整页，瞬间完成，不会挂死"""
    import base64 as _b64
    try:
        r = tab.run_cdp("Page.captureScreenshot", format="png")
        if r and "data" in r:
            return _b64.b64decode(r["data"])
    except Exception as e:
        logger.warning(f"[AI] CDP 截图失败: {e}")
    return None


def _has_challenge_visible(tab) -> bool:
    """用 JS 检测 bframe 是否可见（不进 iframe，不会挂）"""
    try:
        result = tab.run_js("""
            for (const f of document.querySelectorAll('iframe')) {
                if ((f.src||'').includes('bframe') || (f.title||'').includes('recaptcha challenge')) {
                    const r = f.getBoundingClientRect();
                    if (r.width > 50 && r.height > 50) return true;
                }
            }
            return false;
        """)
        return bool(result)
    except Exception:
        return False


def _solve_recaptcha_with_ai(tab, cancel_flag: Callable[[], bool] | None = None) -> bool:
    """
    AI 自动识别 reCAPTCHA：纯 CDP 截图 + 坐标点击，零 iframe 操作。
    整页截图 → AI 看图识别要点击的格子 → 返回坐标 → tab.actions 点击。
    """
    round_num = 0
    # 获取视口大小（一次性）
    try:
        vp = tab.run_js("return {w: window.innerWidth, h: window.innerHeight}")
        page_w, page_h = vp["w"], vp["h"]
    except Exception:
        page_w, page_h = 1920, 1080

    while True:
        round_num += 1
        if cancel_flag and cancel_flag():
            logger.info("[AI] 取消")
            return False
        if _captcha_is_done(tab):
            logger.info(f"[AI] 已通过（第 {round_num} 轮前）")
            return True

        logger.info(f"[AI] ===== 第 {round_num} 轮 =====")

        # 检查 challenge 弹窗是否可见
        if not _has_challenge_visible(tab):
            logger.warning("[AI] 弹窗不可见，等 2 秒")
            time.sleep(2)
            if _captcha_is_done(tab):
                return True
            continue

        # CDP 截图（瞬间完成）
        time.sleep(0.5)
        img = _cdp_screenshot(tab)
        if not img:
            time.sleep(1)
            continue
        logger.info(f"[AI] 截图 {len(img)} bytes")

        # AI 识别坐标
        coords = captcha_solver.solve_click(img)
        if not coords:
            logger.warning("[AI] AI 无坐标返回")
            time.sleep(2)
            continue

        logger.info(f"[AI] {len(coords)} 个坐标: {coords!r}")

        # 坐标换算 + 点击
        clicked = 0
        for nx, ny in coords:
            ax = (page_w / 1000) * nx
            ay = (page_h / 1000) * ny
            try:
                tab.actions.move_to((ax, ay)).click()
                clicked += 1
                logger.info(f"[AI] 点击 ({nx},{ny})→({ax:.0f},{ay:.0f})")
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"[AI] 点击失败: {e}")

        if clicked == 0:
            time.sleep(2)
            continue

        # 点 Verify 按钮：再截图让 AI 找按钮位置（或用固定偏移）
        time.sleep(1)
        # 用 JS 获取 bframe 位置，Verify 在其底部
        try:
            btn_info = tab.run_js("""
                for (const f of document.querySelectorAll('iframe')) {
                    if ((f.src||'').includes('bframe')) {
                        const r = f.getBoundingClientRect();
                        if (r.y >= 0 && r.height > 50)
                            return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height - 35});
                    }
                }
                return '';
            """)
            if btn_info:
                import json as _json
                pos = _json.loads(btn_info)
                if pos["y"] > 0:
                    tab.actions.move_to((pos["x"], pos["y"])).click()
                    logger.info(f"[AI] Verify ({pos['x']:.0f},{pos['y']:.0f})")
                else:
                    logger.warning(f"[AI] Verify 坐标异常 y={pos['y']}")
        except Exception as e:
            logger.warning(f"[AI] Verify 失败: {e}")

        time.sleep(2)
        if _captcha_is_done(tab):
            logger.info(f"[AI] ✅ 通过！（第 {round_num} 轮）")
            return True


# ═══════════════════════════════════════════════════════════
#  Step 5b: 验证码完成后自动提交 Continue
#  reCAPTCHA 在 <dialog open=""> 内，验证通过后 dialog 关闭，
#  但 React 不会自动提交表单，需要再次点击 Continue 按钮。
# ═══════════════════════════════════════════════════════════

def _has_left_email_page(tab) -> bool:
    """检测是否已离开初始邮箱输入页（到了 OTP / 注册表单等下一步）。
    配合 _captcha_is_done 的 dialog_blocking 机制，文字检测不会误判。
    """
    try:
        result = tab.run_js("""
            if (document.querySelector('input[name="otp-1"]')) return 'otp';
            const ml = document.querySelectorAll('input[maxlength="1"]');
            if (ml.length >= 4) return 'otp_ml';
            if (document.querySelector('input[name="firstName"]')
                || document.querySelector('input[placeholder="First name"]')) return 'reg';
            if (document.querySelector('input[type="password"]')) return 'pwd';
            const txt = (document.body.textContent || '').toLowerCase();
            if (txt.includes('confirm your email') || txt.includes('enter the code')
                || txt.includes('check your') || txt.includes('we sent')) return 'txt';
            return '';
        """)
        return bool(result)
    except Exception:
        pass
    return False


def _click_continue_after_captcha(tab) -> bool:
    """
    验证码完成后，自动提交 Continue 表单（移植自 Fork 的 3 策略 25 次循环）。
    核心难点：reCAPTCHA 在 <dialog open=""> 内，验证通过后 dialog 关闭，
    但 React 不会自动提交表单，需要再次点击 Continue 按钮。
    """
    logger.info("[Step 5b] 自动提交 Continue...")

    # ── 阶段1：等 dialog 关闭（最多 30 秒） ──
    dialog_was_open = False
    for wait_i in range(30):
        try:
            dlg_state = tab.run_js("""
                const d = document.querySelector('dialog[open]');
                if (d) {
                    const hasRecaptcha = !!d.querySelector('iframe[title*="reCAPTCHA"], iframe[title*="recaptcha"]');
                    return hasRecaptcha ? 'captcha_active' : 'dialog_open';
                }
                return 'closed';
            """)
            if dlg_state and 'captcha' in str(dlg_state):
                dialog_was_open = True
            if dlg_state == 'closed':
                if dialog_was_open:
                    logger.info(f"[Step 5b] dialog 已关闭（等待 {wait_i}s）")
                break
        except Exception:
            break
        time.sleep(1)

    time.sleep(0.5)

    # ── 阶段2：提交一次 → 等足够久 → 没跳转再重试 ──
    # JetBrains 有风控延迟（30-60秒+），频繁点击提交会重置计时器！
    # 策略：提交一次后安静等待，只轮询检测跳转，不重复提交。
    MAX_SUBMIT_ROUNDS = 3     # 最多提交 3 轮
    WAIT_PER_ROUND = 60       # 每轮等待 60 秒（风控实测需要 30-60 秒）

    for submit_round in range(MAX_SUBMIT_ROUNDS):
        if _has_left_email_page(tab):
            logger.info(f"[Step 5b] 已进入下一步")
            return True

        # 提交一次（3 种策略按优先级尝试，只执行第一个成功的）
        submitted = False

        # 策略A：JS requestSubmit
        if not submitted:
            try:
                result = tab.run_js("""
                    const dialog = document.querySelector('dialog[open]');
                    if (dialog) dialog.removeAttribute('open');
                    const form = document.querySelector('form');
                    if (!form) return 'no_form';
                    try {
                        if (typeof form.requestSubmit === 'function') {
                            form.requestSubmit();
                            return 'requestSubmit';
                        }
                    } catch(e) {}
                    const btn = form.querySelector('button[type="submit"]');
                    if (btn) { btn.click(); return 'btn_click'; }
                    try { form.submit(); return 'form_submit'; } catch(e) {}
                    return 'failed';
                """)
                if result and result in ('requestSubmit', 'btn_click', 'form_submit'):
                    logger.info(f"[Step 5b] 第 {submit_round+1} 轮提交: {result}")
                    submitted = True
            except Exception:
                pass

        # 策略B：DrissionPage 点击 submit
        if not submitted:
            try:
                submit_btn = tab.ele("@type=submit", timeout=2)
                if submit_btn:
                    submit_btn.click()
                    logger.info(f"[Step 5b] 第 {submit_round+1} 轮: DrissionPage click submit")
                    submitted = True
            except Exception:
                pass

        # 策略C：Enter 键
        if not submitted:
            try:
                tab.run_js("""
                    const input = document.querySelector('input[name="email"]');
                    if (input) {
                        input.focus();
                        input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', keyCode:13, bubbles:true}));
                        input.dispatchEvent(new KeyboardEvent('keypress', {key:'Enter', code:'Enter', keyCode:13, bubbles:true}));
                        input.dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', code:'Enter', keyCode:13, bubbles:true}));
                    }
                """)
                logger.info(f"[Step 5b] 第 {submit_round+1} 轮: Enter 键")
                submitted = True
            except Exception:
                pass

        # 提交后安静等待，只轮询检测跳转，不重复提交
        logger.info(f"[Step 5b] 已提交，等待页面跳转（最多 {WAIT_PER_ROUND}s）...")
        for wait_i in range(WAIT_PER_ROUND):
            time.sleep(1)
            if _has_left_email_page(tab):
                logger.info(f"[Step 5b] 第 {submit_round+1} 轮，等待 {wait_i+1}s 后跳转成功")
                return True

        logger.info(f"[Step 5b] 第 {submit_round+1} 轮等待 {WAIT_PER_ROUND}s 未跳转，准备重试...")

    logger.warning(f"[Step 5b] {MAX_SUBMIT_ROUNDS} 轮提交均未跳转")
    return False


# ═══════════════════════════════════════════════════════════
#  Step 6: 邮箱验证码
# ═══════════════════════════════════════════════════════════

def _fill_verification_code(tab, email: str) -> bool:
    logger.info(f"[Step 6] 等待页面加载...")

    # 等验证码输入页面加载完
    try:
        tab.wait.doc_loaded(timeout=15)
    except Exception:
        pass
    time.sleep(3)

    # 打印当前页面状态帮助调试
    try:
        logger.info(f"[Step 6] 当前 URL: {tab.url}")
    except Exception:
        pass

    logger.info(f"[Step 6] 开始轮询邮箱验证码: {email}")
    code = email_service.poll_verification_code(email)
    logger.info(f"[Step 6] 获取到验证码: {code}")

    # 重新等页面（轮询邮件期间页面可能还在加载）
    time.sleep(1)

    # 尝试各种方式找到验证码输入框
    # 方式1: maxlength=1 的单字符输入框（6 个一组）
    code_inputs = None
    try:
        code_inputs = tab.eles("input[maxlength='1']", timeout=5)
    except Exception:
        pass
    if not code_inputs:
        try:
            code_inputs = tab.eles("input[autocomplete='one-time-code']", timeout=3)
        except Exception:
            pass

    if code_inputs and len(code_inputs) >= 4:
        count = min(len(code), len(code_inputs))
        for i in range(count):
            try:
                code_inputs[i].click()
                time.sleep(0.15)
                code_inputs[i].input(code[i])
                time.sleep(0.3)  # 等 JetBrains 前端 JS 处理每位输入
            except Exception:
                pass
        logger.info(f"[Step 6] 已逐个填入 {count} 位验证码")
        time.sleep(2)  # 等前端验证完成再继续
        return True

    # 方式2: 单个输入框
    single_input = None
    for selector in ["input[placeholder*='code']", "input[placeholder*='Code']",
                     "@name=code", "input[inputmode='numeric']", "input[autocomplete='one-time-code']"]:
        try:
            single_input = tab.ele(selector, timeout=3)
            if single_input:
                break
        except Exception:
            pass

    if single_input:
        try:
            single_input.clear()
            single_input.input(code)
            logger.info("[Step 6] 已填入验证码（单输入框）")
            time.sleep(1)
            return True
        except Exception as e:
            logger.warning(f"[Step 6] 填入失败: {e}")

    # 方式3: 找页面上所有可见 input
    all_inputs = []
    try:
        all_inputs = tab.eles("tag:input")
    except Exception:
        pass
    visible_inputs = [inp for inp in all_inputs
                      if inp.attr("type") in ("text", "tel", "number", None)]
    if len(visible_inputs) >= 6:
        for i, ch in enumerate(code[:6]):
            visible_inputs[i].input(ch)
            time.sleep(0.1)
        time.sleep(1)
        return True

    logger.error("[Step 6] 未找到验证码输入框")
    return False


# ═══════════════════════════════════════════════════════════
#  Step 7: 姓名 + 密码 → Create account
# ═══════════════════════════════════════════════════════════

def _fill_profile_and_submit(tab, password: str, first_name: str, last_name: str) -> bool:
    """填写姓名密码 + 勾选协议 + 提交注册（移植自 Fork 的多策略方案）"""
    logger.info(f"[Step 7] 填写: {first_name} {last_name}")

    first_input = (
        tab.ele("@placeholder=First name", timeout=config.PAGE_TIMEOUT)
        or tab.ele("@name=firstName", timeout=5)
    )
    if first_input:
        first_input.clear()
        first_input.input(first_name)

    last_input = (
        tab.ele("@placeholder=Last name", timeout=5)
        or tab.ele("@name=lastName", timeout=5)
    )
    if last_input:
        last_input.clear()
        last_input.input(last_name)

    pwd_input = (
        tab.ele("@placeholder=Password", timeout=5)
        or tab.ele("@type=password", timeout=5)
    )
    if pwd_input:
        pwd_input.clear()
        pwd_input.input(password)

    time.sleep(0.5)

    # 自动勾选所有未勾选的 checkbox（协议、通知等）
    # JetBrains 注册表单有协议 checkbox，不勾就不能提交！
    try:
        checkboxes = tab.eles("@type=checkbox", timeout=2)
        if checkboxes:
            for cb in checkboxes:
                try:
                    if not cb.attr("checked"):
                        cb.click()
                        logger.info("[Step 7] 已勾选 checkbox")
                        time.sleep(0.2)
                except Exception:
                    pass
    except Exception:
        pass

    time.sleep(0.5)

    # 提交 Create account（3 策略循环 20 次）
    for attempt in range(20):
        # 检查是否已离开注册表单（firstName 输入框消失）
        try:
            on_form = tab.run_js("""
                return !!document.querySelector('input[name="firstName"]');
            """)
            if not on_form:
                logger.info(f"[Step 7] 注册提交成功（第 {attempt + 1} 次检查）")
                return True
        except Exception:
            pass

        # 策略A：JS requestSubmit + btn.click
        try:
            result = tab.run_js("""
                const form = document.querySelector('form');
                if (form && typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                    return 'requestSubmit';
                }
                const btn = document.querySelector('button[type="submit"]');
                if (btn) { btn.click(); return 'btn_click'; }
                return 'none';
            """)
            if result and result != 'none':
                if attempt == 0:
                    logger.info(f"[Step 7] JS 提交: {result}")
                time.sleep(1.5)
                continue
        except Exception:
            pass

        # 策略B：DrissionPage 点击 submit
        try:
            submit_btn = tab.ele("@type=submit", timeout=2)
            if submit_btn:
                submit_btn.click()
                if attempt == 0:
                    logger.info("[Step 7] DrissionPage click submit")
                time.sleep(1.5)
                continue
        except Exception:
            pass

        # 策略C：DrissionPage 文字匹配 Create account
        try:
            btn = tab.ele("text:Create account", timeout=2)
            if btn:
                btn.click()
                if attempt == 0:
                    logger.info("[Step 7] DrissionPage click 'Create account'")
                time.sleep(1.5)
                continue
        except Exception:
            pass

        time.sleep(1)

    # 最终检查
    try:
        still = tab.run_js("return !!document.querySelector('input[name=\"firstName\"]')")
        if not still:
            logger.info("[Step 7] 注册提交最终确认成功")
            return True
    except Exception:
        pass

    logger.warning("[Step 7] 20 次尝试后仍在注册表单页")
    return False


# ═══════════════════════════════════════════════════════════
#  Step 8: tokens 页 → 选国家 → Add credit card（可选）
# ═══════════════════════════════════════════════════════════

TOKENS_URL = "https://account.jetbrains.com/licenses/tokens"

_COUNTRY_NAMES = {
    "JP": "Japan", "US": "United States", "GB": "United Kingdom",
    "DE": "Germany", "FR": "France", "KR": "Republic of Korea", "SG": "Singapore",
    "CA": "Canada", "AU": "Australia", "NL": "Netherlands", "SE": "Sweden",
    "CH": "Switzerland", "IN": "India", "BR": "Brazil", "IT": "Italy",
    "ES": "Spain", "PT": "Portugal", "AT": "Austria", "BE": "Belgium",
    "DK": "Denmark", "FI": "Finland", "NO": "Norway", "IE": "Ireland",
    "PL": "Poland", "CZ": "Czech Republic", "GR": "Greece", "IL": "Israel",
    "NZ": "New Zealand", "MX": "Mexico", "AR": "Argentina", "CL": "Chile",
    "CO": "Colombia", "PE": "Peru", "PH": "Philippines", "TH": "Thailand",
    "MY": "Malaysia", "ID": "Indonesia", "VN": "Vietnam", "ZA": "South Africa",
    "EG": "Egypt", "NG": "Nigeria", "KE": "Kenya", "TR": "Türkiye",
    "RO": "Romania", "HU": "Hungary", "BG": "Bulgaria", "HR": "Croatia",
    "SK": "Slovakia", "SI": "Slovenia", "LT": "Lithuania", "LV": "Latvia",
    "EE": "Estonia", "IS": "Iceland", "MT": "Malta", "CY": "Cyprus",
    "LU": "Luxembourg", "AE": "United Arab Emirates", "SA": "Saudi Arabia",
    "QA": "Qatar", "KW": "Kuwait", "BH": "Bahrain", "OM": "Oman",
    "JO": "Jordan", "UY": "Uruguay", "PA": "Panama", "CR": "Costa Rica",
    "EC": "Ecuador", "DO": "Dominican Republic", "GT": "Guatemala",
    "BD": "Bangladesh", "PK": "Pakistan", "LK": "Sri Lanka", "NP": "Nepal",
    "KH": "Cambodia", "MM": "Myanmar", "MN": "Mongolia", "KZ": "Kazakhstan",
    "UZ": "Uzbekistan", "GE": "Georgia", "AM": "Armenia", "AZ": "Azerbaijan",
    "RS": "Serbia", "BA": "Bosnia and Herzegovina", "ME": "Montenegro",
    "MK": "North Macedonia", "AL": "Albania", "MD": "Moldova",
    "TT": "Trinidad and Tobago", "JM": "Jamaica", "MU": "Mauritius",
    "GH": "Ghana", "TZ": "Tanzania", "SN": "Senegal",
}


def get_country_list() -> list[dict]:
    """返回国家列表供前端使用，按名称排序"""
    return sorted(
        [{"code": k, "name": v} for k, v in _COUNTRY_NAMES.items()],
        key=lambda x: x["name"]
    )


def _get_country_name(code: str) -> str:
    return _COUNTRY_NAMES.get(code.upper(), code)


def _wait_tokens_page_ready(tab, timeout: int = 45):
    """等待 tokens 页面 SPA 内容真正加载完成"""
    deadline = time.time() + timeout
    check_count = 0
    while time.time() < deadline:
        check_count += 1
        try:
            # 检查是否被重定向到了登录页或其他页面
            current_url = (tab.url or "").lower()
            if "login" in current_url or "signup" in current_url:
                logger.warning(f"[Step 8] 页面被重定向到 {current_url}，重新导航到 tokens 页...")
                try:
                    tab.get(TOKENS_URL)
                    tab.wait.doc_loaded(timeout=30)
                except Exception:
                    pass
                time.sleep(3)
                continue

            has_content = tab.run_js("""
                return !!(
                    document.querySelector('select[name="country"]')
                    || document.querySelector('.chosen-container')
                    || document.querySelector('a[href*="credit"]')
                    || (document.body.textContent || '').includes('Select country')
                    || (document.body.textContent || '').includes('Add credit card')
                    || (document.body.textContent || '').includes('Tokens')
                );
            """)
            if has_content:
                logger.info(f"[Step 8] tokens 页面内容已就绪（第 {check_count} 次检查）")
                return True
        except Exception:
            pass

        if check_count % 10 == 0:
            logger.info(f"[Step 8] 仍在等待 tokens 页面加载...（第 {check_count} 次）")

        time.sleep(1.5)
    logger.warning("[Step 8] tokens 页面内容加载超时，继续尝试操作")
    return False


def _setup_tokens_page(tab, country_code: str = "JP") -> bool:
    """注册成功后：导航到 tokens 页 → 选国家 → 点 Add credit card"""
    logger.info(f"[Step 8] 跳转到 tokens 页（国家={country_code}）...")

    # 注册完成后等一下让 session 同步（刚创建的账号 session 可能还没完全生效）
    time.sleep(3)

    # 先检查当前页面状态——如果 Create account 后还在 signup 页，
    # 说明注册可能还没完成跳转，等一下
    for pre_wait in range(10):
        try:
            cur = (tab.url or "").lower()
            if "signup" not in cur:
                break
        except Exception:
            break
        time.sleep(2)

    for nav_attempt in range(3):
        try:
            tab.get(TOKENS_URL)
            tab.wait.doc_loaded(timeout=30)
        except Exception as e:
            logger.warning(f"[Step 8] 导航失败 (attempt {nav_attempt + 1}): {e}")
        time.sleep(3)

        # 处理 Cookie 弹窗
        try:
            accept_btn = tab.ele("text:Accept All", timeout=3)
            if accept_btn:
                accept_btn.click()
                time.sleep(0.5)
        except Exception:
            pass

        # 检查是否被重定向到登录页
        # login 页可能会自动完成 OAuth 回调跳转，等待最多 15s
        redirected_to_login = False
        try:
            current_url = (tab.url or "").lower()
            if "login" in current_url or "signup" in current_url:
                redirected_to_login = True
                logger.warning(f"[Step 8] 被重定向到 {current_url}，等待自动登录回调...")
                for login_wait in range(10):
                    time.sleep(1.5)
                    try:
                        new_url = (tab.url or "").lower()
                        if "login" not in new_url and "signup" not in new_url:
                            logger.info(f"[Step 8] 自动登录成功，当前 URL: {new_url}")
                            redirected_to_login = False
                            break
                    except Exception:
                        break
        except Exception:
            pass

        if redirected_to_login:
            logger.warning(f"[Step 8] 仍在 login 页，重试导航 ({nav_attempt + 1}/3)...")
            time.sleep(3)
            continue

        # 等待 SPA 内容渲染
        ready = _wait_tokens_page_ready(tab)
        if ready:
            break
        if nav_attempt < 2:
            logger.info(f"[Step 8] 页面未就绪，重新导航 (attempt {nav_attempt + 2}/3)...")
    else:
        logger.warning("[Step 8] 3 次导航后 tokens 页面仍未就绪，跳过 Step 8")

    # 第一步：点击 "Select country" 链接
    try:
        select_link = (
            tab.ele("text:Select country", timeout=5)
            or tab.ele("text:Select", timeout=3)
        )
        if select_link:
            select_link.click()
            logger.info("[Step 8] 已点击 Select country")
            time.sleep(2)
    except Exception as e:
        logger.info(f"[Step 8] 未找到 Select country（可能已设置）: {e}")

    # 第二步：选国家（JS 设置 select 值 + 触发事件）
    selected = 'no_select'
    cc = country_code.upper()
    for country_attempt in range(5):
        try:
            selected = tab.run_js(f"""
                const sel = document.querySelector('select[name="country"]');
                if (!sel) return 'no_select';
                sel.value = '{cc}';
                sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                const opt = sel.querySelector('option[value="{cc}"]');
                const name = opt ? opt.textContent.trim() : '{cc}';
                const chosenSpan = document.querySelector('.chosen-single span');
                if (chosenSpan) chosenSpan.textContent = name;
                try {{
                    if (window.jQuery) {{
                        jQuery('select[name="country"]').val('{cc}').trigger('chosen:updated').trigger('change');
                    }}
                }} catch(e) {{}}
                return sel.value;
            """)
            if selected and selected != 'no_select':
                break
        except Exception:
            pass
        if country_attempt < 4:
            logger.info(f"[Step 8] select 元素未就绪，等待重试 ({country_attempt + 1}/5)...")
            time.sleep(2)

    logger.info(f"[Step 8] 国家选择结果: {selected}")

    if selected != cc:
        # 通过 Chosen UI 下拉搜索选择
        country_name = _get_country_name(cc)
        try:
            chosen = tab.ele(".chosen-container .chosen-single", timeout=3)
            if chosen:
                chosen.click()
                time.sleep(0.5)
                search = tab.ele(".chosen-container .chosen-search input", timeout=3)
                if search:
                    search.input(country_name)
                    time.sleep(0.5)
                    result_item = tab.ele(".chosen-results li", timeout=3)
                    if result_item and country_name.lower() in (result_item.text or "").lower():
                        result_item.click()
                        logger.info(f"[Step 8] 通过 Chosen UI 选择了 {country_name}")
                        time.sleep(0.5)
        except Exception as e:
            logger.warning(f"[Step 8] Chosen UI 操作失败: {e}")

    # 第三步：点击 Save
    save_clicked = False
    try:
        save_btn = tab.ele("text:Save", timeout=5)
        if save_btn:
            save_btn.click()
            save_clicked = True
            logger.info("[Step 8] 已点击 Save")
            time.sleep(3)
    except Exception:
        pass

    if not save_clicked:
        try:
            tab.run_js("""
                const modals = document.querySelectorAll('.modal');
                for (const m of modals) {
                    if (m.classList.contains('in') || m.style.display === 'block' || getComputedStyle(m).display !== 'none') {
                        const btn = m.querySelector('button.btn-primary');
                        if (btn && btn.textContent.trim() === 'Save') { btn.click(); return; }
                    }
                }
                const allSave = document.querySelectorAll('button.btn-primary');
                for (const b of allSave) {
                    if (b.textContent.trim() === 'Save' && b.offsetParent !== null) { b.click(); return; }
                }
            """)
            logger.info("[Step 8] JS 兜底点击 Save")
            time.sleep(2)
        except Exception as e:
            logger.warning(f"[Step 8] 点击 Save 失败: {e}")

    time.sleep(1.5)

    # 第四步：点击 "Add credit card"
    try:
        tab.wait.doc_loaded(timeout=15)
        time.sleep(1.5)

        for attempt in range(10):
            try:
                add_card = tab.ele("text:Add credit card", timeout=2)
                if add_card:
                    add_card.click()
                    logger.info("[Step 8] 已点击 Add credit card")
                    time.sleep(2)
                    return True
            except Exception:
                pass
            time.sleep(1.5)

        # JS 兜底
        tab.run_js("""
            const links = document.querySelectorAll('a, button');
            for (const l of links) {
                if ((l.textContent||'').trim().includes('Add credit card')) { l.click(); return; }
            }
        """)
        logger.info("[Step 8] JS 兜底点击 Add credit card")
        time.sleep(2)
        return True
    except Exception as e:
        logger.warning(f"[Step 8] 点击 Add credit card 失败: {e}")
        return False


# ═══════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════

def register_one(
    task_id: int = 0,
    password: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    browser_type: str = "chrome",
    country: str = "JP",
    auto_tokens: bool = False,
    ai_captcha: bool = False,
    cancel_flag: Callable[[], bool] | None = None,
    on_status: StatusCallback | None = None,
) -> AccountResult:
    """
    执行注册流程（支持 AI 自动验证码或半自动）。

    Args:
        task_id: 任务编号（前端展示用）
        password: 密码
        first_name / last_name: 姓名（多窗口时自动加序号后缀）
        browser_type: 浏览器类型 chrome / edge / brave
        country: tokens 页面选择的国家代码（如 JP, US, DE），仅 auto_tokens=True 时使用
        auto_tokens: 是否注册成功后自动跳转 tokens 页 + 选国家 + Add credit card
        ai_captcha: 是否启用 AI 自动验证码（True=AI 无限重试, False=等用户手动）
        cancel_flag: 取消检查回调（返回 True 时停止任务）
        on_status: 状态回调，每步更新时调用
    """
    if password is None:
        password = config.DEFAULT_PASSWORD
    if first_name is None:
        first_name = config.DEFAULT_FIRST_NAME
    if last_name is None:
        last_name = config.DEFAULT_LAST_NAME

    # 多窗口时名字加字母后缀（task_id >= 2 时加 b, c, d...）
    if task_id >= 2:
        suffix = chr(ord('a') + task_id - 1)  # 2→b, 3→c, 4→d ...
        first_name = f"{first_name}{suffix}"
        last_name = f"{last_name}{suffix}"
    if on_status is None:
        on_status = _noop_callback

    logger.info(f"[Task {task_id}] ai_captcha={ai_captcha}, cancel_flag={'set' if cancel_flag else 'None'}")

    status = TaskStatus(task_id=task_id, password=password)

    def _update(step: int, label: str):
        status.step = step
        status.step_label = label
        on_status(status)

    browser = None
    _reset_recaptcha_seen()
    try:
        # 0. 申请临时邮箱
        _update(0, "申请邮箱...")
        email = email_service.apply_email()
        status.email = email
        logger.info(f"[Task {task_id}] 邮箱: {email}")

        # 启动浏览器
        _update(0, "启动浏览器...")
        browser = _create_browser(browser_type)
        tab = browser.latest_tab
        logger.info(f"[Task {task_id}] 浏览器已启动，导航到注册页...")
        tab.get(config.SIGNUP_URL)
        # 等待页面加载完成
        try:
            tab.wait.doc_loaded(timeout=config.PAGE_TIMEOUT)
        except Exception:
            pass
        time.sleep(3)

        # Step 1
        _update(1, "Cookie 弹窗")
        if not _handle_cookie_consent(tab):
            status.success = False
            status.error = "Cookie 弹窗处理失败"
            on_status(status)
            return AccountResult(email=email, password=password, success=False,
                                 error=status.error)

        # Step 2
        _update(2, "Continue with email")
        if not _click_continue_with_email(tab):
            status.success = False
            status.error = "未找到 Continue with email"
            on_status(status)
            return AccountResult(email=email, password=password, success=False,
                                 error=status.error)

        # Step 3
        _update(3, "填写邮箱")
        if not _fill_email(tab, email):
            status.success = False
            status.error = "填写邮箱失败"
            on_status(status)
            return AccountResult(email=email, password=password, success=False,
                                 error=status.error)

        # Step 4
        _update(4, "点击人机验证")
        if not _click_recaptcha_checkbox(tab):
            status.success = False
            status.error = "reCAPTCHA checkbox 失败"
            on_status(status)
            return AccountResult(email=email, password=password, success=False,
                                 error=status.error)

        # Step 5 — 验证码处理
        # 勾选「全自动化过验证码」(ai_captcha=True) → 打码平台自动
        # 不勾选 → 纯手动（等用户在浏览器中操作）
        if not _captcha_is_done(tab):
            if ai_captcha and captcha_service.is_enabled():
                # 打码平台自动模式
                _update(5, "打码平台识别中...")
                platform_ok = _solve_with_platform(tab, cancel_flag=cancel_flag)
                if not platform_ok and not _captcha_is_done(tab):
                    # 打码平台失败 → 降级到手动
                    logger.warning("[Step 5] 打码平台失败，降级到手动模式")
                    _update(5, "打码失败，请手动完成验证码")
                    if not _wait_for_manual_captcha(tab, cancel_flag=cancel_flag):
                        status.success = False
                        status.error = "验证码未完成"
                        on_status(status)
                        return AccountResult(email=email, password=password, success=False,
                                             error=status.error)
            elif ai_captcha and not captcha_service.is_enabled():
                # 勾选了自动但没配置打码平台 → 提示后转手动
                logger.warning("[Step 5] 未配置打码平台，降级到手动模式")
                _update(5, "未配置打码平台，请手动完成验证码")
                if not _wait_for_manual_captcha(tab, cancel_flag=cancel_flag):
                    status.success = False
                    status.error = "验证码未完成（未配置打码平台）"
                    on_status(status)
                    return AccountResult(email=email, password=password, success=False,
                                         error=status.error)
            else:
                # 手动模式：等待用户手动完成
                _update(5, "请手动完成验证码")
                if not _wait_for_manual_captcha(tab, cancel_flag=cancel_flag):
                    status.success = False
                    status.error = "验证码超时（用户未操作）"
                    on_status(status)
                    return AccountResult(email=email, password=password, success=False,
                                         error=status.error)

        # Step 5b — 验证码完成后，自动点击 Continue 提交表单
        # _click_continue_after_captcha 内部已含等待逻辑（4 轮 x 30 秒），
        # 不在外面再重复调用，避免频繁提交重置风控计时器。
        if not _has_left_email_page(tab):
            _update(5, "自动提交...")
            _click_continue_after_captcha(tab)
        time.sleep(2)

        # Step 6
        _update(6, "等待邮箱验证码")
        if not _fill_verification_code(tab, email):
            status.success = False
            status.error = "填写验证码失败"
            on_status(status)
            return AccountResult(email=email, password=password, success=False,
                                 error=status.error)

        # Step 7 — 清理 reCAPTCHA 残留遮罩后再填写
        _update(7, "填写密码并注册")
        _cleanup_recaptcha_overlays(tab)
        if not _fill_profile_and_submit(tab, password, first_name, last_name):
            status.success = False
            status.error = "提交注册失败"
            on_status(status)
            return AccountResult(email=email, password=password, success=False,
                                 error=status.error)

        # 检查结果
        time.sleep(2)
        reg_success = False
        page_text = (tab.html or "").lower()
        if any(kw in page_text for kw in [
            "success", "welcome", "account created", "thank you",
            "profile", "dashboard", "verify your email",
        ]):
            reg_success = True

        if not reg_success:
            current_url = (tab.url or "").lower()
            if "signup" not in current_url:
                reg_success = True

        if not reg_success:
            # 即使无法明确确认，Step 7 已提交，视为成功
            reg_success = True

        # Step 8（可选）— 自动跳转 tokens 页 + 选国家 + Add credit card
        if reg_success and auto_tokens:
            _update(8, "设置国家/支付方式")
            _setup_tokens_page(tab, country_code=country)

        status.success = True
        status.step_label = "✅ 注册成功"
        on_status(status)
        return AccountResult(email=email, password=password, success=True, browser=browser)

    except Exception as e:
        logger.error(f"[Task {task_id}] 异常: {e}", exc_info=True)
        status.success = False
        status.error = str(e)
        on_status(status)
        return AccountResult(email=status.email, password=password,
                             success=False, error=str(e))
    finally:
        # 注册完成后（无论成功失败）删除临时邮箱，释放 YYDS Mail 名额
        try:
            if email:
                email_service.delete_email(email)
        except Exception:
            pass

        # 失败时关闭浏览器并清理数据目录；成功时保留浏览器供用户查看
        # 必须先 quit 再清理数据目录，否则 Windows 上文件被锁定导致 rmtree 失败
        if browser and not status.success:
            try:
                browser.quit()
            except Exception:
                pass
            try:
                _cleanup_browser_data(browser)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════
#  扫描并连接系统中已运行的浏览器（Chrome / Edge）
# ═══════════════════════════════════════════════════════════

def scan_debug_browsers() -> list[dict]:
    """
    扫描系统中所有带 --remote-debugging-port 的 Chrome/Edge 主进程。
    返回 [{"pid": int, "port": int, "browser": "chrome"|"edge", "title": str, "url": str}, ...]
    """
    import re
    import subprocess
    results = []
    seen_ports = set()

    import platform
    _sys = platform.system()

    if _sys == "Windows":
        # Windows: 用 PowerShell 获取 chrome.exe 和 msedge.exe 进程的 PID + 命令行
        ps_script = (
            'Get-WmiObject Win32_Process -Filter "name=\'chrome.exe\' or name=\'msedge.exe\'" '
            '| Select-Object ProcessId,Name,CommandLine '
            '| ForEach-Object { $_.ProcessId.ToString() + \"|\" + $_.Name + \"|\" + $_.CommandLine }'
        )
        try:
            raw = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_script],
                timeout=15, stderr=subprocess.DEVNULL
            ).decode("utf-8", errors="ignore")
        except Exception as e:
            logger.warning(f"[Scan] PowerShell 扫描失败: {e}")
            return results

        for line in raw.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            pid_str, proc_name, cmd_line = parts
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            if not cmd_line or "--type=" in cmd_line:
                continue
            m = re.search(r'--remote-debugging-port=(\d+)', cmd_line)
            if not m:
                continue
            port = int(m.group(1))
            if port in seen_ports:
                continue
            seen_ports.add(port)
            browser_name = "edge" if "msedge" in proc_name.lower() else "chrome"
            results.append({"pid": pid, "port": port, "browser": browser_name, "title": "", "url": ""})

    else:
        # Linux / macOS: 用 ps aux 扫描
        try:
            raw = subprocess.check_output(
                ["ps", "aux"],
                timeout=10, stderr=subprocess.DEVNULL
            ).decode("utf-8", errors="ignore")
        except Exception as e:
            logger.warning(f"[Scan] ps aux 扫描失败: {e}")
            return results

        for line in raw.strip().splitlines():
            # 只关心包含 remote-debugging-port 的行
            if "--remote-debugging-port=" not in line:
                continue
            # 跳过子进程
            if "--type=" in line:
                continue
            m_port = re.search(r'--remote-debugging-port=(\d+)', line)
            if not m_port:
                continue
            port = int(m_port.group(1))
            if port in seen_ports:
                continue
            seen_ports.add(port)
            # 解析 PID（ps aux 第2列）
            cols = line.split(None, 2)
            try:
                pid = int(cols[1])
            except (IndexError, ValueError):
                pid = 0
            proc_lower = line.lower()
            browser_name = "edge" if "msedge" in proc_lower else "chrome"
            results.append({"pid": pid, "port": port, "browser": browser_name, "title": "", "url": ""})

    # 通过 DevTools HTTP API 并行获取每个浏览器的当前页面标题和 URL
    import httpx
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_page_info(info):
        try:
            # 必须绕过 TUN 虚拟网卡劫持（DEC-011）
            transport = httpx.HTTPTransport(local_address="0.0.0.0")
            with httpx.Client(timeout=2, proxy=None, transport=transport) as client:
                resp = client.get(f"http://127.0.0.1:{info['port']}/json/list")
                pages = resp.json()
                for page in pages:
                    if page.get("type") == "page":
                        info["title"] = page.get("title", "")
                        info["url"] = page.get("url", "")
                        break
        except Exception:
            pass

    if results:
        with ThreadPoolExecutor(max_workers=min(len(results), 10)) as pool:
            futures = [pool.submit(_fetch_page_info, info) for info in results]
            try:
                for f in as_completed(futures, timeout=8):
                    try:
                        f.result()
                    except Exception:
                        pass
            except TimeoutError:
                logger.debug("[Scan] 部分浏览器 DevTools 查询超时，跳过")

    logger.info(f"[Scan] 扫描到 {len(results)} 个浏览器实例")
    return results


def connect_browser_by_port(port: int):
    """通过 debug 端口连接已有浏览器，返回 Chromium 实例"""
    browser = Chromium(f"127.0.0.1:{port}")
    return browser


def _open_single_browser(port: int, browser_type: str, url: str) -> dict:
    """打开单个浏览器窗口（供并发调用）"""
    try:
        co = ChromiumOptions()
        co.set_local_port(port)
        co.set_user_data_path(str(_BROWSER_DATA_DIR / str(port)))
        co.incognito()
        _stealth_options(co)

        path = _find_browser_path(browser_type)
        if path:
            co.set_browser_path(path)

        browser = Chromium(co)
        tab = browser.latest_tab
        _apply_stealth(tab)

        if url:
            tab.get(url)

        logger.info(f"[OpenBrowser] 已打开浏览器 端口={port}")
        return {"port": port, "ok": True, "message": f"已打开 (端口 {port})"}

    except Exception as e:
        logger.warning(f"[OpenBrowser] 打开浏览器失败 端口={port}: {e}")
        return {"port": port, "ok": False, "message": str(e)}


def open_browsers(count: int = 1, browser_type: str = "chrome", url: str = "") -> list[dict]:
    """
    批量打开带 debug 端口的浏览器窗口（并发，3 路同时打开）。

    Args:
        count: 要打开的窗口数量
        browser_type: "chrome" 或 "edge"
        url: 打开后导航到的 URL（空字符串则打开空白页）

    Returns:
        [{"port": int, "ok": True/False, "message": str}, ...]
    """
    # 扫描已占用的端口，从最大端口 +1 开始分配
    existing = scan_debug_browsers()
    used_ports = {b["port"] for b in existing}
    next_port = max(used_ports, default=9699) + 1
    ports = [next_port + i for i in range(count)]

    if count == 1:
        # 单个直接打开，不走线程池
        return [_open_single_browser(ports[0], browser_type, url)]

    # 多个并发打开（最多 3 路）
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    workers = min(count, 3)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_open_single_browser, p, browser_type, url): p
            for p in ports
        }
        try:
            for f in as_completed(futures, timeout=60):
                try:
                    results.append(f.result())
                except Exception as e:
                    p = futures[f]
                    results.append({"port": p, "ok": False, "message": str(e)})
        except TimeoutError:
            for f, p in futures.items():
                if not f.done():
                    results.append({"port": p, "ok": False, "message": "打开超时"})

    return results


# ═══════════════════════════════════════════════════════════
#  批量自动登录：打开浏览器 → 填邮箱 → 填密码 → 点登录
# ═══════════════════════════════════════════════════════════

LOGIN_URL = "https://account.jetbrains.com/login"


def _login_find_ele(tab, selectors: list[str], label: str, port: int,
                     retries: int = 3, wait_between: float = 2.0):
    """
    带重试的元素查找，解决页面加载慢或 JS 渲染延迟导致检测不到元素的问题。

    Args:
        tab: 浏览器标签页
        selectors: 选择器列表，按优先级依次尝试
        label: 元素描述（用于日志）
        port: 端口号（用于日志）
        retries: 最大重试次数
        wait_between: 每次重试间隔秒数

    Returns:
        找到的元素，或 None
    """
    for attempt in range(retries):
        for sel in selectors:
            try:
                ele = tab.ele(sel, timeout=2)
                if ele:
                    return ele
            except Exception:
                pass
        if attempt < retries - 1:
            logger.info(f"[Login:{port}] {label} 未找到，等待 {wait_between}s 后重试 ({attempt+1}/{retries})...")
            time.sleep(wait_between)
    return None


def _login_single(port: int, browser_type: str, email: str, password: str,
                   country: str = "", auto_tokens: bool = False) -> dict:
    """
    单个浏览器窗口自动登录 JetBrains 账号。

    流程：打开浏览器 → login 页 → Cookie 弹窗 → Continue with email
         → 填邮箱 → Continue → 填密码 → Log in → (可选) 跳转绑卡页

    Returns:
        {"port": int, "email": str, "ok": bool, "message": str}
    """
    result = {"port": port, "email": email, "ok": False, "message": ""}
    browser = None
    try:
        co = ChromiumOptions()
        co.set_local_port(port)
        co.set_user_data_path(str(_BROWSER_DATA_DIR / str(port)))
        co.incognito()
        _stealth_options(co)

        path = _find_browser_path(browser_type)
        if path:
            co.set_browser_path(path)

        browser = Chromium(co)
        tab = browser.latest_tab
        _apply_stealth(tab)

        # Step 1: 打开登录页（带重试，网络不稳定时可能超时）
        logger.info(f"[Login:{port}] 打开登录页...")
        for nav_try in range(3):
            try:
                tab.get(LOGIN_URL)
                tab.wait.doc_loaded(timeout=30)
                break
            except Exception as e:
                if nav_try < 2:
                    logger.warning(f"[Login:{port}] 页面加载失败，重试... ({e})")
                    time.sleep(2)
                else:
                    result["message"] = f"页面加载失败: {e}"
                    return result
        time.sleep(2)

        # Step 2: Cookie 弹窗
        try:
            accept_btn = tab.ele("text:Accept All", timeout=3)
            if accept_btn:
                accept_btn.click()
                time.sleep(0.5)
        except Exception:
            pass

        # Step 3: 点击 "Continue with email"（带重试）
        ce_btn = _login_find_ele(tab,
            ['text:Continue with email', 'tag:button@@text():Continue with email'],
            "Continue with email 按钮", port, retries=3, wait_between=2.0)
        if ce_btn:
            ce_btn.click()
            logger.info(f"[Login:{port}] 点击 Continue with email")
            time.sleep(2)
        else:
            result["message"] = "未找到 Continue with email 按钮"
            return result

        # Step 4: 填邮箱（带重试）
        email_input = _login_find_ele(tab,
            ['@placeholder=Email', '@name=email', '@type=email'],
            "邮箱输入框", port, retries=3, wait_between=2.0)
        if email_input:
            email_input.clear()
            email_input.input(email)
            logger.info(f"[Login:{port}] 填入邮箱: {email}")
            time.sleep(0.5)
        else:
            result["message"] = "未找到邮箱输入框"
            return result

        # Step 5: 点击 Continue（去密码页，带重试）
        cont_btn = _login_find_ele(tab,
            ['tag:button@@text():Continue', 'text:Continue'],
            "Continue 按钮", port, retries=3, wait_between=2.0)
        if cont_btn:
            cont_btn.click()
            logger.info(f"[Login:{port}] 点击 Continue")
            time.sleep(3)
        else:
            result["message"] = "未找到 Continue 按钮"
            return result

        # Step 6: 填密码（带重试，页面跳转可能有延迟）
        pw_input = _login_find_ele(tab,
            ['@type=password', '@placeholder=Password'],
            "密码输入框", port, retries=5, wait_between=2.0)
        if pw_input:
            pw_input.clear()
            pw_input.input(password)
            logger.info(f"[Login:{port}] 填入密码")
            time.sleep(0.5)
        else:
            result["message"] = "未找到密码输入框（可能邮箱不存在或页面加载超时）"
            return result

        # Step 7: 点击 "Log in"（带重试）
        login_btn = _login_find_ele(tab,
            ['tag:button@@text():Log in', '@type=submit'],
            "Log in 按钮", port, retries=3, wait_between=2.0)
        if login_btn:
            login_btn.click()
            logger.info(f"[Login:{port}] 点击 Log in")
            time.sleep(3)
        else:
            result["message"] = "未找到 Log in 按钮"
            return result

        # Step 8: 等待登录结果（检查是否离开了 login 页）
        for wait_i in range(20):
            try:
                cur = (tab.url or "").lower()
                if "login" not in cur and "signup" not in cur:
                    logger.info(f"[Login:{port}] 登录成功! URL: {cur}")
                    result["ok"] = True
                    result["message"] = "登录成功"
                    break
                # 检查错误提示
                err = tab.ele("text:Incorrect email or password", timeout=1)
                if err:
                    result["message"] = "邮箱或密码错误"
                    return result
            except Exception:
                pass
            time.sleep(1)

        if not result["ok"]:
            # 再给一次机会：可能页面跳转慢
            time.sleep(5)
            cur = (tab.url or "").lower()
            if "login" not in cur and "signup" not in cur:
                result["ok"] = True
                result["message"] = "登录成功"
            else:
                result["message"] = "登录超时或失败"

        # Step 9: 可选 — 跳转绑卡页
        if result["ok"] and auto_tokens and country:
            logger.info(f"[Login:{port}] 自动跳转绑卡页 (国家={country})...")
            try:
                _setup_tokens_page(tab, country_code=country)
                result["message"] = "登录成功 + 已跳转绑卡页"
            except Exception as e:
                logger.warning(f"[Login:{port}] 跳转绑卡页失败: {e}")
                result["message"] = "登录成功，但跳转绑卡页失败"

    except Exception as e:
        result["message"] = str(e)
        logger.error(f"[Login:{port}] 登录异常: {e}")

    return result


def auto_login_batch(accounts: list[dict], browser_type: str = "chrome",
                     country: str = "", auto_tokens: bool = False,
                     workers: int = 5) -> list[dict]:
    """
    批量自动登录。

    Args:
        accounts: [{"email": "xxx", "password": "xxx"}, ...]
        browser_type: "chrome" / "edge"
        country: 国家代码（auto_tokens=True 时使用）
        auto_tokens: 登录后是否自动跳转绑卡页
        workers: 并发数（1-15），默认 5

    Returns:
        [{"port": int, "email": str, "ok": bool, "message": str}, ...]
    """
    count = len(accounts)
    if count == 0:
        return []

    workers = max(1, min(workers, 15))  # 限制 1-15

    # 端口分配
    existing = scan_debug_browsers()
    used_ports = {b["port"] for b in existing}
    next_port = max(used_ports, default=9699) + 1
    ports = [next_port + i for i in range(count)]

    logger.info(f"[AutoLogin] 开始批量登录 {count} 个账号 (browser={browser_type}, workers={workers})")

    if count == 1:
        acc = accounts[0]
        return [_login_single(ports[0], browser_type, acc["email"], acc["password"],
                              country, auto_tokens)]

    # 多个并发（用户可配，最多 15 路）
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    actual_workers = min(count, workers)
    with ThreadPoolExecutor(max_workers=actual_workers) as pool:
        futures = {}
        for i, acc in enumerate(accounts):
            f = pool.submit(_login_single, ports[i], browser_type,
                           acc["email"], acc["password"], country, auto_tokens)
            futures[f] = ports[i]
            # 错峰 1 秒（并发多时减少错峰时间以加快启动）
            if i < count - 1:
                stagger = 1.0 if actual_workers <= 5 else 0.5
                time.sleep(stagger)

        try:
            for f in as_completed(futures, timeout=300):
                try:
                    results.append(f.result())
                except Exception as e:
                    p = futures[f]
                    results.append({"port": p, "email": "", "ok": False, "message": str(e)})
        except TimeoutError:
            for f, p in futures.items():
                if not f.done():
                    results.append({"port": p, "email": "", "ok": False, "message": "登录超时"})

    success = sum(1 for r in results if r["ok"])
    logger.info(f"[AutoLogin] 完成: {success}/{count} 成功")
    return results


# ═══════════════════════════════════════════════════════════
#  协议登录（纯 HTTP 登录 + Cookie 注入浏览器）
# ═══════════════════════════════════════════════════════════

_JB_BASE = "https://account.jetbrains.com"

_JB_LOGIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": _JB_BASE,
    "X-Requested-With": "XMLHttpRequest",
}


def _proto_login(email: str, password: str) -> dict:
    """
    纯 HTTP 协议登录 JetBrains 账号（4 步，无浏览器）。

    Returns:
        {"ok": bool, "cookies": {name: value}, "message": str}
    """
    from curl_cffi.requests import Session as CurlSession

    s = CurlSession(impersonate="chrome136", verify=False, timeout=30)
    try:
        s.headers.update(_JB_LOGIN_HEADERS)

        # Step 1: GET /login → CSRF cookie
        r1 = s.get(f"{_JB_BASE}/login")
        if r1.status_code != 200:
            return {"ok": False, "cookies": {}, "message": f"GET /login 失败: {r1.status_code}"}
        csrf = s.cookies.get("_st-JBA", "")
        if not csrf:
            return {"ok": False, "cookies": {}, "message": "未获取到 CSRF cookie"}

        s.headers["X-XSRF-TOKEN"] = csrf
        s.headers["Content-Type"] = "application/json"

        # Step 2: POST /api/auth/sessions
        r2 = s.post(f"{_JB_BASE}/api/auth/sessions", json={})
        if r2.status_code != 200:
            return {"ok": False, "cookies": {}, "message": f"POST sessions 失败: {r2.status_code}"}
        sid = r2.json().get("id", "")

        # Step 3: POST /api/auth/sessions/{id}/email/login
        r3 = s.post(f"{_JB_BASE}/api/auth/sessions/{sid}/email/login",
                     json={"email": email})
        if r3.status_code != 200:
            return {"ok": False, "cookies": {}, "message": f"email/login 失败: {r3.status_code} {r3.text[:200]}"}
        state = r3.json().get("state", "")
        if state != "PASSWORD_REQUIRED":
            return {"ok": False, "cookies": {}, "message": f"意外状态: {state}"}

        # Step 4: POST /api/auth/sessions/{id}/password
        r4 = s.post(f"{_JB_BASE}/api/auth/sessions/{sid}/password",
                     json={"password": password})
        if r4.status_code != 200:
            return {"ok": False, "cookies": {}, "message": f"password 失败: {r4.status_code} {r4.text[:200]}"}
        j4 = r4.json()
        if not j4.get("authenticated"):
            return {"ok": False, "cookies": {}, "message": f"认证失败: {j4.get('state', 'unknown')}"}

        # 收集 cookies
        cookies = dict(s.cookies)
        return {"ok": True, "cookies": cookies, "message": "协议登录成功"}

    except Exception as e:
        return {"ok": False, "cookies": {}, "message": str(e)}
    finally:
        s.close()


def _proto_login_single(port: int, browser_type: str, email: str, password: str,
                         country: str = "", auto_tokens: bool = False) -> dict:
    """
    协议登录单个账号 → 打开浏览器 → CDP 注入 Cookie → 导航到已登录页面。

    比纯浏览器登录快 5-10 倍（无需等页面渲染、填表单、点按钮）。

    Returns:
        {"port": int, "email": str, "ok": bool, "message": str}
    """
    result = {"port": port, "email": email, "ok": False, "message": ""}

    # Phase 1: 协议登录（纯 HTTP）
    logger.info(f"[ProtoLogin:{port}] 协议登录 {email}...")
    login_result = _proto_login(email, password)
    if not login_result["ok"]:
        result["message"] = login_result["message"]
        logger.error(f"[ProtoLogin:{port}] 协议登录失败: {login_result['message']}")
        return result

    cookies = login_result["cookies"]
    logger.info(f"[ProtoLogin:{port}] 协议登录成功, cookies: {list(cookies.keys())}")

    # Phase 2: 打开浏览器 + 注入 Cookie
    browser = None
    try:
        co = ChromiumOptions()
        co.set_local_port(port)
        co.set_user_data_path(str(_BROWSER_DATA_DIR / str(port)))
        co.incognito()
        _stealth_options(co)

        path = _find_browser_path(browser_type)
        if path:
            co.set_browser_path(path)

        browser = Chromium(co)
        tab = browser.latest_tab
        _apply_stealth(tab)

        # 先导航到 JetBrains 域（必须在同域才能设 cookie）
        tab.get(f"{_JB_BASE}/favicon.ico")
        time.sleep(0.5)

        # CDP 注入 cookies
        for name, value in cookies.items():
            tab.run_cdp("Network.setCookie", **{
                "name": name,
                "value": value,
                "domain": ".jetbrains.com",
                "path": "/",
                "secure": True,
                "httpOnly": name != "_st-JBA",
            })

        # Phase 3: 导航到已登录页面
        target_url = f"{_JB_BASE}/licenses"
        tab.get(target_url)
        tab.wait.doc_loaded(timeout=15)
        time.sleep(2)

        cur = (tab.url or "").lower()
        if "login" not in cur and "signup" not in cur:
            result["ok"] = True
            result["message"] = "协议登录成功"
            logger.info(f"[ProtoLogin:{port}] 浏览器已登录! URL: {cur}")
        else:
            result["message"] = "Cookie 注入后仍被重定向到登录页"
            logger.warning(f"[ProtoLogin:{port}] Cookie 注入失败, URL: {cur}")

        # Phase 4: 可选 — 跳转绑卡页
        if result["ok"] and auto_tokens and country:
            logger.info(f"[ProtoLogin:{port}] 自动跳转绑卡页 (国家={country})...")
            try:
                _setup_tokens_page(tab, country_code=country)
                result["message"] = "协议登录成功 + 已跳转绑卡页"
            except Exception as e:
                logger.warning(f"[ProtoLogin:{port}] 跳转绑卡页失败: {e}")
                result["message"] = "协议登录成功，但跳转绑卡页失败"

    except Exception as e:
        result["message"] = f"浏览器阶段失败: {e}"
        logger.error(f"[ProtoLogin:{port}] 浏览器异常: {e}")
    finally:
        # 登录失败时关闭浏览器，避免进程泄漏；成功时保留供用户使用
        if not result["ok"] and browser:
            try:
                browser.quit()
            except Exception:
                pass

    return result


def proto_login_batch(accounts: list[dict], browser_type: str = "chrome",
                      country: str = "", auto_tokens: bool = False,
                      workers: int = 5) -> list[dict]:
    """
    协议批量登录：纯 HTTP 登录 + Cookie 注入浏览器。

    比浏览器自动化登录快 5-10 倍。

    Args:
        accounts: [{"email": "xxx", "password": "xxx"}, ...]
        browser_type: "chrome" / "edge"
        country: 国家代码
        auto_tokens: 登录后是否自动跳转绑卡页
        workers: 并发数（1-15）

    Returns:
        [{"port": int, "email": str, "ok": bool, "message": str}, ...]
    """
    count = len(accounts)
    if count == 0:
        return []

    workers = max(1, min(workers, 15))

    # 端口分配
    existing = scan_debug_browsers()
    used_ports = {b["port"] for b in existing}
    next_port = max(used_ports, default=9699) + 1
    ports = [next_port + i for i in range(count)]

    logger.info(f"[ProtoLogin] 开始协议批量登录 {count} 个账号 (workers={workers})")

    if count == 1:
        acc = accounts[0]
        return [_proto_login_single(ports[0], browser_type, acc["email"], acc["password"],
                                     country, auto_tokens)]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    actual_workers = min(count, workers)
    with ThreadPoolExecutor(max_workers=actual_workers) as pool:
        futures = {}
        for i, acc in enumerate(accounts):
            f = pool.submit(_proto_login_single, ports[i], browser_type,
                           acc["email"], acc["password"], country, auto_tokens)
            futures[f] = ports[i]
            # 错峰 0.5 秒（协议登录很快，不需要长间隔）
            if i < count - 1:
                time.sleep(0.5)

        try:
            for f in as_completed(futures, timeout=300):
                try:
                    results.append(f.result())
                except Exception as e:
                    p = futures[f]
                    results.append({"port": p, "email": "", "ok": False, "message": str(e)})
        except TimeoutError:
            for f, p in futures.items():
                if not f.done():
                    results.append({"port": p, "email": "", "ok": False, "message": "登录超时"})

    success = sum(1 for r in results if r["ok"])
    logger.info(f"[ProtoLogin] 完成: {success}/{count} 成功")
    return results


# ═══════════════════════════════════════════════════════════
#  一键填卡：在已打开的浏览器中自动填写银行卡信息
# ═══════════════════════════════════════════════════════════

def fill_card_info(
    browser,
    card_number: str,
    expiry_date: str,
    cvv: str,
    card_name: str,
) -> dict:
    """
    在已打开的浏览器窗口中自动填写 JetBrains Add credit card 表单。

    用户必须先手动导航到 Add credit card 页面（JetBrains 的付款方式页面）。
    Adyen 支付表单的卡号/到期日/CVV 通常在 iframe 中，姓名可能在主页面。

    Args:
        browser: Chromium 浏览器实例
        card_number: 卡号（纯数字，如 "4111111111111111"）
        expiry_date: 有效期 MM/YY（如 "12/28"）
        cvv: 安全码（如 "123"）
        card_name: 持卡人姓名（如 "ZHANG SAN"）

    Returns:
        {"ok": True/False, "message": "...", "filled": {...}}
    """
    filled = {}

    try:
        tab = browser.latest_tab
        logger.info(f"[FillCard] 当前页面: {tab.url}")

        # ── 1. 尝试在 Adyen iframe 中填写卡号/到期日/CVV ──
        # Adyen 使用多个独立 iframe，每个字段一个 iframe
        adyen_filled = _fill_adyen_iframes(tab, card_number, expiry_date, cvv)
        filled.update(adyen_filled)

        # ── 2. 如果 iframe 方式没找到，尝试直接在主页面找输入框 ──
        if not filled.get("card_number"):
            direct_filled = _fill_card_direct(tab, card_number, expiry_date, cvv)
            filled.update(direct_filled)

        # ── 3. 持卡人姓名（通常在主页面而非 iframe 中） ──
        name_filled = _fill_card_name(tab, card_name)
        filled.update(name_filled)

        # 汇总结果
        success_count = sum(1 for v in filled.values() if v)
        total_fields = 4  # card_number, expiry, cvv, name
        if success_count >= 3:
            return {"ok": True, "message": f"已填写 {success_count}/{total_fields} 个字段", "filled": filled}
        elif success_count > 0:
            return {"ok": True, "message": f"部分填写 {success_count}/{total_fields} 个字段，请检查", "filled": filled}
        else:
            return {"ok": False, "message": "未找到任何支付表单字段，请确认已打开 Add credit card 页面", "filled": filled}

    except Exception as e:
        logger.error(f"[FillCard] 异常: {e}", exc_info=True)
        return {"ok": False, "message": f"填写失败: {str(e)}", "filled": filled}


def _fill_adyen_iframes(tab, card_number: str, expiry_date: str, cvv: str) -> dict:
    """
    在 JetBrains/Adyen 的 iframe 中填写卡号、到期日、CVV。
    JetBrains 的 Adyen 表单使用 3 个独立 iframe，title 分别为：
      - "Iframe for card number"
      - "Iframe for expiry date"
      - "Iframe for security code"
    """
    result = {"card_number": False, "expiry": False, "cvv": False}

    # 按精确 title 定位（最可靠），顺序：卡号 → 到期日 → CVV
    iframe_map = [
        ("@title=Iframe for card number", card_number, "card_number", "卡号"),
        ("@title=Iframe for expiry date", expiry_date, "expiry", "到期日"),
        ("@title=Iframe for security code", cvv, "cvv", "CVV"),
    ]

    for selector, value, key, label in iframe_map:
        if not value:
            continue
        try:
            frame = tab.get_frame(selector, timeout=3)
            if not frame:
                logger.warning(f"[FillCard] 未找到 {label} iframe: {selector}")
                continue

            inp = _find_input_in_frame(frame)
            if not inp:
                logger.warning(f"[FillCard] {label} iframe 内未找到可见 input")
                continue

            _type_into_input(inp, value, frame=frame)
            result[key] = True
            logger.info(f"[FillCard] 已填写{label} (Adyen iframe)")
            time.sleep(0.3)  # 等 Adyen JS 处理完再填下一个

        except Exception as e:
            logger.warning(f"[FillCard] 填写{label}异常: {e}")

    # 回退：如果精确 title 没匹配到，按 iframe 顺序填（兼容其他语言页面）
    if not any(result.values()):
        logger.info("[FillCard] 精确匹配失败，尝试按顺序填写 Adyen iframe...")
        try:
            iframes = tab.eles("tag:iframe")
            adyen_iframes = [ifr for ifr in iframes
                             if "adyen" in (ifr.attr("src") or "").lower()
                             or "adyen" in (ifr.attr("class") or "").lower()]
            logger.info(f"[FillCard] 找到 {len(adyen_iframes)} 个 Adyen iframe")

            fields = [(card_number, "card_number"), (expiry_date, "expiry"), (cvv, "cvv")]
            for i, (value, key) in enumerate(fields):
                if i < len(adyen_iframes) and not result[key]:
                    try:
                        frame = tab.get_frame(adyen_iframes[i])
                        if frame:
                            inp = _find_input_in_frame(frame)
                            if inp:
                                _type_into_input(inp, value, frame=frame)
                                result[key] = True
                                logger.info(f"[FillCard] 已填写 {key} (顺序)")
                                time.sleep(0.3)
                    except Exception as e:
                        logger.debug(f"[FillCard] 顺序填写 {key} 异常: {e}")
        except Exception:
            pass

    return result


# ═══════════════════════════════════════════════════════════
#  一键清空：清除 Adyen 表单所有字段
# ═══════════════════════════════════════════════════════════

def _clear_input_in_frame(frame, inp):
    """用 CDP Ctrl+A → Backspace 清空 Adyen iframe 内的输入框"""
    inp.click()
    time.sleep(0.2)
    # Ctrl+A 全选（必须带 windowsVirtualKeyCode 才能生效）
    frame.run_cdp('Input.dispatchKeyEvent', type='keyDown',
                  key='a', code='KeyA', windowsVirtualKeyCode=65, modifiers=2)
    frame.run_cdp('Input.dispatchKeyEvent', type='keyUp',
                  key='a', code='KeyA', windowsVirtualKeyCode=65, modifiers=2)
    time.sleep(0.1)
    # Backspace 删除
    frame.run_cdp('Input.dispatchKeyEvent', type='keyDown',
                  key='Backspace', code='Backspace', windowsVirtualKeyCode=8)
    frame.run_cdp('Input.dispatchKeyEvent', type='keyUp',
                  key='Backspace', code='Backspace', windowsVirtualKeyCode=8)
    time.sleep(0.3)


def clear_card_info(browser) -> dict:
    """清空 Add credit card 表单的所有字段（卡号/到期日/CVV/姓名）"""
    cleared = {}
    try:
        tab = browser.latest_tab

        # 清空 3 个 Adyen iframe 内的输入框
        for selector, key, label in [
            ("@title=Iframe for card number", "card_number", "卡号"),
            ("@title=Iframe for expiry date", "expiry", "到期日"),
            ("@title=Iframe for security code", "cvv", "CVV"),
        ]:
            try:
                frame = tab.get_frame(selector, timeout=2)
                if frame:
                    inp = _find_input_in_frame(frame)
                    if inp:
                        _clear_input_in_frame(frame, inp)
                        cleared[key] = True
                        logger.info(f"[ClearCard] 已清空{label}")
            except Exception as e:
                logger.debug(f"[ClearCard] 清空{label}异常: {e}")
                cleared[key] = False

        # 清空持卡人姓名（主页面）
        try:
            name_inp = tab.ele("@name=holderName", timeout=1)
            if name_inp:
                name_inp.click()
                time.sleep(0.1)
                name_inp.clear()
                cleared["card_name"] = True
                logger.info("[ClearCard] 已清空持卡人姓名")
        except Exception:
            cleared["card_name"] = False

        success = sum(1 for v in cleared.values() if v)
        return {"ok": success > 0, "message": f"已清空 {success}/4 个字段", "cleared": cleared}
    except Exception as e:
        return {"ok": False, "message": f"清空失败: {str(e)}", "cleared": cleared}


# ═══════════════════════════════════════════════════════════
#  一键确认：点击蓝色 Confirm 按钮
# ═══════════════════════════════════════════════════════════

def confirm_card(browser) -> dict:
    """点击 Add credit card 页面的蓝色 Confirm 按钮"""
    try:
        tab = browser.latest_tab
        current_url = tab.url or ""
        logger.info(f"[ConfirmCard] 当前页面: {current_url}")

        btn = None
        # 优先：精确匹配 class 含 add-credit-card 的按钮
        try:
            btns = tab.eles("tag:button", timeout=3)
            for b in btns:
                cls = b.attr("class") or ""
                txt = (b.text or "").strip()
                if "add-credit-card" in cls and txt == "Confirm":
                    btn = b
                    break
        except Exception:
            pass

        # 回退1：text=Confirm
        if not btn:
            try:
                btn = tab.ele("text:Confirm", timeout=2)
            except Exception:
                pass

        # 回退2：submit 按钮
        if not btn:
            try:
                btn = tab.ele("@type=submit", timeout=2)
            except Exception:
                pass

        if not btn:
            return {"ok": False, "message": "未找到 Confirm 按钮"}

        btn.click()
        logger.info("[ConfirmCard] 已点击 Confirm 按钮")
        time.sleep(0.5)
        return {"ok": True, "message": "已点击 Confirm"}

    except Exception as e:
        logger.warning(f"[ConfirmCard] 异常: {e}")
        return {"ok": False, "message": f"点击失败: {str(e)}"}


def _fill_card_direct(tab, card_number: str, expiry_date: str, cvv: str) -> dict:
    """在主页面直接查找输入框填写（非 iframe 模式）"""
    result = {"card_number": False, "expiry": False, "cvv": False}

    # 卡号
    for sel in ["input[name*='cardNumber']", "input[name*='card_number']",
                "input[placeholder*='Card number']", "input[placeholder*='card number']",
                "input[data-fieldtype='encryptedCardNumber']",
                "input[aria-label*='Card number']", "input[autocomplete='cc-number']"]:
        try:
            inp = tab.ele(sel, timeout=2)
            if inp:
                _type_into_input(inp, card_number)
                result["card_number"] = True
                logger.info(f"[FillCard] 已填写卡号 (直接: {sel})")
                break
        except Exception:
            pass

    # 到期日
    for sel in ["input[name*='expiry']", "input[name*='Expiry']",
                "input[placeholder*='MM/YY']", "input[placeholder*='Expiry']",
                "input[data-fieldtype='encryptedExpiryDate']",
                "input[aria-label*='Expiry']", "input[autocomplete='cc-exp']"]:
        try:
            inp = tab.ele(sel, timeout=2)
            if inp:
                _type_into_input(inp, expiry_date)
                result["expiry"] = True
                logger.info(f"[FillCard] 已填写到期日 (直接: {sel})")
                break
        except Exception:
            pass

    # CVV
    for sel in ["input[name*='security']", "input[name*='cvv']", "input[name*='cvc']",
                "input[placeholder*='Security']", "input[placeholder*='CVV']", "input[placeholder*='CVC']",
                "input[data-fieldtype='encryptedSecurityCode']",
                "input[aria-label*='Security']", "input[autocomplete='cc-csc']"]:
        try:
            inp = tab.ele(sel, timeout=2)
            if inp:
                _type_into_input(inp, cvv)
                result["cvv"] = True
                logger.info(f"[FillCard] 已填写 CVV (直接: {sel})")
                break
        except Exception:
            pass

    return result


def _fill_card_name(tab, card_name: str) -> dict:
    """填写持卡人姓名（通常在主页面而非 iframe 中）"""
    result = {"card_name": False}

    # DrissionPage 用 @ 语法做属性查找，不支持 CSS [name*=xxx]
    for sel in ["@name=holderName", "@name=cardHolder",
                "@name=name_on_card", "@name=card-name",
                "@placeholder=Name on card",
                "@autocomplete=cc-name",
                "@aria-label=Name on card"]:
        try:
            inp = tab.ele(sel, timeout=0.5)
            if inp:
                _type_into_input(inp, card_name)
                result["card_name"] = True
                logger.info(f"[FillCard] 已填写持卡人姓名 (直接: {sel})")
                return result
        except Exception:
            pass

    # 如果上面都没找到，尝试找 label 文字关联的 input
    try:
        label = tab.ele("text:Name on card", timeout=2)
        if label:
            parent = label.parent()
            if parent:
                inp = parent.ele("tag:input", timeout=2)
                if inp:
                    _type_into_input(inp, card_name)
                    result["card_name"] = True
                    logger.info("[FillCard] 已填写持卡人姓名 (label 关联)")
    except Exception:
        pass

    return result


def _find_input_in_frame(frame) -> object:
    """在 Adyen iframe 内查找真正的可见输入框（跳过隐藏的辅助 input）"""
    # Adyen iframe 内有多个 input，只有一个是真正可见的（其余 aria-hidden=true）
    # CSS 属性选择器在 DrissionPage 的 iframe 内可能不工作，直接遍历
    try:
        inputs = frame.eles("tag:input", timeout=2)
        for inp in inputs:
            if inp.attr("aria-hidden") != "true" and inp.attr("type") != "hidden":
                return inp
    except Exception:
        pass
    return None


def _type_into_input(inp, value: str, frame=None):
    """
    Adyen 支付表单输入：点击激活 → CDP Input.insertText 一次性写入。
    这是最可靠的方式，Adyen 的 JS 能正确处理 insertText 事件。
    frame: 如果 input 在 iframe 内，传入 frame 对象用于发送 CDP 命令。
    """
    try:
        inp.click()
        time.sleep(0.3)
    except Exception:
        pass

    cdp_target = frame if frame else inp
    try:
        # Input.insertText 一次性写入，Adyen 会自动格式化（如卡号加空格）
        cdp_target.run_cdp('Input.insertText', text=value)
        time.sleep(0.5)
    except Exception:
        # 回退到逐字符 keyDown/keyUp
        for ch in value:
            try:
                cdp_target.run_cdp('Input.dispatchKeyEvent', type='keyDown', text=ch)
                cdp_target.run_cdp('Input.dispatchKeyEvent', type='keyUp', text=ch)
                time.sleep(0.03)
            except Exception:
                try:
                    inp.input(ch)
                except Exception:
                    pass
        time.sleep(0.3)
