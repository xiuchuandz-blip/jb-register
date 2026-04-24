"""GhostReg 配置文件"""
import json
from pathlib import Path

# ── JetBrains 注册页 ──
SIGNUP_URL = "https://account.jetbrains.com/signup"

# ── YYDS Mail 临时邮箱 (替代已被风控的 nimail.cn) ──
YYDS_API_BASE = "https://maliapi.215.im/v1"
YYDS_API_KEY = ""  # 运行时从 settings.json 加载，用户在 UI 中配置

# ── AI 验证码识别（备用功能，开发中） ──
AI_API_BASE = ""                # 用户自行配置反代地址
AI_API_KEY = ""                 # 用户自行配置反代 Key
AI_MODEL = "google-gemini-3-0-flash"
AI_CAPTCHA_ENABLED = False      # AI 验证码默认关闭，打码平台优先

# ── 打码平台（YesCaptcha / CapSolver 等） ──
CAPTCHA_PLATFORM = ""           # "yescaptcha" / "capsolver" / ""(禁用)
CAPTCHA_CLIENT_KEY = ""         # 打码平台的 API Key (clientKey)
YESCAPTCHA_API_BASE = "https://api.yescaptcha.com"  # 国际节点，国内可换 cn.yescaptcha.com
CAPSOLVER_API_BASE = "https://api.capsolver.com"

# ── Cloudflare Workers 代理池 ──
CF_API_TOKEN = ""               # Cloudflare API Token
CF_ACCOUNT_ID = ""              # Cloudflare Account ID
CF_WORKERS_COUNT = 1            # 部署 Workers 数量（默认 1，每个 Workers 自带 IP 轮转）
CF_LADDER_PROXY = ""            # 梯子代理地址（如 http://127.0.0.1:7890）
CF_PROXY_ENABLED = False        # 是否启用 CF 代理池

# ── 注册参数 ──
DEFAULT_PASSWORD = "GhostReg2026"
DEFAULT_FIRST_NAME = "Ghost"
DEFAULT_LAST_NAME = "Reg"

# ── 超时（秒） ──
EMAIL_POLL_INTERVAL = 3         # 邮件轮询间隔
EMAIL_POLL_TIMEOUT = 600        # 邮件等待超时（10 分钟，梯子慢时邮件可能延迟）
PAGE_TIMEOUT = 30               # 页面操作超时

# ── HTTP 客户端（绕过 TUN 虚拟网卡） ──
HTTP_LOCAL_ADDRESS = "0.0.0.0"  # 强制本地直连，防止梯子 TUN 劫持

# ── 持久化设置文件 ──
_PROJECT_DIR = Path(__file__).parent.parent
SETTINGS_FILE = _PROJECT_DIR / "output" / "settings.json"


def _load_settings() -> dict:
    """从 settings.json 加载所有设置"""
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def _save_settings(data: dict):
    """保存所有设置到 settings.json"""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_api_key() -> str:
    """从 settings.json 加载 YYDS Mail API Key"""
    global YYDS_API_KEY
    data = _load_settings()
    YYDS_API_KEY = data.get("yyds_api_key", "")
    return YYDS_API_KEY


def save_api_key(key: str):
    """保存 YYDS Mail API Key 到 settings.json"""
    global YYDS_API_KEY
    YYDS_API_KEY = key
    data = _load_settings()
    data["yyds_api_key"] = key
    _save_settings(data)


def load_captcha_config():
    """从 settings.json 加载打码平台配置"""
    global CAPTCHA_PLATFORM, CAPTCHA_CLIENT_KEY
    data = _load_settings()
    CAPTCHA_PLATFORM = data.get("captcha_platform", "")
    CAPTCHA_CLIENT_KEY = data.get("captcha_client_key", "")


def save_captcha_config(platform: str, client_key: str):
    """保存打码平台配置到 settings.json"""
    global CAPTCHA_PLATFORM, CAPTCHA_CLIENT_KEY
    CAPTCHA_PLATFORM = platform
    CAPTCHA_CLIENT_KEY = client_key
    data = _load_settings()
    data["captcha_platform"] = platform
    data["captcha_client_key"] = client_key
    _save_settings(data)


def load_cf_config():
    """从 settings.json 加载 Cloudflare 代理池配置"""
    global CF_API_TOKEN, CF_ACCOUNT_ID, CF_WORKERS_COUNT, CF_LADDER_PROXY, CF_PROXY_ENABLED
    data = _load_settings()
    CF_API_TOKEN = data.get("cf_api_token", "")
    CF_ACCOUNT_ID = data.get("cf_account_id", "")
    CF_WORKERS_COUNT = data.get("cf_workers_count", 1)
    CF_LADDER_PROXY = data.get("cf_ladder_proxy", "")
    CF_PROXY_ENABLED = data.get("cf_proxy_enabled", False)


def save_cf_config(api_token: str, account_id: str, workers_count: int = 1,
                   ladder_proxy: str = "", enabled: bool = False):
    """保存 Cloudflare 代理池配置到 settings.json"""
    global CF_API_TOKEN, CF_ACCOUNT_ID, CF_WORKERS_COUNT, CF_LADDER_PROXY, CF_PROXY_ENABLED
    CF_API_TOKEN = api_token
    CF_ACCOUNT_ID = account_id
    CF_WORKERS_COUNT = workers_count
    CF_LADDER_PROXY = ladder_proxy
    CF_PROXY_ENABLED = enabled
    data = _load_settings()
    data["cf_api_token"] = api_token
    data["cf_account_id"] = account_id
    data["cf_workers_count"] = workers_count
    data["cf_ladder_proxy"] = ladder_proxy
    data["cf_proxy_enabled"] = enabled
    _save_settings(data)


# 启动时自动加载
load_api_key()
load_captcha_config()
load_cf_config()
