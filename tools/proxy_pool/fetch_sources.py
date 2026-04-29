"""
SOCKS5 公开源拉取 + 合并去重

源:
  - monosans/proxy-list (~250, 每 15 分钟)
  - Vakhov/fresh-proxy-list (~150, 每 5-20 分钟)
  - TheSpeedX/PROXY-List (~6600, 每 60 分钟)
  - zloi-user/hideip.me (~500, 每 60 分钟)

输出:
  - 内存返回 list[dict]
  - 写盘 raw_pool.json (源标签 + IP:Port 列表)

用法:
  python tools/proxy_pool/fetch_sources.py
  python -c "from tools.proxy_pool.fetch_sources import fetch_all; print(len(fetch_all()))"
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests

logger = logging.getLogger("proxy_pool.fetch")

# ── 4 个公开源 ─────────────────────────────────────────────────────────────
SOURCES = [
    {
        "name": "monosans",
        "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
        "fmt": "host:port",
        "weight": 5,  # 高质量,带本地验证
    },
    {
        "name": "vakhov",
        "url": "https://raw.githubusercontent.com/Vakhov/fresh-proxy-list/master/socks5.txt",
        "fmt": "host:port",
        "weight": 4,  # 更新最频繁
    },
    {
        "name": "thespeedx",
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "fmt": "host:port",
        "weight": 2,  # 量大噪声多
    },
    {
        "name": "zloi",
        "url": "https://raw.githubusercontent.com/zloi-user/hideip.me/master/socks5.txt",
        "fmt": "host:port_country",  # 形如 1.2.3.4:1080_CN
        "weight": 3,
    },
]

# 默认梯子代理(可通过 PROXY_LADDER 环境变量覆盖)
DEFAULT_LADDER = os.environ.get("PROXY_LADDER", "http://127.0.0.1:7890")

# 输出文件
OUTPUT_DIR = Path(__file__).parent
RAW_POOL_PATH = OUTPUT_DIR / "raw_pool.json"

# IP:Port 正则
RE_HOST_PORT = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})")


# ── 数据结构 ───────────────────────────────────────────────────────────────
@dataclass
class RawProxy:
    """合并去重后的原始代理条目"""
    host: str
    port: int
    sources: list[str] = field(default_factory=list)  # 命中的源标签
    country: str = ""  # zloi 源带,其他没有

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def subnet24(self) -> str:
        """/24 子网 key,用于后续去重"""
        parts = self.host.split(".")
        return ".".join(parts[:3]) + ".0/24"


# ── 单源拉取 ───────────────────────────────────────────────────────────────
def fetch_one_source(source: dict, ladder: str = "", timeout: int = 30) -> list[RawProxy]:
    """从单个源拉取并解析"""
    name = source["name"]
    url = source["url"]
    fmt = source["fmt"]

    proxies = {}
    if ladder:
        proxies = {"http": ladder, "https": ladder}

    try:
        r = requests.get(url, proxies=proxies, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"[{name}] fetch failed: {e}")
        return []

    out: list[RawProxy] = []
    for line in r.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = RE_HOST_PORT.match(line)
        if not m:
            continue
        host, port = m.group(1), int(m.group(2))
        if not (1 <= port <= 65535):
            continue
        # zloi 形如 1.2.3.4:1080_CN
        country = ""
        if fmt == "host:port_country":
            tail = line[m.end():]
            tm = re.match(r"_([A-Z]{2})", tail)
            if tm:
                country = tm.group(1)
        out.append(RawProxy(host=host, port=port, sources=[name], country=country))

    logger.info(f"[{name}] fetched {len(out)} entries")
    return out


# ── 全量拉取 + 合并去重 ──────────────────────────────────────────────────
def fetch_all(ladder: str = DEFAULT_LADDER, save: bool = True) -> list[RawProxy]:
    """从所有源拉取,按 host:port 合并去重"""
    merged: dict[str, RawProxy] = {}
    stats = {}

    for src in SOURCES:
        items = fetch_one_source(src, ladder=ladder)
        stats[src["name"]] = len(items)
        for p in items:
            key = p.addr
            if key in merged:
                # 合并源标签 + 国别
                if src["name"] not in merged[key].sources:
                    merged[key].sources.append(src["name"])
                if p.country and not merged[key].country:
                    merged[key].country = p.country
            else:
                merged[key] = p

    pool = list(merged.values())
    # 排序:命中源数多的优先
    pool.sort(key=lambda x: (-len(x.sources), x.host, x.port))

    logger.info(
        f"[fetch_all] sources={stats}, merged={len(pool)} unique IP:Port"
    )

    if save:
        snapshot = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ladder": ladder if ladder else "direct",
            "stats": stats,
            "total": len(pool),
            "proxies": [asdict(p) for p in pool],
        }
        RAW_POOL_PATH.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(f"[fetch_all] snapshot written -> {RAW_POOL_PATH}")

    return pool


# ── /24 子网维度统计(给 validate.py 用) ─────────────────────────────────
def subnet_stats(pool: list[RawProxy]) -> dict[str, int]:
    """按 /24 子网统计条目数,识别集中的服务集群(如 206.123.156.x)"""
    counter: dict[str, int] = {}
    for p in pool:
        counter[p.subnet24] = counter.get(p.subnet24, 0) + 1
    # 高于 5 条的 /24 就值得警惕
    suspicious = {k: v for k, v in counter.items() if v >= 5}
    return suspicious


# ── CLI ─────────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    ladder = DEFAULT_LADDER
    if len(sys.argv) > 1:
        ladder = sys.argv[1]
        if ladder.lower() in ("direct", "none", ""):
            ladder = ""

    print(f"梯子代理: {ladder if ladder else '直连'}")
    pool = fetch_all(ladder=ladder)

    print(f"\n=== 合并结果 ===")
    print(f"总条目: {len(pool)}")

    # 国别统计
    by_country = {}
    for p in pool:
        c = p.country or "??"
        by_country[c] = by_country.get(c, 0) + 1
    if any(c != "??" for c in by_country):
        top = sorted(by_country.items(), key=lambda x: -x[1])[:10]
        print(f"国别 Top10: {dict(top)}")

    # /24 集中段
    sus = subnet_stats(pool)
    if sus:
        print(f"\n=== /24 集中段(≥5 条,需去重) ===")
        for sn, cnt in sorted(sus.items(), key=lambda x: -x[1])[:10]:
            print(f"  {sn}: {cnt} 条")

    # 命中多源的高置信
    high_conf = [p for p in pool if len(p.sources) >= 2]
    print(f"\n命中 ≥2 源: {len(high_conf)} 条")

    print(f"\n快照已写: {RAW_POOL_PATH}")


if __name__ == "__main__":
    main()
