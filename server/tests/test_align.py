"""⑥ 对齐层测试。

重点是**句数不等**这一条不变量（AGENTS.md §6）：样板 12/12 篇中英句数都不同，
按索引 ``zip()`` 配对必然整体错位，而且错位后每对仍是通顺中英文、肉眼看不出，
只有背诵时才发现内容对不上。所以每个用例都用不等长输入。
"""

from __future__ import annotations

import re

import pytest
from app.align.base import (
    LOW_CONFIDENCE,
    MIN_COVERAGE,
    Aligner,
    AlignError,
    AlignResult,
    PairDraft,
    count_sentences,
)
from app.align.regular import MAX_GROUP, MERGE_PENALTY, RegularAligner

_ZH_LETTERS = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"


def zh_at(i: int, size: int = 41) -> str:
    """第 i 个中文句，**恰好 size 个字符**，且各句内容互不相同。"""
    head = f"中{i:05d}"
    return head + "甲" * (size - len(head) - 1) + "。"


def en_at(i: int, size: int = 141) -> str:
    """第 i 个英文句，**恰好 size 个字符**，且各句内容互不相同。

    默认 141/41 ≈ 3.4，贴近样板实测的中英整体长度比。
    """
    head = f"en{i:05d}"
    return head + "e" * (size - len(head) - 1) + "."


def zh_n(n: int, *, size: int = 41) -> list[str]:
    return [zh_at(i, size) for i in range(n)]


def en_n(n: int, *, size: int = 141) -> list[str]:
    return [en_at(i, size) for i in range(n)]


class TestEqualCounts:
    def test_should_pair_each_sentence_directly(self):
        r = RegularAligner().align(zh_n(10), en_n(10), loc_page=3)
        assert len(r.pairs) == 10
        assert all(p.how == "direct" for p in r.pairs)
        assert r.coverage == 1.0
        assert r.dropped_en == []

    def test_should_keep_page_on_every_pair(self):
        r = RegularAligner().align(zh_n(4), en_n(4), loc_page=17)
        assert {p.loc_page for p in r.pairs} == {17}

    def test_should_number_pairs_from_zero(self):
        r = RegularAligner().align(zh_n(6), en_n(6), loc_page=1)
        assert [p.seq for p in r.pairs] == list(range(6))


class TestUnequalCounts:
    @pytest.mark.parametrize("nzh,nen", [(10, 14), (10, 11), (10, 18), (25, 31), (40, 55)])
    def test_should_emit_one_pair_per_chinese_sentence(self, nzh: int, nen: int):
        """等长句是对齐器最难的情形（长度信号完全无噪声，DP 最容易退化并组）。

        理想输出是对数 = 中文句数：多余的英文句以少数 1:2 吸收。
        """
        r = RegularAligner().align(zh_n(nzh), en_n(nen), loc_page=1)
        assert len(r.pairs) == nzh, f"{nzh}中/{nen}英 应得 {nzh} 对，实得 {len(r.pairs)}"
        assert r.coverage == 1.0
        assert r.dropped_zh == []
        assert r.dropped_en == []

    @pytest.mark.parametrize("nzh,nen", [(14, 10), (18, 10), (31, 25), (55, 40)])
    def test_should_emit_one_pair_per_english_sentence_when_chinese_is_longer(
        self, nzh: int, nen: int
    ):
        r = RegularAligner().align(zh_n(nzh), en_n(nen), loc_page=1)
        assert len(r.pairs) == nen
        assert r.coverage == 1.0
        assert r.dropped_zh == []

    @pytest.mark.parametrize(
        "nzh,nen", [(10, 14), (14, 10), (10, 11), (25, 31), (40, 55), (55, 40)]
    )
    def test_should_never_drop_content(self, nzh: int, nen: int):
        r = RegularAligner().align(zh_n(nzh), en_n(nen), loc_page=1)
        assert r.dropped_zh == []
        assert r.dropped_en == []
        assert r.coverage >= MIN_COVERAGE

    def test_should_concat_every_english_sentence_exactly_once(self):
        """内容守恒：不能吞句，也不能重复。

        并组时英文句之间会插入一个空格（每句自带句号），所以按句拆分后比对
        集合，而不是直接拼字符串。
        """
        zs, es = zh_n(12), en_n(17)
        r = RegularAligner().align(zs, es, loc_page=1)
        got = {s.rstrip(".") for p in r.pairs for s in p.en.split(". ")}
        assert got == {s.rstrip(".") for s in es}
        # 中文并组时不插空格，可以直接拼
        assert "".join(p.zh for p in r.pairs) == "".join(zs)

    def test_should_report_more_english_merged_than_chinese(self):
        r = RegularAligner().align(zh_n(10), en_n(14), loc_page=1)
        assert any(p.how == "merged" for p in r.pairs)
        assert any(p.how == "direct" for p in r.pairs)


