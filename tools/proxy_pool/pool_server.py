"""
SOCKS5 代理池 HTTP 服务 + 持续补货

启动:
  python tools/proxy_pool/pool_server.py
  默认端口 7861(与 GhostReg 7860 错开)

HTTP API:
  GET  /pool/get          → 随机吐 1 个可用 SOCKS5 {"proxy": "socks5://..."}
  GET  /pool/route        → 返回可用于 429 对照实验的路由描述
  GET  /pool/oauth-route  → OAuth sessions 专用: 活 SOCKS5 + 用户专属 Worker(不依赖 Route D 表)
  GET  /pool/all          → 返回所有可用列表
  GET  /pool/status       → 池子状态
  POST /pool/report-bad   → body {"proxy": "socks5://...", "reason": "timeout|rate_limited"}
  POST /pool/refresh      → 手动触发补货
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 复用同目录工具
import sys
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent))  # 让 tools.proxy_pool 可导入

from tools.proxy_pool.fetch_sources import fetch_all  # type: ignore
from tools.proxy_pool.validate import (  # type: ignore
    ValidatedProxy,
    test_http_via_socks5,
    test_l1,
    L3_URL,
    TIMEOUT_L3,
    validate_pool,
    dedup_by_subnet,
)

logger = logging.getLogger("proxy_pool.server")

# 配置
DEFAULT_PORT = 7861
TARGET_ALIVE = 20
REFRESH_INTERVAL = 90
HEALTH_INTERVAL = 60
BLACKLIST_TTL = 24 * 3600
DEFAULT_COOLDOWN = 45
DEFAULT_WORKER = ""
SNAPSHOT_PATH = HERE / "pool_snapshot.json"
VALIDATED_POOL_PATH = HERE / "validated_pool.json"
ROUTE_D_POOL_PATH = HERE / "route_d_pool.json"


def load_user_worker_url() -> str:
    """从 GhostReg settings.json 读取用户自己部署的 Worker URL。"""
    settings_path = HERE.parent.parent / "output" / "settings.json"
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        return str(data.get("cf_worker_url", "")).rstrip("/")
    except Exception:
        return ""


@dataclass
class PoolState:
    proxies: dict[str, ValidatedProxy] = field(default_factory=dict)
    blacklist: dict[str, float] = field(default_factory=dict)
    cooldown: dict[str, float] = field(default_factory=dict)  # addr -> until_ts (429/rate limited)
    fail_count: dict[str, int] = field(default_factory=dict)
    last_refresh_ts: float = 0
    last_health_ts: float = 0
    last_refresh_added: int = 0
    last_health_kicked: int = 0
    fetch_stats: dict = field(default_factory=dict)
    route_d: dict[str, dict] = field(default_factory=dict)  # addr -> route d metadata
    route_d_colo_cursor: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)

    def alive_count(self) -> int:
        with self.lock:
            return len(self.proxies)

    def _cleanup_expired_locked(self):
        now = time.time()
        for store in (self.blacklist, self.cooldown):
            expired = [addr for addr, until in store.items() if until and now >= until]
            for addr in expired:
                store.pop(addr, None)
                if store is self.blacklist:
                    self.fail_count.pop(addr, None)

    def is_blacklisted(self, addr: str) -> bool:
        with self.lock:
            self._cleanup_expired_locked()
            return time.time() < self.blacklist.get(addr, 0)

    def is_cooling(self, addr: str) -> bool:
        with self.lock:
            self._cleanup_expired_locked()
            return time.time() < self.cooldown.get(addr, 0)

    def add(self, p: ValidatedProxy):
        with self.lock:
            if self.is_blacklisted(p.addr):
                return False
            self.proxies[p.addr] = p
            return True

    def kick(self, addr: str, reason: str = "unknown"):
        with self.lock:
            self.proxies.pop(addr, None)
            self.route_d.pop(addr, None)
            cnt = self.fail_count.get(addr, 0) + 1
            self.fail_count[addr] = cnt
            if cnt >= 3:
                self.blacklist[addr] = time.time() + BLACKLIST_TTL
                logger.info(f"[pool] blacklisted {addr} (fail={cnt}, reason={reason})")
            else:
                logger.debug(f"[pool] kicked {addr} (fail={cnt}, reason={reason})")

    def mark_cooldown(self, addr: str, seconds: int = DEFAULT_COOLDOWN, reason: str = "rate_limited"):
        with self.lock:
            self.cooldown[addr] = time.time() + max(seconds, 1)
            logger.info(f"[pool] cooldown {addr} {seconds}s ({reason})")

    def get_random(self, country: str = "") -> ValidatedProxy | None:
        with self.lock:
            self._cleanup_expired_locked()
            candidates = [p for p in self.proxies.values() if not self.is_cooling(p.addr)]
            if country:
                candidates = [p for p in candidates if p.country == country]
            if not candidates:
                return None
            return random.choice(candidates)

    def _route_d_candidates_locked(self) -> list[tuple[str, ValidatedProxy, dict]]:
        self._cleanup_expired_locked()
        out: list[tuple[str, ValidatedProxy, dict]] = []
        for addr, meta in self.route_d.items():
            if addr in self.proxies and addr not in self.cooldown and addr not in self.blacklist:
                out.append((addr, self.proxies[addr], meta))
        return out

    def get_route_d_balanced(self) -> tuple[ValidatedProxy | None, dict]:
        """按 worker_colo 轮询返回 Route D 节点,避免连续同 colo。"""
        with self.lock:
            candidates = self._route_d_candidates_locked()
            if not candidates:
                return None, {}
            by_colo: dict[str, list[tuple[str, ValidatedProxy, dict]]] = {}
            for item in candidates:
                colo = item[2].get("worker_colo") or "unknown"
                by_colo.setdefault(colo, []).append(item)
            colos = sorted(by_colo)
            if not colos:
                return None, {}
            colo = colos[self.route_d_colo_cursor % len(colos)]
            self.route_d_colo_cursor += 1
            # 同 colo 内优先低延迟,再随机打散前 3 个
            items = sorted(by_colo[colo], key=lambda x: x[2].get("ms", 999999))
            addr, p, meta = random.choice(items[: min(3, len(items))])
            return p, meta

    def all(self) -> list[ValidatedProxy]:
        with self.lock:
            return list(self.proxies.values())

    def status(self) -> dict:
        with self.lock:
            self._cleanup_expired_locked()
            by_country = {}
            for p in self.proxies.values():
                c = p.country or "??"
                by_country[c] = by_country.get(c, 0) + 1
            route_items = self._route_d_candidates_locked()
            route_by_colo: dict[str, int] = {}
            route_ms = []
            for _addr, _p, meta in route_items:
                c = meta.get("worker_colo") or "unknown"
                route_by_colo[c] = route_by_colo.get(c, 0) + 1
                if isinstance(meta.get("ms"), int) and meta["ms"] >= 0:
                    route_ms.append(meta["ms"])
            return {
                "alive": len(self.proxies),
                "worker_url_set": bool(load_user_worker_url()),
                "worker_url": load_user_worker_url(),
                "blacklisted": len(self.blacklist),
                "cooldown": len(self.cooldown),
                "target_alive": TARGET_ALIVE,
                "last_refresh_ts": int(self.last_refresh_ts),
                "last_refresh_age_s": int(time.time() - self.last_refresh_ts) if self.last_refresh_ts else -1,
                "last_refresh_added": self.last_refresh_added,
                "last_health_ts": int(self.last_health_ts),
                "last_health_kicked": self.last_health_kicked,
                "fetch_stats": self.fetch_stats,
                "route_d_alive": len(route_items),
                "route_d_colos": sorted(route_by_colo),
                "route_d_by_colo": dict(sorted(route_by_colo.items())),
                "route_d_avg_ms": int(sum(route_ms) / max(len(route_ms), 1)) if route_ms else -1,
                "by_country": dict(sorted(by_country.items(), key=lambda x: -x[1])),
            }


STATE = PoolState()


def load_validated_pool() -> int:
    if not VALIDATED_POOL_PATH.exists():
        return 0
    try:
        data = json.loads(VALIDATED_POOL_PATH.read_text(encoding="utf-8"))
        count = 0
        for raw in data.get("proxies", []):
            p = ValidatedProxy(
                host=raw["host"],
                port=int(raw["port"]),
                sources=raw.get("sources", []),
                country=raw.get("country", ""),
                l1_pass=raw.get("l1_pass", True),
                l2_pass=raw.get("l2_pass", True),
                l3_pass=raw.get("l3_pass", True),
                l1_ms=raw.get("l1_ms", -1),
                l2_ms=raw.get("l2_ms", -1),
                l3_ms=raw.get("l3_ms", -1),
                exit_ip=raw.get("exit_ip", ""),
            )
            if p.passed_all and STATE.add(p):
                count += 1
        return count
    except Exception as e:
        logger.warning(f"load validated pool failed: {e}")
        return 0


def load_route_d_pool() -> int:
    if not ROUTE_D_POOL_PATH.exists():
        return 0
    try:
        data = json.loads(ROUTE_D_POOL_PATH.read_text(encoding="utf-8"))
        count = 0
        with STATE.lock:
            for raw in data.get("routes", []):
                addr = f"{raw['host']}:{int(raw['port'])}"
                if addr in STATE.proxies:
                    STATE.route_d[addr] = raw
                    count += 1
            STATE.fetch_stats["route_d_worker"] = data.get("worker", "")
            STATE.fetch_stats["route_d_ok"] = data.get("ok", 0)
            STATE.fetch_stats["route_d_unique_colos"] = data.get("unique_colos", 0)
        return count
    except Exception as e:
        logger.warning(f"load route d pool failed: {e}")
        return 0


def health_check_once():
    with STATE.lock:
        targets = list(STATE.proxies.values())
    if not targets:
        return
    logger.info(f"[health] checking {len(targets)} alive proxies")
    t0 = time.monotonic()
    kicked = 0
    for p in targets:
        ok, _ = test_l1(p.host, p.port, timeout=3)
        if not ok:
            STATE.kick(p.addr, "L1 fail in health")
            kicked += 1
            continue
        if random.random() < 0.33:
            ok, _ms, _text, err = test_http_via_socks5(p.socks5_url, L3_URL, TIMEOUT_L3)
            if not ok:
                STATE.kick(p.addr, "L3 fail in health: " + err)
                kicked += 1
    elapsed = int(time.monotonic() - t0)
    with STATE.lock:
        STATE.last_health_ts = time.time()
        STATE.last_health_kicked = kicked
    logger.info(f"[health] done in {elapsed}s, kicked={kicked}")


def refresh_once(workers: int = 50, max_validate: int = 400):
    logger.info("[refresh] start")
    t0 = time.monotonic()
    raw = fetch_all(save=False)
    with STATE.lock:
        STATE.fetch_stats["last_raw_count"] = len(raw)
    seen = set()
    with STATE.lock:
        seen.update(STATE.proxies.keys())
    new_candidates: list[ValidatedProxy] = []
    for r in raw:
        if r.addr in seen:
            continue
        if STATE.is_blacklisted(r.addr):
            continue
        new_candidates.append(ValidatedProxy(host=r.host, port=r.port, sources=r.sources, country=r.country))
    if len(new_candidates) > max_validate:
        new_candidates.sort(key=lambda x: -len(x.sources))
        new_candidates = new_candidates[:max_validate]
    logger.info(f"[refresh] {len(raw)} raw, {len(new_candidates)} new candidates to validate")
    if not new_candidates:
        with STATE.lock:
            STATE.last_refresh_ts = time.time()
            STATE.last_refresh_added = 0
        return
    done = validate_pool(new_candidates, workers=workers)
    passed = [p for p in done if p.passed_all]
    keep, _dedup_stats = dedup_by_subnet(passed)
    added = 0
    for p in keep:
        if STATE.add(p):
            added += 1
    elapsed = int(time.monotonic() - t0)
    with STATE.lock:
        STATE.last_refresh_ts = time.time()
        STATE.last_refresh_added = added
    logger.info(f"[refresh] done in {elapsed}s: {len(passed)} passed, {len(keep)} after /24 dedup, {added} added")
    write_snapshot()


def write_snapshot():
    try:
        snap = {
            "ts": int(time.time()),
            "alive": STATE.alive_count(),
            "status": STATE.status(),
            "proxies": [asdict(p) for p in STATE.all()],
        }
        SNAPSHOT_PATH.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"snapshot write failed: {e}")


def background_loop(refresh_interval: int, health_interval: int):
    next_refresh = time.time()
    next_health = time.time() + health_interval
    while True:
        now = time.time()
        try:
            if now >= next_refresh or STATE.alive_count() < TARGET_ALIVE:
                refresh_once()
                next_refresh = time.time() + refresh_interval
            if now >= next_health and STATE.alive_count() > 0:
                health_check_once()
                next_health = time.time() + health_interval
        except Exception as e:
            logger.exception(f"background loop error: {e}")
        time.sleep(5)


def _json_response(handler: BaseHTTPRequestHandler, status: int, data: dict):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _route_payload(p: ValidatedProxy, mode: str, worker: str, route_meta: dict, purpose: str = "") -> dict:
    return {
        "purpose": purpose,
        "mode": mode,
        "chain": ["local", p.socks5_url, worker if worker else "target", "target" if worker else ""],
        "entry_proxy": p.socks5_url,
        "worker": worker,
        "expected_target_seen_ip": "worker_exit_ip" if mode == "socks5_to_worker" else "socks5_exit_ip",
        "cooldown_policy": "429/rate_limited means cooldown, not bad proxy",
        "route_d_meta": {
            "worker_ip": route_meta.get("worker_ip", ""),
            "worker_colo": route_meta.get("worker_colo", ""),
            "worker_ray": route_meta.get("worker_ray", ""),
            "route_ms": route_meta.get("ms", -1),
        } if route_meta else {},
        "proxy_meta": {
            "host": p.host,
            "port": p.port,
            "country": p.country,
            "exit_ip": p.exit_ip,
            "l3_ms": p.l3_ms,
            "sources": p.sources,
        },
    }


class PoolHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/pool/get":
            country = q.get("country", [""])[0].upper()
            p = STATE.get_random(country=country)
            if not p:
                return _json_response(self, 503, {"error": "no available proxy", "alive": STATE.alive_count()})
            return _json_response(self, 200, {
                "proxy": p.socks5_url,
                "host": p.host,
                "port": p.port,
                "country": p.country,
                "exit_ip": p.exit_ip,
                "l3_ms": p.l3_ms,
                "sources": p.sources,
            })

        if u.path in ("/pool/route", "/pool/oauth-route"):
            if u.path == "/pool/oauth-route":
                mode = "socks5_to_worker"
                worker = q.get("worker", [""])[0] or load_user_worker_url() or DEFAULT_WORKER
                purpose = "oauth_sessions_ip_bucket"
            else:
                mode = q.get("mode", ["socks5_direct"])[0]
                worker = q.get("worker", [""])[0]
                purpose = q.get("purpose", [""])[0]
            country = q.get("country", [""])[0].upper()
            if mode not in ("socks5_direct", "socks5_to_worker"):
                return _json_response(self, 400, {"error": "invalid mode"})
            if mode == "socks5_to_worker" and not worker:
                return _json_response(self, 400, {"error": "worker_url_not_configured", "message": "请先在 GhostReg 前端部署专属 Cloudflare Worker"})
            route_meta = {}
            if mode == "socks5_to_worker":
                # 发布版不依赖 Route D 表:直接从当前活 SOCKS5 池抽一个,拼用户自己的 Worker URL。
                # 失败由注册机 fail-over + /pool/report-bad 自愈,无需等待重建 SOCKS5→colo 映射表。
                p = STATE.get_random(country=country)
            else:
                p = STATE.get_random(country=country)
            if not p:
                st = STATE.status()
                return _json_response(self, 503, {
                    "error": "no available proxy",
                    "alive": st.get("alive", 0),
                    "route_d_alive": st.get("route_d_alive", 0),
                    "worker_url_set": st.get("worker_url_set", False),
                    "cooldown": st.get("cooldown", 0),
                })
            if mode == "socks5_direct":
                with STATE.lock:
                    route_meta = STATE.route_d.get(p.addr, {})
            return _json_response(self, 200, _route_payload(p, mode, worker, route_meta, purpose))

        if u.path == "/pool/all":
            arr = STATE.all()
            return _json_response(self, 200, {
                "alive": len(arr),
                "proxies": [
                    {"proxy": p.socks5_url, "country": p.country, "exit_ip": p.exit_ip, "l3_ms": p.l3_ms, "sources": p.sources}
                    for p in arr
                ],
            })

        if u.path == "/pool/status":
            return _json_response(self, 200, STATE.status())

        return _json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        body_bytes = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except Exception:
            body = {}

        if u.path == "/pool/report-bad":
            target = body.get("proxy", "")
            if target.startswith("socks5://"):
                target = target[len("socks5://"):]
            reason = str(body.get("reason", "client report"))
            if reason in ("rate_limited", "http_429", "429"):
                seconds = int(body.get("cooldown_seconds", DEFAULT_COOLDOWN) or DEFAULT_COOLDOWN)
                STATE.mark_cooldown(target, seconds=seconds, reason=reason)
                return _json_response(self, 200, {"ok": True, "action": "cooldown", "cooldown_seconds": seconds, "status": STATE.status()})
            STATE.kick(target, reason)
            return _json_response(self, 200, {"ok": True, "action": "kicked", "alive": STATE.alive_count(), "status": STATE.status()})

        if u.path == "/pool/refresh":
            threading.Thread(target=refresh_once, daemon=True).start()
            return _json_response(self, 200, {"triggered": True})

        return _json_response(self, 404, {"error": "not found"})


def main():
    global TARGET_ALIVE, REFRESH_INTERVAL, HEALTH_INTERVAL
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--target-alive", type=int, default=TARGET_ALIVE)
    ap.add_argument("--refresh-interval", type=int, default=REFRESH_INTERVAL)
    ap.add_argument("--health-interval", type=int, default=HEALTH_INTERVAL)
    ap.add_argument("--no-bootstrap", action="store_true", help="跳过启动时立即跑一次补货")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        pass

    TARGET_ALIVE = args.target_alive
    REFRESH_INTERVAL = args.refresh_interval
    HEALTH_INTERVAL = args.health_interval

    print("=== proxy_pool 服务启动 ===")
    print(f"端口: {args.port}")
    print(f"目标活节点: {TARGET_ALIVE}")
    print(f"补货间隔: {REFRESH_INTERVAL}s")
    print(f"健康检查间隔: {HEALTH_INTERVAL}s")

    loaded = load_validated_pool()
    if loaded:
        print(f"已加载 validated_pool.json: {loaded} 个可用 SOCKS5")
    route_d_loaded = load_route_d_pool()
    if route_d_loaded:
        print(f"已加载 route_d_pool.json: {route_d_loaded} 个 Route D 可用节点")

    if not args.no_bootstrap and STATE.alive_count() < TARGET_ALIVE:
        threading.Thread(target=refresh_once, daemon=True).start()
        print("启动时已触发首次补货(后台跑,可能需要 1-3 分钟)")

    threading.Thread(target=background_loop, args=(REFRESH_INTERVAL, HEALTH_INTERVAL), daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), PoolHandler)
    print("\nHTTP API:")
    print(f"  GET  http://127.0.0.1:{args.port}/pool/status")
    print(f"  GET  http://127.0.0.1:{args.port}/pool/get")
    print(f"  GET  http://127.0.0.1:{args.port}/pool/route?mode=socks5_direct")
    print(f"  GET  http://127.0.0.1:{args.port}/pool/oauth-route")
    print(f"  POST http://127.0.0.1:{args.port}/pool/report-bad")
    print(f"  POST http://127.0.0.1:{args.port}/pool/refresh")
    print("\n按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中...")


if __name__ == "__main__":
    main()
