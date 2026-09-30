"""校验 docs/ 下 Markdown 的相对链接不悬空。

写文档时最容易悄悄坏掉的就是链接：文件改名或删掉，`.md` 里留一个点不动的死链，
读者点到才发现。CI 里没人 review 文档，等到有人点就是几周后。

    uv run --project server python scripts/check_doc_links.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

#: 只查行内的 ``](...)``，够用且不会误伤代码块里的示例
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)\s]*)?\)")
EXTERNAL = re.compile(r"^(https?:|mailto:)")


def main() -> int:
    if not DOCS.is_dir():
        print(f"没有 {DOCS}，跳过", file=sys.stderr)
        return 0

    files = sorted(DOCS.rglob("*.md"))
    if not files:
        print(f"{DOCS} 下没有 .md，跳过", file=sys.stderr)
        return 0

    dead: list[str] = []
    checked = 0
    for md in files:
        text = md.read_text(encoding="utf-8")
        for match in LINK.finditer(text):
            target = match.group(1)
            if EXTERNAL.match(target):
                continue
            checked += 1
            if not (md.parent / target).exists():
                dead.append(f"{md.relative_to(ROOT)} -> {target}")

    if dead:
        print(f"{len(dead)} 条死链：", file=sys.stderr)
        for d in dead:
            print(f"  {d}", file=sys.stderr)
        return 1

    print(f"{len(files)} 个文档，{checked} 条相对链接全部有效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