class TestMonotonicity:
    """单条约束：译文顺序不乱序。"""

    @staticmethod
    def _mk_zh(n: int) -> list[str]:
        return [f"第{i}号中文{'零' * 30}。" for i in range(1, n + 1)]

    @staticmethod
    def _mk_en(n: int) -> list[str]:
        return [f"SENT{i:03d} " * 20 for i in range(1, n + 1)]

    def test_should_never_cross_over(self):
        r = RegularAligner().align(self._mk_zh(15), self._mk_en(20), loc_page=1)
        firsts = [min(int(x) for x in re.findall(r"SENT(\d{3})", p.en)) for p in r.pairs]
        assert firsts == sorted(firsts)

    def test_should_not_repeat_english_indices_across_pairs(self):
        r = RegularAligner().align(self._mk_zh(15), self._mk_en(20), loc_page=1)
        seen: list[int] = []
        for p in r.pairs:
            seen.extend(int(x) for x in re.findall(r"SENT(\d{3})", p.en))
        # 每句英文只应被吸收到一处（构造里每句重复 20 次，用集合去重后计数）
        per_pair = [len(set(re.findall(r"SENT(\d{3})", p.en))) for p in r.pairs]
        assert sum(per_pair) == 20
        assert all(n >= 1 for n in per_pair)

    def test_should_pair_in_source_order(self):
        r = RegularAligner().align(self._mk_zh(12), self._mk_en(18), loc_page=1)
        zh_idx = [int(re.search(r"第(\d+)号", p.zh).group(1)) for p in r.pairs]
        assert zh_idx == sorted(zh_idx)
        assert zh_idx[0] == 1
        assert zh_idx[-1] == 12


class TestCountSentences:
    def test_should_count_cjk_terminators(self):
        assert count_sentences("你好。世界！好吗？") == 3

    def test_should_count_english_terminators(self):
        assert count_sentences("Hello there. How are you? Fine!") == 3

    def test_should_ignore_decimal_point(self):
        """样板中文满是「6340.5 平方千米」「占地 2.5 平方公里」。

        不排掉小数点，1 句会被算成 2 句，长度比基准随之压偏，
        实测会多出十几个假告警。
        """
        assert count_sentences("上海的面积约 6340.5 平方千米。") == 1
        assert count_sentences("占地 2.5 平方公里。") == 1
        assert count_sentences("Version 3.14 released.") == 1

    def test_should_still_count_period_after_digit_at_end(self):
        assert count_sentences("区域 1. 上海很大。") == 2

    def test_should_return_at_least_one(self):
        assert count_sentences("") == 1
        assert count_sentences("   ") == 1
        assert count_sentences("没有句号的残句") == 1


