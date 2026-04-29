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
from typing import Callable

from protocol_reg.client import new_session
from protocol_reg.jb_api import (
    JBRegisterer, JBRegError, TencentCaptchaError, SessionsBucketHitError,
    CaptchaTokenInvalidError,
    BASE_URL, RECAPTCHA_SITEKEY,
)
from protocol_reg.pool_client import PoolClient, Route

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


MAX_TENCENT_RETRIES = 3       # 遇到腾讯验证码时最多重试次数
MAX_SESSION_BUCKET_RETRIES = 5  # sessions IP 桶 429 撞墙时最多换 IP 重试次数


def _acquire_route_with_wait(
    pool_client: PoolClient | None,
    task_id: int,
    max_wait_s: int = 60,
) -> Route | None:
    """
    从 A 池领路由。池子空时等待补货(最多 max_wait_s 秒)。
    
    Returns:
        Route 对象 → 双层代理模式
        None → A 池没在跑 / 等待超时 → 调用方回退到单层模式
    """
    if pool_client is None:
        return None
    
    if not pool_client.is_alive():
        logger.info(f"[Task {task_id}] A 池服务未运行,回退单层 Worker 模式")
        return None
    
    # 短轮询:每 5 秒问一次,最多 max_wait_s 秒
    deadline = time.monotonic() + max_wait_s
    waited = 0
    while True:
        route = pool_client.acquire_route()
        if route:
            if waited > 0:
                logger.info(f"[Task {task_id}] 等了 {waited}s 拿到双层路由: {route}")
            else:
                logger.info(f"[Task {task_id}] 拿到双层路由: {route}")
            return route
        
        # 池子空,看要不要等
        if time.monotonic() >= deadline:
            logger.warning(f"[Task {task_id}] A 池等待 {max_wait_s}s 仍无可用路由,回退单层 Worker 模式")
            return None
        
        # 第一次空时打个日志,后面就不刷屏了
        if waited == 0:
            status = pool_client.status() or {}
            alive = status.get("alive", 0)
            logger.info(
                f"[Task {task_id}] A 池暂时没货 (alive={alive}),等待补货中(最多 {max_wait_s}s)..."
            )
        time.sleep(5)
        waited += 5


