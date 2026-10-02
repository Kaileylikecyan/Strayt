"""设置 / 重置访问口令。

单用户免账号（决策 D-01），口令是唯一准入。这条命令是**忘记口令时的唯一逃生口**
—— 服务端没有任何后门，HTTP 侧的改口令接口本身就要求先登录。丢了口令就只能靠它。

    uv run python -m app.scripts.set_password            # 交互式设置（已设置则拒绝）
    uv run python -m app.scripts.set_password --reset    # 强制重置（旧口令与所有会话失效）
    uv run python -m app.scripts.set_password --show     # 只看是否已设置

口令从 TTY 读，不走命令行参数 —— 参数会进 shell history 和进程列表。
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app.core.deps import PASSWORD_HASH_KEY
from app.core.security import MIN_PASSWORD_LEN, hash_password
from app.db.models import Setting
from app.db.session import SessionLocal


def _ask(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130) from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="设置 Strayt 访问口令")
    ap.add_argument("--reset", action="store_true", help="强制重置已有口令（所有会话立即失效）")
    ap.add_argument("--show", action="store_true", help="只看当前状态，不改动")
    args = ap.parse_args(argv)

    if not sys.stdout.isatty():  # 重定向时保持 UTF-8，避免中文花屏
        sys.stdout.reconfigure(encoding="utf-8")

    with SessionLocal() as db:
        row = db.get(Setting, PASSWORD_HASH_KEY)
        exists = row is not None and bool(row.v)

        if args.show:
            print("访问口令：已设置" if exists else "访问口令：未设置")
            return 0 if exists else 1

        if exists and not args.reset:
            print(
                "访问口令已存在，不会覆盖。确需重置请加 --reset"
                "（旧口令与所有已登录会话会立即失效，客户端需重新登录）。"
            )
            return 0

        first = _ask(f"请输入新口令（至少 {MIN_PASSWORD_LEN} 位）: ")
        if not first:
            print("口令不能为空。", file=sys.stderr)
            return 2
        again = _ask("请再输入一次: ")
        if first != again:
            print("两次输入不一致，未改动。", file=sys.stderr)
            return 2

        try:
            encoded = hash_password(first)
        except ValueError as exc:
            print(f"口令不合格：{exc}", file=sys.stderr)
            return 2

        if row is None:
            db.add(Setting(k=PASSWORD_HASH_KEY, v=encoded))
        else:
            row.v = encoded
        db.commit()

    print("访问口令已设置。客户端首次接入会自动进入「输入口令」流程。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