class TestLengthRatioCalibration:
    def test_should_expose_overall_ratio_of_the_piece(self):
        r = RegularAligner().align(zh_n(12, size=41), en_n(12, size=141), loc_page=1)
        assert r.expected_ratio == pytest.approx(141 / 41, rel=0.01)

    def test_should_not_flag_wide_zh_en_ratio_as_misaligned(self):
        """样板实测中英整体比约 1:3.4。若按绝对阈值判定，全篇都会被误报为错位。"""
        r = RegularAligner().align(zh_n(12, size=41), en_n(12, size=141), loc_page=1)
        assert [p.needs_review for p in r.pairs] == [False] * 12
        assert all(p.confidence >= LOW_CONFIDENCE for p in r.pairs)

    def test_should_absorb_an_outlier_sentence_into_a_neighbour(self):
        """混入一条 2 字短句时，DP 应该把它并进邻组，而不是留一个畸形对。

        孤立它的话长度比是 141/2 = 70（局部代价 ~2.9），并组只要 0.4 罚分。
        所以正确行为是并组 —— 不会出现 ``needs_review``。
        """
        zs = zh_n(9)
        zs[4] = "好。"
        r = RegularAligner().align(zs, en_n(9), loc_page=1)
        assert r.coverage == 1.0
        assert not any(p.needs_review for p in r.pairs)

    def test_should_flag_pair_far_off_its_own_piece_ratio(self):
        """直接构造结果来测闸门本身（走 DP 的话异常对会被并组吸收，测不到）。"""
        res = AlignResult(
            pairs=[
                PairDraft(seq=0, zh=zh_n(1)[0], en=en_n(1)[0], loc_page=1),
                PairDraft(seq=1, zh="好。", en=en_n(1)[0], loc_page=1),
            ]
        )
        out = RegularAligner._finalize(res)
        assert out.pairs[0].needs_review is False
        assert out.pairs[1].needs_review is True
        assert out.pairs[1].confidence < LOW_CONFIDENCE

    def test_should_flag_one_sided_pair(self):
        res = AlignResult(pairs=[PairDraft(seq=0, zh="中文。", en="", loc_page=1)])
        out = RegularAligner._finalize(res)
        assert out.pairs[0].needs_review is True

    def test_should_scale_baseline_by_sentence_count_of_group(self):
        """``1 中 ↔ 3 英`` 的长度比天然是整篇比的 3 倍，不能因此判为错位。

        三句英文必须是正常句长（样板实测英文句均 130 字）。若拿三条 60 字的
        短句凑成 ``1↔3``，比值对不上基准，闸门判它异常是对的 —— 那组英文
        确实比本篇平均水平短了一半。
        """
        en1 = "En" * 64 + "."  # 129 字符，1 句
        en3 = en1 * 3  # 387 字符，3 句
        res = AlignResult(
            pairs=[
                PairDraft(seq=0, zh="甲" * 40 + "。", en=en3, loc_page=1),
                *[PairDraft(seq=i, zh="乙" * 40 + "。", en=en1, loc_page=1) for i in range(1, 5)],
            ]
        )
        out = RegularAligner._finalize(res)
        assert out.pairs[0].needs_review is False

    def test_should_flag_when_group_length_blows_past_scaled_baseline(self):
        en1 = "En" * 64 + "."
        res = AlignResult(
            pairs=[
                PairDraft(
                    seq=0, zh="甲" * 40 + "。", en="En" * 200 + ".", loc_page=1
                ),  # 1↔1 却长 3 倍
                *[PairDraft(seq=i, zh="乙" * 40 + "。", en=en1, loc_page=1) for i in range(1, 5)],
            ]
        )
        out = RegularAligner._finalize(res)
        assert out.pairs[0].needs_review is True
        assert out.pairs[1].needs_review is False


class TestConfidence:
    def test_direct_pair_should_keep_high_confidence(self):
        r = RegularAligner().align(zh_n(8), en_n(8), loc_page=1)
        assert all(p.confidence >= LOW_CONFIDENCE for p in r.pairs)

    def test_merged_pair_should_score_below_direct(self):
        r = RegularAligner().align(zh_n(10), en_n(14), loc_page=1)
        direct = [p.confidence for p in r.pairs if p.how == "direct"]
        merged = [p.confidence for p in r.pairs if p.how == "merged"]
        assert max(merged) < max(direct)


class TestEdgeCases:
    def test_should_raise_on_both_sides_empty(self):
        with pytest.raises(AlignError) as ei:
            RegularAligner().align([], [], loc_page=1)
        assert ei.value.kind == "empty"

    def test_should_raise_when_only_whitespace(self):
        with pytest.raises(AlignError):
            RegularAligner().align(["   "], ["\n\t"], loc_page=1)

    def test_should_report_chinese_only_as_unpaired(self):
        r = RegularAligner().align(zh_n(5), [], loc_page=1)
        assert r.pairs == []
        assert r.coverage == 0.0  # 一对都没配上 → 覆盖率 0，⑦ 会据此拦下整篇
        assert len(r.dropped_zh) == 5
        assert r.warnings

    def test_should_report_english_only_as_unpaired(self):
        r = RegularAligner().align([], en_n(5), loc_page=1)
        assert r.pairs == []
        assert len(r.dropped_en) == 5
        assert r.warnings

    def test_should_ignore_blank_and_whitespace_segments(self):
        r = RegularAligner().align(
            ["  ", "中文一句话。", "\n"], ["", "An English one."], loc_page=1
        )
        assert len(r.pairs) == 1
        assert r.pairs[0].zh == "中文一句话。"
        assert r.pairs[0].en == "An English one."

    def test_max_group_should_bound_the_merge(self):
        assert MAX_GROUP >= 2

    def test_merge_penalty_must_exceed_legitimate_1x2_local_cost(self):
        """守卫实测调出来的常量：罚分太小会让 DP 为了比例完美而并组。"""
        assert MERGE_PENALTY >= 0.3


class TestResultContract:
    def test_coverage_of_fresh_result_is_zero(self):
        assert AlignResult().coverage == 0.0
        assert AlignResult().expected_ratio == 0.0

    def test_abstract_aligner_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            Aligner()  # type: ignore[abstract]

    def test_pair_draft_ratio_handles_empty_chinese(self):
        assert PairDraft(seq=0, zh="", en="abc", loc_page=1).length_ratio == 0.0
