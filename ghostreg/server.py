"""
GhostReg Web 控制面板后端
FastAPI + WebSocket 实时推送 + 线程池并发注册 + 结果持久化
"""
import asyncio
import csv
import json
import logging
import os
import subprocess
import sys as _sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .register import (register_one, fill_card_info, clear_card_info, confirm_card,
                       scan_debug_browsers, connect_browser_by_port, open_browsers,
                       cleanup_stale_browser_data, get_country_list,
                       TaskStatus, AccountResult)
from . import captcha_service

# 协议注册机（纯 HTTP）
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from protocol_reg.worker import register_single as proto_register_single, RegResult as ProtoResult
    PROTOCOL_REG_AVAILABLE = True
except ImportError:
    PROTOCOL_REG_AVAILABLE = False

# A 池（双层代理）HTTP 客户端
try:
    from protocol_reg.pool_client import PoolClient, DEFAULT_POOL_URL
    POOL_CLIENT_AVAILABLE = True
except ImportError:
    POOL_CLIENT_AVAILABLE = False
    DEFAULT_POOL_URL = "http://127.0.0.1:7861"

logger = logging.getLogger("ghostreg.server")

app = FastAPI(title="GhostReg", version="0.2.0")

# ── 静态文件 ──
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── 持久化路径（用项目目录下的绝对路径） ──
_PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = _PROJECT_DIR / "output"
DATA_DIR.mkdir(exist_ok=True)
ACCOUNTS_JSON = DATA_DIR / "accounts.json"
ACCOUNTS_CSV = DATA_DIR / "accounts.csv"


# ── 持久化函数 ──

def _load_history() -> list[dict]:
    """启动时从 JSON 加载历史成功账号"""
    if ACCOUNTS_JSON.exists():
        try:
            data = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as e:
            logger.warning(f"加载历史记录失败: {e}")
    return []


_file_lock = threading.Lock()


