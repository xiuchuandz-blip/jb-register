"""
Outlook 邮箱注册 Worker
使用用户提供的 Outlook/Microsoft 邮箱凭证完成 JetBrains 账号注册。
邮件验证码通过 IMAP 实时读取，无需第三方临时邮箱服务。
"""
import csv
import json
import logging
import random
import string
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Callable, Optional

from protocol_reg.client import new_session
from protocol_reg.jb_api import (
    JBRegisterer, JBRegError, TencentCaptchaError, SessionsBucketHitError,
    CaptchaTokenInvalidError, BASE_URL, RECAPTCHA_SITEKEY,
)

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from ghostreg import captcha_service
from ghostreg.outlook_email_service import (
    connect as imap_connect,
    disconnect as imap_disconnect,
    get_uid_snapshot,
    poll_for_otp,
    test_connection,
)

logger = logging.getLogger("outlook_worker")

OUTPUT_DIR = Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
OUTLOOK_ACCOUNTS_JSON = OUTPUT_DIR / "outlook_accounts.json"
OUTLOOK_ACCOUNTS_CSV = OUTPUT_DIR / "outlook_accounts.csv"

_file_lock = Lock()


@dataclass
class OutlookRegResult:
    task_id: int
    email: str = ""
    password: str = ""
    success: bool = False
    error: str = ""
    elapsed: float = 0.0
    timestamp: str = ""
    jb_email: str = ""  # 用于注册 JB 的 Outlook 邮箱地址


def _random_name(length: int = 5) -> str:
    first = random.choice(string.ascii_uppercase)
    rest = "".join(random.choices(string.ascii_lowercase, k=length - 1))
    return first + rest


def _save_result(result: OutlookRegResult):
    """线程安全保存成功结果"""
    if not result.success:
        return
    with _file_lock:
        records = []
        if OUTLOOK_ACCOUNTS_JSON.exists():
            try:
                records = json.loads(OUTLOOK_ACCOUNTS_JSON.read_text(encoding="utf-8"))
            except Exception:
                pass
        records.append(asdict(result))
        OUTLOOK_ACCOUNTS_JSON.write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        write_header = not OUTLOOK_ACCOUNTS_CSV.exists() or OUTLOOK_ACCOUNTS_CSV.stat().st_size == 0
        with open(OUTLOOK_ACCOUNTS_CSV, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["jb_email", "password", "outlook_email", "timestamp"])
            w.writerow([
                result.jb_email or result.email,
                result.password,
                result.email,
                result.timestamp,
            ])


MAX_SESSION_BUCKET_RETRIES = 5


