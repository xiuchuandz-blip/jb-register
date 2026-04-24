"""python -m protocol_reg"""
import argparse
import logging
import sys
import os

# Windows GBK 终端兼容
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from protocol_reg.worker import batch_register, register_single


def main():
    parser = argparse.ArgumentParser(
        description="GhostReg Protocol — 纯 HTTP 协议注册机",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m protocol_reg                    # 注册 1 个账号
  python -m protocol_reg -n 5              # 注册 5 个账号
  python -m protocol_reg -n 10 -w 5        # 10 个账号，5 路并发
  python -m protocol_reg -p MyPass123      # 指定密码
  python -m protocol_reg --first Ghost --last Reg  # 指定姓名
        """,
    )
    parser.add_argument("-n", "--count", type=int, default=1, help="注册数量 (默认 1)")
    parser.add_argument("-w", "--workers", type=int, default=3, help="并发数 (默认 3，推荐不超过 3)")
    parser.add_argument("-p", "--password", default="QPalzmw123", help="密码 (默认 QPalzmw123)")
    parser.add_argument("--first", default=None, help="First name (默认随机)")
    parser.add_argument("--last", default=None, help="Last name (默认随机)")
    parser.add_argument("-s", "--stagger", type=float, default=5, help="错峰间隔秒数 (默认 5，推荐 ≥5)")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    args = parser.parse_args()

    # 日志
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # 抑制 httpx 的 INFO 日志
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # 检查打码平台
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from ghostreg import captcha_service
    if not captcha_service.is_enabled():
        print("错误：打码平台未配置！请先运行 GhostReg 控制面板配置 YesCaptcha API Key")
        sys.exit(1)

    print(f"GhostReg Protocol v0.3 — 纯 HTTP 协议注册机")
    print(f"注册 {args.count} 个账号, 并发 {args.workers}, 错峰 {args.stagger}s, 密码 {args.password}")
    print()

    if args.count == 1:
        result = register_single(1, args.password, args.first, args.last)
        if result.success:
            print(f"\n✓ 注册成功: {result.email} / {result.password}")
        else:
            print(f"\n✗ 注册失败: {result.error}")
        sys.exit(0 if result.success else 1)
    else:
        results = batch_register(
            count=args.count,
            password=args.password,
            max_workers=args.workers,
            first_name=args.first,
            last_name=args.last,
            stagger=args.stagger,
        )
        success = sum(1 for r in results if r.success)
        sys.exit(0 if success > 0 else 1)


if __name__ == "__main__":
    main()
