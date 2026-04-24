"""
CF Workers 代理池管理器

通过 Cloudflare Workers HTTP 反向代理实现动态 IP 注册。
Workers 的 fetch() 每次请求自动获取不同的 CF 出口 IP。

架构：
  GhostReg → curl_cffi (本地梯子) → CF Workers /proxy → JetBrains API
  打码/邮箱服务 → 直连（不走 Workers）

用户只需提供 CF API Token + Account ID，程序自动部署 Workers。
"""
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

import requests as _requests  # 仅用于 CF API 管理

logger = logging.getLogger("ghostreg.proxy_pool")

# Workers 代码路径
_WORKERS_JS = Path(__file__).parent.parent / "workers" / "ghostreg_proxy.js"


@dataclass
class WorkerInfo:
    """单个 Workers 实例信息"""
    name: str
    url: str
    healthy: bool = True


class ProxyPool:
    """
    CF Workers 代理池。
    
    部署 N 个 Workers → 每个 Workers 有不同的 CF 出口 IP 池
    → 注册任务按组分配到不同 Workers → 避免单 IP 触发 429
    """
    
    def __init__(self, api_token: str, account_id: str, ladder_proxy: str = ""):
        """
        Args:
            api_token: Cloudflare API Token
            account_id: Cloudflare Account ID
            ladder_proxy: 本地梯子代理地址（如 http://127.0.0.1:7890），
                          用于访问被墙的 workers.dev 域名
        """
        self.api_token = api_token
        self.account_id = account_id
        self.ladder_proxy = ladder_proxy
        self.workers: list[WorkerInfo] = []
        self._subdomain: str = ""
        self._lock = threading.Lock()
        self._round_robin_idx = 0
    
    @property
    def _proxies(self) -> dict:
        """requests 库的代理配置"""
        if self.ladder_proxy:
            return {"https": self.ladder_proxy, "http": self.ladder_proxy}
        return {}
    
    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_token}"}
    
    def deploy(self, count: int = 1) -> bool:
        """
        部署 N 个 Workers。
        
        Args:
            count: 部署数量（每个 Workers 有独立的 CF 出口 IP 池）
        
        Returns:
            是否全部部署成功
        """
        if not _WORKERS_JS.exists():
            logger.error(f"Workers 代码不存在: {_WORKERS_JS}")
            return False
        
        script = _WORKERS_JS.read_text(encoding="utf-8")
        
        # 获取 subdomain
        self._subdomain = self._get_subdomain()
        if not self._subdomain:
            logger.error("无法获取 workers.dev 子域名")
            return False
        
        success_count = 0
        for i in range(count):
            name = f"ghostreg-p{i}"
            if self._deploy_one(name, script):
                url = f"https://{name}.{self._subdomain}.workers.dev"
                self.workers.append(WorkerInfo(name=name, url=url))
                success_count += 1
                logger.info(f"[ProxyPool] Workers #{i} 部署成功: {url}")
            else:
                logger.error(f"[ProxyPool] Workers #{i} 部署失败")
        
        logger.info(f"[ProxyPool] 部署完成: {success_count}/{count} 成功")
        return success_count > 0
    
    def _deploy_one(self, name: str, script: str) -> bool:
        """部署单个 Workers"""
        api_url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/workers/scripts/{name}"
        metadata = {"main_module": "worker.js", "bindings": []}
        
        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            "worker.js": ("worker.js", script, "application/javascript+module"),
        }
        
        try:
            resp = _requests.put(
                api_url, headers=self._headers, files=files,
                timeout=30, proxies=self._proxies,
            )
            if resp.status_code in (200, 201) and resp.json().get("success"):
                # 启用 subdomain
                _requests.post(
                    f"{api_url}/subdomain",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json={"enabled": True},
                    timeout=10, proxies=self._proxies,
                )
                return True
            else:
                logger.error(f"Deploy {name} failed: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Deploy {name} error: {e}")
        return False
    
    def _get_subdomain(self) -> str:
        """获取 workers.dev 子域名"""
        try:
            r = _requests.get(
                f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/workers/subdomain",
                headers=self._headers, timeout=10, proxies=self._proxies,
            )
            return r.json().get("result", {}).get("subdomain", "")
        except Exception as e:
            logger.error(f"Get subdomain error: {e}")
            return ""
    
    def get_worker_url(self, group_id: int = 0) -> Optional[str]:
        """
        为指定组分配一个 Workers URL（round-robin）。
        
        Args:
            group_id: 任务组 ID（同组任务用同一 Workers）
        
        Returns:
            Workers URL 或 None（无可用 Workers）
        """
        if not self.workers:
            return None
        
        # 按组 ID 分配：group_id % len(workers)
        idx = group_id % len(self.workers)
        w = self.workers[idx]
        return w.url if w.healthy else None
    
    def health_check(self) -> dict:
        """检查所有 Workers 健康状态"""
        from curl_cffi.requests import Session as CurlSession
        
        results = {}
        s = CurlSession(
            impersonate="chrome136", verify=False,
            proxy=self.ladder_proxy or None,
        )
        
        for w in self.workers:
            try:
                r = s.get(f"{w.url}/health", timeout=10)
                data = r.json()
                w.healthy = data.get("status") == "ok"
                results[w.name] = {"healthy": w.healthy, "url": w.url}
            except Exception as e:
                w.healthy = False
                results[w.name] = {"healthy": False, "error": str(e)}
        
        return results
    
    def get_exit_ips(self) -> dict:
        """获取所有 Workers 的当前出口 IP"""
        from curl_cffi.requests import Session as CurlSession
        
        results = {}
        s = CurlSession(
            impersonate="chrome136", verify=False,
            proxy=self.ladder_proxy or None,
        )
        
        for w in self.workers:
            try:
                r = s.get(f"{w.url}/ip", timeout=10)
                data = r.json()
                results[w.name] = {
                    "ip": data.get("origin"),
                    "colo": data.get("cf_colo"),
                }
            except Exception as e:
                results[w.name] = {"error": str(e)}
        
        return results
    
    def cleanup(self):
        """删除所有已部署的 Workers"""
        for w in self.workers:
            try:
                _requests.delete(
                    f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/workers/scripts/{w.name}",
                    headers=self._headers, timeout=10, proxies=self._proxies,
                )
                logger.info(f"[ProxyPool] 已删除 Workers: {w.name}")
            except Exception as e:
                logger.warning(f"[ProxyPool] 删除 {w.name} 失败: {e}")
        self.workers.clear()
    
    @property
    def available(self) -> bool:
        """是否有可用的 Workers"""
        return any(w.healthy for w in self.workers)
    
    @property
    def count(self) -> int:
        return len(self.workers)
    
    def status_dict(self) -> dict:
        """返回状态信息（供 API 返回）"""
        return {
            "deployed": self.count,
            "healthy": sum(1 for w in self.workers if w.healthy),
            "workers": [
                {"name": w.name, "url": w.url, "healthy": w.healthy}
                for w in self.workers
            ],
        }


