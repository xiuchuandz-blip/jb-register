"""
批量并发注册调度器 — asyncio + ThreadPoolExecutor
"""
import csv
import json
import logging
import os
import time
import random
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from threading import Lock

from protocol_reg.client import new_session
from protocol_reg.jb_api import JBRegisterer, JBRegError, TencentCaptchaError, BASE_URL, RECAPTCHA_SITEKEY

# 复用 ghostreg 的邮箱和打码服务
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from ghostreg import email_service, captcha_service

logger = logging.getLogger("protocol_reg.worker")

# ── 输出目录 ──
OUTPUT_DIR = Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
ACCOUNTS_JSON = OUTPUT_DIR / "protocol_accounts.json"
ACCOUNTS_CSV = OUTPUT_DIR / "protocol_accounts.csv"

_file_lock = Lock()


@dataclass
class RegResult:
    """单个注册结果"""
    task_id: int
    email: str = ""
    password: str = ""
    success: bool = False
    error: str = ""
    elapsed: float = 0.0
    timestamp: str = ""


def _random_name(length: int = 5) -> str:
    """生成随机英文名"""
    first = random.choice(string.ascii_uppercase)
    rest = ''.join(random.choices(string.ascii_lowercase, k=length - 1))
    return first + rest


def _save_result(result: RegResult):
    """线程安全地保存成功的注册结果"""
    if not result.success:
        return
    with _file_lock:
        # JSON
        records = []
        if ACCOUNTS_JSON.exists():
            try:
                records = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8"))
            except Exception:
                pass
        records.append(asdict(result))
        ACCOUNTS_JSON.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")

        # CSV
        write_header = not ACCOUNTS_CSV.exists()
        with open(ACCOUNTS_CSV, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["email", "password", "timestamp"])
            w.writerow([result.email, result.password, result.timestamp])


MAX_TENCENT_RETRIES = 3  # 遇到腾讯验证码时最多重试次数


def register_single(
    task_id: int,
    password: str,
    first_name: str | None = None,
    last_name: str | None = None,
    worker_url: str = "",
    ladder_proxy: str = "",
) -> RegResult:
    """
    执行单个注册任务（完整流程），阻塞直到完成。
    遇到腾讯验证码自动重开（最多 MAX_TENCENT_RETRIES 次）。
    """
    result = RegResult(task_id=task_id)
    start = time.time()

    fname = first_name or _random_name(random.randint(4, 7))
    lname = last_name or _random_name(random.randint(4, 7))

    for tencent_attempt in range(MAX_TENCENT_RETRIES + 1):
        email = None
        try:
            # Step 0: 申请邮箱
            logger.info(f"[Task {task_id}] 申请临时邮箱...")
            email = email_service.apply_email()
            if not email:
                raise JBRegError("邮箱申请失败")
            result.email = email
            result.password = password
            logger.info(f"[Task {task_id}] 邮箱: {email}")

            # 创建 HTTP Session（默认 verify=False 避免 TUN SSL 问题）
            session = new_session(
                verify=False,
                worker_url=worker_url,
                ladder_proxy=ladder_proxy,
            )
            reg = JBRegisterer(session)

            # Step 1-3: 初始化
            reg._init_session()
            reg._create_auth_session()
            reg._submit_email_first(email)

            # Step 4: 打码平台
            logger.info(f"[Task {task_id}] 调用打码平台...")
            page_url = f"{BASE_URL}/signup?authSessionId={reg.session_id}"
            token = captcha_service.solve_recaptcha_v2(page_url, RECAPTCHA_SITEKEY)
            logger.info(f"[Task {task_id}] Token: {len(token)} chars")

            reg._verify_captcha(token)

            # Step 5: 发验证码
            reg._submit_email_retry(email)

            # Step 6: 等 OTP
            logger.info(f"[Task {task_id}] 等待 OTP...")
            otp = email_service.poll_verification_code(email, timeout=120, interval=3)
            if not otp:
                raise JBRegError("OTP 超时")
            logger.info(f"[Task {task_id}] OTP: {otp}")

            reg._submit_otp(otp)

            # Step 7-8: 密码 + 注册
            reg._submit_password(password, fname, lname, email)
            reg._complete_signup(password, fname, lname, email)

            result.success = True
            result.elapsed = time.time() - start
            result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"[Task {task_id}] ✓ 注册成功! {email} ({result.elapsed:.1f}s)")
            break  # 成功，退出重试循环

        except TencentCaptchaError:
            # 腾讯验证码 — 清理当前邮箱，重新开始
            logger.warning(f"[Task {task_id}] 遇到腾讯验证码，跳过重开 ({tencent_attempt+1}/{MAX_TENCENT_RETRIES})")
            if email:
                try:
                    email_service.delete_email(email)
                except Exception:
                    pass
            if tencent_attempt >= MAX_TENCENT_RETRIES:
                result.error = "连续遇到腾讯验证码，放弃"
                result.elapsed = time.time() - start
                result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.error(f"[Task {task_id}] ✗ {result.error}")
            else:
                time.sleep(2)  # 短暂等待再重试
                continue

        except Exception as e:
            result.error = str(e)
            result.elapsed = time.time() - start
            result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.error(f"[Task {task_id}] ✗ 失败: {e} ({result.elapsed:.1f}s)")
            break  # 非腾讯验证码错误，不重试

        finally:
            # 清理邮箱（成功/失败都清理，但腾讯重试时已提前清理）
            if email and result.success or (result.error and "腾讯" not in result.error):
                try:
                    email_service.delete_email(email)
                except Exception:
                    pass

    # 保存结果
    _save_result(result)
    return result


