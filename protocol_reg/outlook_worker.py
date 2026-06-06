"""
Outlook 邮箱注册 Worker
使用用户提供的 Microsoft 邮箱凭证完成 JetBrains 账号注册。
支持两种凭证格式：
  OAuth：email----password----client_id----refresh_token  （Graph API 读邮件，推荐）
  Basic：email----password                                 （IMAP 读邮件）
"""
import csv
import json
import logging
import random
import string
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
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
from ghostreg.outlook_email_service import get_snapshot, poll_otp, test_connection

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
    jb_email: str = ""


def _random_name(length: int = 5) -> str:
    first = random.choice(string.ascii_uppercase)
    rest = "".join(random.choices(string.ascii_lowercase, k=length - 1))
    return first + rest


def _save_result(result: OutlookRegResult):
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
    cred: dict,
    jb_password: str,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    worker_url: str = "",
    ladder_proxy: str = "",
    should_stop: Callable[[], bool] | None = None,
    otp_timeout: float = 120.0,
) -> OutlookRegResult:
    """
    使用单个 Outlook 凭证完成一个 JetBrains 账号注册。

    cred 格式：
      {"email", "password", "client_id"?, "refresh_token"?}
    """
    outlook_email = cred["email"]
    result = OutlookRegResult(task_id=task_id, email=outlook_email, jb_email=outlook_email)
    start = time.time()

    fname = first_name or _random_name(random.randint(4, 7))
    lname = last_name or _random_name(random.randint(4, 7))
    method = "Graph API" if cred.get("refresh_token") else "IMAP"

    def _check_stop(stage: str):
        if should_stop and should_stop():
            raise JBRegError(f"用户已停止({stage})")

    # Step 1: 获取邮件快照（OTP 触发前）
    logger.info(f"[Task {task_id}] [{outlook_email}] 获取邮件快照 ({method})...")
    try:
        snapshot = get_snapshot(cred)
        logger.info(f"[Task {task_id}] 快照完成，方式={snapshot['method']}")
    except Exception as e:
        result.error = f"邮箱快照失败({method}): {e}"
        result.elapsed = time.time() - start
        result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.error(f"[Task {task_id}] [FAIL] {result.error}")
        return result

    _check_stop("快照后")

    # Step 2: JetBrains 注册流程（带 IP 桶重试）
    for bucket_attempt in range(MAX_SESSION_BUCKET_RETRIES + 1):
        try:
            session = new_session(verify=False, worker_url=worker_url, ladder_proxy=ladder_proxy)
            reg = JBRegisterer(session)

            _check_stop("开始前")
            reg._init_session()
            _check_stop("Step1后")
            reg._create_auth_session()
            _check_stop("Step2后")
            reg._submit_email_first(outlook_email)
            _check_stop("打码前")

            logger.info(f"[Task {task_id}] 调用打码平台...")
            page_url = f"{BASE_URL}/signup?authSessionId={reg.session_id}"
            token = captcha_service.solve_recaptcha_v2(page_url, RECAPTCHA_SITEKEY)
            logger.info(f"[Task {task_id}] Token 长度: {len(token)} chars")

            reg._verify_captcha(token)
            _check_stop("打码后")

            reg._submit_email_retry(outlook_email)
            _check_stop("发OTP后")

            # Step 3: 读取 OTP
            logger.info(f"[Task {task_id}] 等待 OTP ({method})...")
            otp = poll_otp(cred, snapshot, timeout=otp_timeout, interval=5.0)
            logger.info(f"[Task {task_id}] OTP: {otp}")
            _check_stop("OTP后")

            reg._submit_otp(otp)
            reg._submit_password(jb_password, fname, lname, outlook_email)
            reg._complete_signup(jb_password, fname, lname, outlook_email)

            result.password = jb_password
            result.success = True
            result.elapsed = time.time() - start
            result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(f"[Task {task_id}] [OK] {outlook_email} ({result.elapsed:.1f}s)")
            break

        except SessionsBucketHitError as e:
            if bucket_attempt >= MAX_SESSION_BUCKET_RETRIES:
                result.error = f"sessions IP 桶连续限流: {e}"
                result.elapsed = time.time() - start
                result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                break
            logger.warning(f"[Task {task_id}] sessions 429，重试 {bucket_attempt+1}/{MAX_SESSION_BUCKET_RETRIES}")
            time.sleep(3)
            continue

        except CaptchaTokenInvalidError as e:
            if bucket_attempt >= MAX_SESSION_BUCKET_RETRIES:
                result.error = f"打码 token 连续被拒: {e}"
                result.elapsed = time.time() - start
                result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                break
            logger.warning(f"[Task {task_id}] 打码 token 被拒，重试 {bucket_attempt+1}")
            time.sleep(2)
            continue

        except TencentCaptchaError:
            result.error = "遇到腾讯验证码（基于 IP），请更换网络/代理后重试"
            result.elapsed = time.time() - start
            result.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    """批量并发使用 Outlook 邮箱注册 JetBrains 账号"""
    total = len(credentials)
    logger.info(f"Outlook 批量注册: {total} 个, 并发: {max_workers}, 错峰: {stagger}s")

    results: list[OutlookRegResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i, cred in enumerate(credentials, start=1):
            fname = first_name or _random_name(random.randint(4, 7))
            lname = last_name or _random_name(random.randint(4, 7))
            f = pool.submit(
                register_with_outlook, i, cred, jb_password,
                fname, lname, worker_url, ladder_proxy, should_stop, otp_timeout,
            )
            futures[f] = i
            if i < total and stagger > 0:
                time.sleep(stagger)

        timeout = max(600, total * 180)
        for f in as_completed(futures, timeout=timeout):
            tid = futures[f]
            try:
                results.append(f.result())
            except Exception as e:
                logger.error(f"[Task {tid}] 线程异常: {e}")
                results.append(OutlookRegResult(task_id=tid, error=str(e)))

    success = sum(1 for r in results if r.success)
    logger.info(f"Outlook 注册完成: 成功 {success}/{total}, 失败 {total - success}")
    return results
