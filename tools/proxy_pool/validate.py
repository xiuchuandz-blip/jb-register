"""
SOCKS5 三级验证 + /24 子网去重

输入: raw_pool.json (来自 fetch_sources.py)
输出: validated_pool.json (验证通过的代理)

三级验证:
  L1: TCP 通断 (socket.connect)            5s, 过滤 ~50%
  L2: Cloudflare 可达                       8s, 过滤 ~60%
       curl https://cloudflare.com/cdn-cgi/trace via SOCKS5
  L3: JetBrains 可达 (HTTP 200)             10s, 过滤 ~70%
       curl https://account.jetbrains.com via SOCKS5

最后按 /24 子网去重(206.123.156.x 段大量重复,只留延迟最低的一个)。

依赖: requests + pysocks (pip install requests[socks])

用法:
  python tools/proxy_pool/validate.py            # 验证 raw_pool.json
  python tools/proxy_pool/validate.py --workers 50  # 50 并发
  python tools/proxy_pool/validate.py --max 200  # 仅取前 200 个测
"""
from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("proxy_pool.validate")

# 输入输出
HERE = Path(__file__).parent
RAW_POOL_PATH = HERE / "raw_pool.json"
VALIDATED_POOL_PATH = HERE / "validated_pool.json"

# 验证目标
L2_URL = "https://www.cloudflare.com/cdn-cgi/trace"
L3_URL = "https://account.jetbrains.com/login"  # GET 200,不带账号信息

# 超时
TIMEOUT_L1 = 5
TIMEOUT_L2 = 8
TIMEOUT_L3 = 10

# 默认并发
DEFAULT_WORKERS = 50


@dataclass
class ValidatedProxy:
    """三级验证后的代理"""
    host: str
    port: int
    sources: list[str] = field(default_factory=list)
    country: str = ""
    # 验证结果
    l1_pass: bool = False
    l2_pass: bool = False
    l3_pass: bool = False
    l1_ms: int = -1
    l2_ms: int = -1
    l3_ms: int = -1
    # L2 返回的真实出口 IP(从 cloudflare trace 抽 ip=)
    exit_ip: str = ""
    # 错误
    error: str = ""

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def socks5_url(self) -> str:
        return f"socks5://{self.host}:{self.port}"

    @property
    def subnet24(self) -> str:
        parts = self.host.split(".")
        return ".".join(parts[:3]) + ".0/24"

    @property
    def passed_all(self) -> bool:
        return self.l1_pass and self.l2_pass and self.l3_pass

    @property
    def total_ms(self) -> int:
        return max(self.l1_ms, 0) + max(self.l2_ms, 0) + max(self.l3_ms, 0)


# ── L1: TCP 通断 ─────────────────────────────────────────────────────────
def test_l1(host: str, port: int, timeout: int = TIMEOUT_L1) -> tuple[bool, int]:
    """TCP socket connect"""
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, int((time.monotonic() - t0) * 1000)
    except Exception:
        return False, -1


# ── L2/L3: HTTP via SOCKS5 ───────────────────────────────────────────────
def test_http_via_socks5(
    socks5_url: str, target_url: str, timeout: int
) -> tuple[bool, int, str, str]:
    """通过 SOCKS5 代理访问 target,返回 (ok, 耗时ms, 响应文本, error)

    需要 pysocks: pip install requests[socks]
    """
    try:
        import requests as _r
    except ImportError:
        return False, -1, "", "requests not installed"

    # 使用 socks5h 让 DNS 解析也走代理。部分公开 SOCKS5 对本地解析后的
    # HTTPS 目标兼容性更差,而 429/IP 实验需要完整模拟“远端出口”。
    socks5h_url = socks5_url.replace("socks5://", "socks5h://", 1)
    proxies = {"http": socks5h_url, "https": socks5h_url}
    t0 = time.monotonic()
    try:
        r = _r.get(
            target_url,
            proxies=proxies,
            timeout=timeout,
            verify=False,  # 部分代理 MITM,跳过证书校验
            allow_redirects=False,
        )
        ms = int((time.monotonic() - t0) * 1000)
        if 200 <= r.status_code < 400:
            return True, ms, r.text[:1000], ""
        return False, ms, "", f"http {r.status_code}"
    except Exception as e:
        return False, -1, "", type(e).__name__ + ": " + str(e)[:100]


def parse_cloudflare_trace(text: str) -> str:
    """从 Cloudflare trace 响应抽 ip="""
    for line in text.splitlines():
        if line.startswith("ip="):
            return line[3:].strip()
    return ""


