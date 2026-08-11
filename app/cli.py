"""采集账号管理 CLI。

用法：
  uv run python -m app.cli add --username U --password P --email E --email_password EP [--proxy P] [--mfa CODE]
  uv run python -m app.cli add-cookies --username U --cookies "auth_token=...; ct0=..."
  uv run python -m app.cli login
  uv run python -m app.cli relogin [--username U]
  uv run python -m app.cli delete --username U
  uv run python -m app.cli accounts
"""

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import twscrape

from .config import get_settings


def _accounts_db() -> str:
    return get_settings().accounts_db_path


async def _cmd_add(args: argparse.Namespace) -> None:
    pool = twscrape.AccountsPool(db_file=_accounts_db())
    await pool.add_account(
        args.username,
        args.password,
        args.email,
        args.email_password,
        proxy=args.proxy,
        mfa_code=args.mfa,
    )
    counter = await pool.login_all()
    print(f"login 结果: {counter}")


async def _cmd_add_cookies(args: argparse.Namespace) -> None:
    pool = twscrape.AccountsPool(db_file=_accounts_db())
    await pool.add_account_cookies(args.username, args.cookies)
    print(f"cookies 已写入账号 {args.username}")


async def _cmd_login(args: argparse.Namespace) -> None:
    pool = twscrape.AccountsPool(db_file=_accounts_db())
    counter = await pool.login_all()
    print(f"login 结果: {counter}")


async def _cmd_delete(args: argparse.Namespace) -> None:
    pool = twscrape.AccountsPool(db_file=_accounts_db())
    await pool.delete_accounts(args.username)
    print(f"已删除账号 {args.username}")


async def _cmd_relogin(args: argparse.Namespace) -> None:
    pool = twscrape.AccountsPool(db_file=_accounts_db())
    if args.username:
        await pool.relogin(args.username)
    else:
        usernames = [a.username for a in await pool.get_all()]
        if not usernames:
            print("没有账号需要重新登录")
            return
        await pool.relogin(usernames)
    print("重新登录完成")


def _cmd_accounts(args: argparse.Namespace) -> None:
    path = _accounts_db()
    if not Path(path).exists():
        print("还没有账号库，先运行 add 命令添加账号")
        return
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT username, active, cookies, error_msg, last_used, stats FROM accounts"
    ).fetchall()
    conn.close()
    for username, active, cookies, error_msg, last_used, stats in rows:
        cookies = json.loads(cookies or "{}")
        stats = json.loads(stats or "{}")
        logged_in = bool(cookies.get("auth_token") and cookies.get("ct0"))
        total_req = sum(v for v in stats.values() if isinstance(v, int))
        print(
            f"{username:<24} active={bool(active)} logged_in={logged_in} "
            f"req={total_req} last_used={last_used} error={error_msg}"
        )


def _add_account_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("add", help="添加采集账号并通过密码登录")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--email", default="")
    p.add_argument("--email_password", default="")
    p.add_argument("--proxy", default=None, help="代理，如 http://user:pass@host:port")
    p.add_argument("--mfa", default=None, help="两步验证码（如为固定 TOTP 可留空）")
    p.set_defaults(func=_cmd_add)


def _add_cookies_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("add-cookies", help="用浏览器导出的 cookies 配置账号（需含 auth_token 和 ct0）")
    p.add_argument("--username", required=True)
    p.add_argument("--cookies", required=True, help='形如 "auth_token=...; ct0=..."')
    p.set_defaults(func=_cmd_add_cookies)


def _login_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("login", help="登录所有待登录的采集账号")
    p.set_defaults(func=_cmd_login)


def _accounts_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("accounts", help="列出采集账号状态")
    p.set_defaults(func=_cmd_accounts)


def _delete_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("delete", help="删除采集账号")
    p.add_argument("--username", required=True)
    p.set_defaults(func=_cmd_delete)


def _relogin_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("relogin", help="重新登录采集账号（默认全部）")
    p.add_argument("--username", default=None, help="只重新登录指定账号")
    p.set_defaults(func=_cmd_relogin)


def main() -> None:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="app.cli", description="X-Crawler 采集账号管理")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_account_parser(sub)
    _add_cookies_parser(sub)
    _login_parser(sub)
    _accounts_parser(sub)
    _delete_parser(sub)
    _relogin_parser(sub)

    args = parser.parse_args()
    if args.command == "accounts":
        _cmd_accounts(args)
    else:
        asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
