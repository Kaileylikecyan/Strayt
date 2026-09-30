"""① 版面提取（PDF）。

**这是整条管线里最容易做错的一步，结论来自对 50 页样板 PDF 的实测：**

``extraction_mode="layout"``
    保留纵向间距，会用空行推断段落边界（样板 PDF 零空行全硬换行，plain 模式
    拿不到段落边界）。**但会把英文单词间的空格重复**::

        layout: 'revolutionary   years,  countless  patriots  and  heroes  fought'

``extraction_mode="plain"``（默认）
    字符序列正确、英文空格正常。**但完全没有段落边界**，整页就是一段。

所以：**正文取 plain，段落边界取 layout**，两路混合。这不是折中，是两者
各有所长、缺点不重叠。

混合算法（``_hybrid_page``）：

1. layout 按空行切成「视觉段落组」——组内是同一段（含硬换行）;
2. 每组取归一化指纹（压掉所有空白），在 plain 里**按顺序**匹配对应连续行；
3. 取 **plain 的原始行**作为该段正文（字符级正确），边界沿用 layout 的分组；
4. 返回**段落列表**，空行信息到此为止已经变成了 list 的分隔 —— 这是关键，
   返回扁平行列表会把段落边界弄丢，等于白做混合。

匹配不上时降级用 layout 文本并记 warning：宁可一段里空格多一点，也不能丢内容。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.parse.base import Line, ParsedDoc, ParseError, Parser, TextBlock
from app.parse.clean import DEFAULT_RULES, clean_group, clean_page_lines
from app.parse.lang import annotate

#: 归一化：压掉所有空白，用于比对两路提取是否指向同一段
_WS = re.compile(r"\s+")

#: layout 行数合理上限，用来识别「提取退化」而不是硬编码页数
_MAX_LINES_PER_PAGE = 400

#: 单段归一化长度上限，防止 layout 整页塞一行时把 plain 吃光
_MAX_PARA_CHARS = 200_000


def _norm(s: str) -> str:
    return _WS.sub("", s)


class PdfParser(Parser):
    name = "pdf"
    extensions = (".pdf",)

    def parse(self, path: Path) -> ParsedDoc:
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError
        except ImportError as e:  # pragma: no cover
            raise ParseError("no_parser", "缺少 pypdf 依赖") from e

        if not path.exists():
            raise ParseError("no_parser", f"文件不存在：{path}")

        try:
            reader = PdfReader(str(path))
        except PdfReadError as e:
            raise ParseError("no_parser", f"PDF 无法打开（可能已损坏）：{e}") from e

        if reader.is_encrypted:
            # 试空口令：不少「加密」PDF 只是设了 owner password
            try:
                if reader.decrypt("") == 0:
                    raise ParseError("no_parser", "PDF 已加密，需要口令才能解析")
            except ParseError:
                raise
            except Exception as e:
                raise ParseError("no_parser", f"PDF 解密失败：{e}") from e

        page_count = len(reader.pages)
        warnings: list[str] = []
        blocks: list[TextBlock] = []
        order = 0

        for page_no, page in enumerate(reader.pages):
            try:
                paras, w = _hybrid_page(page)
            except ParseError:
                raise
            except Exception as e:  # 单页失败不拖垮整份
                warnings.append(f"第 {page_no + 1} 页提取失败：{type(e).__name__}: {e}")
                continue
            warnings.extend(f"第 {page_no + 1} 页：{x}" for x in w)

            for para in paras:
                text = _strip_edge_noise(para)
                if not text:
                    continue
                blocks.append(TextBlock(text=text, page=page_no, order=order))
                order += 1

        if not blocks:
            # 把 warning 一并带出去：解析器里的编程错误（比如漏导入）会被上面的
            # 单页 except 吞掉，只留一句「疑似扫描件」会把真问题指向错误方向。
            detail = "；".join(warnings[:3]) if warnings else "无附加信息"
            raise ParseError(
                "scan_needs_vision",
                f"没能从 PDF 提取出任何文字（{len(reader.pages)} 页，"
                f"疑似扫描件）。请为该资料启用视觉（OCR）通道。提取记录：{detail}",
            )

        for b in blocks:
            b.zh_ratio = annotate(b.text)

        return ParsedDoc(channel="text", page_count=page_count, blocks=blocks, warnings=warnings)


def _strip_edge_noise(text: str) -> str:
    """去掉段落首尾的页码/水印行，但**不删中间行**（正文中间可能有页码）。"""
    lines = text.split("\n")
    kept, _ = clean_page_lines([Line(t) for t in lines], rules=DEFAULT_RULES)
    return "\n".join(ln.text for ln in kept).strip()


def _layout_groups(page) -> tuple[list[list[Line]], list[str]]:
    """① layout 提取 + 按空行切成视觉段落组。"""
    warnings: list[str] = []
    try:
        layout_text = page.extract_text(extraction_mode="layout") or ""
    except Exception as e:
        raise ParseError("no_parser", f"layout 模式提取失败：{e}") from e

    lines = [Line(t) for t in layout_text.split("\n")]
    if len(lines) > _MAX_LINES_PER_PAGE:
        warnings.append(f"layout 行数异常（{len(lines)}），提取可能退化")

    groups: list[list[Line]] = []
    cur: list[Line] = []
    for ln in lines:
        if not ln.text.strip():
            if cur:
                groups.append(cur)
                cur = []
            continue
        cur.append(ln)
    if cur:
        groups.append(cur)
    return groups, warnings


def _find_start(plain_lines: list[str], pi: int, start_norm: str) -> int | None:
    """在 plain 行里从 ``pi`` 起找与 ``start_norm`` 对齐的下标。

    只比**前 24 个归一化字符**：样板里一段的开头常有水印/页码干扰导致整体偏移，
    比全等太脆；比太短又容易撞上重复的开头句式。
    """
    if not start_norm:
        return None
    key = start_norm[:24]
    end = min(pi + 12, len(plain_lines))
    for cand in range(pi, end):
        n = _norm(plain_lines[cand])
        if n[:24] == key:
            return cand
    return None


def _drop_junk(groups: list[list[Line]]) -> tuple[list[list[Line]], int]:
    """② 在参与 plain 对齐**之前**剔掉页码/水印组。

    顺序很关键。样板 PDF 每页最后一组是「第 5 页，共 50 页」；如果先对齐再清洗，
    plain 游标会一路走到行尾，页码组匹配不上 → 每页一条假 warning，噪声掩盖真问题。
    剔在前面，顺带把 plain 里那行页码也一起让过去。
    """
    kept: list[list[Line]] = []
    dropped = 0
    n = len(groups)
    for i, grp in enumerate(groups):
        edge = i == 0 or i == n - 1
        out, _ = clean_group(grp, edge=edge, rules=DEFAULT_RULES)
        if not out:
            dropped += len(grp)
            continue
        dropped += len(grp) - len(out)
        kept.append(out)
    return kept, dropped


def _hybrid_page(page) -> tuple[list[str], list[str]]:
    """单页混合提取。返回 ``(段落文本列表, warnings)``。"""
    raw_groups, warnings = _layout_groups(page)
    if not raw_groups:
        return [], warnings

    groups, _ = _drop_junk(raw_groups)
    if not groups:
        return [], warnings

    try:
        plain_text = page.extract_text() or ""
    except Exception as e:
        warnings.append(f"plain 提取失败，全页退回 layout：{e}")
        return ["\n".join(ln.text for ln in g) for g in groups], warnings

    plain_lines = plain_text.split("\n")
    if not plain_text.strip():
        warnings.append("plain 为空，全页退回 layout（英文空格会重复）")
        return ["\n".join(ln.text for ln in g) for g in groups], warnings

    paras: list[str] = []
    pi = 0  # plain 游标
    matched = 0

    for grp in groups:
        want = min(sum(len(_norm(ln.text)) for ln in grp), _MAX_PARA_CHARS)
        start_norm = _norm(grp[0].text)

        j = _find_start(plain_lines, pi, start_norm)
        if j is None:
            # 降级：这一段用 layout 文本。宁可空格多一点，也不能丢内容。
            paras.append("\n".join(ln.text for ln in grp))
            warnings.append("段落起点匹配失败，该段退回 layout")
            continue

        # 逐行吃 plain，直到累计归一化长度追平整组
        got = 0
        take: list[str] = []
        k = j
        while k < len(plain_lines) and got < want:
            n = _norm(plain_lines[k])
            if not n:
                k += 1
                continue
            take.append(plain_lines[k])
            got += len(n)
            k += 1

        paras.append("\n".join(take))
        matched += 1
        pi = k

    if matched == 0:
        warnings.append("所有段落都匹配失败，全页退回 layout")

    return paras, warnings