# ── 单代理验证 ───────────────────────────────────────────────────────────
def validate_one(p: ValidatedProxy) -> ValidatedProxy:
    """对单个代理跑 L1 → L2 → L3"""
    # L1
    p.l1_pass, p.l1_ms = test_l1(p.host, p.port)
    if not p.l1_pass:
        p.error = "L1 fail"
        return p

    # L2 Cloudflare
    ok, ms, text, err = test_http_via_socks5(p.socks5_url, L2_URL, TIMEOUT_L2)
    p.l2_pass, p.l2_ms = ok, ms
    if not ok:
        p.error = "L2 fail: " + err
        return p
    p.exit_ip = parse_cloudflare_trace(text)

    # L3 JetBrains
    ok, ms, _text, err = test_http_via_socks5(p.socks5_url, L3_URL, TIMEOUT_L3)
    p.l3_pass, p.l3_ms = ok, ms
    if not ok:
        p.error = "L3 fail: " + err
        return p

    return p


# ── 并发验证 ────────────────────────────────────────────────────────────
def validate_pool(
    proxies: list[ValidatedProxy], workers: int = DEFAULT_WORKERS
) -> list[ValidatedProxy]:
    """并发跑三级验证"""
    if not proxies:
        return []

    logger.info(f"[validate] starting: {len(proxies)} proxies, {workers} workers")
    t0 = time.monotonic()
    done: list[ValidatedProxy] = []
    last_log = t0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(validate_one, p): p for p in proxies}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                done.append(fut.result(timeout=TIMEOUT_L1 + TIMEOUT_L2 + TIMEOUT_L3 + 5))
            except Exception as e:
                p = futs[fut]
                p.error = "exec fail: " + str(e)[:100]
                done.append(p)
            if time.monotonic() - last_log > 5:
                last_log = time.monotonic()
                ok = sum(1 for x in done if x.passed_all)
                logger.info(f"[validate] progress {i}/{len(proxies)}, passed={ok}")

    elapsed = int(time.monotonic() - t0)
    ok = sum(1 for x in done if x.passed_all)
    logger.info(f"[validate] done in {elapsed}s: {ok}/{len(done)} passed all 3 levels")
    return done


# ── /24 子网去重 ────────────────────────────────────────────────────────
def dedup_by_subnet(passed: list[ValidatedProxy]) -> tuple[list[ValidatedProxy], dict]:
    """按 /24 子网去重,每个子网只留 total_ms 最小(最快)的那个

    返回 (去重后的列表, 被合并的 /24 段统计)
    """
    by_subnet: dict[str, list[ValidatedProxy]] = {}
    for p in passed:
        by_subnet.setdefault(p.subnet24, []).append(p)

    keep: list[ValidatedProxy] = []
    merged_stats: dict[str, int] = {}
    for subnet, items in by_subnet.items():
        items.sort(key=lambda x: x.total_ms)
        keep.append(items[0])
        if len(items) > 1:
            merged_stats[subnet] = len(items)

    keep.sort(key=lambda x: x.total_ms)
    return keep, merged_stats


def diverse_prefix(proxies: list[ValidatedProxy], limit: int) -> list[ValidatedProxy]:
    """按 /24 分散抽样,避免前 N 条被同一代理集群污染。

    公开源里 206.123.156.x 这种段可能有几百条,如果直接取前 N 条,
    很容易得到“同一服务商的一组坏代理”,从而误判整个池质量。
    这里先每个 /24 取 1 条,不够再补第二轮。
    """
    if limit <= 0 or len(proxies) <= limit:
        return proxies

    by_subnet: dict[str, list[ValidatedProxy]] = {}
    for p in proxies:
        by_subnet.setdefault(p.subnet24, []).append(p)

    selected: list[ValidatedProxy] = []
    round_idx = 0
    while len(selected) < limit:
        added = 0
        for subnet in sorted(by_subnet):
            items = by_subnet[subnet]
            if round_idx < len(items):
                selected.append(items[round_idx])
                added += 1
                if len(selected) >= limit:
                    break
        if added == 0:
            break
        round_idx += 1
    return selected


# ── 加载/保存 ────────────────────────────────────────────────────────────
def load_raw_pool() -> list[ValidatedProxy]:
    if not RAW_POOL_PATH.exists():
        raise FileNotFoundError(
            f"{RAW_POOL_PATH} 不存在。先跑 fetch_sources.py"
        )
    data = json.loads(RAW_POOL_PATH.read_text(encoding="utf-8"))
    out = []
    for raw in data.get("proxies", []):
        out.append(
            ValidatedProxy(
                host=raw["host"],
                port=int(raw["port"]),
                sources=raw.get("sources", []),
                country=raw.get("country", ""),
            )
        )
    return out


