"""② 页级清洗：剔除页眉/页脚/水印/页码。

样板 PDF 每页首行都有水印、紧跟一行「第 N 页，共 50 页」。这些不剔掉，
后面每一段都会带上「共 50 页」，正文里到处是页码噪声。

**页数绝不硬编码**（AGENTS.md §6）。老原型把「共 50 页」写死是已修 bug，
这里用规则匹配而不是匹配具体页数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.parse.base import Line, TextBlock

# --------------------------------------------------------------------------
# 规则表
# --------------------------------------------------------------------------
#: 页码行。刻意不写死「共 50 页」，只匹配「第…页」这个结构。
_PAGE_NO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*第\s*\d+\s*页\s*[,，/]?\s*共\s*\d+\s*页\s*$"),
    re.compile(r"^\s*第\s*\d+\s*页\s*$"),
    re.compile(r"^\s*(?:page|Page|PAGE)\s+\d+(\s*(?:/|of)\s*\d+)?\s*$"),
    re.compile(r"^\s*-?\s*\d+\s*-\s*-?\s*$"),  # 纯数字行（Bates 编号）
    re.compile(r"^\s*\d{1,4}\s*$"),
)

#: **强**水印标记：这些几乎只出现在账号水印里，正文不会出现。
_STRONG_WATERMARK_HINTS: tuple[str, ...] = (
    "gzh",
    "xhs",
    "公众号",
    "扫码",
    "加微信",
    "vx",
    "违者必究",
)
#: **弱**标记：导游词正文里非常常见（"欢迎关注…"），**绝不能**单独用来判水印。
#: 保留在表里只是文档说明，需要启用时由 CleanRules 显式打开。
_WEAK_WATERMARK_HINTS: tuple[str, ...] = ("更多资料", "仅供学习", "关注")

#: 行首噪声：尾随的「——」分隔符、孤立的符号行
_NOISE_ONLY = re.compile(r"^[\s\-=_*#~·—–—|]+$")


@dataclass
class CleanRules:
    """清洗规则。做成数据而不是散在代码里的 if，是为了让不同资料能各自调。"""

    page_no_patterns: tuple[re.Pattern[str], ...] = field(default_factory=lambda: _PAGE_NO_PATTERNS)
    #: 只用强标记判水印。弱标记（「关注」等）默认不启用 —— 导游词正文里
    #: 「欢迎关注」很常见，误删正文比留个水印更糟。
    watermark_hints: tuple[str, ...] = field(default_factory=lambda: _STRONG_WATERMARK_HINTS)
    #: 连续出现这么多次的水印行，认定为页眉水印（防止误伤正文里的弱标记）
    watermark_min_repeat: int = 3
    #: 首尾各多少行算页眉/页脚区
    header_zone: int = 1
    footer_zone: int = 2


DEFAULT_RULES = CleanRules()


def looks_like_page_no(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    return any(p.match(t) for p in _PAGE_NO_PATTERNS)


def looks_like_watermark(text: str, rules: CleanRules = DEFAULT_RULES) -> bool:
    t = text.strip()
    if not t:
        return False
    low = t.lower()
    return any(h in low for h in rules.watermark_hints)


def is_noise(text: str) -> bool:
    return not text.strip() or bool(_NOISE_ONLY.match(text))


def is_junk_line(text: str, rules: CleanRules = DEFAULT_RULES) -> str | None:
    """单行是不是页级垃圾。返回原因（"page_no"/"watermark"/"noise"）或 ``None``。"""
    if is_noise(text):
        return "noise"
    if looks_like_page_no(text):
        return "page_no"
    if looks_like_watermark(text, rules):
        return "watermark"
    return None


def clean_group(
    lines: list[Line], *, edge: bool, rules: CleanRules = DEFAULT_RULES
) -> tuple[list[Line], list[str]]:
    """清洗一个 layout 段落组。

    ``edge`` 表示该组在页首或页尾 —— 只有页面边缘的水印才敢直接删，正文里的
    账号字样一律保留。
    """
    dropped: list[str] = []
    out: list[Line] = []
    for ln in lines:
        reason = is_junk_line(ln.text, rules)
        if reason == "watermark" and not edge:
            reason = None  # 正文里的疑似水印，放过
        if reason:
            dropped.append(reason)
            continue
        out.append(ln)
    return out, dropped


def clean_page_lines(
    lines: list[Line], *, rules: CleanRules = DEFAULT_RULES
) -> tuple[list[Line], list[str]]:
    """清洗一页。返回 ``(保留的行, 剔除原因)``。

    水印要「重复出现才认定」：正文里出现一次「关注」是内容，连续 3 页首行
    都带账号标记才是水印。这条区分很重要，误删正文比留个水印更糟。
    """
    dropped: list[str] = []

    # 先判水印候选：页眉区出现、且含标记的
    head_zone = lines[: rules.header_zone]
    head_hits = sum(1 for ln in head_zone if looks_like_watermark(ln.text))
    watermark_line = head_zone[0].text if head_hits else None

    out: list[Line] = []
    for i, ln in enumerate(lines):
        t = ln.text
        if is_noise(t):
            continue
        if looks_like_page_no(t):
            dropped.append("page_no")
            continue
        if watermark_line is not None and t == watermark_line:
            dropped.append("watermark")
            continue
        if looks_like_watermark(t) and i < rules.header_zone + rules.footer_zone:
            dropped.append("watermark")
            continue
        out.append(ln)
    return out, dropped


def blocks_from_page(
    page_no: int, lines: list[Line], *, start_order: int = 0, rules: CleanRules = DEFAULT_RULES
) -> tuple[list[TextBlock], int]:
    """把一页的清洗后行组装成块。页号在此处落定，之后一路带下去。"""
    kept, _ = clean_page_lines(lines, rules=rules)
    if not kept:
        return [], start_order
    # 一页内的连续行先合成一个块：PDF 里段落边界信息有限，
    # 真正的段落切分交给 ⑤（segments.py）按文本特征切。
    text = "\n".join(ln.text for ln in kept)
    return [TextBlock(text=text, page=page_no, order=start_order)], start_order + 1