class WorkersSession:
    """
    通过 CF Workers /proxy 端点转发 HTTP 请求的 Session。
    
    实现与 curl_cffi.Session 兼容的 .get()/.post() 接口，
    内部手动管理 Cookie 转发（Workers fetch() 无状态）。
    
    用法：
        ws = WorkersSession(worker_url="https://ghostreg-p0.xxx.workers.dev",
                            ladder_proxy="http://127.0.0.1:7890")
        ws.get("https://account.jetbrains.com/signup")
        ws.post("https://account.jetbrains.com/api/auth/sessions", json={})
    """
    
    def __init__(self, worker_url: str, ladder_proxy: str = "", verify: bool = False):
        from curl_cffi.requests import Session as CurlSession
        
        self.worker_url = worker_url.rstrip("/")
        self.ladder_proxy = ladder_proxy
        
        # 内部 curl_cffi session 用于连接 Workers（走梯子）
        self._s = CurlSession(
            impersonate="chrome136",
            verify=verify,
            proxy=ladder_proxy or None,
            timeout=30,
        )
        
        # 手动管理的 Cookie Jar
        self.cookies = _SimpleCookieJar()
        
        # 存储 headers（兼容 JBRegisterer 直接设置 headers）
        self.headers: dict = {}
    
    def get(self, url: str, **kwargs) -> "_WorkersResponse":
        return self._request("GET", url, **kwargs)
    
    def post(self, url: str, **kwargs) -> "_WorkersResponse":
        return self._request("POST", url, **kwargs)
    
    def _request(self, method: str, url: str, **kwargs) -> "_WorkersResponse":
        """通过 Workers /proxy 转发请求"""
        
        # 合并 headers
        req_headers = {**self.headers}
        if "headers" in kwargs:
            req_headers.update(kwargs.pop("headers"))
        
        # 构建 Workers 请求体
        payload: dict = {
            "target_url": url,
            "method": method,
            "headers": req_headers,
            "cookies": self.cookies.to_header(),
        }
        
        # 处理 JSON body
        json_data = kwargs.pop("json", None)
        if json_data is not None:
            payload["json_body"] = json_data
        
        # 处理 data body
        data = kwargs.pop("data", None)
        if data is not None:
            if isinstance(data, dict):
                payload["body"] = "&".join(f"{k}={v}" for k, v in data.items())
            else:
                payload["body"] = str(data)
        
        # 发送到 Workers /proxy
        try:
            r = self._s.post(
                f"{self.worker_url}/proxy",
                json=payload,
                timeout=kwargs.get("timeout", 30),
            )
        except Exception as e:
            raise ConnectionError(f"Workers 连接失败: {e}")
        
        if r.status_code != 200:
            raise ConnectionError(f"Workers 返回 HTTP {r.status_code}: {r.text[:200]}")
        
        # 解析 Workers 响应
        try:
            wd = r.json()
        except Exception:
            raise ConnectionError(f"Workers 返回非 JSON: {r.text[:200]}")
        
        inner_status = wd.get("status", 0)
        inner_body = wd.get("body", "")
        inner_headers = wd.get("headers", {})
        
        # 更新 Cookie Jar
        set_cookie = inner_headers.get("set-cookie", "")
        if set_cookie:
            self.cookies.update_from_header(set_cookie)
        
        return _WorkersResponse(
            status_code=inner_status,
            text=inner_body,
            headers=inner_headers,
            cookies=self.cookies,
        )


