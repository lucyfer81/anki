# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import argparse
import getpass
from pathlib import Path

from webstudy import USERS
from webstudy.auth import Auth

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def init_users() -> None:
    passwords = {}
    for user in USERS:
        password = getpass.getpass(f"账号 {user.upper()} 的新密码：")
        if getpass.getpass("再输入一次：") != password:
            raise ValueError("两次密码不一致；未修改账号。")
        passwords[user] = password
    Auth(DATA / "auth.sqlite3").initialize(passwords)
    print("A、B 密码已保存；旧会话已失效。")


def main() -> None:
    parser = argparse.ArgumentParser(prog="webstudy")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-users", help="交互设置或更换 A、B 的密码")
    args = parser.parse_args()
    try:
        if args.command == "init-users":
            init_users()
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
