"""
并发注册调度器
多开浏览器窗口 + 线程池限流 + CSV 写入
"""
import csv
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config
from .register import register_one, AccountResult

logger = logging.getLogger("ghostreg.worker")


def _ensure_csv(filepath: str) -> Path:
    """确保 CSV 文件存在并写好表头"""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["email", "password", "success", "error"])
    
    return path


def _append_csv(filepath: str, result: AccountResult):
    """追加一条记录到 CSV"""
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([result.email, result.password, result.success, result.error])


def batch_register(
    count: int | None = None,
    concurrency: int | None = None,
    password: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    headless: bool = False,
    output_csv: str | None = None,
) -> list[AccountResult]:
    """
    批量注册 JetBrains 账号。
    
    Args:
        count: 注册数量，默认 config.BATCH_COUNT
        concurrency: 并发数，默认 config.CONCURRENCY
        password: 统一密码，默认 config.DEFAULT_PASSWORD
        first_name: 名，默认 config.DEFAULT_FIRST_NAME
        last_name: 姓，默认 config.DEFAULT_LAST_NAME
        headless: 无头模式
        output_csv: 输出 CSV 路径，默认 config.OUTPUT_CSV
        
    Returns:
        AccountResult 列表
    """
    if count is None:
        count = config.BATCH_COUNT
    if concurrency is None:
        concurrency = config.CONCURRENCY
    if password is None:
        password = config.DEFAULT_PASSWORD
    if first_name is None:
        first_name = config.DEFAULT_FIRST_NAME
    if last_name is None:
        last_name = config.DEFAULT_LAST_NAME
    if output_csv is None:
        output_csv = config.OUTPUT_CSV
    
    csv_path = _ensure_csv(output_csv)
    results = []
    success_count = 0
    fail_count = 0
    
    logger.info(f"开始批量注册: 数量={count}, 并发={concurrency}, 密码={'*' * len(password)}")
    logger.info(f"输出文件: {csv_path.absolute()}")
    
    def _task(task_id: int) -> AccountResult:
        logger.info(f"[任务 {task_id}/{count}] 开始注册")
        result = register_one(password=password, first_name=first_name,
                              last_name=last_name, headless=headless)
        _append_csv(output_csv, result)
        status = "成功" if result.success else f"失败({result.error})"
        logger.info(f"[任务 {task_id}/{count}] {status}: {result.email}")
        return result
    
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(_task, i + 1): i + 1 for i in range(count)}
        
        for future in as_completed(futures):
            task_id = futures[future]
            try:
                result = future.result()
                results.append(result)
                if result.success:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                logger.error(f"[任务 {task_id}] 异常: {e}")
                fail_count += 1
    
    logger.info(f"批量注册完成: 成功={success_count}, 失败={fail_count}, 总计={count}")
    return results
