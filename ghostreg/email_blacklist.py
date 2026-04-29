# -*- coding: utf-8 -*-
r"""
YYDS Mail 域名黑名单管理。

策略：
- 同域名累计 2 次邮件超时 → 加入黑名单
- 黑名单冷却 24 小时后自动恢复（避免误杀）
- 持久化到 D:\GhostRegV2\data\email_blacklist.json
"""
import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger("ghostreg.email_blacklist")

# 配置
BLACKLIST_FILE = Path(__file__).parent.parent / "data" / "email_blacklist.json"
BLACKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)

FAILURE_THRESHOLD = 2          # 累计 N 次失败进黑名单
COOLDOWN_SECONDS = 24 * 3600   # 黑名单冷却时长（24 小时）

# TLD 整段黑名单（永久生效，不参与冷却机制）
# 当前为手动维护；未来可考虑实现「同一 TLD N 个不同子域名失败自动加入」
_BLACKLISTED_TLDS = {".edu.kg"}  # 启动默认黑名单 TLD（基于 2026-04-25 实测，7/7 子域名失败）

# 数据结构: { domain: {"failures": int, "last_failure": float, "blacklisted_at": float|None} }
_data: dict[str, dict] = {}
_lock = threading.RLock()
_loaded = False


def _load():
    """从磁盘加载黑名单"""
    global _data, _loaded
    if _loaded:
        return
    try:
        if BLACKLIST_FILE.exists():
            _data = json.loads(BLACKLIST_FILE.read_text(encoding="utf-8"))
            logger.info(f"邮箱黑名单加载: {len(_data)} 域名记录")
    except Exception as e:
        logger.warning(f"加载黑名单失败（忽略）: {e}")
        _data = {}
    _loaded = True


def _save():
    """保存到磁盘（线程安全调用方需持锁）"""
    try:
        BLACKLIST_FILE.write_text(
            json.dumps(_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"保存黑名单失败: {e}")


def _matches_blacklisted_tld(domain: str) -> bool:
    """检查域名是否命中 TLD 整段黑名单"""
    if not domain:
        return False
    d = domain.lower()
    for tld in _BLACKLISTED_TLDS:
        if d.endswith(tld):
            return True
    return False


def is_blacklisted(domain: str) -> bool:
    """检查域名是否在黑名单（且仍在冷却期）"""
    # TLD 整段黑名单
    if _matches_blacklisted_tld(domain):
        return True
    with _lock:
        _load()
        rec = _data.get(domain)
        if not rec:
            return False
        bl_at = rec.get("blacklisted_at")
        if not bl_at:
            return False
        # 检查冷却
        if time.time() - bl_at > COOLDOWN_SECONDS:
            # 冷却结束，自动恢复（清空失败计数）
            logger.info(f"域名 {domain} 黑名单已过期，自动恢复")
            _data.pop(domain, None)
            _save()
            return False
        return True


def report_failure(domain: str, reason: str = "邮件超时"):
    """报告域名失败一次。累计达阈值则加入黑名单。"""
    if not domain:
        return
    with _lock:
        _load()
        rec = _data.setdefault(domain, {"failures": 0, "last_failure": 0, "blacklisted_at": None})
        rec["failures"] += 1
        rec["last_failure"] = time.time()
        rec["last_reason"] = reason
        if rec["failures"] >= FAILURE_THRESHOLD and not rec.get("blacklisted_at"):
            rec["blacklisted_at"] = time.time()
            logger.warning(
                f"⚫ 域名 {domain} 已加入黑名单 (累计 {rec['failures']} 次失败, 原因: {reason})"
            )
        else:
            logger.info(
                f"域名 {domain} 失败累计 {rec['failures']}/{FAILURE_THRESHOLD} 次"
            )
        _save()


def report_success(domain: str):
    """域名成功使用一次。清空失败计数（如果之前失败过但未拉黑）"""
    if not domain:
        return
    with _lock:
        _load()
        rec = _data.get(domain)
        if rec and not rec.get("blacklisted_at") and rec.get("failures", 0) > 0:
            logger.info(f"域名 {domain} 注册成功，清空失败计数（之前 {rec['failures']} 次）")
            _data.pop(domain, None)
            _save()


def filter_domains(domains: list[str]) -> list[str]:
    """从域名列表中剔除黑名单域名（含 TLD 整段拉黑）。返回可用域名。"""
    with _lock:
        _load()
        # 先按 TLD 整段过滤（无锁开销，最高效）
        result = [d for d in domains if not _matches_blacklisted_tld(d)]
        tld_removed = len(domains) - len(result)
        if tld_removed > 0:
            logger.info(f"TLD 整段过滤掉 {tld_removed} 个域名 (TLD: {_BLACKLISTED_TLDS})")

        if not _data:
            return result

        # 再按精确域名过滤
        now = time.time()
        active_blacklist = set()
        expired = []
        for d, rec in _data.items():
            bl_at = rec.get("blacklisted_at")
            if bl_at:
                if now - bl_at > COOLDOWN_SECONDS:
                    expired.append(d)
                else:
                    active_blacklist.add(d)
        # 清理过期
        if expired:
            for d in expired:
                _data.pop(d, None)
            _save()
            logger.info(f"清理过期黑名单: {expired}")
        # 过滤
        before = len(result)
        result = [d for d in result if d not in active_blacklist]
        domain_removed = before - len(result)
        if domain_removed > 0:
            logger.info(f"精确域名过滤掉 {domain_removed} 个")
        return result


def get_status() -> dict:
    """返回当前黑名单状态供 UI 展示"""
    with _lock:
        _load()
        now = time.time()
        active = []
        watching = []
        for d, rec in _data.items():
            bl_at = rec.get("blacklisted_at")
            if bl_at:
                remaining = COOLDOWN_SECONDS - (now - bl_at)
                if remaining > 0:
                    active.append({
                        "domain": d,
                        "failures": rec.get("failures", 0),
                        "blacklisted_hours_left": round(remaining / 3600, 1),
                        "last_reason": rec.get("last_reason", ""),
                    })
            else:
                watching.append({
                    "domain": d,
                    "failures": rec.get("failures", 0),
                })
        return {
            "blacklisted": active,
            "watching": watching,
            "threshold": FAILURE_THRESHOLD,
            "cooldown_hours": COOLDOWN_SECONDS // 3600,
        }
