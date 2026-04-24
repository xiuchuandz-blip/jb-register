"""
HTTP 客户端封装 — curl_cffi + Chrome TLS 指纹伪造
"""
import logging
from curl_cffi.requests import Session

logger = logging.getLogger("protocol_reg.client")

# Chrome 147 的浏览器 headers（从抓包结果精确复制）
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Origin": "https://account.jetbrains.com",
    "Sec-Ch-Ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-Requested-With": "XMLHttpRequest",
}


def new_session(verify: bool = True, worker_url: str = "",
                ladder_proxy: str = "") -> Session:
    """
    创建一个伪装 Chrome 147 TLS 指纹的 HTTP Session。
    - JA3/JA4/HTTP2 指纹与真实 Chrome 一致
    - 自带 Cookie Jar（自动管理 JSESSIONID、_st-JBA 等）
    - verify=False 用于梯子 TUN 模式 SSL 兼容
    
    Args:
        verify: SSL 验证开关
        worker_url: CF Workers URL（如有则返回 WorkersSession 代理模式）
        ladder_proxy: 梯子代理地址（Workers 模式下用于连接被墙的 workers.dev）
    """
    if worker_url:
        # Workers 代理模式：通过 CF Workers /proxy 端点转发请求
        from ghostreg.proxy_pool import WorkersSession
        ws = WorkersSession(
            worker_url=worker_url,
            ladder_proxy=ladder_proxy,
            verify=verify,
        )
        # 设置默认 headers（与直连模式一致）
        ws.headers.update(_BROWSER_HEADERS)
        logger.info(f"[Client] Workers 代理模式: {worker_url}")
        return ws  # type: ignore  # WorkersSession 实现了兼容接口
    
    # 直连模式
    s = Session(
        impersonate="chrome136",   # curl_cffi 的 Chrome 指纹
        headers=_BROWSER_HEADERS,
        timeout=30,
        verify=verify,
    )
    return s
