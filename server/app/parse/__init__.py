"""解析器注册与按扩展名分发。

``build_plan`` 原来写死 ``PdfParser``；随着「图片 → OCR → 文本」桥接
（ADR-0013）引入纯文本输入，改为按扩展名挑选解析器。
"""

from __future__ import annotations

from pathlib import Path

from app.parse.base import ParsedDoc, ParseError, Parser
from app.parse.pdf import PdfParser
from app.parse.txt import TxtParser

_PARSERS: tuple[Parser, ...] = (PdfParser(), TxtParser())


def parse_document(path: Path) -> ParsedDoc:
    """按扩展名选择解析器并解析。没有匹配的解析器抛 ``ParseError``。"""
    ext = path.suffix.lower()
    for parser in _PARSERS:
        if ext in parser.extensions:
            return parser.parse(path)
    raise ParseError("no_parser", f"没有可用的解析器处理 {ext or '（无扩展名）'} 文件")


__all__ = ["parse_document"]
