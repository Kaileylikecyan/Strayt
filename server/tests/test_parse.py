"""解析层测试（①版面提取 ②清洗 ③结构 ④语言 ⑤切段）。

设计取向：**结构识别与切段用合成输入测**（不依赖 PDF、不受版式变动影响），
只有混合提取那一步才用真实样板 PDF（本地文件，已被 .gitignore 排除，
缺失时 skip 而不是 fail —— CI 上没有这个文件）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.parse import blocks as B
from app.parse.base import Line, ParseError, TextBlock
from app.parse.clean import (
    clean_group,
    is_junk_line,
    looks_like_page_no,
    looks_like_watermark,
)
from app.parse.lang import (
    QUALITY_EN_MAX,
    QUALITY_ZH_MIN,
    cjk_ratio,
    classify,
    zh_ratio,
)

FIXTURE_PDF = Path(__file__).resolve().parents[1] / "fixtures" / "daoyouci.pdf"
FIXTURE_REF = Path(__file__).resolve().parents[1] / "fixtures" / "daoyouci_reference.json"


# --------------------------------------------------------------------------
# ④ 语言判别
# --------------------------------------------------------------------------
class TestLang:
    def test_纯中文(self):
        assert zh_ratio("欢迎来到繁华璀璨的上海。") > 0.9

    def test_纯英文(self):
        assert zh_ratio("Welcome to the bustling and brilliant Shanghai.") < 0.1

    def test_分母不含数字标点(self):
        """页码/年份大量出现，计入分母会把纯中文块误判成英文。"""
        a = zh_ratio("上海概况")
        b = zh_ratio("上海概况 2024年，第 5 页，共 50 页")
        assert abs(a - b) < 0.05

    def test_英文里混一个汉字仍判英文(self):
        assert classify("Thank you all! 谢谢") == "en"

    def test_空串(self):
        assert zh_ratio("") == 0.0

    @pytest.mark.parametrize(
        ("text", "want"),
        [
            ("大家好，欢迎来到上海！", "zh"),
            ("Dear tourists and friends, hello everyone!", "en"),
        ],
    )
    def test_classify(self, text, want):
        assert classify(text) == want

    def test_cjk_ratio_口径含标点(self):
        """⑦ 自检口径分母含标点，能抓到「中文字段里混进大段英文」。"""
        assert cjk_ratio("大家好。") == 1.0
        assert cjk_ratio("Hello world") == 0.0
        assert 0 < cjk_ratio("大家好 Hello") < 0.5


# --------------------------------------------------------------------------
# ② 页级清洗
# --------------------------------------------------------------------------
class TestClean:
    @pytest.mark.parametrize(
        "text",
        [
            "第 5 页，共 50 页",
            "第5页，共50页",
            "第 12 页",
            "第 12 页 / 共 50 页",
            "Page 5 of 50",
            "PAGE 5",
            "5",
            "- 5 -",
        ],
    )
    def test_页码(self, text):
        assert looks_like_page_no(text)

    def test_正文里的数字不是页码(self):
        assert not looks_like_page_no("2024年 上海接待 5000 万人次")

    def test_水印(self):
        assert looks_like_watermark("gzh/xhs找到我：英语导游证看 Ellie")
        assert looks_like_watermark("公众号：某某资料")

    def test_正文弱标记不算水印(self):
        """导游词正文里「欢迎关注」很常见，误删正文比留水印更糟。"""
        assert not looks_like_watermark("感谢您的关注，欢迎下次再来！")

    def test_组内逐行剔除(self):
        g = [Line("gzh/xhs找到我：X"), Line("正文第一行"), Line("第 5 页，共 50 页")]
        out, reasons = clean_group(g, edge=True)
        assert [x.text for x in out] == ["正文第一行"]
        assert "page_no" in reasons and "watermark" in reasons

    def test_非边缘组保留疑似水印(self):
        g = [Line("正文里提到 gzh 账号")]
        out, reasons = clean_group(g, edge=False)
        assert len(out) == 1 and not reasons

    def test_全组是垃圾则清空(self):
        out, _ = clean_group([Line("第 5 页，共 50 页")], edge=True)
        assert out == []

    def test_空行是噪声(self):
        assert is_junk_line("   ") == "noise"
        assert is_junk_line("——") == "noise"


# --------------------------------------------------------------------------
# ③ 结构识别
# --------------------------------------------------------------------------
def _blocks(*rows: tuple[str, int]) -> list[TextBlock]:
    """把 ``(行, 页)`` 组装成块。"""
    out = []
    for i, (text, page) in enumerate(rows):
        out.append(TextBlock(text=text, page=page, order=i))
    return out


SAMPLE = [
    ("上海市导游资格证考试（英语导游词参考）", 0),
    ("省情范文", 0),
    ("（1）上海概况讲解（1 题，20 题）。包括上海的城市定位、地理环境。", 0),
    ("范文（一）", 0),
    ("亲爱的游客朋友们，大家好！欢迎来到上海。", 0),
    ("希望您玩得开心，谢谢大家！", 1),
    ("Dear tourists and friends, hello everyone! Welcome to Shanghai.", 1),
    ("Enjoy your stay. Thank you all!", 2),
    ("范文（二）", 2),
    ("亲爱的游客朋友们，大家好！上海欢迎您。", 2),
    ("祝您旅途愉快，谢谢大家！", 3),
    ("Dear tourists and friends, hello everyone! Shanghai welcomes you.", 3),
    ("Have a nice trip. Thank you all!", 3),
]


class TestStructure:
    def test_认考区与范文(self):
        st = B.scan(_blocks(*SAMPLE))
        assert st.areas == ["上海概况"]
        assert sum(1 for s in st.sections if s.kind == "piece") == 2

    def test_中英各一段且不串味(self):
        pieces = B.to_pieces(B.scan(_blocks(*SAMPLE)))
        assert len(pieces) == 2
        for p in pieces:
            assert cjk_ratio(p.zh) >= QUALITY_ZH_MIN, p.zh
            assert cjk_ratio(p.en) <= QUALITY_EN_MAX, p.en

    def test_标题不套壳(self):
        pieces = B.to_pieces(B.scan(_blocks(*SAMPLE)))
        assert pieces[0].title == "上海概况·范文（一）"
        assert "范文（范文" not in pieces[0].title

    def test_页号随段落落定(self):
        """loc_page 是二期出处回看的唯一依据，必须跟着正文走。"""
        st = B.scan(_blocks(*SAMPLE))
        zh = [s for s in st.sections if s.kind == "body_zh"]
        en = [s for s in st.sections if s.kind == "body_en"]
        assert zh[0].page == 0
        assert en[0].page == 1

    def test_考区归属不串到下一篇(self):
        """area 在 flush 之前就会被改掉，必须用范文开始时锁定的考区。"""
        rows = [
            ("（1）上海概况讲解（1 题，20 题）。包括。", 0),
            ("范文（一）", 0),
            ("大家好，谢谢大家！", 0),
            ("Dear all, thank you all!", 0),
            ("（1）外滩游览区主要景点：黄浦江。", 1),
            ("范文（一）", 1),
            ("大家好，欢迎来外滩，谢谢大家！", 1),
            ("Dear all, thank you all!", 1),
        ]
        pieces = B.to_pieces(B.scan(_blocks(*rows)))
        assert [p.area_title for p in pieces] == ["上海概况", "外滩"]
        assert pieces[1].title == "外滩·范文（一）"

    def test_区域小标题也是考区边界(self):
        st = B.scan(_blocks(*SAMPLE, ("（1）外滩游览区主要景点：黄浦江。", 3)))
        assert "外滩" in st.areas

    def test_终止符后紧跟标题的行(self):
        """实测存在 `Thank you all!（1）外滩游览区…` 这种挤在一行的排版。"""
        rows = [
            ("范文（一）", 0),
            ("大家好，谢谢大家！", 0),
            ("Dear all, thank you all!（1）外滩游览区主要景点：黄浦江。", 1),
        ]
        st = B.scan(_blocks(*rows))
        areas = [s.text for s in st.sections if s.kind == "area"]
        assert areas == ["外滩"]
        # 英文正文不能吞掉考区标题
        en = [s for s in st.sections if s.kind == "body_en"]
        assert en and "主要景点" not in en[0].text

    def test_范文标题不认正文里的范文二字(self):
        rows = [("今天我们来学一篇范文（一）的写法。", 0)]
        st = B.scan(_blocks(*rows))
        assert not [s for s in st.sections if s.kind == "piece"]

    def test_无终止符记warning(self):
        rows = [("范文（一）", 0), ("正文一直没有结束。", 0)]
        st = B.scan(_blocks(*rows))
        assert st.warnings

    def test_区域名清洗(self):
        assert B._zone_title("（1）外滩游览区主要景点：黄浦江。", B.DEFAULT_RULES) == "外滩"
        assert B._zone_title("（二）玉佛寺游览区主要景点：天王殿。", B.DEFAULT_RULES) == "玉佛寺"


# --------------------------------------------------------------------------
# ⑤ 段落切分
# --------------------------------------------------------------------------
class TestSegments:
    def test_按空行切(self):
        got = B.split_segments("第一段。\n\n第二段。")
        assert got == ["第一段。", "第二段。"]

    def test_硬换行不切(self):
        """样板段落内是硬换行，段内不能被切碎。"""
        got = B.split_segments("第一行内容\n第二行内容")
        assert got == ["第一行内容第二行内容"]

    def test_长句按句末标点细分(self):
        body = "第一句话内容足够长了所以会在这里断开。第二句话同样足够长所以也会断开。"
        got = B.split_segments(body)
        assert len(got) == 2

    def test_空输入(self):
        assert B.split_segments("   \n\n ") == []


# --------------------------------------------------------------------------
# ⑤b 版面块归属（ADR-0012：背诵舱「按段」粒度的数据来源）
# --------------------------------------------------------------------------
class TestBlockAttribution:
    """块序号是「按段」能不能用起来的唯一前提。

    这里全部用合成输入测：不依赖 PDF 版式，且能把边界情况穷举出来
    （块表长度对不上、匹配不上、没有块表），这些在真实样板里几乎撞不到，
    撞到了却是静默的错分组。
    """

    def test_逐字符块表覆盖规范化后的文本(self):
        raw = "第一行内容\n第二行内容"
        got = B._block_map(raw, [7, 9], raw)
        # 规范化后 10 个字符：前 5 个属块 7、后 5 个属块 9
        assert len(got) == len(B.norm_text(raw)) == 10
        assert got == [7] * 5 + [9] * 5

    def test_剥掉首尾客套话后偏移能自愈(self):
        """``_trim_markers`` 只删首尾，块号必须跟着内容走而不是停在 0。"""
        raw = "谢谢大家！后面还有内容"
        trimmed = raw[len("谢谢大家！") :]
        got = B._block_map(raw, [3], trimmed)
        assert set(got) == {3}
        assert len(got) == len(B.norm_text(trimmed))

    def test_行数与块号数对不上就整体放弃(self):
        """宁可没有块号，也不要错位的块号 —— 错的会让「按段」聚出错误的组。"""
        got = B._block_map("一二三\n四五六", [1], "一二三四五六")
        assert got == [-1] * 6

    def test_句归属到块(self):
        body = "第一句话。第二句话。第三句话。"
        segs = B.split_segments(body)
        blocks = [0] * len(B.norm_text(segs[0]))
        blocks += [1] * (len(B.norm_text(segs[1])) + len(B.norm_text(segs[2])))
        got = B.attribute_blocks(segs, body, blocks)
        assert got == [0, 1, 1]

    def test_句归属单调不回退(self):
        body = "第一句话。第二句话。第三句话。第四句话。"
        segs = B.split_segments(body)
        n = [len(B.norm_text(s)) for s in segs]
        blocks = [0] * n[0] + [1] * n[1] + [2] * (n[2] + n[3])
        got = B.attribute_blocks(segs, body, blocks)
        assert got == sorted(got), "块号必须单调，否则版面上靠后的句子会跑回前面的段"

    def test_没有块表时全给None(self):
        """存量篇目的处境：客户端据此降级为按句，而不是给一个假的分组。"""
        segs = B.split_segments("第一句话。第二句话。")
        assert B.attribute_blocks(segs, "第一句话。第二句话。", None) == [None, None]

    def test_块表长度不符时全给None(self):
        segs = B.split_segments("第一句话。第二句话。")
        assert B.attribute_blocks(segs, "第一句话。第二句话。", [0]) == [None, None]

    def test_匹配不上时给None而不是猜(self):
        body = "第一句话。第二句话。"
        segs = ["根本不在原文里出现过的一段话"]
        assert B.attribute_blocks(segs, body, [0] * len(B.norm_text(body))) == [None]

    def test_规范化后可首尾相接(self):
        """``_chain_maps`` 依赖的不变式：分隔符是空白时，两种拼法等价。"""
        parts = ["甲 乙", "丙丁"]
        joined = B.norm_text("\n\n".join(parts))
        # "甲 乙" 规范化后 2 字 → 块表 2 项；"丙丁" → 2 项
        chained = B._chain_maps([[0, 1], [2, 3]])
        assert len(chained) == len(joined) == 4
        assert joined == "甲乙丙丁"

    def test_链式拼接遇到空表就整体放弃(self):
        """空段没块身份时，合并出来的篇目也不该有 —— 半个真半个假最没用。"""
        assert B._chain_maps([[0, 1], []]) == []
        assert B._chain_maps([]) == []


@pytest.mark.skipif(not FIXTURE_PDF.exists(), reason="本地样板 PDF 不在（已 gitignore）")
class TestBlockAttributionReal:
    @pytest.fixture(scope="class")
    def pieces(self):
        from app.parse.pdf import PdfParser

        return B.to_pieces(B.scan(PdfParser().parse(FIXTURE_PDF).blocks))

    def test_真实样本每句都拿到块号(self, pieces):
        """样板是用户唯一在用的资料，它拿不到块号就等于功能没上线。"""
        segs = [B.split_segments(p.zh) for p in pieces[:4]]
        got = [
            B.attribute_blocks(s, p.zh, p.zh_blocks) for s, p in zip(segs, pieces[:4], strict=True)
        ]
        for blocks, part_blocks in zip(got, [p.zh_blocks for p in pieces[:4]], strict=True):
            assert part_blocks is not None, "样板 PDF 应能产出块表"
            assert all(b is not None for b in blocks), "有句子拿不到块号"

    def test_真实样本块号单调(self, pieces):
        for p in pieces[:4]:
            segs = B.split_segments(p.zh)
            got = B.attribute_blocks(segs, p.zh, p.zh_blocks)
            known = [b for b in got if b is not None]
            assert known == sorted(known), f"{p.title} 的块号非单调：{known}"

    def test_真实样本块号能把句合成段(self, pieces):
        """块号若与句号一一对应，这列就白存了（等于没分组）。"""
        for p in pieces[:4]:
            segs = B.split_segments(p.zh)
            got = B.attribute_blocks(segs, p.zh, p.zh_blocks)
            units = len({b for b in got if b is not None})
            assert 1 < units < len(segs) / 2, (
                f"{p.title}: {len(segs)} 句只分出 {units} 个块，粒度没意义"
            )


# --------------------------------------------------------------------------
# ① 混合提取（真实样板）
# --------------------------------------------------------------------------
@pytest.mark.skipif(not FIXTURE_PDF.exists(), reason="本地样板 PDF 不在（已 gitignore）")
class TestHybridExtraction:
    @pytest.fixture(scope="class")
    def doc(self):
        from app.parse.pdf import PdfParser

        return PdfParser().parse(FIXTURE_PDF)

    def test_无warning即全组匹配成功(self, doc):
        assert doc.warnings == [], doc.warnings[:3]

    def test_页数不被硬编码(self, doc):
        assert doc.page_count == len(__import__("pypdf").PdfReader(str(FIXTURE_PDF)).pages)

    def test_水印与页码清干净(self, doc):
        t = doc.raw_text()
        assert "gzh" not in t
        assert not re.search(r"第\s*\d+\s*页", t)
        assert "共 50 页" not in t

    def test_英文无粘连与重复空格(self, doc):
        """layout 抽出的英文会重复空格、plain 抽的会粘词；混合后两者都不该有。"""
        for b in doc.blocks:
            if b.zh_ratio >= 0.3:
                continue
            assert not re.search(r"\S  +\S", b.text), b.text[:80]

    def test_结构完整(self, doc):
        st = B.scan(doc.blocks)
        pieces = B.to_pieces(st)
        assert len(st.areas) == 6
        assert len(pieces) == 12
        assert all(p.zh and p.en for p in pieces)

    def test_语言自检全过(self, doc):
        """⑦ 入库前必过的自检：中文字段≈全中文，英文字段≈无中文。"""
        for p in B.to_pieces(B.scan(doc.blocks)):
            assert cjk_ratio(p.zh) >= QUALITY_ZH_MIN, p.title
            assert cjk_ratio(p.en) <= QUALITY_EN_MAX, p.title

    def test_中文正文与参考逐字一致(self, doc):
        import json

        if not FIXTURE_REF.exists():
            pytest.skip("本地参考 JSON 不在（已 gitignore）")
        ref = json.loads(FIXTURE_REF.read_text(encoding="utf-8"))
        pieces = B.to_pieces(B.scan(doc.blocks))
        # 参考是 6 考区 × 2 范文，范文序列与 pieces 顺序一一对应
        want = [re.sub(r"\s+", "", f["zh"]) for e in ref for f in e["fws"]]
        assert len(want) == len(pieces)
        hit = sum(1 for w, p in zip(want, pieces, strict=True) if re.sub(r"\s+", "", p.zh) in w)
        assert hit == 12, f"仅 {hit}/12 篇中文正文与参考逐字一致"


class TestParseError:
    def test_缺文件(self):
        from app.parse.pdf import PdfParser

        with pytest.raises(ParseError) as ei:
            PdfParser().parse(Path("不存在.pdf"))
        assert ei.value.kind == "no_parser"
