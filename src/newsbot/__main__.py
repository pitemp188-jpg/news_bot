"""
模块: newsbot.__main__
职责: 命令行入口——run（启动服务）、login（微信扫码登录）、notify（推送文本）
依赖: core.config, gateway.weixin
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from newsbot.core.config import get_config
from newsbot.core.log import setup_logging
from newsbot.gateway.weixin import WeixinStore, qr_login


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="newsbot", description="AI 资讯机器人")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="启动服务")

    login = sub.add_parser("login", help="登录平台账号")
    login.add_argument("platform", choices=["weixin"], help="要登录的平台")

    notify = sub.add_parser("notify", help="推送一条文本消息")
    notify.add_argument("text", help="要推送的内容")
    return parser


async def _login(platform: str) -> int:
    config = get_config()
    setup_logging(level=config.app.log_level, log_dir=config.log_dir)
    if platform != "weixin":
        print(f"暂不支持登录 {platform}")
        return 2
    account = await qr_login(WeixinStore(config.data_dir))
    if account is None:
        return 1
    print(f"\n微信连接成功，account_id={account.account_id}")
    print("可执行 python -m newsbot run 启动服务。")
    return 0


async def _notify(text: str) -> int:
    config = get_config()
    setup_logging(level=config.app.log_level, log_dir=config.log_dir)
    # 由 app 组装后再实现投递，当前先提示
    print(f"待推送: {text}")
    return 0


async def _run() -> int:
    # 服务组装在 M6（T6.2）实现，届时改为调用 app.main()
    print("服务尚未组装完成（M6 T6.2 实现）")
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "login":
        return asyncio.run(_login(args.platform))
    if args.command == "notify":
        return asyncio.run(_notify(args.text))
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