def register_with_outlook(
    task_id: int,
    outlook_email: str,
    outlook_password: str,
    jb_password: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    worker_url: str = "",
    ladder_proxy: str = "",
    should_stop: Callable[[], bool] | None = None,
    otp_timeout: float = 120.0,
) -> OutlookRegResult:
    """
    使用指定的 Outlook 邮箱完成一个 JetBrains 账号注册。
    邮箱本身即为 JB 账号邮箱（用于接收 OTP）。

    Args:
        task_id: 任务编号（用于日志）
        outlook_email: Outlook 邮箱地址（同时也是 JB 注册邮箱）
        outlook_password: Outlook 邮箱登录密码（或应用密码）
        jb_password: 要为 JetBrains 账号设置的密码
        first_name / last_name: 可选，留空则随机生成
        worker_url: CF Worker 代理 URL（可选）
        ladder_proxy: 梯子代理地址（可选）
        otp_timeout: 等待 OTP 的超时秒数
    """
    result = OutlookRegResult(task_id=task_id, email=outlook_email, jb_email=outlook_email)
    start = time.time()

    fname = first_name or _random_name(random.randint(4, 7))
    lname = last_name or _random_name(random.randint(4, 7))

    def _check_stop(stage: str):
        if should_stop and should_stop():
            raise JBRegError(f"用户已停止任务({stage})")

    # 第 1 步：先连接 IMAP 取快照（在触发 OTP 之前）
    logger.info(f"[Task {task_id}] [{outlook_email}] 连接 IMAP 获取邮件快照...")
    try:
        imap_conn = imap_connect(outlook_email, outlook_password)
        known_uids = get_uid_snapshot(imap_conn)
        imap_disconnect(imap_conn)
        logger.info(f"[Task {task_id}] IMAP 快照: {len(known_uids)} 封已有邮件")
    except Exception as e:
        result.error = f"IMAP 连接失败: {e}"
        result.elapsed = time.time() - start
        result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.error(f"[Task {task_id}] [FAIL] {result.error}")
        return result

    _check_stop("IMAP快照后")

    # 第 2 步：JetBrains 注册流程（带 sessions IP 桶重试）
    for bucket_attempt in range(MAX_SESSION_BUCKET_RETRIES + 1):
        try:
            session = new_session(
                verify=False,
                worker_url=worker_url,
                ladder_proxy=ladder_proxy,
            )
            reg = JBRegisterer(session)

            _check_stop("开始前")

            reg._init_session()
            _check_stop("Step1后")

            reg._create_auth_session()
            _check_stop("Step2后")

            reg._submit_email_first(outlook_email)
            _check_stop("打码前")

            # 打码
            logger.info(f"[Task {task_id}] 调用打码平台...")
            page_url = f"{BASE_URL}/signup?authSessionId={reg.session_id}"
            token = captcha_service.solve_recaptcha_v2(page_url, RECAPTCHA_SITEKEY)
            logger.info(f"[Task {task_id}] Token: {len(token)} chars")

            reg._verify_captcha(token)
            _check_stop("打码后")

            reg._submit_email_retry(outlook_email)
            _check_stop("发OTP后")

            # 第 3 步：通过 IMAP 轮询 OTP
            logger.info(f"[Task {task_id}] 等待 OTP (IMAP)...")
            otp = poll_for_otp(
                outlook_email, outlook_password,
                known_uids=known_uids,
                timeout=otp_timeout,
                interval=5.0,
            )
            logger.info(f"[Task {task_id}] OTP: {otp}")
            _check_stop("OTP后")

            reg._submit_otp(otp)

            reg._submit_password(jb_password, fname, lname, outlook_email)
            reg._complete_signup(jb_password, fname, lname, outlook_email)

            result.password = jb_password
            result.success = True
            result.elapsed = time.time() - start
            result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"[Task {task_id}] [OK] 注册成功! {outlook_email} ({result.elapsed:.1f}s)")
            break

        except SessionsBucketHitError as e:
            if bucket_attempt >= MAX_SESSION_BUCKET_RETRIES:
                result.error = f"sessions IP 桶连续 {MAX_SESSION_BUCKET_RETRIES + 1} 次失败: {e}"
                result.elapsed = time.time() - start
                result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.error(f"[Task {task_id}] [FAIL] {result.error}")
                break
            logger.warning(f"[Task {task_id}] sessions IP 桶 429，重试 ({bucket_attempt + 1}/{MAX_SESSION_BUCKET_RETRIES})")
            time.sleep(3)
            continue

        except CaptchaTokenInvalidError as e:
            if bucket_attempt >= MAX_SESSION_BUCKET_RETRIES:
                result.error = f"打码 token 连续被拒: {e}"
                result.elapsed = time.time() - start
                result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                break
            logger.warning(f"[Task {task_id}] 打码 token 被拒，重新打码 ({bucket_attempt + 1}/{MAX_SESSION_BUCKET_RETRIES})")
            time.sleep(2)
            continue

        except TencentCaptchaError:
            result.error = "JetBrains 要求腾讯验证码（基于 IP），当前不支持，请更换网络/代理"
            result.elapsed = time.time() - start
            result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.error(f"[Task {task_id}] [FAIL] {result.error}")
            break

        except Exception as e:
            result.error = str(e)
            result.elapsed = time.time() - start
            result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.error(f"[Task {task_id}] [FAIL] {e} ({result.elapsed:.1f}s)")
            break

    _save_result(result)
    return result


def batch_register_outlook(
    credentials: list[dict],
    jb_password: str,
    max_workers: int = 3,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    stagger: float = 3.0,
    worker_url: str = "",
    ladder_proxy: str = "",
    otp_timeout: float = 120.0,
    should_stop: Callable[[], bool] | None = None,
) -> list[OutlookRegResult]:
    """
    批量并发使用 Outlook 邮箱注册 JetBrains 账号。

    Args:
        credentials: [{"email": "x@outlook.com", "password": "pass"}, ...]
        jb_password: 为 JetBrains 账号设置的密码
        max_workers: 最大并发数
        stagger: 错峰启动间隔秒数
        worker_url: CF Worker 代理 URL（可选）
        otp_timeout: 等待 OTP 超时秒数
    """
    total = len(credentials)
    logger.info(f"Outlook 批量注册: {total} 个账号, 并发: {max_workers}, 错峰: {stagger}s")

    results: list[OutlookRegResult] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i, cred in enumerate(credentials, start=1):
            fname = first_name or _random_name(random.randint(4, 7))
            lname = last_name or _random_name(random.randint(4, 7))

            f = pool.submit(
                register_with_outlook,
                i,
                cred["email"],
                cred["password"],
                jb_password,
                fname,
                lname,
                worker_url,
                ladder_proxy,
                should_stop,
                otp_timeout,
            )
            futures[f] = i

            if i < total and stagger > 0:
                time.sleep(stagger)

        timeout = max(600, total * 180)
        for f in as_completed(futures, timeout=timeout):
            task_id = futures[f]
            try:
                result = f.result()
                results.append(result)
            except Exception as e:
                logger.error(f"[Task {task_id}] 线程异常: {e}")
                results.append(OutlookRegResult(task_id=task_id, error=str(e)))

    success = sum(1 for r in results if r.success)
    failed = len(results) - success
    logger.info(f"\n{'='*50}")
    logger.info(f"Outlook 注册完成: 成功 {success}/{total}, 失败 {failed}")
    for r in sorted(results, key=lambda x: x.task_id):
        status = "OK  " if r.success else "FAIL"
        logger.info(f"  [{status}] Task {r.task_id}: {r.email} ({r.elapsed:.1f}s) {r.error}")
    logger.info(f"{'='*50}")

    return results
