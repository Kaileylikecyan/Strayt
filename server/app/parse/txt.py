"""① 文本提取（纯文本 ``.txt`` / Markdown ``.md``）。

PDF 走版面提取，纯文本没有版面，只有字符流。这里把文件读成 UTF-8（带 BOM 也认，
退回 GB18030 兜底），按空行切成段，每段一个 ``TextBlock``。

用途：把「图片 → OCR → 文本」这条离线桥接的产物喂进图谱管线（见 ADR-0013）。
``page`` 恒为 0 —— 纯文本无页概念，``loc_page`` 出处回看对它无意义。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.parse.base import ParsedDoc, ParseError, Parser, TextBlock
from app.parse.lang import annotate

#: 段落分隔：空行（一个或多个）。
_PARA_SPLIT = re.compile(r"\n\s*\n")

#: 依次尝试的编码。Windows 记事本默认常带 BOM；老资料可能是 GBK。
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")


class TxtParser(Parser):
    name = "txt"
    extensions = (".txt", ".md")

    def parse(self, path: Path) -> ParsedDoc:
        if not path.exists():
            raise ParseError("no_parser", f"文件不存在：{path}")
        try:
            data = path.read_bytes()
        except OSError as e:
            raise ParseError("no_parser", f"文本文件读取失败：{e}") from e

        text: str | None = None
        for enc in _ENCODINGS:
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ParseError("no_parser", "无法识别文本编码（试过 UTF-8 / GB18030）")

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        blocks: list[TextBlock] = []
        order = 0
        for para in _PARA_SPLIT.split(text):
            body = para.strip()
            if not body:
                continue
            b = TextBlock(text=body, page=0, order=order)
            b.zh_ratio = annotate(b.text)
            blocks.append(b)
            order += 1

        if not blocks:
            raise ParseError("empty", "文本文件没有可加工的内容")

        return ParsedDoc(channel="text", page_count=1, blocks=blocks)