def _save_account(account: dict):
    """追加一条成功账号到 JSON 和 CSV（线程安全）"""
    with _file_lock:
        # JSON — 直接用内存中的 state.history，不重新读文件
        ACCOUNTS_JSON.write_text(
            json.dumps(state.history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # CSV（追加模式）
        csv_exists = ACCOUNTS_CSV.exists() and ACCOUNTS_CSV.stat().st_size > 0
        with open(ACCOUNTS_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not csv_exists:
                writer.writerow(["#", "email", "password", "time"])
            writer.writerow([
                account.get("id", ""),
                account.get("email", ""),
                account.get("password", ""),
                account.get("time", ""),
            ])


# ── 全局状态 ──

class AppState:
    def __init__(self):
        self.tasks: dict[int, dict] = {}
        self.results: list[dict] = []
        self.history: list[dict] = []   # 历史成功记录（持久化）
        self.browsers: dict[int, object] = {}  # task_id → Chromium 实例（成功注册后保留）
        self.running = False
        self.total_count = 0
        self.lock = threading.Lock()
        self.ws_connections: list[WebSocket] = []

state = AppState()


# ── WebSocket 管理 ──

async def _broadcast(message: dict):
    dead = []
    for ws in state.ws_connections:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in state.ws_connections:
            state.ws_connections.remove(ws)


_event_loop: Optional[asyncio.AbstractEventLoop] = None


def _broadcast_from_thread(message: dict):
    if _event_loop and _event_loop.is_running():
        asyncio.run_coroutine_threadsafe(_broadcast(message), _event_loop)


@app.on_event("startup")
async def _on_startup():
    global _event_loop
    _event_loop = asyncio.get_event_loop()
    # 加载历史成功记录
    state.history = _load_history()
    logger.info(f"已加载 {len(state.history)} 条历史成功记录")
    # 清理过期浏览器缓存数据（避免持续占用磁盘空间）
    try:
        cleaned = cleanup_stale_browser_data()
        if cleaned:
            logger.info(f"已清理 {cleaned} 个过期浏览器数据目录")
    except Exception as e:
        logger.warning(f"清理浏览器数据失败: {e}")
    # 启动 A 池（双层代理）子进程
    try:
        _start_proxy_pool_subprocess()
    except Exception as e:
        logger.warning(f"启动 A 池服务失败（双层代理将不可用，会回退单层）: {e}")


@app.on_event("shutdown")
async def _on_shutdown():
    """关闭时杀掉 A 池子进程"""
    _stop_proxy_pool_subprocess()


# ── A 池（双层代理）子进程管理 ──

_pool_subprocess: "subprocess.Popen | None" = None
_PROJECT_ROOT = Path(__file__).parent.parent
_POOL_LOG_DIR = _PROJECT_ROOT / "output"


def _start_proxy_pool_subprocess() -> bool:
    """后台启动 A 池服务（tools/proxy_pool/pool_server.py，默认端口 7861）。"""
    global _pool_subprocess

    if _pool_subprocess and _pool_subprocess.poll() is None:
        logger.info("[A 池] 子进程已在运行")
        return False

    pool_script = _PROJECT_ROOT / "tools" / "proxy_pool" / "pool_server.py"
    if not pool_script.exists():
        logger.warning(f"[A 池] 脚本不存在，跳过启动: {pool_script}")
        return False

    # 检查端口 7861 是否已被占用
    try:
        import socket as _socket
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", 7861)) == 0:
            s.close()
            logger.info("[A 池] 7861 端口已被占用（可能用户手动启动了 A 池），跳过自动启动")
            return False
        s.close()
    except Exception:
        pass

    try:
        _POOL_LOG_DIR.mkdir(exist_ok=True)
        log_fd = open(_POOL_LOG_DIR / "proxy_pool.log", "ab")
        creationflags = 0
        if _sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore
        _pool_subprocess = subprocess.Popen(
            [_sys.executable, "-u", "-m", "tools.proxy_pool.pool_server"],
            cwd=str(_PROJECT_ROOT),
            stdout=log_fd,
            stderr=log_fd,
            creationflags=creationflags,
        )
        logger.info(f"[A 池] 已启动子进程 PID={_pool_subprocess.pid}")
        return True
    except Exception as e:
        logger.warning(f"[A 池] 启动失败: {e}")
        return False


def _stop_proxy_pool_subprocess():
    """关闭 A 池子进程"""
    global _pool_subprocess
    if _pool_subprocess and _pool_subprocess.poll() is None:
        try:
            _pool_subprocess.terminate()
            try:
                _pool_subprocess.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _pool_subprocess.kill()
            logger.info("[A 池] 子进程已停止")
        except Exception as e:
            logger.warning(f"[A 池] 停止失败: {e}")
    _pool_subprocess = None


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.ws_connections.append(ws)
    logger.info(f"WebSocket 连接 +1 (当前 {len(state.ws_connections)})")

    # 发送当前状态快照（含历史记录）
    await ws.send_json({
        "type": "snapshot",
        "running": state.running,
        "tasks": state.tasks,
        "results": state.results,
        "history": state.history,
    })

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in state.ws_connections:
            state.ws_connections.remove(ws)
        logger.info(f"WebSocket 连接 -1 (当前 {len(state.ws_connections)})")


# ── API ──

class StartRequest(BaseModel):
    count: int = 3
    password: str = config.DEFAULT_PASSWORD
    first_name: str = config.DEFAULT_FIRST_NAME
    last_name: str = config.DEFAULT_LAST_NAME
    browser: str = "chrome"  # chrome / edge / brave
    country: str = "JP"       # tokens 页选择的国家代码
    auto_tokens: bool = False  # 是否注册后自动跳转 tokens 页 + 选国家 + Add credit card
    ai_captcha: bool = False   # 是否启用 AI 自动验证码


class FillCardRequest(BaseModel):
    task_id: int = 0            # 0 = 所有窗口
    card_number: str = ""       # 纯数字卡号
    expiry_date: str = ""       # MM/YY
    cvv: str = ""               # 3-4 位安全码
    card_name: str = ""         # 持卡人姓名


@app.get("/")
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>GhostReg</h1><p>static/index.html not found</p>")


# ── API Key 配置 ──

class ApiKeyRequest(BaseModel):
    api_key: str = ""


@app.get("/api/settings")
async def get_settings():
    """获取当前设置（含 API Key 脱敏显示）"""
    key = config.YYDS_API_KEY
    masked = ""
    if key:
        if len(key) > 10:
            masked = key[:6] + "****" + key[-4:]
        else:
            masked = key[:3] + "****"
    return {
        "api_key_masked": masked,
        "api_key_set": bool(key),
        "browser_path": config.BROWSER_PATH,
    }


class BrowserPathRequest(BaseModel):
    browser_path: str = ""


@app.post("/api/settings/browser-path")
async def set_browser_path(req: BrowserPathRequest):
    """保存浏览器可执行文件路径（不做服务器端文件检查，浏览器在客户端本机上）"""
    try:
        config.save_browser_path(req.browser_path.strip())
        return {"ok": True, "message": "已保存" if req.browser_path.strip() else "已清空（将自动检测）"}
    except Exception as e:
        return {"ok": False, "error": f"保存失败: {e}"}


@app.post("/api/settings/api-key")
async def set_api_key(req: ApiKeyRequest):
    """保存 YYDS Mail API Key"""
    key = req.api_key.strip()
    if not key:
        return {"ok": False, "error": "API Key 不能为空"}
    if not key.startswith("AC-"):
        return {"ok": False, "error": "API Key 格式不对，应以 AC- 开头"}
    try:
        config.save_api_key(key)
        return {"ok": True, "message": "API Key 已保存"}
    except Exception as e:
        return {"ok": False, "error": f"保存失败: {e}"}


# ── 打码平台配置 ──

class CaptchaConfigRequest(BaseModel):
    platform: str = ""        # "yescaptcha" / "capsolver" / ""
    client_key: str = ""      # API Key


@app.get("/api/captcha-settings")
async def get_captcha_settings():
    """获取打码平台配置（脱敏显示）"""
    platform = config.CAPTCHA_PLATFORM
    key = config.CAPTCHA_CLIENT_KEY
    masked = ""
    if key:
        if len(key) > 10:
            masked = key[:6] + "****" + key[-4:]
        else:
            masked = key[:3] + "****"
    balance = None
    if platform and key:
        try:
            balance = captcha_service.get_balance()
        except Exception:
            pass
    return {
        "platform": platform,
        "client_key_masked": masked,
        "client_key_set": bool(key),
        "balance": balance,
    }


@app.post("/api/captcha-settings")
async def set_captcha_settings(req: CaptchaConfigRequest):
    """保存打码平台配置"""
    platform = req.platform.strip().lower()
    key = req.client_key.strip()

    if platform and platform not in ("yescaptcha", "capsolver"):
        return {"ok": False, "error": "不支持的平台，请选择 yescaptcha 或 capsolver"}

    if platform and not key:
        return {"ok": False, "error": "请输入 API Key (clientKey)"}

    try:
        config.save_captcha_config(platform, key)

        # 验证 key 可用性
        if platform and key:
            try:
                balance = captcha_service.get_balance()
                return {"ok": True, "message": f"已保存！当前余额: {balance} 积分", "balance": balance}
            except Exception as e:
                return {"ok": True, "message": f"已保存，但验证余额失败: {e}（请检查 Key 是否正确）"}
        else:
            return {"ok": True, "message": "已清除打码平台配置"}
    except Exception as e:
        return {"ok": False, "error": f"保存失败: {e}"}


@app.post("/api/start")
async def start_registration(req: StartRequest):
    if state.running:
        return {"ok": False, "error": "已有任务在运行"}

    if req.count < 1 or req.count > 10:
        return {"ok": False, "error": "窗口数量限制 1-10"}

    state.running = True
    state.total_count = req.count
    state.tasks.clear()
    state.results.clear()

    for i in range(1, req.count + 1):
        state.tasks[i] = {
            "task_id": i,
            "step": 0,
            "step_label": "排队中",
            "email": "",
            "password": req.password,
            "success": None,
            "error": "",
        }

    _broadcast_from_thread({
        "type": "started",
        "count": req.count,
        "tasks": state.tasks,
    })

    thread = threading.Thread(
        target=_run_batch,
        args=(req.count, req.password, req.first_name, req.last_name, req.browser,
              req.country, req.auto_tokens, req.ai_captcha),
        daemon=True,
    )
    thread.start()

    return {"ok": True, "count": req.count}


@app.get("/api/status")
async def get_status():
    return {
        "running": state.running,
        "tasks": state.tasks,
        "results": state.results,
        "history": state.history,
    }


@app.get("/api/history")
async def get_history():
    """获取历史成功记录"""
    return state.history


class DeleteHistoryRequest(BaseModel):
    ids: list[int] = []  # 要删除的 id 列表，空列表 = 全部删除


@app.post("/api/history/delete")
async def delete_history(req: DeleteHistoryRequest):
    """删除指定的历史记录（或全部删除）"""
    with _file_lock:
        if not req.ids:
            # 全部删除
            count = len(state.history)
            state.history.clear()
        else:
            # 删除指定 id
            id_set = set(req.ids)
            before = len(state.history)
            state.history = [h for h in state.history if h.get("id") not in id_set]
            count = before - len(state.history)

        # 重新编号
        for i, h in enumerate(state.history):
            h["id"] = i + 1

        # 重写 JSON 文件
        ACCOUNTS_JSON.write_text(
            json.dumps(state.history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 重写 CSV 文件
        with open(ACCOUNTS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["#", "email", "password", "time"])
            for h in state.history:
                writer.writerow([h.get("id", ""), h.get("email", ""),
                                 h.get("password", ""), h.get("time", "")])

    # 广播更新
    _broadcast_from_thread({"type": "history_update", "history": state.history})

    return {"ok": True, "deleted": count, "remaining": len(state.history)}


@app.post("/api/stop")
async def stop_registration():
    state.running = False
    _broadcast_from_thread({"type": "stopped"})
    return {"ok": True}


@app.post("/api/force-start")
async def force_start_registration(req: StartRequest):
    """强制启动新窗口，无视当前是否有任务在运行。新旧窗口共存。"""
    if req.count < 1 or req.count > 10:
        return {"ok": False, "error": "窗口数量限制 1-10"}

    existing_ids = set(state.tasks.keys())
    start_id = max(existing_ids, default=0) + 1

    state.running = True

    for i in range(req.count):
        tid = start_id + i
        state.tasks[tid] = {
            "task_id": tid,
            "step": 0,
            "step_label": "排队中",
            "email": "",
            "password": req.password,
            "success": None,
            "error": "",
        }

    _broadcast_from_thread({
        "type": "started",
        "count": req.count,
        "tasks": state.tasks,
    })

    thread = threading.Thread(
        target=_run_batch_force,
        args=(start_id, req.count, req.password, req.first_name, req.last_name,
              req.browser, req.country, req.auto_tokens, req.ai_captcha),
        daemon=True,
    )
    thread.start()

    return {"ok": True, "count": req.count, "start_id": start_id}


@app.post("/api/kill-all-browsers")
async def kill_all_browsers():
    """一键关闭所有由 GhostReg 管理的浏览器"""
    # scan_debug_browsers / browser.quit() 是同步阻塞操作，
    # 必须放到线程池里，否则会阻塞 FastAPI 事件循环导致前端卡死
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do_kill_all_browsers)
    return result


def _do_kill_all_browsers() -> dict:
    """同步执行关闭所有浏览器（在线程池中调用），并发关闭提速"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    killed = 0
    errors = []

    # 1. 收集所有要关闭的目标
    targets = []  # [(label, browser_or_port), ...]

    # 注册时保留的浏览器
    with state.lock:
        for task_id, browser in list(state.browsers.items()):
            targets.append((f"#{task_id}", browser))
        state.browsers.clear()

    # 扫描到的 debug 端口浏览器
    scanned = scan_debug_browsers()
    for info in scanned:
        targets.append((f"port:{info['port']}", info["port"]))

    # 2. 并发关闭（3 路同时）
    def _kill_one(item):
        label, target = item
        try:
            if isinstance(target, int):
                br = connect_browser_by_port(target)
                br.quit()
            else:
                target.quit()
            logger.info(f"[KillAll] 已关闭 {label}")
            return label, True, ""
        except Exception as e:
            return label, False, str(e)

    if targets:
        workers = min(len(targets), 3)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_kill_one, t) for t in targets]
            try:
                for f in as_completed(futures, timeout=30):
                    label, ok, err = f.result()
                    if ok:
                        killed += 1
                    else:
                        errors.append(f"{label}: {err}")
            except TimeoutError:
                errors.append("部分浏览器关闭超时")

    # 3. 清理浏览器数据目录
    try:
        cleanup_stale_browser_data()
    except Exception:
        pass

    msg = f"已关闭 {killed} 个浏览器"
    if errors:
        msg += f"，{len(errors)} 个失败"
    return {"ok": True, "message": msg, "killed": killed, "errors": errors}


@app.get("/api/countries")
async def get_countries():
    """获取可选国家列表"""
    return get_country_list()


class OpenBrowsersRequest(BaseModel):
    count: int = 1
    browser: str = "chrome"  # chrome / edge
    url: str = "https://account.jetbrains.com/login"  # 默认打开 JetBrains 登录页


@app.post("/api/open-browsers")
async def open_browsers_api(req: OpenBrowsersRequest):
    """打开带调试端口的浏览器窗口，用于一键填卡"""
    if req.count < 1 or req.count > 20:
        return {"ok": False, "error": "数量限制 1-20"}

    results = open_browsers(count=req.count, browser_type=req.browser, url=req.url)
    success = sum(1 for r in results if r.get("ok"))
    return {
        "ok": success > 0,
        "message": f"已打开 {success}/{req.count} 个浏览器窗口",
        "results": results,
    }


@app.get("/api/browsers")
async def get_browsers():
    """扫描系统中所有带 debug 端口的 Chrome/Edge 浏览器实例"""
    # 1. 扫描系统进程
    scanned = scan_debug_browsers()

    # 2. 合并 GhostReg 自己注册时保留的浏览器
    with state.lock:
        for task_id, browser in list(state.browsers.items()):
            try:
                tab = browser.latest_tab
                url = tab.url or ""
                scanned.append({
                    "pid": 0,
                    "port": 0,
                    "browser": "ghostreg",
                    "title": f"GhostReg #{task_id}",
                    "url": url,
                    "task_id": task_id,
                    "email": state.tasks.get(task_id, {}).get("email", ""),
                })
            except Exception:
                del state.browsers[task_id]

    return scanned


@app.post("/api/fill-card")
async def fill_card(req: FillCardRequest):
    """在已打开的浏览器窗口中一键填写银行卡信息（支持扫描到的任意浏览器）"""
    if not req.card_number:
        return {"ok": False, "error": "请输入卡号"}

    # 收集目标浏览器
    targets = []  # [(label, browser_instance), ...]

    if req.task_id > 0:
        # task_id > 10000 表示是通过端口指定的（前端约定：port 作为 task_id）
        if req.task_id >= 9000:
            # 当作端口号处理
            port = req.task_id
            try:
                browser = connect_browser_by_port(port)
                targets.append((f"port:{port}", browser))
            except Exception as e:
                return {"ok": False, "error": f"连接端口 {port} 失败: {str(e)}"}
        else:
            # GhostReg 自己注册的浏览器
            with state.lock:
                browser = state.browsers.get(req.task_id)
            if not browser:
                return {"ok": False, "error": f"窗口 #{req.task_id} 不存在或已关闭"}
            targets.append((f"#{req.task_id}", browser))
    else:
        # task_id == 0：填所有扫描到的浏览器
        scanned = scan_debug_browsers()
        if not scanned:
            # 回退到 GhostReg 自己的浏览器
            with state.lock:
                if not state.browsers:
                    return {"ok": False, "error": "没有扫描到任何浏览器窗口"}
                for tid, br in state.browsers.items():
                    targets.append((f"#{tid}", br))
        else:
            logger.info(f"[FillCard] 准备连接 {len(scanned)} 个浏览器...")
            for info in scanned:
                port = info["port"]
                try:
                    logger.info(f"[FillCard] 正在连接端口 {port}...")
                    browser = connect_browser_by_port(port)
                    targets.append((f"port:{port}", browser))
                    logger.info(f"[FillCard] 端口 {port} 连接成功")
                except Exception as e:
                    logger.warning(f"[FillCard] 连接端口 {port} 失败: {e}")

    if not targets:
        return {"ok": False, "error": "没有可用的浏览器窗口"}

    def _do_fill(label_browser):
        label, browser = label_browser
        try:
            r = fill_card_info(
                browser=browser,
                card_number=req.card_number,
                expiry_date=req.expiry_date,
                cvv=req.cvv,
                card_name=req.card_name,
            )
            r["label"] = label
            return r
        except Exception as e:
            return {"label": label, "ok": False, "message": str(e)}

    results = _parallel_exec(targets, _do_fill)
    success_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": success_count > 0,
        "message": f"成功 {success_count}/{len(results)} 个窗口",
        "results": results,
    }


def _parallel_exec(targets: list, func, max_workers: int = 5) -> list:
    """并发执行操作，targets 为 [(label, browser), ...]，func 接收 (label, browser) 元组"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=min(len(targets), max_workers)) as pool:
        futures = {pool.submit(func, t): t for t in targets}
        try:
            for f in as_completed(futures, timeout=120):
                try:
                    results.append(f.result())
                except Exception as e:
                    label = futures[f][0]
                    results.append({"label": label, "ok": False, "message": str(e)})
        except TimeoutError:
            # 部分任务超时，记录未完成的
            for f, t in futures.items():
                if not f.done():
                    results.append({"label": t[0], "ok": False, "message": "操作超时"})
    return results


def _collect_browser_targets(task_id: int) -> tuple[list, str | None]:
    """
    根据 task_id 收集目标浏览器列表。
    task_id=0 表示所有窗口，>=9000 当作端口号，其余当作 GhostReg task_id。
    返回 (targets, error)，targets 为 [(label, browser), ...]。
    """
    targets = []
    if task_id > 0:
        if task_id >= 9000:
            try:
                browser = connect_browser_by_port(task_id)
                targets.append((f"port:{task_id}", browser))
            except Exception as e:
                return [], f"连接端口 {task_id} 失败: {str(e)}"
        else:
            with state.lock:
                browser = state.browsers.get(task_id)
            if not browser:
                return [], f"窗口 #{task_id} 不存在或已关闭"
            targets.append((f"#{task_id}", browser))
    else:
        scanned = scan_debug_browsers()
        if not scanned:
            with state.lock:
                if not state.browsers:
                    return [], "没有扫描到任何浏览器窗口"
                for tid, br in state.browsers.items():
                    targets.append((f"#{tid}", br))
        else:
            for info in scanned:
                try:
                    browser = connect_browser_by_port(info["port"])
                    targets.append((f"port:{info['port']}", browser))
                except Exception as e:
                    logger.warning(f"连接端口 {info['port']} 失败: {e}")
    return targets, None


class CardActionRequest(BaseModel):
    task_id: int = 0  # 0 = 所有窗口


@app.post("/api/clear-card")
async def clear_card_api(req: CardActionRequest):
    """一键清空所有浏览器窗口的银行卡表单"""
    targets, error = _collect_browser_targets(req.task_id)
    if error:
        return {"ok": False, "error": error}
    if not targets:
        return {"ok": False, "error": "没有可用的浏览器窗口"}

    def _do_clear(label_browser):
        label, browser = label_browser
        try:
            r = clear_card_info(browser)
            r["label"] = label
            return r
        except Exception as e:
            return {"label": label, "ok": False, "message": str(e)}

    results = _parallel_exec(targets, _do_clear)
    success_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": success_count > 0,
        "message": f"成功清空 {success_count}/{len(results)} 个窗口",
        "results": results,
    }


@app.post("/api/confirm-card")
async def confirm_card_api(req: CardActionRequest):
    """一键点击所有浏览器窗口的 Confirm 按钮"""
    targets, error = _collect_browser_targets(req.task_id)
    if error:
        return {"ok": False, "error": error}
    if not targets:
        return {"ok": False, "error": "没有可用的浏览器窗口"}

    def _do_confirm(label_browser):
        label, browser = label_browser
        try:
            r = confirm_card(browser)
            r["label"] = label
            return r
        except Exception as e:
            return {"label": label, "ok": False, "message": str(e)}

    results = _parallel_exec(targets, _do_confirm)
    success_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": success_count > 0,
        "message": f"成功确认 {success_count}/{len(results)} 个窗口",
        "results": results,
    }


# ── Worker 逻辑 ──

def _make_status_callback(task_id: int):
    def callback(status: TaskStatus):
        task_dict = {
            "task_id": status.task_id,
            "step": status.step,
            "step_label": status.step_label,
            "email": status.email,
            "password": status.password,
            "success": status.success,
            "error": status.error,
        }
        with state.lock:
            state.tasks[task_id] = task_dict
            if status.success is not None:
                state.results.append(task_dict)

                # 成功的持久化保存
                if status.success:
                    account = {
                        "id": len(state.history) + 1,
                        "email": status.email,
                        "password": status.password,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    state.history.append(account)
                    _save_account(account)
                    logger.info(f"[Task {task_id}] 账号已保存: {status.email}")

        _broadcast_from_thread({
            "type": "task_update",
            "task": task_dict,
        })

    return callback


def _random_password(length: int = 10) -> str:
    """生成随机密码：2 大写 + (length-4) 小写 + 2 数字，打乱顺序"""
    import random as _rng, string as _s
    chars = (
        _rng.choices(_s.ascii_uppercase, k=2)
        + _rng.choices(_s.ascii_lowercase, k=length - 4)
        + _rng.choices(_s.digits, k=2)
    )
    _rng.shuffle(chars)
    return "".join(chars)


def _run_batch(count: int, password: str, first_name: str, last_name: str, browser: str = "chrome",
               country: str = "JP", auto_tokens: bool = False, ai_captcha: bool = False):
    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = {}
        for i in range(1, count + 1):
            if not state.running:
                break
            task_pwd = _random_password()   # 每个账号独立随机密码
            future = executor.submit(
                _run_single_task, i, task_pwd, first_name, last_name, browser,
                country, auto_tokens, ai_captcha
            )
            futures[future] = i

        for future in futures:
            try:
                future.result()
            except Exception as e:
                task_id = futures[future]
                logger.error(f"[Task {task_id}] 未捕获异常: {e}")

    state.running = False

    _broadcast_from_thread({
        "type": "completed",
        "results": state.results,
        "history": state.history,
    })

    logger.info("批量注册全部完成")


def _run_batch_force(start_id: int, count: int, password: str, first_name: str, last_name: str,
                     browser: str = "chrome", country: str = "JP", auto_tokens: bool = False,
                     ai_captcha: bool = False):
    """强制启动模式：不清空已有任务，追加新任务"""
    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = {}
        for i in range(count):
            tid = start_id + i
            if not state.running:
                break
            task_pwd = _random_password()   # 每个账号独立随机密码
            future = executor.submit(
                _run_single_task, tid, task_pwd, first_name, last_name, browser,
                country, auto_tokens, ai_captcha
            )
            futures[future] = tid

        for future in futures:
            try:
                future.result()
            except Exception as e:
                task_id = futures[future]
                logger.error(f"[Task {task_id}] 未捕获异常: {e}")

    has_pending = any(
        t.get("success") is None and t.get("step", 0) > 0
        for t in state.tasks.values()
    )
    if not has_pending:
        state.running = False

    _broadcast_from_thread({
        "type": "completed",
        "results": state.results,
        "history": state.history,
    })
    logger.info(f"强制启动批次完成 (start_id={start_id}, count={count})")


def _run_single_task(task_id: int, password: str, first_name: str, last_name: str, browser: str = "chrome",
                     country: str = "JP", auto_tokens: bool = False, ai_captcha: bool = False):
    callback = _make_status_callback(task_id)
    # cancel_flag: 用户点停止时 state.running 变 False
    cancel = lambda: not state.running
    result = register_one(
        task_id=task_id,
        password=password,
        first_name=first_name,
        last_name=last_name,
        browser_type=browser,
        country=country,
        auto_tokens=auto_tokens,
        ai_captcha=ai_captcha,
        cancel_flag=cancel,
        on_status=callback,
    )
    # 成功注册后保留浏览器实例引用，供一键填卡使用
    if result.success and result.browser:
        with state.lock:
            state.browsers[task_id] = result.browser
        logger.info(f"[Task {task_id}] 浏览器实例已保留（可用于填卡）")


# ═══════════════════════════════════════════════════════════
#  批量自动登录 API
# ═══════════════════════════════════════════════════════════

class AutoLoginRequest(BaseModel):
    accounts: list[dict] = []       # [{"email":"xxx","password":"xxx"}, ...]
    browser: str = "chrome"
    country: str = "JP"
    auto_tokens: bool = False       # 登录后是否自动跳转绑卡页
    workers: int = 5                # 并发数（1-15）


@app.post("/api/auto-login")
async def auto_login(req: AutoLoginRequest):
    """批量自动登录 JetBrains 账号"""
    from .register import auto_login_batch

    if not req.accounts:
        return {"ok": False, "error": "请提供至少一个账号"}

    # 过滤空账号
    accounts = [a for a in req.accounts if a.get("email") and a.get("password")]
    if not accounts:
        return {"ok": False, "error": "没有有效的账号（需要 email 和 password）"}

    count = min(len(accounts), 20)  # 最多 20 个
    accounts = accounts[:count]

    login_workers = max(1, min(req.workers, 15))

    # 后台线程执行
    def _run():
        results = auto_login_batch(
            accounts=accounts,
            browser_type=req.browser,
            country=req.country,
            auto_tokens=req.auto_tokens,
            workers=login_workers,
        )
        success = sum(1 for r in results if r["ok"])
        _broadcast_from_thread({
            "type": "login_completed",
            "results": results,
            "success": success,
            "total": len(results),
        })

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {"ok": True, "message": f"已启动 {count} 个账号的自动登录"}


# ═══════════════════════════════════════════════════════════
#  协议登录 API（纯 HTTP 登录 + Cookie 注入浏览器）
# ═══════════════════════════════════════════════════════════

class ProtoLoginRequest(BaseModel):
    accounts: list[dict] = []       # [{"email":"xxx","password":"xxx"}, ...]
    browser: str = "chrome"
    country: str = "JP"
    auto_tokens: bool = False
    workers: int = 5                # 并发数（1-15）


@app.post("/api/proto-login")
async def proto_login(req: ProtoLoginRequest):
    """协议批量登录：纯 HTTP 登录 + Cookie 注入浏览器（比浏览器登录快 5-10 倍）"""
    from .register import proto_login_batch

    if not req.accounts:
        return {"ok": False, "error": "请提供至少一个账号"}

    accounts = [a for a in req.accounts if a.get("email") and a.get("password")]
    if not accounts:
        return {"ok": False, "error": "没有有效的账号（需要 email 和 password）"}

    count = min(len(accounts), 20)
    accounts = accounts[:count]
    login_workers = max(1, min(req.workers, 15))

    def _run():
        results = proto_login_batch(
            accounts=accounts,
            browser_type=req.browser,
            country=req.country,
            auto_tokens=req.auto_tokens,
            workers=login_workers,
        )
        success = sum(1 for r in results if r["ok"])
        _broadcast_from_thread({
            "type": "login_completed",
            "results": results,
            "success": success,
            "total": len(results),
        })

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return {"ok": True, "message": f"已启动 {count} 个账号的协议登录（并发={login_workers}）"}


# ═══════════════════════════════════════════════════════════
#  协议注册机 API（纯 HTTP，零浏览器）
# ═══════════════════════════════════════════════════════════

class ProtoState:
    """协议注册机独立状态"""
    def __init__(self):
        self.tasks: dict[int, dict] = {}
        self.running = False
        self.lock = threading.Lock()

proto_state = ProtoState()

PROTO_ACCOUNTS_JSON = DATA_DIR / "protocol_accounts.json"
PROTO_ACCOUNTS_CSV = DATA_DIR / "protocol_accounts.csv"


class ProtoStartRequest(BaseModel):
    count: int = 1
    password: str = "QPalzmw123"
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    workers: int = 3
    use_proxy: bool = False  # 是否使用 CF 代理池


# ── CF 代理池全局实例 ──
_proxy_pool = None  # type: ignore


@app.get("/api/cf/settings")
async def cf_get_settings():
    """获取 CF 代理池配置"""
    return {
        "api_token": config.CF_API_TOKEN,
        "account_id": config.CF_ACCOUNT_ID,
        "workers_count": config.CF_WORKERS_COUNT,
        "ladder_proxy": config.CF_LADDER_PROXY,
        "enabled": config.CF_PROXY_ENABLED,
        "worker_url": config.CF_WORKER_URL,
        "deployed": _proxy_pool.status_dict() if _proxy_pool and _proxy_pool.available else None,
    }


class CFWorkerUrlRequest(BaseModel):
    worker_url: str = ""


@app.post("/api/cf/worker-url")
async def cf_save_worker_url(req: CFWorkerUrlRequest):
    """保存用户手动部署的单个 CF Worker URL"""
    config.save_cf_worker_url(req.worker_url.strip())
    return {"ok": True, "worker_url": config.CF_WORKER_URL}


class CFSettingsRequest(BaseModel):
    api_token: str = ""
    account_id: str = ""
    workers_count: int = 1
    ladder_proxy: str = ""
    enabled: bool = False


@app.post("/api/cf/settings")
async def cf_save_settings(req: CFSettingsRequest):
    """保存 CF 代理池配置"""
    config.save_cf_config(
        api_token=req.api_token,
        account_id=req.account_id,
        workers_count=max(1, min(req.workers_count, 10)),
        ladder_proxy=req.ladder_proxy,
        enabled=req.enabled,
    )
    return {"ok": True}


@app.post("/api/cf/deploy")
async def cf_deploy():
    """一键部署 CF Workers"""
    global _proxy_pool
    
    if not config.CF_API_TOKEN or not config.CF_ACCOUNT_ID:
        return {"ok": False, "error": "请先配置 API Token 和 Account ID"}
    
    from .proxy_pool import ProxyPool
    
    # 清理旧的
    if _proxy_pool:
        try:
            _proxy_pool.cleanup()
        except Exception:
            pass
    
    _proxy_pool = ProxyPool(
        api_token=config.CF_API_TOKEN,
        account_id=config.CF_ACCOUNT_ID,
        ladder_proxy=config.CF_LADDER_PROXY,
    )
    
    count = max(1, min(config.CF_WORKERS_COUNT, 10))
    ok = _proxy_pool.deploy(count=count)
    
    if ok:
        return {"ok": True, "status": _proxy_pool.status_dict()}
    else:
        return {"ok": False, "error": "Workers 部署失败，请检查 API Token 和 Account ID"}


@app.post("/api/cf/health")
async def cf_health_check():
    """检查 Workers 健康状态"""
    if not _proxy_pool or not _proxy_pool.workers:
        return {"ok": False, "error": "没有已部署的 Workers"}
    return {"ok": True, "health": _proxy_pool.health_check()}


@app.post("/api/cf/cleanup")
async def cf_cleanup():
    """删除所有已部署的 Workers"""
    global _proxy_pool
    if _proxy_pool:
        _proxy_pool.cleanup()
        _proxy_pool = None
    return {"ok": True}


@app.get("/api/proto/available")
async def proto_available():
    return {"available": PROTOCOL_REG_AVAILABLE}


@app.post("/api/proto/start")
async def proto_start(req: ProtoStartRequest):
    if not PROTOCOL_REG_AVAILABLE:
        return {"ok": False, "error": "协议注册机模块未安装"}
    if proto_state.running:
        return {"ok": False, "error": "已有任务在运行"}

    count = max(1, min(req.count, 50))
    workers = max(1, min(req.workers, 20))

    proto_state.running = True
    proto_state.tasks = {}

    # 初始化任务
    for i in range(1, count + 1):
        proto_state.tasks[i] = {
            "task_id": i, "email": "", "step": "等待中",
            "success": None, "error": "", "elapsed": 0,
        }

    # 广播初始状态
    _broadcast_from_thread({"type": "proto_started", "tasks": proto_state.tasks})

    # 代理池参数
    use_proxy = req.use_proxy and _proxy_pool is not None and _proxy_pool.available
    proxy_msg = ""
    if use_proxy:
        proxy_msg = f"，CF 代理池 {_proxy_pool.count} Workers"

    # 后台线程执行
    thread = threading.Thread(
        target=_run_proto_batch,
        args=(count, req.password, workers, req.first_name, req.last_name,
              _proxy_pool if use_proxy else None, config.CF_LADDER_PROXY),
        daemon=True,
    )
    thread.start()

    return {"ok": True, "message": f"已启动 {count} 个协议注册任务（{workers} 路并发{proxy_msg}）"}


@app.post("/api/proto/stop")
async def proto_stop():
    proto_state.running = False
    return {"ok": True}


@app.get("/api/proto/status")
async def proto_status():
    return {
        "running": proto_state.running,
        "tasks": proto_state.tasks,
    }


@app.get("/api/proto/history")
async def proto_history():
    if PROTO_ACCOUNTS_JSON.exists():
        try:
            data = json.loads(PROTO_ACCOUNTS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


class ProtoDeleteRequest(BaseModel):
    indexes: list[int] = []       # 要删除的记录索引（0-based）
    delete_all: bool = False


@app.post("/api/proto/history/delete")
async def proto_history_delete(req: ProtoDeleteRequest):
    """删除协议注册历史记录"""
    records = []
    if PROTO_ACCOUNTS_JSON.exists():
        try:
            records = json.loads(PROTO_ACCOUNTS_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass

    if req.delete_all:
        records = []
    elif req.indexes:
        # 按索引删除（倒序删，避免索引偏移）
        for idx in sorted(set(req.indexes), reverse=True):
            if 0 <= idx < len(records):
                records.pop(idx)

    # 保存
    PROTO_ACCOUNTS_JSON.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 同步重写 CSV
    import csv as _csv
    with open(PROTO_ACCOUNTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["email", "password", "timestamp"])
        for r in records:
            w.writerow([r.get("email", ""), r.get("password", ""), r.get("timestamp", "")])

    return {"ok": True, "history": records}


def _run_proto_batch(count: int, password: str, workers: int,
                     first_name: str | None, last_name: str | None,
                     proxy_pool=None, ladder_proxy: str = ""):
    """后台线程：批量协议注册"""
    import random, string
    from concurrent.futures import ThreadPoolExecutor, as_completed

    has_proxy = proxy_pool is not None and proxy_pool.available
    # 用户手动配置的单 Worker URL（无需部署旧 CF 代理池）
    _manual_worker_url = config.CF_WORKER_URL or ""

    # A 池双层代理客户端（若服务在运行则启用）
    _pool_cli = None
    if POOL_CLIENT_AVAILABLE:
        try:
            _test_cli = PoolClient(pool_url=DEFAULT_POOL_URL)
            if _test_cli.is_alive():
                _pool_cli = _test_cli
                logger.info("[A 池] 批量任务使用双层代理（SOCKS5 + Worker）")
        except Exception as _pe:
            logger.debug(f"[A 池] ping 失败，回退单层: {_pe}")

    def _random_name(length=5):
        first = random.choice(string.ascii_uppercase)
        rest = ''.join(random.choices(string.ascii_lowercase, k=length - 1))
        return first + rest

    def _run_one(task_id):
        if not proto_state.running:
            return
        fname = first_name or _random_name(random.randint(4, 7))
        lname = last_name or _random_name(random.randint(4, 7))
        if task_id > 1 and first_name:
            fname = first_name + chr(96 + task_id)
        task_pwd = _random_password()   # 每个账号独立随机密码

        # 为任务分配 Workers URL（旧代理池 > 手动配置 Worker URL）
        worker_url = ""
        if has_proxy:
            group_id = (task_id - 1) // 5
            worker_url = proxy_pool.get_worker_url(group_id) or ""
        if not worker_url and _manual_worker_url:
            worker_url = _manual_worker_url

        # 更新状态
        if _pool_cli:
            step_label = "申请邮箱 (A池双层)"
        elif worker_url:
            step_label = "申请邮箱 (CF代理)"
        else:
            step_label = "申请邮箱"
        proto_state.tasks[task_id]["step"] = step_label
        _broadcast_from_thread({"type": "proto_update", "task": proto_state.tasks[task_id]})

        result = proto_register_single(
            task_id, task_pwd, fname, lname,
            worker_url=worker_url, ladder_proxy=ladder_proxy,
            pool_client=_pool_cli,
        )

        # 更新最终状态（含密码，保证 CSV 导出记录正确密码）
        proto_state.tasks[task_id].update({
            "email": result.email,
            "password": task_pwd if result.success else "",
            "success": result.success,
            "error": result.error,
            "elapsed": round(result.elapsed, 1),
            "step": "注册成功" if result.success else f"失败: {result.error[:50]}",
        })
        _broadcast_from_thread({"type": "proto_update", "task": proto_state.tasks[task_id]})

    STAGGER = 2 if (has_proxy or _manual_worker_url or _pool_cli) else 8  # 有代理/Worker 时错峰缩短

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for i in range(1, count + 1):
            if not proto_state.running:
                break
            futures[pool.submit(_run_one, i)] = i
            # 错峰启动
            if i < count:
                time.sleep(STAGGER)
        for f in as_completed(futures, timeout=max(600, count * 120)):
            try:
                f.result()
            except Exception as e:
                tid = futures[f]
                proto_state.tasks[tid].update({"success": False, "error": str(e), "step": f"异常: {e}"})

    proto_state.running = False

    # A 池客户端无需显式关闭（HTTP 短连接，无持久资源）

    # 加载最新的 protocol_accounts.json 作为历史
    proto_history_list = []
    if PROTO_ACCOUNTS_JSON.exists():
        try:
            proto_history_list = json.loads(PROTO_ACCOUNTS_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass

    _broadcast_from_thread({
        "type": "proto_completed",
        "tasks": proto_state.tasks,
        "history": proto_history_list,
    })


# ══════════════════════════════════════════════════════
#  Outlook 邮箱注册 — 使用用户提供的 Outlook 凭证注册 JetBrains 账号
# ══════════════════════════════════════════════════════

try:
    from protocol_reg.outlook_worker import (
        register_with_outlook as _outlook_register_one,
        OutlookRegResult,
        OUTLOOK_ACCOUNTS_JSON,
        OUTLOOK_ACCOUNTS_CSV,
    )
    from ghostreg.outlook_email_service import test_connection as outlook_test_conn, parse_credentials_text
    OUTLOOK_REG_AVAILABLE = True
except ImportError as _e:
    OUTLOOK_REG_AVAILABLE = False
    logger.warning(f"Outlook 注册模块加载失败: {_e}")


class OutlookState:
    def __init__(self):
        self.tasks: dict[int, dict] = {}
        self.running = False
        self.lock = threading.Lock()


outlook_state = OutlookState()


class OutlookStartRequest(BaseModel):
    credentials: str = ""        # 多行文本：每行 email----password
    jb_password: str = "QPalzmw123"
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    workers: int = 1
    otp_timeout: int = 120


class OutlookTestRequest(BaseModel):
    email: str
    password: str


@app.post("/api/outlook/test-imap")
async def outlook_test_imap(req: OutlookTestRequest):
    """测试单个 Outlook 账号的 IMAP 连接"""
    if not OUTLOOK_REG_AVAILABLE:
        return {"ok": False, "message": "Outlook 模块未加载"}
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, outlook_test_conn, req.email, req.password)
    return result


@app.post("/api/outlook/start")
async def outlook_start(req: OutlookStartRequest):
    if not OUTLOOK_REG_AVAILABLE:
        return {"ok": False, "error": "Outlook 注册模块未加载"}
    if outlook_state.running:
        return {"ok": False, "error": "已有 Outlook 注册任务在运行"}

    credentials = parse_credentials_text(req.credentials)
    if not credentials:
        return {"ok": False, "error": "未解析到有效的邮箱凭证，请检查格式（每行：邮箱----密码）"}

    count = len(credentials)
    workers = max(1, min(req.workers, 10))

    outlook_state.running = True
    outlook_state.tasks = {}
    for i, cred in enumerate(credentials, start=1):
        outlook_state.tasks[i] = {
            "task_id": i,
            "email": cred["email"],
            "step": "等待中",
            "success": None,
            "error": "",
            "elapsed": 0,
        }

    _broadcast_from_thread({"type": "outlook_started", "tasks": outlook_state.tasks})

    thread = threading.Thread(
        target=_run_outlook_batch,
        args=(credentials, req.jb_password, workers,
              req.first_name, req.last_name, req.otp_timeout),
        daemon=True,
    )
    thread.start()

    return {"ok": True, "message": f"已启动 {count} 个 Outlook 注册任务（{workers} 路并发）"}


@app.post("/api/outlook/stop")
async def outlook_stop():
    outlook_state.running = False
    return {"ok": True}


@app.get("/api/outlook/status")
async def outlook_status():
    return {"running": outlook_state.running, "tasks": outlook_state.tasks}


@app.get("/api/outlook/history")
async def outlook_history():
    if OUTLOOK_ACCOUNTS_JSON.exists():
        try:
            data = json.loads(OUTLOOK_ACCOUNTS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


class OutlookDeleteRequest(BaseModel):
    indexes: list[int] = []
    delete_all: bool = False


@app.post("/api/outlook/history/delete")
async def outlook_history_delete(req: OutlookDeleteRequest):
    records = []
    if OUTLOOK_ACCOUNTS_JSON.exists():
        try:
            records = json.loads(OUTLOOK_ACCOUNTS_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass

    if req.delete_all:
        records = []
    elif req.indexes:
        for idx in sorted(set(req.indexes), reverse=True):
            if 0 <= idx < len(records):
                records.pop(idx)

    OUTLOOK_ACCOUNTS_JSON.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    import csv as _csv
    with open(OUTLOOK_ACCOUNTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["jb_email", "password", "outlook_email", "timestamp"])
        for r in records:
            w.writerow([
                r.get("jb_email") or r.get("email", ""),
                r.get("password", ""),
                r.get("email", ""),
                r.get("timestamp", ""),
            ])
    return {"ok": True, "history": records}


def _run_outlook_batch(
    credentials: list[dict],
    jb_password: str,
    workers: int,
    first_name: Optional[str],
    last_name: Optional[str],
    otp_timeout: int,
):
    """后台线程：批量 Outlook 注册"""
    import random, string
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    _manual_worker_url = config.CF_WORKER_URL or ""
    _ladder_proxy = config.CF_LADDER_PROXY or ""

    def _random_name(length=5):
        first = random.choice(string.ascii_uppercase)
        rest = "".join(random.choices(string.ascii_lowercase, k=length - 1))
        return first + rest

    def _run_one(task_id: int, cred: dict):
        if not outlook_state.running:
            return
        fname = first_name or _random_name(random.randint(4, 7))
        lname = last_name or _random_name(random.randint(4, 7))

        outlook_state.tasks[task_id]["step"] = "IMAP 连接中..."
        _broadcast_from_thread({"type": "outlook_update", "task": outlook_state.tasks[task_id]})

        def _progress_step(label: str):
            outlook_state.tasks[task_id]["step"] = label
            _broadcast_from_thread({"type": "outlook_update", "task": outlook_state.tasks[task_id]})

        _progress_step("IMAP 快照")
        result = _outlook_register_one(
            task_id=task_id,
            outlook_email=cred["email"],
            outlook_password=cred["password"],
            jb_password=jb_password,
            first_name=fname,
            last_name=lname,
            worker_url=_manual_worker_url,
            ladder_proxy=_ladder_proxy,
            should_stop=lambda: not outlook_state.running,
            otp_timeout=float(otp_timeout),
        )

        outlook_state.tasks[task_id].update({
            "success": result.success,
            "error": result.error,
            "elapsed": round(result.elapsed, 1),
            "step": "注册成功" if result.success else f"失败: {result.error[:60]}",
            "password": result.password if result.success else "",
        })
        _broadcast_from_thread({"type": "outlook_update", "task": outlook_state.tasks[task_id]})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_one, i, cred): i for i, cred in enumerate(credentials, start=1)}
        timeout = max(600, len(credentials) * 180)
        for f in _as_completed(futures, timeout=timeout):
            tid = futures[f]
            try:
                f.result()
            except Exception as e:
                logger.error(f"[Outlook Task {tid}] 线程异常: {e}")
                outlook_state.tasks[tid].update({
                    "success": False, "error": str(e), "step": f"线程异常: {e}"
                })
                _broadcast_from_thread({"type": "outlook_update", "task": outlook_state.tasks[tid]})

    outlook_state.running = False

    history_list = []
    if OUTLOOK_ACCOUNTS_JSON.exists():
        try:
            history_list = json.loads(OUTLOOK_ACCOUNTS_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass

    _broadcast_from_thread({
        "type": "outlook_completed",
        "tasks": outlook_state.tasks,
        "history": history_list,
    })


# ══════════════════════════════════════════════════════
#  改密功能 — 通过邮箱找回并重置 JetBrains 账号密码
# ══════════════════════════════════════════════════════

# 改密任务全局状态
_reset_tasks: dict[str, dict] = {}   # email → {status, message, ok}
_reset_running = False
_reset_cancel = False   # 用户点"停止"时置 True，循环与轮询据此中断


class ResetPasswordRequest(BaseModel):
    emails: list[str]          # 要改密的邮箱列表
    new_password: str          # 新密码
    poll_timeout: int = 120    # 等待重置邮件的超时秒数（默认 2 分钟）


@app.post("/api/reset-password")
async def reset_password_batch(req: ResetPasswordRequest):
    """批量通过邮箱改密接口"""
    global _reset_running, _reset_tasks, _reset_cancel

    if _reset_running:
        return {"ok": False, "error": "已有改密任务在运行，请等待完成"}

    if not req.emails:
        return {"ok": False, "error": "未指定任何邮箱"}

    if not req.new_password or len(req.new_password) < 8:
        return {"ok": False, "error": "新密码长度至少 8 位"}

    # 去重：同一邮箱只改一次，避免重复改密浪费限流额度、产生重复记录
    emails = list(dict.fromkeys(req.emails))

    _reset_running = True
    _reset_cancel = False
    _reset_tasks = {}
    for email in emails:
        _reset_tasks[email] = {"email": email, "status": "pending", "message": "等待中", "ok": None, "new_password": ""}

    _broadcast_from_thread({"type": "reset_started", "tasks": _reset_tasks})

    thread = threading.Thread(
        target=_run_reset_batch,
        args=(emails, req.new_password, req.poll_timeout),
        daemon=True,
    )
    thread.start()

    return {"ok": True, "count": len(emails)}


@app.get("/api/reset-status")
async def reset_status():
    return {"running": _reset_running, "tasks": _reset_tasks}


@app.post("/api/reset-cancel")
async def reset_cancel():
    """停止正在运行的改密任务：标记取消，正在轮询的账号会立即中断，
    尚未开始的账号会被跳过。"""
    global _reset_cancel
    if not _reset_running:
        return {"ok": False, "error": "当前没有正在运行的改密任务"}
    _reset_cancel = True
    return {"ok": True}


def _run_reset_batch(emails: list[str], new_password: str, poll_timeout: int):
    """后台线程：逐个执行改密任务（每个账号随机生成独立密码）"""
    global _reset_running, _reset_tasks

    from .reset_password import reset_account_password

    # 读取 CF Worker 代理配置：启用时改密"触发忘记密码邮件"环节走代理，
    # 每次经不同出口 IP，绕开 JetBrains 对单一来源 IP 的发信速率限制。
    config.load_cf_config()
    reset_worker_url = config.CF_WORKER_URL if config.CF_PROXY_ENABLED else ""
    if reset_worker_url:
        logger.info(f"[改密] 启用 CF Worker 代理触发: {reset_worker_url}")
    else:
        logger.info("[改密] 未启用代理，直连触发（可能被 JB 按 IP 限流）")

    def _progress(info: dict):
        email = info.get("email", "")
        if email in _reset_tasks:
            _reset_tasks[email]["message"] = info.get("message", "")
            _reset_tasks[email]["status"] = "running"
        _broadcast_from_thread({"type": "reset_update", "tasks": _reset_tasks})

    for email in emails:
        # 用户已点"停止"：尚未开始的账号标记为已取消并跳过
        if _reset_cancel:
            _reset_tasks[email]["status"] = "failed"
            _reset_tasks[email]["message"] = "已取消"
            _reset_tasks[email]["ok"] = False
            _reset_tasks[email]["new_password"] = ""
            _broadcast_from_thread({"type": "reset_update", "tasks": _reset_tasks})
            continue

        # 每个账号独立生成随机密码
        task_pwd = _random_password()
        _reset_tasks[email]["new_password"] = task_pwd
        _reset_tasks[email]["status"] = "running"
        _reset_tasks[email]["message"] = "正在处理..."
        _broadcast_from_thread({"type": "reset_update", "tasks": _reset_tasks})

        result = reset_account_password(
            email=email,
            new_password=task_pwd,
            progress_cb=_progress,
            poll_timeout=float(poll_timeout),
            cancel_cb=lambda: _reset_cancel,
            worker_url=reset_worker_url,
        )

        _reset_tasks[email]["ok"] = result.get("ok")
        _reset_tasks[email]["status"] = "done" if result.get("ok") else "failed"
        _reset_tasks[email]["message"] = result.get("message") or result.get("error") or ""
        if not result.get("ok"):
            _reset_tasks[email]["new_password"] = ""   # 失败则不展示密码
        else:
            # 关键：成功后立即落盘。密码改在 JetBrains 端不可逆，若等整批结束
            # 才写文件，一旦中途重启/崩溃，已改好的密码就永久丢失（账号被锁）。
            _persist_reset_success(email, task_pwd)
        _broadcast_from_thread({"type": "reset_update", "tasks": _reset_tasks})

        # 用户取消则立即收尾，不再等待间隔
        if _reset_cancel:
            continue

        # 每个账号间稍微间隔，避免过快触发速率限制
        time.sleep(3)

    _reset_running = False
    _broadcast_from_thread({"type": "reset_completed", "tasks": _reset_tasks})


def _persist_reset_success(email: str, password: str):
    """单个账号改密成功后【立即】落盘：追加 reset_results.json + 更新历史记录。

    必须每成功一个就调用一次。改密在 JetBrains 端是不可逆的（旧密码已失效），
    若等整批结束才统一写文件，一旦批次中途被重启/崩溃/杀进程打断，
    那些已经改好的随机新密码就会永久丢失，账号被锁——只能重新改密找回。
    """
    import datetime as _dt
    RESET_RESULTS_JSON = DATA_DIR / "reset_results.json"
    changed = False
    proto_changed = False
    with _file_lock:
        # ── 1) 追加写入 reset_results.json（密码的权威记录，最优先保护） ──
        try:
            existing = []
            if RESET_RESULTS_JSON.exists():
                try:
                    existing = json.loads(RESET_RESULTS_JSON.read_text(encoding="utf-8"))
                except Exception:
                    existing = []
            ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            existing.append({"email": email, "password": password, "reset_time": ts})
            RESET_RESULTS_JSON.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.info(f"[改密] {email} 成功，已即时写入 reset_results.json")
        except Exception as _e:
            logger.warning(f"[改密] 写入 reset_results.json 失败: {_e}")

        # ── 2) 更新浏览器注册历史 ──
        for h in state.history:
            if h.get("email") == email:
                h["password"] = password
                changed = True
        if changed:
            ACCOUNTS_JSON.write_text(
                json.dumps(state.history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # ── 3) 更新协议注册历史 ──
        try:
            if PROTO_ACCOUNTS_JSON.exists():
                proto_records = json.loads(PROTO_ACCOUNTS_JSON.read_text(encoding="utf-8"))
            else:
                proto_records = []
            for r in proto_records:
                if r.get("email") == email:
                    r["password"] = password
                    proto_changed = True
            if proto_changed:
                PROTO_ACCOUNTS_JSON.write_text(
                    json.dumps(proto_records, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                import csv as _csv
                with open(PROTO_ACCOUNTS_CSV, "w", newline="", encoding="utf-8") as f:
                    w = _csv.writer(f)
                    w.writerow(["email", "password", "timestamp"])
                    for r in proto_records:
                        w.writerow([r.get("email", ""), r.get("password", ""), r.get("timestamp", "")])
        except Exception as e:
            logger.warning(f"更新协议账号密码记录失败: {e}")

    if changed:
        _broadcast_from_thread({"type": "history_update", "history": state.history})
