"""
模块: newsbot.__main__
职责: 命令行入口——run（机器人）、api（管理后台）、login（领取微信 bot 凭证）、notify、doctor、backup
依赖: app, api.server, core.config, core.log, gateway.weixin
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from newsbot.app import App, build_app, run_service
from newsbot.core.config import get_config
from newsbot.core.log import setup_logging
from newsbot.gateway.weixin import WeixinStore, qr_login


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="newsbot", description="AI 资讯机器人")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="启动机器人服务（轮询收消息 + 定时推送）")

    api = sub.add_parser("api", help="启动管理后台（Web 界面 + REST 接口）")
    api.add_argument("--host", help="监听地址，默认取配置 api.host")
    api.add_argument("--port", type=int, help="监听端口，默认取配置 api.port")

    login = sub.add_parser("login", help="可选：向 iLink 领取微信 bot 凭证并写入 data/weixin/")
    login.add_argument("platform", choices=["weixin"], help="要领取凭证的平台")

    notify = sub.add_parser("notify", help="推送一条文本消息")
    notify.add_argument("text", help="要推送的内容")
    notify.add_argument("--platform", choices=["weixin", "qqbot"], help="要推送的平台")
    notify.add_argument("--chat-id", help="要推送的聊天 ID")

    sub.add_parser("doctor", help="检查配置与依赖，不会联网")

    backup = sub.add_parser("backup", help="生成数据库快照")
    backup.add_argument("--out", help="输出目录，默认 data/backups")
    return parser


async def _login(platform: str) -> int:
    config = get_config()
    setup_logging(level=config.app.log_level, log_dir=config.log_dir)
    if platform != "weixin":
        print(f"暂不支持领取 {platform} 凭证")
        return 2
    print("提示：如果平台控制台已经给了 account_id / token，直接写进 .env 即可，无需本命令。")
    account = await qr_login(WeixinStore(config.data_dir), base_url=config.secrets.weixin_base_url)
    if account is None:
        return 1
    print(f"\n凭证已保存，account_id={account.account_id}")
    print(f"也可以写入 .env：WEIXIN_ACCOUNT_ID={account.account_id} / WEIXIN_TOKEN=<token>")
    print("然后执行 python -m newsbot run 启动服务。")
    return 0


async def _notify(text: str, platform: str | None = None, chat_id: str | None = None) -> int:
    config = get_config()
    setup_logging(level=config.app.log_level, log_dir=config.log_dir)
    app = App(config)
    await app.start()
    try:
        ok = await app.notify(text, platform=platform, chat_id=chat_id)
    finally:
        await app.stop()
    if not ok:
        print("没有可用的推送目标：请先给机器人发一条消息，或用 --platform/--chat-id 指定。")
        return 1
    print("已推送。")
    return 0


def _doctor() -> int:
    """离线自检：凭证从哪来、是否齐全、可选依赖是否装上。"""
    config = get_config()
    setup_logging(level=config.app.log_level, log_dir=config.log_dir)
    print(f"数据目录: {config.data_dir}")
    print(f"数据库:   {config.db_url}")
    print("\n[模型]")
    print(f"  base_url: {config.secrets.llm_base_url}")
    print(f"  model:    {config.secrets.llm_model}")
    print(f"  api_key:  {'已配置' if config.secrets.llm_api_key else '缺失（必填）'}")

    print("\n[搜索]")
    provider = config.secrets.search_provider.lower()
    print(f"  provider: {provider}")
    if provider == "tavily":
        print(f"  tavily:   {'已配置' if config.secrets.tavily_api_key else '缺失（必填）'}")
    else:
        print(f"  searxng:  {config.secrets.searxng_url}")

    print("\n[平台凭证]（来自各自开放平台，运行期无需登录）")
    store = WeixinStore(config.data_dir)
    weixin_ready = bool(config.secrets.weixin_token and config.secrets.weixin_account_id) or (
        store.load_account() is not None
    )
    if config.secrets.weixin_token and config.secrets.weixin_account_id:
        source = ".env"
    elif store.load_account() is not None:
        source = "data/weixin/account.json"
    else:
        source = "缺失"
    print(f"  weixin:  {source}（WEIXIN_ACCOUNT_ID + WEIXIN_TOKEN）")
    qq_ready = bool(config.secrets.qq_app_id and config.secrets.qq_client_secret)
    print(f"  qqbot:   {'已配置' if qq_ready else '缺失'}（QQ_APP_ID + QQ_CLIENT_SECRET）")
    if not (weixin_ready or qq_ready):
        print("  → 两个平台都没配，服务仍可运行（定时任务与本地查询），但无法收发消息")

    print("\n[可选依赖]")
    try:
        import browser_use  # noqa: F401

        browser = "已安装"
    except ImportError:
        browser = "未安装（uv sync --extra browser 启用浏览器兜底）"
    print(f"  browser-use: {browser}")

    problems = []
    if not config.secrets.llm_api_key:
        problems.append("LLM_API_KEY 缺失")
    if provider == "tavily" and not config.secrets.tavily_api_key:
        problems.append("TAVILY_API_KEY 缺失")
    print()
    if problems:
        print("自检发现必填项缺失：" + "；".join(problems))
        return 1
    print("自检通过。")
    return 0


async def _backup(out: str | None) -> int:
    from newsbot.core.config import get_config as _get

    config = _get()
    setup_logging(level=config.app.log_level, log_dir=config.log_dir)
    app = build_app(config)
    try:
        path = await app.backup(out)
    except FileNotFoundError as exc:
        # 全新部署时数据库还没建，这是可预期的状态，给提示而不是抛堆栈
        print(f"{exc}")
        print("数据库还没创建。先启动一次服务（python -m newsbot run）或跑一次任务，再执行备份。")
        return 1
    print(f"数据库快照已生成：{path}")
    return 0


async def _run() -> int:
    return await run_service()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "login":
        return asyncio.run(_login(args.platform))
    if args.command == "notify":
        return asyncio.run(_notify(args.text, args.platform, args.chat_id))
    if args.command == "doctor":
        return _doctor()
    if args.command == "backup":
        return asyncio.run(_backup(args.out))
    if args.command == "api":
        from newsbot.api.server import serve_api

        return asyncio.run(serve_api(host=args.host, port=args.port))
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
