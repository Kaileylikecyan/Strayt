"""生成/轮换访问令牌。

单用户免账号（决策 D-01），令牌是唯一准入凭证。服务端只存哈希，明文仅在此刻出现一次。

    uv run python -m app.scripts.gen_token          # 首次生成
    uv run python -m app.scripts.gen_token --rotate # 轮换（旧令牌立即失效）
    uv run python -m app.scripts.gen_token --show   # 只看是否已初始化
"""

from __future__ import annotations

import argparse
import sys

from app.core.deps import TOKEN_HASH_KEY
from app.core.security import generate_access_token, hash_access_token
from app.db.models import Setting
from app.db.session import SessionLocal

BANNER = """
============================================================
 访问令牌已生成 —— 只显示这一次，请立刻复制
============================================================
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 Strayt 访问令牌")
    ap.add_argument("--rotate", action="store_true", help="轮换：覆盖已有令牌（旧令牌立即失效）")
    ap.add_argument("--show", action="store_true", help="只看当前状态，不改动")
    args = ap.parse_args(argv)

    if not sys.stdout.isatty():  # 重定向时保持 UTF-8，避免中文花屏
        sys.stdout.reconfigure(encoding="utf-8")

    with SessionLocal() as db:
        row = db.get(Setting, TOKEN_HASH_KEY)
        exists = row is not None and bool(row.v)

        if args.show:
            print("访问令牌：已初始化" if exists else "访问令牌：未初始化")
            return 0 if exists else 1

        if exists and not args.rotate:
            print(
                "访问令牌已存在，不会覆盖。确需轮换请加 --rotate（旧令牌会立即失效，客户端需重新配置）。"
            )
            return 0

        token = generate_access_token()
        if row is None:
            row = Setting(k=TOKEN_HASH_KEY, v="")
            db.add(row)
        row.v = hash_access_token(token)
        db.commit()

    print(BANNER)
    print(token)
    print("\n在客户端「接入配置」页填入服务器地址与此令牌。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