def save_validated(passed: list[ValidatedProxy], stats: dict, failures: list[ValidatedProxy] | None = None):
    snapshot = {
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_pass": len(passed),
        "subnet_dedup_stats": stats,
        "proxies": [asdict(p) for p in passed],
        "sample_failures": [asdict(p) for p in (failures or [])[:50]],
    }
    VALIDATED_POOL_PATH.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(f"[validate] written -> {VALIDATED_POOL_PATH}")


# ── CLI ─────────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # 抑制 urllib3 SSL warnings(代理可能 MITM)
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="并发数 (默认 50)")
    ap.add_argument("--max", type=int, default=0,
                    help="最多验证多少条 (默认全部,用于快测取 50-200)")
    ap.add_argument("--high-conf-only", action="store_true",
                    help="只验证命中 ≥2 源的高置信代理")
    ap.add_argument("--source", action="append",
                    help="只验证指定源(可多次),如 --source monosans --source vakhov")
    args = ap.parse_args()

    pool = load_raw_pool()
    print(f"\n=== 加载 {len(pool)} 条原始代理 ===")

    if args.high_conf_only:
        pool = [p for p in pool if len(p.sources) >= 2]
        print(f"高置信过滤后: {len(pool)} 条")

    if args.source:
        wanted = set(args.source)
        pool = [p for p in pool if wanted.intersection(p.sources)]
        print(f"源过滤 {sorted(wanted)} 后: {len(pool)} 条")

    if args.max and args.max < len(pool):
        pool = diverse_prefix(pool, args.max)
        print(f"按 /24 分散抽样到 {args.max} 条快测")

    print(f"\n=== 开始三级验证 ({args.workers} 并发) ===")
    done = validate_pool(pool, workers=args.workers)

    passed = [p for p in done if p.passed_all]
    print(f"\n=== L1+L2+L3 全过 ===")
    print(f"通过: {len(passed)}/{len(done)} ({100*len(passed)//max(len(done),1)}%)")

    # 阶段性失败统计
    l1_fail = sum(1 for p in done if not p.l1_pass)
    l2_fail = sum(1 for p in done if p.l1_pass and not p.l2_pass)
    l3_fail = sum(1 for p in done if p.l2_pass and not p.l3_pass)
    print(f"  L1 fail: {l1_fail}")
    print(f"  L2 fail: {l2_fail} (L1 过但 Cloudflare 不通)")
    print(f"  L3 fail: {l3_fail} (Cloudflare 过但 JB 不通)")

    # /24 去重
    keep, dedup_stats = dedup_by_subnet(passed)
    print(f"\n=== /24 子网去重 ===")
    print(f"独立 IP: {len(keep)} (从 {len(passed)} 条)")
    if dedup_stats:
        print(f"合并的高密集段(取最快保留 1 个,丢弃其余):")
        for sn, cnt in sorted(dedup_stats.items(), key=lambda x: -x[1])[:10]:
            print(f"  {sn}: {cnt} 条 → 1")

    # 出口 IP 统计
    exit_ips = [p.exit_ip for p in keep if p.exit_ip]
    unique_exit_ips = len(set(exit_ips))
    print(f"\n=== 真实出口 IP ===")
    print(f"L2 抽到的不同出口 IP: {unique_exit_ips}/{len(keep)}")
    if unique_exit_ips < len(keep):
        print(f"  (有 {len(keep) - unique_exit_ips} 个代理共享出口 IP,可能是同集群)")

    # 国别 Top
    by_country = {}
    for p in keep:
        c = p.country or "??"
        by_country[c] = by_country.get(c, 0) + 1
    print(f"\n国别 Top10: {dict(sorted(by_country.items(), key=lambda x: -x[1])[:10])}")

    # 写盘
    failures = [p for p in done if not p.passed_all]
    save_validated(keep, dedup_stats, failures=failures)
    print(f"\n→ 写入 {VALIDATED_POOL_PATH}")
    print(f"→ 池中前 5 个最快代理:")
    for p in keep[:5]:
        print(
            f"  {p.addr:25s} country={p.country or '??'} "
            f"L1={p.l1_ms}ms L2={p.l2_ms}ms L3={p.l3_ms}ms "
            f"exit={p.exit_ip} src={p.sources}"
        )


if __name__ == "__main__":
    main()
