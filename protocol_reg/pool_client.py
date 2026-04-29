"""
A 池 HTTP API 客户端封装

跟本机 A 池服务(tools/proxy_pool/pool_server.py,默认端口 7861)对话:
- acquire_route():领一个双层路由 {SOCKS5 + Worker URL}
- report_bad():汇报失败的路由(撞 429 / 网络异常 / 其他)

策略:
- A 池没货 → acquire_route 返回 None,调用方自行回退到单层 Worker 模式
- 撞 429 → report_bad reason="rate_limited",A 池把该 IP 冷却 45 秒
- 网络异常 → report_bad reason="timeout"/"network",A 池累计 3 次拉黑 24 小时
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import requests as _requests

logger = logging.getLogger("protocol_reg.pool_client")

DEFAULT_POOL_URL = "http://127.0.0.1:7861"
DEFAULT_TIMEOUT = 1  # A 池在本机,不应该超过 1 秒;超时快速回退/自愈


@dataclass
class Route:
    """A 池返回的双层路由"""
    entry_proxy: str           # SOCKS5 地址(如 "socks5://1.2.3.4:1080")
    worker: str                # 用户专属 Worker URL(如 "https://ghostreg-p0.<subdomain>.workers.dev")
    worker_colo: str = ""      # Worker 落点机房(如 "FRA")
    worker_ip: str = ""        # Worker 出口 IP
    proxy_country: str = ""    # SOCKS5 国别
    
    @classmethod
    def from_payload(cls, data: dict) -> "Route":
        meta = data.get("route_d_meta") or {}
        proxy_meta = data.get("proxy_meta") or {}
        return cls(
            entry_proxy=data.get("entry_proxy", ""),
            worker=data.get("worker", ""),
            worker_colo=meta.get("worker_colo", ""),
            worker_ip=meta.get("worker_ip", ""),
            proxy_country=proxy_meta.get("country", ""),
        )
    
    def __str__(self) -> str:
        return f"Route(SOCKS5={self.entry_proxy} country={self.proxy_country} → Worker[{self.worker_colo}])"


class PoolClient:
    """A 池 HTTP 客户端"""
    
    def __init__(self, pool_url: str = DEFAULT_POOL_URL, timeout: float = DEFAULT_TIMEOUT):
        self.pool_url = pool_url.rstrip("/")
        self.timeout = timeout
    
    def is_alive(self) -> bool:
        """检查 A 池服务是否在运行"""
        try:
            r = _requests.get(f"{self.pool_url}/pool/status", timeout=self.timeout)
            return r.status_code == 200
        except Exception:
            return False
    
    def status(self) -> dict:
        """获取 A 池状态(alive 数量、route_d 数量等)"""
        try:
            r = _requests.get(f"{self.pool_url}/pool/status", timeout=self.timeout)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            logger.debug(f"[pool_client] status 失败: {e}")
        return {}
    
    def acquire_route(self) -> Optional[Route]:
        """
        领一个双层路由(OAuth sessions 专用,按 colo 均衡)。
        
        Returns:
            Route 对象 或 None(池子空时)
        """
        try:
            r = _requests.get(f"{self.pool_url}/pool/oauth-route", timeout=self.timeout)
        except Exception as e:
            logger.warning(f"[pool_client] acquire_route 网络失败: {e}")
            return None
        
        if r.status_code == 503:
            # 池子空,返回 None 让调用方回退
            return None
        if r.status_code != 200:
            logger.warning(f"[pool_client] acquire_route HTTP {r.status_code}: {r.text[:200]}")
            return None
        
        try:
            data = r.json()
        except Exception:
            logger.warning(f"[pool_client] acquire_route 非 JSON 响应: {r.text[:200]}")
            return None
        
        # 必须同时有 entry_proxy 和 worker 才算双层路由
        if not data.get("entry_proxy") or not data.get("worker"):
            logger.warning(f"[pool_client] acquire_route 路由不完整: {data}")
            return None
        
        return Route.from_payload(data)
    
    def report_bad(self, proxy: str, reason: str = "unknown", cooldown_seconds: int = 45):
        """
        汇报路由失败。
        
        Args:
            proxy: SOCKS5 地址(socks5://host:port)
            reason: 失败原因
                - "rate_limited" / "http_429" / "429" → 进 cooldown(默认 45s)
                - 其他 → kick + 累计 3 次拉黑 24h
            cooldown_seconds: 仅 rate_limited 类有效,默认 45 秒
        """
        if not proxy:
            return
        body = {"proxy": proxy, "reason": reason, "cooldown_seconds": cooldown_seconds}
        try:
            _requests.post(
                f"{self.pool_url}/pool/report-bad",
                json=body,
                timeout=self.timeout,
            )
        except Exception as e:
            # 报告失败不影响主流程
            logger.debug(f"[pool_client] report_bad 失败 (可忽略): {e}")


# 模块级单例(供 worker.py 直接使用)
_default_client: Optional[PoolClient] = None


def get_default_client() -> PoolClient:
    global _default_client
    if _default_client is None:
        _default_client = PoolClient()
    return _default_client