class _WorkersResponse:
    """模拟 curl_cffi Response 的关键属性"""
    
    def __init__(self, status_code: int, text: str, headers: dict, cookies):
        self.status_code = status_code
        self.text = text
        self.headers = headers
        self.cookies = cookies
    
    def json(self, **kwargs):
        import json as _json
        return _json.loads(self.text, **kwargs)


class _SimpleCookieJar:
    """简易 Cookie 管理（线程安全）"""
    
    def __init__(self):
        self._cookies: dict[str, str] = {}
        self._lock = threading.Lock()
    
    def get(self, name: str, default: str = "") -> str:
        with self._lock:
            return self._cookies.get(name, default)
    
    def set(self, name: str, value: str):
        with self._lock:
            self._cookies[name] = value
    
    def update_from_header(self, set_cookie: str):
        """从 Set-Cookie header 提取 cookie"""
        with self._lock:
            # set-cookie 可能有多个 cookie 用逗号分隔
            # 但也可能 expires 里有逗号，所以简单按 name=value; 分割
            for part in set_cookie.split(","):
                part = part.strip()
                if "=" in part and not part[0] == " ":
                    name_val = part.split(";")[0].strip()
                    if "=" in name_val:
                        name, val = name_val.split("=", 1)
                        name = name.strip()
                        # 跳过属性（Path, Domain, Secure 等）
                        if name.lower() not in ("path", "domain", "expires", "max-age",
                                                  "secure", "httponly", "samesite"):
                            self._cookies[name] = val
    
    def to_header(self) -> str:
        """生成 Cookie header 字符串"""
        with self._lock:
            return "; ".join(f"{k}={v}" for k, v in self._cookies.items())
    
    def __repr__(self):
        return f"CookieJar({self._cookies})"
