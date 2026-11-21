# Copyright: Anki Software, LLC and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import argparse
import fcntl
import getpass
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx2
import uvicorn

from webstudy import USERS
from webstudy.app import MAX_UPLOAD, create_app
from webstudy.auth import Auth

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


@contextmanager
def data_lock(data_dir: Path) -> Iterator[None]:
    """Hold the data directory exclusively for the lifetime of the server."""
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Leave the file in place after exit: replacing it would create a second lock.
    with (data_dir / "serve.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"已有进程正在使用数据目录：{data_dir}") from exc
        yield


def init_users() -> None:
    passwords = {}
    for user in USERS:
        password = getpass.getpass(f"账号 {user.upper()} 的新密码：")
        if getpass.getpass("再输入一次：") != password:
            raise ValueError("两次密码不一致；未修改账号。")
        passwords[user] = password
    with data_lock(DATA):
        Auth(DATA / "auth.sqlite3").initialize(passwords)
    print("A、B 密码已保存；旧会话已失效。")


def serve(host: str, port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间。")
    with data_lock(DATA):
        if not Auth(DATA / "auth.sqlite3").initialized():
            raise ValueError("请先运行 init-users 设置账号密码。")
        uvicorn.run(
            create_app(DATA), host=host, port=port, workers=1, proxy_headers=False
        )


def _json(response: httpx2.Response) -> dict:
    if not response.is_success:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise ValueError(f"服务返回 {response.status_code}：{detail or '请求未完成'}")
    return response.json()


def _show_diff(preview: dict) -> dict[str, str] | None:
    if preview["previous_version"] is not None:
        print(f"此文件已在版本 {preview['previous_version']} 提交；不会重放修改。")
    print(
        f"新增 {preview['added']} 条，更新 {preview['updated']} 条。修改对 A、B 均生效。"
    )
    if preview["rows"] and not preview["rows"][0]["example_provided"]:
        print("本文件未提供例句列，更新词条时保留原例句。")
    resolutions = {}
    for row in preview["rows"]:
        entry, before, candidates = row["entry"], row["before"], row["candidates"]
        if entry == before:
            continue
        print(
            f"第 {row['line']} 行：{entry['word']!r} — {entry['meaning']!r} {entry['example']!r}"
        )
        if before:
            print(
                f"  原词条：{before['word']!r} — {before['meaning']!r} {before['example']!r}"
            )
        if not candidates:
            continue
        for index, word in enumerate(candidates, 1):
            print(f"  {index}. 更新 {word['id']!r}：{word['meaning']!r}")
        while True:
            decision = (
                input("输入序号更新、n 新增词义、s 跳过、q 取消：").strip().lower()
            )
            if decision == "q":
                return None
            if decision in ("n", "s"):
                choice = "new" if decision == "n" else "skip"
                break
            if decision.isascii() and decision.isdecimal() and len(decision) < 10:
                index = int(decision)
                if 1 <= index <= len(candidates):
                    choice = f"update:{candidates[index - 1]['id']}"
                    break
            print("无效选项，请重试。")
        resolutions[f"choice_{row['line']}"] = choice
    return resolutions


def import_file(path: Path, url: str, user: str | None) -> None:
    path = (ROOT / path).resolve()
    if not path.is_relative_to(
        (ROOT / "inbox").resolve()
    ) or path.suffix.lower() not in (".csv", ".tsv"):
        raise ValueError("请指定 inbox/ 下的 .csv 或 .tsv 文件。")
    with path.open("rb") as file:
        content = file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise ValueError("文件不能超过 2 MiB。")
    user = user or input("登录账号（A/B）：").strip().lower()
    if user not in USERS:
        raise ValueError("请选择账号 A 或 B。")
    password = getpass.getpass(f"账号 {user.upper()} 的密码：")
    # Talk directly to the LAN service, never via ambient proxy settings.
    with httpx2.Client(
        base_url=url,
        headers={"Accept": "application/json"},
        trust_env=False,
        timeout=60,
    ) as client:
        csrf = _json(client.get("/login"))["csrf"]
        login = _json(
            client.post(
                "/login", data={"user": user, "password": password, "csrf": csrf}
            )
        )
        csrf = login["csrf"]
        preview = _json(
            client.post(
                "/imports/preview",
                data={"csrf": csrf},
                files={"file": (path.name, content)},
            )
        )
        choices = _show_diff(preview)
        if choices is None or input("确认导入？[y/N] ").strip().lower() != "y":
            print("已取消，词库未修改。")
            return
        result = _json(
            client.post(
                "/imports/confirm",
                data={"csrf": csrf, "preview_id": preview["id"], **choices},
            )
        )
    print(f"词表已确认（文件对应版本 {result['version']}）。")
    if result["pending"]:
        print(
            "仍待投递的账号：" + "、".join(user.upper() for user in result["pending"])
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="webstudy")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-users", help="交互设置或更换 A、B 的密码")
    serve_parser = commands.add_parser("serve", help="启动单进程 Web 学习服务")
    serve_parser.add_argument("--host", required=True)
    serve_parser.add_argument("--port", required=True, type=int)
    importer = commands.add_parser(
        "import-file", help="从 inbox/ 导入词表到运行中的服务"
    )
    importer.add_argument("file", type=Path)
    importer.add_argument("--url", default="http://127.0.0.1:8765")
    importer.add_argument("--user", choices=USERS)
    args = parser.parse_args(argv)
    try:
        if args.command == "init-users":
            init_users()
        elif args.command == "serve":
            serve(args.host, args.port)
        else:
            import_file(args.file, args.url, args.user)
    except (ValueError, OSError, EOFError, httpx2.RequestError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
