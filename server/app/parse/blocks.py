"""③ 块结构识别 + ④ 语言判别辅助 + ⑤ 段落切分。

从「整篇纯文本流」里认出**考区 → 范文 → 中英文正文**的层级。

样板 PDF 实测出的文档文法（50 页、6 考区 × 2 范文）::

    省情范文
    （一）上海概况讲解（1 题，20 题）。包括上海的城市定位…      <- 考区
    范文（一）                                                <- 范文
    亲爱的游客朋友们，大家好！…谢谢大家！                        <- 中文正文
    Dear tourists and friends, hello everyone!… Thank you all!  <- 英文正文
    范文（二）
    …
    （一）东方明珠游览区主要景点：东方明珠广播电视塔…            <- 区域小标题

**为什么必须靠终止符而不是靠语言判别切块**：解析阶段给出的块会在语言边界上
连片 —— 实测 `Thank you all!\\n范文（二）\\n亲爱的游客朋友们` 三个语义单元被 PDF
的硬换行并进了同一个块。若先按 ``zh_ratio`` 过滤块再拼文本，终止符会随
「被误判为另一语言的块」一起被丢掉，整篇就废了。所以切块必须**先于**语言判别，
用高精度终止符（``谢谢大家！`` / ``Thank you all!``）和标题正则来定位。

③ 的正则**外置为 :class:`StructureRules`**（AGENTS.md §6）：不同资料版式不同，
写成散落的 if 就等于把「改一处炸全流程」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.parse.base import LANG_ZH_THRESHOLD, TextBlock
from app.parse.lang import cjk_ratio

_CN_NUM = "一二三四五六七八九十零〇两"


@dataclass
class StructureRules:
    """结构识别规则。**改版式只改这里，不要改代码。**"""

    #: 考区标题。样板形态：「（一）上海概况讲解（1 题，20 题）。包括…」
    #: 编号**中文数字和阿拉伯数字都要收** —— 样板用 `(1)`，别的资料多用「（一）」。
    area: re.Pattern[str] = field(
        default_factory=lambda: re.compile(
            rf"^[（(](?P<num>[{_CN_NUM}\d])[）)]\s*"
            r"(?P<title>[^\n。]{2,40}?)\s*"
            r"(?:讲解|介绍|说明|导游词)"
            r"(?:[（(][^）)]{0,20}[)）])?"
            r"[。.、]?"
        )
    )
    #: 区域小标题：「（一）东方明珠游览区主要景点：…」
    zone: re.Pattern[str] = field(
        default_factory=lambda: re.compile(
            # 注意 ``{{2,30}}``：这行是 f-string，裸的 ``{2,30}`` 会被当成
            # 替换字段去求值元组 ``(2,30)``，正则量词直接失效成捕获组。
            # area 的 ``{2,40}`` 写在普通 r-string 里所以没事 —— 同一个文件里
            # 两种写法混用最容易踩这个坑。
            rf"^[（(][{_CN_NUM}\d][）)]\s*[^\n：:]{{2,30}}(?:主要景点|景点|介绍)"
        )
    )
    #: 范文标题：「范文（一）」「范文2」
    piece: re.Pattern[str] = field(
        default_factory=lambda: re.compile(
            rf"^范文\s*[（(]?\s*(?P<num>[{_CN_NUM}\d]+)\s*[）)]?\s*$"
        )
    )
    #: 范文正文开头标记（辅助定位正文起点）
    opening_markers: tuple[str, ...] = ("亲爱的游客朋友们", "Dear tourists and friends")
    #: ④ 语言判别辅助：高精度终止符（AGENTS.md §6）
    terminators: tuple[str, ...] = ("谢谢大家！", "谢谢大家!", "Thank you all!", "Thank you all.")

    def terminator_re(self) -> re.Pattern[str]:
        """终止符正则。**不锚定行尾，且忽略大小写**。

        两条都是实测踩出来的：

        - 不锚 ``$``：存在 ``Thank you all!（1）玉佛寺游览区主要景点：…`` 这种
          「终止符 + 下一个考区标题挤在同一行」的排版（PDF 硬换行不可控），
          锚 ``$`` 会漏掉，正文一路吞到文档末尾。
        - 忽略大小写：样板里英文终止符**两种大小写都出现过**（行首 ``Thank you
          all!`` 与行中小写 ``thank you all!``）。大小写敏感会让行内切分整个
          失效，症状是「外滩游览区」被并进上一篇的英文正文。
        """
        alts = "|".join(re.escape(t) for t in self.terminators)
        return re.compile(alts, re.IGNORECASE)


DEFAULT_RULES = StructureRules()


@dataclass
class Section:
    """一个语义片段。``page`` 是 0 起页号，一路带到底。

    ``block_map`` 是 ADR-0012 的「这块正文里每个字符来自哪个版面块」。
    值为 ``None`` 表示这条正文没有可用的块身份（老数据 / 非版面块来源），
    下游据此把「按段」降级为「按句」。
    """

    kind: str  # area | zone | piece | body_zh | body_en | note
    text: str
    page: int
    #: ``text`` 去掉全部空白后，逐字符的版面块序号。索引域与 ``norm(text)`` 对齐。
    block_map: list[int] | None = None


@dataclass
class PieceDraft:
    """一篇范文的草稿。⑤ 切段后交给对齐层。"""

    title: str
    page: int
    area_title: str
    zh: str
    en: str
    zh_page: int
    en_page: int
    #: 与 ``zh`` / ``en`` 逐字符对齐的版面块序号表（索引域 = 去掉空白后的字符）。
    #: ``None`` = 这篇没有块身份，「按段」会降级为「按句」。
    zh_blocks: list[int] | None = None
    en_blocks: list[int] | None = None


@dataclass
class Structure:
    areas: list[str] = field(default_factory=list)
    pieces: list[PieceDraft] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def piece_count(self) -> int:
        return len(self.pieces)


def _flatten(blocks: list[TextBlock]) -> list[tuple[str, int, int]]:
    """块 → ``(行, 页号, 版面块序号)`` 列表。

    **块边界要留住**（ADR-0012）。历史版本在这里把块拍平成行、边界丢掉，docstring
    写的是「块边界本身不保留 —— 后续靠标记重新切」：那是对**结构识别**而言的
    （考区/范文/正文靠正则和终止符重新切，与块边界无关），但代价是「这句来自
    原文哪一段」这个信息从此不可恢复，背诵舱只能按字数硬拼段落。

    样板实测：281 个版面块 / 50 页，字数中位 227、p75 456，与「原文自然段」
    同量级 —— 这就是可用的段落身份。用 ``enumerate`` 而不是 ``b.order``，因为
    块序号只在**单篇 piece 内**有意义，文档级序号可能来自不同解析器而不稠密。
    """
    out: list[tuple[str, int, int]] = []
    for bno, b in enumerate(blocks):
        for line in b.text.split("\n"):
            out.append((line, b.page, bno))
    return out


def _split_at_terminator(line: str, rules: StructureRules) -> tuple[str, str | None]:
    """在终止符处切开一行。返回 ``(正文, 终止符或 None)``。"""
    m = rules.terminator_re().search(line)
    if not m:
        return line, None
    return line[: m.start()].rstrip(), m.group(0).strip()


def _unanchored(pat: re.Pattern[str]) -> re.Pattern[str]:
    """去掉 ``^`` 锚定的版本，用来在**行内**找标记。

    ``re.search`` 对带 ``^`` 的模式也只从位置 0 开始试，等于 ``match``。样板
    里终止符后面紧跟着考区标题（``Thank you all!（1）外滩游览区…``），必须能
    在任意位置命中。
    """
    src = pat.pattern
    if src.startswith("^"):
        return re.compile(src[1:])
    return pat


def _normalize_lines(
    lines: list[tuple[str, int, int]], rules: StructureRules
) -> list[tuple[str, int, int]]:
    """把「行中」的结构标记拆成独立行。

    PDF 的硬换行不可控，实测存在这种行::

        'Thank you all!（1）外滩游览区主要景点：黄浦江、外白渡桥…'

    终止符后面紧跟着下一个考区的标题 —— 行级判断（``^`` 锚定的正则）会整行漏掉，
    结果考区的景点列表被并进**上一篇的中文正文**，背诵内容直接被污染。
    所以在扫描之前先把这类行拆开，让后面的逻辑可以放心按行判断。

    版面块序号原样带走：拆出来的新片段继承源行那一块的块号。
    """
    out: list[tuple[str, int, int]] = []
    term_re = rules.terminator_re()
    # 行内搜索必须用去锚定版，否则 search 等于 match（见 _unanchored 注释）
    mid_pats = tuple(_unanchored(p) for p in (rules.piece, rules.zone, rules.area))
    for line, page, bno in lines:
        rest = line
        while rest:
            best: tuple[int, re.Match[str]] | None = None
            for pat in mid_pats:
                m = pat.search(rest)
                if m and m.start() > 0 and (best is None or m.start() < best[0]):
                    best = (m.start(), m)
            if best is None:
                out.append((rest, page, bno))
                break
            cut, _ = best
            head = rest[:cut]
            # 只在「终止符正好紧贴标记」时拆，避免把正文里正常的括号说明切碎。
            # 判定是「head 的最后一个终止符恰好结束在 cut 处」，不是拿 head 的
            # 尾部切片去搜 —— 切片里永远不含完整终止符。
            tms = list(term_re.finditer(head))
            if tms and tms[-1].end() == len(head):
                out.append((head, page, bno))
                rest = rest[cut:]
            else:
                out.append((rest, page, bno))
                break
    return out


def _starts_piece(line: str, rules: StructureRules) -> str | None:
    """是范文标题则返回编号，否则 ``None``。"""
    m = rules.piece.match(line.strip())
    return m.group("num") if m else None


def scan(blocks: list[TextBlock], rules: StructureRules = DEFAULT_RULES) -> Structure:
    """单遍扫描，把行流切成语义片段。"""
    st = Structure()
    lines = _normalize_lines(_flatten(blocks), rules)
    n = len(lines)
    i = 0

    while i < n:
        line, page, _bno = lines[i]
        s = line.strip()

        if not s:
            i += 1
            continue

        # 范文标题
        if (num := _starts_piece(line, rules)) is not None:
            st.sections.append(Section(kind="piece", text=num, page=page))
            i += 1
            # 范文正文：按终止符切到下个标题为止
            i = _consume_body(lines, i, rules, st, page)
            continue

        # 区域小标题 / 考区标题。两者都是**考区边界** —— 样板里第 1 个考区用
        # 「讲解」句式，后 5 个用「XX游览区主要景点：」句式（参考 JSON 的 6 个
        # 考区名正是这么来的），所以都要记成 area，否则考区名会只剩第一个。
        if rules.zone.match(s):
            name = _zone_title(s, rules)
            st.sections.append(Section(kind="area", text=name, page=page))
            if name and name not in st.areas:
                st.areas.append(name)
            i += 1
            continue

        if rules.area.match(s) and ("讲解" in s or "题" in s or len(s) > 20):
            title = _area_title(s, rules)
            st.sections.append(Section(kind="area", text=title, page=page))
            if title and title not in st.areas:
                st.areas.append(title)
            i += 1
            continue

        # 其余（文档标题、考区描述续行、区域小标题后的说明）统一归 note
        st.sections.append(Section(kind="note", text=line, page=page))
        i += 1

    return st


def _zone_title(s: str, rules: StructureRules) -> str:
    """从「（1）外滩游览区主要景点：黄浦江…」里取出考区名「外滩」。"""
    t = re.sub(r"^[（(][^）)]*[）)]\s*", "", s.strip())
    t = re.split(r"[：:]", t)[0]
    t = re.sub(r"(游览区|景区|风景区)?(主要景点|景点|介绍|概况).*$", "", t).strip()
    return t or s.strip()


def _area_title(s: str, rules: StructureRules) -> str:
    """从考区标题行里取干净的标题（去掉题量括注和描述）。"""
    m = rules.area.match(s)
    if not m:
        return s
    t = m.group("title").strip()
    # 「上海概况讲解（1 题，20 题）。包括…」→「上海概况」
    t = re.sub(r"(讲解|介绍|说明|导游词)$", "", t).strip()
    return t or s


def _lang_runs(body: list[tuple[str, int, int]]) -> list[tuple[str, str, int, list[int]]]:
    """把正文行按**语言**切成连续段：``[(语言, 文本, 页号, 行块号)]``。

    这是 ④ 语言判别的真正用武之地。**不能靠「终止符个数奇偶」推断语言** ——
    样板实测玉佛寺两篇的形态是::

        谢谢大家！希望您在玉佛寺游览区度过一段难忘而美好的时光。
        Thank you all! I hope you have an unforgettable and wonderful time…

    终止符后面还跟着**同语言**的收尾句。按奇偶交替会把这段收尾句判给另一种语言，
    再连带把英文正文尾巴错标成中文（实测 48 个英文字符混进 zh）。

    逐行判 ``cjk_ratio`` 切「语言游程」则稳定：中文行归中文段，英文行归英文段，
    同一语言里终止符在哪都不影响。

    行块号随行带走，供 ADR-0012 的「按段」粒度使用。注意一个版面块**可以横跨
    中英两侧**（PDF 版面分组与语言无关），所以这里不能拿块号当语言判据。
    """
    runs: list[list] = []  # [lang, [lines], first_page, [bno]]
    cur_lang: str | None = None
    for line, page, bno in body:
        if not line.strip():
            continue
        r = cjk_ratio(line)
        if r >= LANG_ZH_THRESHOLD:
            lang = "zh"
        elif r <= 0.1:
            lang = "en"
        else:
            # 0.1~0.3 的模糊行（中英混排的标题、引子）：不构成语言切换，
            # 跟着当前段走，避免一段正文被切成碎片。
            lang = cur_lang or ("zh" if r > 0.2 else "en")
        if cur_lang is None or lang == cur_lang:
            cur_lang = lang
            if not runs:
                runs.append([lang, [], page, []])
            runs[-1][1].append(line)
            runs[-1][3].append(bno)
        else:
            cur_lang = lang
            runs.append([lang, [line], page, [bno]])
    return [(lang, "\n".join(lines).strip(), page, bnos) for lang, lines, page, bnos in runs]


def _consume_body(
    lines: list[tuple[str, int, int]],
    i: int,
    rules: StructureRules,
    st: Structure,
    start_page: int,
) -> int:
    """收集本篇正文，按语言切成中文段与英文段。

    正文的中英分界用**语言游程**判（见 ``_lang_runs``），终止符只用来剥掉首尾
    的客套话 —— 样板里终止符可能出现在段落中段（后接同语言收尾句），拿它当
    语言分界会整篇错位。
    """
    n = len(lines)

    # 1) 收集正文行范围（到下一个 piece/area/zone 标题为止）
    body: list[tuple[str, int, int]] = []
    while i < n:
        line, page, bno = lines[i]
        s = line.strip()
        if s:
            if _starts_piece(line, rules) is not None:
                break
            if rules.zone.match(s):
                break
            if rules.area.match(s) and ("讲解" in s or "题" in s or len(s) > 20):
                break
        body.append((line, page, bno))
        i += 1

    if not body:
        st.warnings.append(f"第 {start_page + 1} 页开始的范文正文为空")
        return i

    # 2) 按语言切段
    runs = _lang_runs(body)
    if not runs:
        st.warnings.append(f"第 {start_page + 1} 页开始的范文正文为空")
        return i

    # 3) 剥掉每段首尾的终止符/客套话，再按语言归位
    zh_parts: list[str] = []
    en_parts: list[str] = []
    zh_maps: list[list[int]] = []
    en_maps: list[list[int]] = []
    for lang, raw_text, page, bnos in runs:
        text = _trim_markers(raw_text, rules)
        if not text:
            continue
        cmap = _block_map(raw_text, bnos, text)
        (zh_parts if lang == "zh" else en_parts).append(text)
        (zh_maps if lang == "zh" else en_maps).append(cmap)
        if lang == "zh" and len(zh_parts) == 1:
            st.sections.append(Section(kind="body_zh", text=text, page=page, block_map=cmap))
        elif lang == "en" and len(en_parts) == 1:
            st.sections.append(Section(kind="body_en", text=text, page=page, block_map=cmap))

    # 后续同语言段拼回第一段（中文/英文各自可能因为游程抖动分成多段）
    for extra_idx, (kind, parts, maps) in enumerate(
        (("body_zh", zh_parts, zh_maps), ("body_en", en_parts, en_maps))
    ):
        if not parts:
            st.warnings.append(
                f"第 {start_page + 1} 页的范文正文缺少{'中文' if extra_idx == 0 else '英文'}段"
            )
            continue
        merged = "\n\n".join(parts)
        # 逐字符块表首尾相接就是合并后的块表：``norm(join(parts, sep))`` 恰好等于
        # ``"".join(norm(p) for p in parts)``（分隔符是空白，规范化后消失）。
        merged_map = _chain_maps(maps)
        for sec in st.sections[::-1]:
            if sec.kind == kind:
                if parts[0] != merged:
                    sec.text = merged
                sec.block_map = merged_map
                break

    if len(runs) == 1:
        st.warnings.append(
            f"第 {start_page + 1} 页的范文正文只有一种语言（{runs[0][0]}），中英未配齐"
        )
    return i


def _trim_markers(text: str, rules: StructureRules) -> str:
    """剥掉正文段首尾的终止符与范文开场白（终止符大小写不敏感）。"""
    t = text.strip()
    term = rules.terminator_re()
    # 尾：反复剥终止符
    while True:
        m = None
        for x in term.finditer(t):
            m = x
        if m is not None and t[m.start() :].strip() == "":
            t = t[: m.start()].rstrip()
            continue
        break
    # 头：开场白
    for x in rules.opening_markers:
        if t.lower().startswith(x.lower()):
            t = t[len(x) :]
    t = _strip_leading_terms(t, term)
    return t.strip()


def _strip_leading_terms(t: str, term: re.Pattern[str]) -> str:
    while True:
        m = term.match(t.lstrip())
        if m is None:
            break
        t = t.lstrip()[m.end() :]
    return re.sub(r"^[\s，。！？,.\-—…]+", "", t)


_WS = re.compile(r"\s+")


def norm_text(s: str) -> str:
    """规范化：只留非空白字符。

    段块归属全靠它做**贪心子序列匹配**（见 :func:`_block_map`），
    所以必须保证 ``norm_text(join(parts, sep)) == "".join(norm_text(p) for p in parts)``
    对空白分隔符恒成立 —— 否则逐字符块表无法直接首尾相接。
    """
    return _WS.sub("", s)


def _block_map(raw: str, line_blocks: list[int], trimmed: str) -> list[int]:
    """``trimmed`` 的每个字符属于哪个版面块。

    ``raw`` 是同一段未剥客套话的原文，行块号与 ``raw`` 的行一一对应
    （``_lang_runs`` 用 ``"\\n"`` 拼行，所以两边的行结构完全一致）。

    :func:`_trim_markers` 只删**首尾**，不做任何内部改写，因此
    ``norm(trimmed)`` 是 ``norm(raw)`` 的子序列。贪心前向扫描找子序列是正确的
    （任何贪心匹配到的都合法），从而不依赖精确偏移 —— PDF 里空白排版不可控，
    按字符偏移对齐反而脆。

    返回长度恒等于 ``len(norm(trimmed))``；匹配不上的字符记 ``-1``
    （下游当作「无块身份」，退化成按句）。
    """
    src = norm_text(raw)
    if len(line_blocks) != raw.count("\n") + 1:
        # 行数与块号数对不上：宁可整段放弃块身份，也不要给出错位的归属。
        return [-1] * len(norm_text(trimmed))
    # src 的逐字符块号。行与行之间没有额外字符（"\\n" 已被 norm 去掉）。
    owner: list[int] = []
    for ln, bno in zip(raw.split("\n"), line_blocks, strict=True):
        owner.extend([bno] * len(norm_text(ln)))

    tgt = norm_text(trimmed)
    out: list[int] = []
    i = 0
    for ch in tgt:
        j = i
        while j < len(src) and src[j] != ch:
            j += 1
        if j >= len(src):
            out.append(-1)
        else:
            out.append(owner[j])
            i = j + 1
    return out


def _chain_maps(maps: list[list[int]]) -> list[int]:
    """把多段的逐字符块表首尾相接。任一段为空则整体退化为空表。"""
    if not maps or any(not m for m in maps):
        return []
    return [x for m in maps for x in m]


def to_pieces(st: Structure) -> list[PieceDraft]:
    """把扫描结果拼成「一篇范文 = 一条 zh + 一条 en」的草稿。

    范文正文是中英交替出现的：中文段（``谢谢大家！`` 收）→ 英文段
    （``Thank you all!`` 收）→ 下一篇。所以按 piece 边界切片后，每片里
    第一个 ``body_zh`` 归中文、第一个 ``body_en`` 归英文。
    """
    pieces: list[PieceDraft] = []
    cur_title = ""
    cur_page = 0
    cur_area = ""
    zh_parts: list[str] = []
    en_parts: list[str] = []
    zh_maps: list[list[int]] = []
    en_maps: list[list[int]] = []
    zh_page = 0
    en_page = 0
    in_piece = False

    def flush() -> None:
        nonlocal zh_parts, en_parts, zh_page, en_page, zh_maps, en_maps
        if not in_piece:
            return
        if not zh_parts and not en_parts:
            return
        zh = "\n\n".join(zh_parts).strip()
        en = "\n\n".join(en_parts).strip()
        if not zh and not en:
            return
        # ``strip()`` 只削空白，规范化后长度不变，所以逐字符块表可以原样带走。
        zh_blocks = _chain_maps(zh_maps)
        en_blocks = _chain_maps(en_maps)
        if len(zh_blocks) != len(norm_text(zh)):
            zh_blocks = []
        if len(en_blocks) != len(norm_text(en)):
            en_blocks = []
        title = cur_title or f"范文 {len(pieces) + 1}"
        # 用 **cur_area**（范文开始时锁定的考区）而不是 area —— area 在 flush
        # 之前就可能被下一个考区标题改掉了，会把上一篇挂到下一个考区名下，
        # 表现为「外滩·范文（二）」排在「外滩·范文（一）」前面。
        if cur_area:
            title = f"{cur_area}·{title}"
        pieces.append(
            PieceDraft(
                title=title,
                page=zh_page or en_page or cur_page,
                area_title=cur_area,
                zh=zh,
                en=en,
                zh_page=zh_page,
                en_page=en_page,
                zh_blocks=zh_blocks or None,
                en_blocks=en_blocks or None,
            )
        )
        zh_parts, en_parts = [], []
        zh_maps, en_maps = [], []
        zh_page = en_page = 0

    for sec in st.sections:
        if sec.kind == "area":
            area = sec.text
        elif sec.kind == "piece":
            flush()
            in_piece = True
            cur_title = f"范文（{sec.text}）"
            cur_page = sec.page
            cur_area = area  # 锁定本篇所属考区
        elif sec.kind == "body_zh":
            if not zh_parts:
                zh_page = sec.page
            zh_parts.append(sec.text)
            zh_maps.append(sec.block_map or [])
        elif sec.kind == "body_en":
            if not en_parts:
                en_page = sec.page
            en_parts.append(sec.text)
            en_maps.append(sec.block_map or [])
    flush()
    return pieces


#: 句子终止标点。中英混用一套：中文的 。！？ 和英文的 .!? 都收，
#: 外加省略号（样板里有「…」，切在半截会很难看）。
_SENT_END = re.compile(r"(?<=[。！？!?…])|(?<=[.!?])(?=\s)")


def split_segments(body: str, *, min_len: int = 1) -> list[str]:
    """⑤ 段落切分。

    两级切：

    1. **按空行切** —— 可靠边界，样板里段与段之间才有空行；
    2. 块内**按句末标点切**（硬换行 + 句末）—— 样板段落内全是硬换行
       （无空行），只按换行切会得到 2000 字的一大块，对齐根本没法用。

    ``min_len`` 是防碎下限：短句（"好的。"、"OK."）粘回前一句。

    **默认必须是 1（等价于不粘连）。** 中文句子天生短，``min_len`` 一旦设成
    十几，粘连只发生在中文侧、几乎不发生在英文侧，两侧粒度就差出近一倍：

        min_len=1   中 49 句  英 54 句   （与参考 49/55 基本一致）
        min_len=12  中 47 句  英 54 句

    粒度不对称会直接污染 ⑥ —— 长度平衡 DP 会认为「3 中 + 1 英」才比例正确，
    于是把 49+54 句压成 13 对，等于把三句话糊成一条。宁可留下「大家好！」这种
    短句独立成对，也不要换来整篇的错位。
    """
    if not body.strip():
        return []
    out: list[str] = []
    for chunk in re.split(r"\n\s*\n", body):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = [x.strip() for x in chunk.split("\n") if x.strip()]
        # 块内先按硬换行+句末标点断句
        sentences: list[str] = []
        cur = ""
        for line in lines:
            cur += line
            if re.search(r"[。！？!?….]$", line):
                sentences.append(cur)
                cur = ""
        if cur:
            sentences.append(cur)

        # 再按句末标点细分长句（同一行里可能有多句）
        buf = ""
        for s in sentences:
            for piece in _SENT_END.split(s):
                if not piece:
                    continue
                if buf and len(buf) >= min_len:
                    out.append(buf)
                    buf = piece
                else:
                    buf += piece
        if buf:
            out.append(buf)
    return [s for s in (x.strip() for x in out) if s]


def attribute_blocks(
    parts: list[str], source: str, char_blocks: list[int] | None
) -> list[int | None]:
    """把 ``parts``（原文的连续切片）映射回 ``source`` 的版面块。

    ``char_blocks`` 是 ``source`` 逐字符（规范化后）的块表，见 :func:`_block_map`。
    返回值与 ``parts`` 等长，元素为块序号或 ``None``（无块身份）。

    同样靠**贪心子序列匹配**：``parts`` 里的每一段都是 ``source`` 的连续切片
    （对齐器就是用 ``"".join(zh[i] for i in range(...))`` 拼的），规范化后必然
    是 ``norm(source)`` 的子序列。游标只前进不回退 —— 对齐是单调的，
    这与「文本对齐单调」这个前提是同一条性质；若哪天出现非单调映射，
    这里会返回一批 ``None``（退化成按句）而不是给出错位的块号。

    匹配不上的元素一律给 ``None``：**宁可没有块号，也不要错的块号** ——
    错的块号会让「按段」把不相干的句子聚成一组，用户背的是错的分组。
    """
    if char_blocks is None or len(char_blocks) != len(norm_text(source)):
        return [None] * len(parts)
    src = norm_text(source)
    out: list[int | None] = []
    i = 0
    for part in parts:
        tgt = norm_text(part)
        start = -1
        ok = bool(tgt)
        for ch in tgt:
            j = i
            while j < len(src) and src[j] != ch:
                j += 1
            if j >= len(src):
                ok = False
                break
            if start < 0:
                start = j
            i = j + 1
        if not ok or start < 0 or char_blocks[start] < 0:
            out.append(None)
        else:
            out.append(char_blocks[start])
    return out
