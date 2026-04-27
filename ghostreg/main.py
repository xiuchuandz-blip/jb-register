"""
GhostReg 启动入口
用法:
    python -m ghostreg            # 启动 Web 控制面板（默认端口 7860）
    python -m ghostreg --port 8080
"""
import argparse
import logging
import sys
import threading
import time
import webbrowser

import uvicorn


def main():
    parser = argparse.ArgumentParser(
        description="GhostReg — JetBrains 账号半自动注册机",
    )
    parser.add_argument(
        "--port", type=int, default=5000,
        help="Web 控制面板端口 (默认: 5000)",
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0",
        help="监听地址 (默认: 0.0.0.0)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="启动后不自动打开浏览器",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="显示详细日志",
    )

    args = parser.parse_args()

    # 切换工作目录到项目根目录（这样无论从哪里启动，output/ 等相对路径都正确）
    import os
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(project_dir)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    url = f"http://{args.host}:{args.port}"

    print()
    print("=" * 52)
    print("  GhostReg — JetBrains 账号半自动注册机")
    print("=" * 52)
    print(f"  控制面板: {url}")
    print(f"  流程: 自动填邮箱 → 你过验证码 → 自动填验证码+密码")
    print("=" * 52)
    print()

    # 延迟打开浏览器（等 server 启动）
    if not args.no_browser:
        def _open():
            time.sleep(1.5)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(
        "ghostreg.server:app",
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
