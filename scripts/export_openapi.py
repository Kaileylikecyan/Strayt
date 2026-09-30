"""把 FastAPI 的 OpenAPI 契约导出到 ``docs/api/openapi.yaml``。

客户端的类型由 ``packages/api-client`` 从这份文件生成（AGENTS.md §5），所以它必须
是**可复现的产物**：跑一次和跑十次结果完全一致，否则每次 diff 都噪声。

做法：先 ``json.dumps(sort_keys=True)`` 排好序，再转 YAML —— 字典顺序由 JSON 层保证，
不依赖 PyYAML 5.x 之后的 ``sort_keys`` 支持差异。

顺带把导出做成"要么更新、要么报 stale"两种模式，好挂进 CI：

    uv run python scripts/export_openapi.py          # 写入
    uv run python scripts/export_openapi.py --check  # 只比对，不一致则退出码 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app.main import app

TARGET = ROOT / "docs" / "api" / "openapi.yaml"

_HEADER = """# 本文件是**生成物**，不要手改。
#
#   uv run python scripts/export_openapi.py           # 重新生成
#   uv run python scripts/export_openapi.py --check   # 校验是否过期（CI 用）
#
# 客户端类型由 packages/api-client 消费，改了服务端接口就重新导出。
"""


def render() -> str:
    schema = json.loads(json.dumps(app.openapi(), sort_keys=True, ensure_ascii=False))
    body = yaml.safe_dump(
        schema,
        allow_unicode=True,
        sort_keys=True,
        default_flow_style=False,
        width=100,
    )
    return _HEADER + body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不写入")
    args = ap.parse_args()

    wanted = render()
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""

    if args.check:
        if current == wanted:
            print(f"openapi.yaml 是最新的（{len(app.openapi()['paths'])} 条路径）")
            return 0
        print(
            "openapi.yaml 已过期，跑 `uv run python scripts/export_openapi.py`",
            file=sys.stderr,
        )
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(wanted, encoding="utf-8", newline="\n")
    print(f"已写入 {TARGET.relative_to(ROOT)}（{len(app.openapi()['paths'])} 条路径）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