def batch_register(
    count: int,
    password: str,
    max_workers: int = 3,
    first_name: str | None = None,
    last_name: str | None = None,
    stagger: float = 5,
    proxy_pool=None,
    ladder_proxy: str = "",
) -> list[RegResult]:
    """
    批量并发注册（带错峰启动）。
    - 每 STAGGER_INTERVAL 秒启动一个新任务
    - 最多同时 max_workers 个任务在跑
    - 任务内部遇到 429 会自动退避重试
    
    Args:
        proxy_pool: ProxyPool 实例（可选）。有代理池时，
                    按 IP 分组调度（每 5 个任务共用 1 个 Workers IP）
        ladder_proxy: 梯子代理地址（Workers 模式下需要）
    """
    has_proxy = proxy_pool is not None and proxy_pool.available
    
    if has_proxy:
        # 代理池模式：按组分配 Workers，每组 5 并发
        GROUP_SIZE = 5  # 每个 IP 承载的并发数（8轮压测验证：5并发/IP 100%成功）
        total_groups = (count + GROUP_SIZE - 1) // GROUP_SIZE
        effective_workers = min(max_workers, count)
        logger.info(
            f"批量注册: {count} 个, 代理池: {proxy_pool.count} Workers, "
            f"分 {total_groups} 组, 并发: {effective_workers}, 错峰: {stagger}s"
        )
    else:
        logger.info(f"批量注册: {count} 个, 并发: {max_workers}, 错峰: {stagger}s (直连模式)")

    results: list[RegResult] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i in range(1, count + 1):
            fname = first_name or _random_name(random.randint(4, 7))
            lname = last_name or _random_name(random.randint(4, 7))
            if i > 1 and first_name:
                fname = first_name + chr(96 + i)

            # 为任务分配 Workers URL（按组：task 1-5 → group 0, task 6-10 → group 1, ...）
            worker_url = ""
            if has_proxy:
                group_id = (i - 1) // GROUP_SIZE  # 每 GROUP_SIZE 个任务一组
                worker_url = proxy_pool.get_worker_url(group_id) or ""

            f = pool.submit(
                register_single, i, password, fname, lname,
                worker_url=worker_url, ladder_proxy=ladder_proxy,
            )
            futures[f] = i

            # 错峰启动
            if i < count and stagger > 0:
                time.sleep(stagger)

        # 等待全部完成（超时 = 每个任务最多 5 分钟）
        timeout = max(600, count * 120)
        for f in as_completed(futures, timeout=timeout):
            task_id = futures[f]
            try:
                result = f.result()
                results.append(result)
            except Exception as e:
                logger.error(f"[Task {task_id}] 线程异常: {e}")
                results.append(RegResult(task_id=task_id, error=str(e)))

    # 汇总
    success = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    logger.info(f"\n{'='*50}")
    logger.info(f"批量注册完成: 成功 {success}/{count}, 失败 {failed}")
    for r in sorted(results, key=lambda x: x.task_id):
        status = "✓" if r.success else "✗"
        logger.info(f"  [{status}] Task {r.task_id}: {r.email or 'N/A'} ({r.elapsed:.1f}s) {r.error}")
    logger.info(f"{'='*50}")

    return results
