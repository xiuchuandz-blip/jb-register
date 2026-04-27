"""
GhostReg Web 控制面板后端
FastAPI + WebSocket 实时推送 + 线程池并发注册 + 结果持久化
"""
import asyncio
import csv
import json
import logging
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
        # 显示前 6 位 + **** + 后 4 位
        if len(key) > 10:
            masked = key[:6] + "****" + key[-4:]
        else:
            masked = key[:3] + "****"
    return {"api_key_masked": masked, "api_key_set": bool(key)}


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


def _run_batch(count: int, password: str, first_name: str, last_name: str, browser: str = "chrome",
               country: str = "JP", auto_tokens: bool = False, ai_captcha: bool = False):
    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = {}
        for i in range(1, count + 1):
            if not state.running:
                break
            future = executor.submit(
                _run_single_task, i, password, first_name, last_name, browser,
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
            future = executor.submit(
                _run_single_task, tid, password, first_name, last_name, browser,
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
        "deployed": _proxy_pool.status_dict() if _proxy_pool and _proxy_pool.available else None,
    }


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

        # 为任务分配 Workers URL（每 5 个任务一组）
        worker_url = ""
        if has_proxy:
            group_id = (task_id - 1) // 5
            worker_url = proxy_pool.get_worker_url(group_id) or ""

        # 更新状态
        step_label = "申请邮箱"
        if worker_url:
            step_label = f"申请邮箱 (CF代理)"
        proto_state.tasks[task_id]["step"] = step_label
        _broadcast_from_thread({"type": "proto_update", "task": proto_state.tasks[task_id]})

        result = proto_register_single(
            task_id, password, fname, lname,
            worker_url=worker_url, ladder_proxy=ladder_proxy,
        )

        # 更新最终状态
        proto_state.tasks[task_id].update({
            "email": result.email,
            "success": result.success,
            "error": result.error,
            "elapsed": round(result.elapsed, 1),
            "step": "注册成功" if result.success else f"失败: {result.error[:50]}",
        })
        _broadcast_from_thread({"type": "proto_update", "task": proto_state.tasks[task_id]})

    STAGGER = 8 if not has_proxy else 2  # 有代理池时错峰缩短（8轮压测验证：2s足够）

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
#  改密功能 — 通过邮箱找回并重置 JetBrains 账号密码
# ══════════════════════════════════════════════════════

# 改密任务全局状态
_reset_tasks: dict[str, dict] = {}   # email → {status, message, ok}
_reset_running = False


class ResetPasswordRequest(BaseModel):
    emails: list[str]          # 要改密的邮箱列表
    new_password: str          # 新密码
    poll_timeout: int = 120    # 等待重置邮件的超时秒数（默认 2 分钟）


@app.post("/api/reset-password")
async def reset_password_batch(req: ResetPasswordRequest):
    """批量通过邮箱改密接口"""
    global _reset_running, _reset_tasks

    if _reset_running:
        return {"ok": False, "error": "已有改密任务在运行，请等待完成"}

    if not req.emails:
        return {"ok": False, "error": "未指定任何邮箱"}

    if not req.new_password or len(req.new_password) < 8:
        return {"ok": False, "error": "新密码长度至少 8 位"}

    _reset_running = True
    _reset_tasks = {}
    for email in req.emails:
        _reset_tasks[email] = {"email": email, "status": "pending", "message": "等待中", "ok": None}

    _broadcast_from_thread({"type": "reset_started", "tasks": _reset_tasks})

    thread = threading.Thread(
        target=_run_reset_batch,
        args=(req.emails, req.new_password, req.poll_timeout),
        daemon=True,
    )
    thread.start()

    return {"ok": True, "count": len(req.emails)}


@app.get("/api/reset-status")
async def reset_status():
    return {"running": _reset_running, "tasks": _reset_tasks}


def _run_reset_batch(emails: list[str], new_password: str, poll_timeout: int):
    """后台线程：逐个执行改密任务"""
    global _reset_running, _reset_tasks

    from .reset_password import reset_account_password

    def _progress(info: dict):
        email = info.get("email", "")
        if email in _reset_tasks:
            _reset_tasks[email]["message"] = info.get("message", "")
            _reset_tasks[email]["status"] = "running"
        _broadcast_from_thread({"type": "reset_update", "tasks": _reset_tasks})

    for email in emails:
        _reset_tasks[email]["status"] = "running"
        _reset_tasks[email]["message"] = "正在处理..."
        _broadcast_from_thread({"type": "reset_update", "tasks": _reset_tasks})

        result = reset_account_password(
            email=email,
            new_password=new_password,
            progress_cb=_progress,
            poll_timeout=float(poll_timeout),
        )

        _reset_tasks[email]["ok"] = result.get("ok")
        _reset_tasks[email]["status"] = "done" if result.get("ok") else "failed"
        _reset_tasks[email]["message"] = result.get("message") or result.get("error") or ""
        _broadcast_from_thread({"type": "reset_update", "tasks": _reset_tasks})

        # 每个账号间稍微间隔，避免过快触发速率限制
        time.sleep(3)

    _reset_running = False
    _broadcast_from_thread({"type": "reset_completed", "tasks": _reset_tasks})

    # 改密成功的账号，更新 history 中的密码
    _apply_reset_to_history(emails, new_password)


def _apply_reset_to_history(emails: list[str], new_password: str):
    """将改密成功的账号更新到 history 记录中"""
    success_emails = {e for e, t in _reset_tasks.items() if t.get("ok")}
    if not success_emails:
        return

    changed = False
    with _file_lock:
        for h in state.history:
            if h.get("email") in success_emails:
                h["password"] = new_password
                changed = True
        if changed:
            ACCOUNTS_JSON.write_text(
                json.dumps(state.history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    if changed:
        _broadcast_from_thread({"type": "history_update", "history": state.history})
        logger.info(f"已更新 history 中 {len(success_emails)} 个账号的密码记录")