def register_single(
    task_id: int,
    password: str,
    first_name: str | None = None,
    last_name: str | None = None,
    worker_url: str = "",
    ladder_proxy: str = "",
    pool_client: PoolClient | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RegResult:
    """
    执行单个注册任务(完整流程),阻塞直到完成。
    
    Args:
        worker_url: 回退用的单层 Worker URL(A 池没货时用)
        ladder_proxy: 单层 Worker 模式下的本地梯子地址
        pool_client: A 池 HTTP 客户端。提供时优先双层代理(SOCKS5+Worker)
    
    重试策略:
    - 腾讯验证码 → 整任务重试(换邮箱),最多 MAX_TENCENT_RETRIES 次
    - sessions IP 桶 429 → 报告坏路由 + 换 IP 重试,最多 MAX_SESSION_BUCKET_RETRIES 次
        * Step2 撞:还没打码,重试无打码积分浪费
        * Step5 撞:打码已用,重试需重新打码(花积分),日志会标注
    - 其他异常 → 不重试,任务失败
    """
    result = RegResult(task_id=task_id)
    start = time.time()

    fname = first_name or _random_name(random.randint(4, 7))
    lname = last_name or _random_name(random.randint(4, 7))

    def _check_stop(stage: str):
        if should_stop and should_stop():
            raise JBRegError(f"用户已停止任务({stage})")

    for tencent_attempt in range(MAX_TENCENT_RETRIES + 1):
        email = None
        # 邮箱在腾讯重试间不换;sessions 重试期间也共用同一邮箱
        try:
            email = email_service.apply_email()
            if not email:
                raise JBRegError("邮箱申请失败")
            result.email = email
            result.password = password
            logger.info(f"[Task {task_id}] 邮箱: {email}")
        except Exception as e:
            result.error = f"申请邮箱失败: {e}"
            result.elapsed = time.time() - start
            result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.error(f"[Task {task_id}] [FAIL] {result.error}")
            break
        
        # 内层循环:sessions IP 桶 429 时换 IP 重试
        bucket_outcome: str = ""  # "success" / "tencent" / "fail" / "give_up_bucket"
        last_route: Route | None = None
        
        for bucket_attempt in range(MAX_SESSION_BUCKET_RETRIES + 1):
            try:
                # ==== 领路由(双层代理优先,A 池空回退单层) ====
                route = _acquire_route_with_wait(pool_client, task_id, max_wait_s=60)
                last_route = route
                
                if route:
                    # 双层代理:SOCKS5 → Worker → JB
                    session = new_session(
                        verify=False,
                        worker_url=route.worker,
                        socks5_proxy=route.entry_proxy,
                    )
                else:
                    # 单层回退:本机梯子 → Worker → JB(老逻辑)
                    session = new_session(
                        verify=False,
                        worker_url=worker_url,
                        ladder_proxy=ladder_proxy,
                    )
                reg = JBRegisterer(session)

                _check_stop("开始前")

                # ==== Step 1-3: 初始化 + 首次提交邮箱(触发 captcha) ====
                reg._init_session()
                _check_stop("Step1后")
                reg._create_auth_session()  # 撞 429 → SessionsBucketHitError
                _check_stop("Step2后")
                reg._submit_email_first(email)
                _check_stop("打码前")

                # ==== Step 4: 打码 ====
                if bucket_attempt > 0:
                    logger.warning(
                        f"[Task {task_id}] [bucket-retry {bucket_attempt}] 重新打码(本次会消耗打码积分)"
                    )
                logger.info(f"[Task {task_id}] 调用打码平台...")
                page_url = f"{BASE_URL}/signup?authSessionId={reg.session_id}"
                token = captcha_service.solve_recaptcha_v2(page_url, RECAPTCHA_SITEKEY)
                logger.info(f"[Task {task_id}] Token: {len(token)} chars")

                reg._verify_captcha(token)
                _check_stop("打码后")

                # ==== Step 5: 发 OTP(撞 429 → SessionsBucketHitError) ====
                reg._submit_email_retry(email)
                _check_stop("发OTP后")

                # ==== Step 6: 等 OTP ====
                logger.info(f"[Task {task_id}] 等待 OTP...")
                otp = email_service.poll_verification_code(email, timeout=120, interval=3)
                if not otp:
                    raise JBRegError("OTP 超时")
                logger.info(f"[Task {task_id}] OTP: {otp}")

                reg._submit_otp(otp)

                # ==== Step 7-8: 密码 + 完成注册 ====
                reg._submit_password(password, fname, lname, email)
                reg._complete_signup(password, fname, lname, email)

                # 成功
                result.success = True
                result.elapsed = time.time() - start
                result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"[Task {task_id}] [OK] 注册成功! {email} ({result.elapsed:.1f}s)")
                try:
                    from ghostreg import email_blacklist
                    if email and "@" in email:
                        email_blacklist.report_success(email.split("@", 1)[1])
                except Exception:
                    pass
                bucket_outcome = "success"
                break

            except SessionsBucketHitError as e:
                # sessions IP 桶 429 / SOCKS5 死 → 报告坏路由 + 换 IP 重试
                if last_route and pool_client:
                    pool_client.report_bad(
                        last_route.entry_proxy,
                        reason="rate_limited",
                        cooldown_seconds=60,  # sessions 桶 >45s 恢复,给 60s 安全
                    )
                if bucket_attempt >= MAX_SESSION_BUCKET_RETRIES:
                    logger.error(
                        f"[Task {task_id}] [FAIL] sessions IP 桶 {e.step} 撞了 "
                        f"{MAX_SESSION_BUCKET_RETRIES + 1} 个不同 IP,放弃"
                    )
                    result.error = f"sessions IP 桶 {e.step} 连续 {MAX_SESSION_BUCKET_RETRIES + 1} 次失败"
                    result.elapsed = time.time() - start
                    result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    bucket_outcome = "give_up_bucket"
                    break
                logger.warning(
                    f"[Task {task_id}] [{e.step}] 撞 IP 桶,换路由重试 "
                    f"({bucket_attempt + 1}/{MAX_SESSION_BUCKET_RETRIES})"
                )
                time.sleep(1)
                continue

            except CaptchaTokenInvalidError as e:
                # 打码平台偶发吐错 token,IP 是好的,只重新打码即可
                # 走和 sessions 桶一样的 fail-over 循环(最简实现:全流程重来)
                # 不报告坏路由(IP 是无辜的),也不消耗 cooldown
                if bucket_attempt >= MAX_SESSION_BUCKET_RETRIES:
                    logger.error(
                        f"[Task {task_id}] [FAIL] 打码 token 连续被拒 "
                        f"{MAX_SESSION_BUCKET_RETRIES + 1} 次,放弃"
                    )
                    result.error = f"打码 token 连续被拒: {e}"
                    result.elapsed = time.time() - start
                    result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    bucket_outcome = "give_up_bucket"
                    break
                logger.warning(
                    f"[Task {task_id}] 打码 token 被 JB 拒,重新打码 "
                    f"({bucket_attempt + 1}/{MAX_SESSION_BUCKET_RETRIES})"
                )
                time.sleep(1)
                continue

            except TencentCaptchaError:
                logger.warning(
                    f"[Task {task_id}] 遇到腾讯验证码 ({tencent_attempt+1}/{MAX_TENCENT_RETRIES})"
                )
                bucket_outcome = "tencent"
                break

            except Exception as e:
                result.error = str(e)
                result.elapsed = time.time() - start
                result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.error(f"[Task {task_id}] [FAIL] 失败: {e} ({result.elapsed:.1f}s)")
                # 域名失败上报
                err_str = str(e)
                should_report = False
                reason = ""
                if "等待验证码超时" in err_str or "TimeoutError" in type(e).__name__:
                    should_report = True
                    reason = "邮件超时(120s)"
                elif "Step5-email" in err_str and "429" in err_str:
                    should_report = True
                    reason = "Step5邮件提交-429"
                if should_report and email and "@" in email:
                    try:
                        from ghostreg import email_blacklist
                        domain = email.split("@", 1)[1]
                        email_blacklist.report_failure(domain, reason=reason)
                    except Exception as ex:
                        logger.warning(f"报告域名失败时异常(忽略): {ex}")
                bucket_outcome = "fail"
                break

        # 内层循环退出后处理
        if bucket_outcome == "success" or bucket_outcome == "fail" or bucket_outcome == "give_up_bucket":
            # 清理邮箱
            if email:
                try:
                    email_service.delete_email(email)
                except Exception:
                    pass
            break  # 退出腾讯重试循环
        
        if bucket_outcome == "tencent":
            # 清理当前邮箱,重新开始(包括重申请邮箱)
            if email:
                try:
                    email_service.delete_email(email)
                except Exception:
                    pass
            if tencent_attempt >= MAX_TENCENT_RETRIES:
                result.error = "连续遇到腾讯验证码,放弃"
                result.elapsed = time.time() - start
                result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.error(f"[Task {task_id}] [FAIL] {result.error}")
                break
            time.sleep(2)
            continue

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
    pool_client: PoolClient | None = None,
) -> list[RegResult]:
    """
    批量并发注册(带错峰启动)。
    
    Args:
        proxy_pool: ghostreg.ProxyPool 实例(可选,单层 Worker 模式)
        ladder_proxy: 梯子代理地址(单层模式下需要,用于连 workers.dev)
        pool_client: A 池 HTTP 客户端(可选,双层代理模式)
            - 提供时优先双层 SOCKS5+Worker 链路(成功率最高)
            - A 池没货时自动回退单层(用 proxy_pool 的 Worker)
    """
    has_dual = pool_client is not None
    has_proxy = proxy_pool is not None and proxy_pool.available
    
    if has_dual:
        logger.info(
            f"批量注册: {count} 个, 并发: {max_workers}, 错峰: {stagger}s "
            f"[双层代理: A 池 SOCKS5 + Worker]"
        )
    elif has_proxy:
        # 代理池模式:按组分配 Workers,每组 5 并发
        GROUP_SIZE = 5  # 每个 IP 承载的并发数(8轮压测验证:5并发/IP 100%成功)
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
                # 多窗口字母后缀:b-z 共 25 个;超过 26 用双字母 ab/ac/.../zz
                if i <= 26:
                    fname = first_name + chr(96 + i)
                else:
                    n = i - 1  # 0-based 索引
                    fname = first_name + chr(97 + n // 26 - 1) + chr(97 + n % 26)

            # 单层模式下为任务分配 Workers URL(按组)
            # 双层模式下 worker_url 仅作为 A 池没货时的回退(同样按组分配)
            worker_url = ""
            if has_proxy:
                GROUP_SIZE = 5
                group_id = (i - 1) // GROUP_SIZE
                worker_url = proxy_pool.get_worker_url(group_id) or ""

            f = pool.submit(
                register_single, i, password, fname, lname,
                worker_url=worker_url, ladder_proxy=ladder_proxy,
                pool_client=pool_client,
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
        status = "OK  " if r.success else "FAIL"
        logger.info(f"  [{status}] Task {r.task_id}: {r.email or 'N/A'} ({r.elapsed:.1f}s) {r.error}")
    logger.info(f"{'='*50}")

    return results
