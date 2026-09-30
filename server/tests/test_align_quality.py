"""⑦ 质量自检（``app.align.quality``）单元测试。

重点在**反向用例**：真实范文 12 篇实测全部 ``ok``（见 ``test_quality_e2e.py``
与 ``var_test/quality_e2e.py``），所以这里主要验「坏数据必须被拦住」——
一个从不误杀真数据的闸门和一个从不放行坏数据的闸门同样没用。
"""

from __future__ import annotations

import pytest
from app.align.base import AlignResult, PairDraft
from app.align.quality import (
    MAX_ONE_SIDED_ABS,
    MAX_ONE_SIDED_RATIO,
    QualityReport,
    Verdict,
    check,
)

ZH_SENT = "上海的快速发展吸引了大批投资者前来兴业。"
EN_SENT = "The rapid development of Shanghai has attracted investors."


def _pairs(n: int = 8, **kw) -> list[PairDraft]:
    """造 n 对「正常」段落对，刻意让长度有变化（否则会触发均匀性告警）。"""
    out = []
    for i in range(n):
        pad = "长" * (i % 5 * 7)
        en_pad = "En" * (i % 5 * 20)
        out.append(
            PairDraft(
                seq=i,
                zh=ZH_SENT + pad + f"第{i}段。",
                en=EN_SENT + en_pad + f" Paragraph {i}.",
                loc_page=1,
                **kw,
            )
        )
    return out


def _res(**kw) -> AlignResult:
    pairs = kw.pop("pairs", None)
    if pairs is None:
        pairs = _pairs()
    return AlignResult(pairs=pairs, **kw)


class Test正常数据:
    def test__should_pass_clean_pairs(self):
        rep = check(_res())
        assert rep.verdict is Verdict.OK
        assert rep.issues == []
        assert rep.ok is True
        assert rep.score == pytest.approx(1.0, abs=0.01)

    def test_should_expose_messages_and_errors(self):
        rep = check(_res(dropped_zh=["丢了的中文"]))
        assert rep.messages
        assert rep.errors and rep.errors[0].code == "dropped_zh"

    def test_should_round_report_to_dict(self):
        d = check(_res()).to_dict()
        assert d["verdict"] == "ok"
        assert d["ok"] is True
        assert d["issues"] == []
        assert isinstance(d["score"], float)


class Test拒收:
    """以下每一类都必须**整篇拒收**（``error`` 级）。"""

    def test_should_reject_when_all_segments_dropped(self):
        rep = check(_res(dropped_en=["x"] * 3))
        assert rep.verdict is Verdict.REJECTED
        assert rep.ok is False

    def test_should_reject_swapped_fields(self):
        """中英写反：zh 栏是英文、en 栏是中文。"""
        pairs = _pairs()
        pairs[2] = PairDraft(seq=2, zh=EN_SENT, en=ZH_SENT, loc_page=1)
        rep = check(_res(pairs=pairs))
        assert rep.verdict is Verdict.REJECTED
        assert "swapped" in {i.code for i in rep.issues}

    def test_should_reject_english_field_with_chinese(self):
        # 中文必须多到超过 QUALITY_EN_MAX(0.2) 才会触发；只掺一句是不够的
        pairs = _pairs()
        pairs[1] = PairDraft(
            seq=1, zh=ZH_SENT, en="这是中文，这是中文，这是中文，这是中文。" + EN_SENT, loc_page=1
        )
        rep = check(_res(pairs=pairs))
        assert rep.verdict is Verdict.REJECTED
        assert "en_field_has_zh" in {i.code for i in rep.issues}

    def test_should_tolerate_trace_chinese_in_english(self):
        """英文栏里偶尔夹一个中文专名是正常的，不能因此拒收整篇。"""
        pairs = _pairs()
        pairs[1] = PairDraft(seq=1, zh=ZH_SENT, en="这是中文。" + EN_SENT, loc_page=1)
        rep = check(_res(pairs=pairs))
        assert rep.verdict is Verdict.OK

    def test_should_reject_chinese_field_without_chinese(self):
        pairs = _pairs()
        pairs[3] = PairDraft(seq=3, zh=EN_SENT * 2, en=EN_SENT, loc_page=1)
        rep = check(_res(pairs=pairs))
        assert rep.verdict is Verdict.REJECTED
        assert "zh_field_not_zh" in {i.code for i in rep.issues}

    def test_should_reject_no_pairs(self):
        rep = check(AlignResult(pairs=[]))
        assert rep.verdict is Verdict.REJECTED
        assert rep.score == 0.0

    def test_should_reject_when_every_pair_is_empty(self):
        pairs = [PairDraft(seq=i, zh="", en="", loc_page=1) for i in range(4)]
        rep = check(_res(pairs=pairs))
        assert rep.verdict is Verdict.REJECTED
        assert "empty_pair" in {i.code for i in rep.issues}

    def test_should_reject_above_one_sided_ratio(self):
        """单侧为空超过 ``MAX_ONE_SIDED_ABS`` 或占比超限就是硬错误。"""
        pairs = _pairs(n=20)
        for i in range(4):
            pairs[i] = PairDraft(seq=i, zh=ZH_SENT, en="", loc_page=1)
        rep = check(_res(pairs=pairs))
        assert rep.verdict is Verdict.REJECTED
        assert "one_sided" in {i.code for i in rep.issues}

    def test_should_tolerate_single_one_sided_pair(self):
        """真实篇目只有 45~66 对，一对单侧就占 1.5%~2.2%。单个必须放行 ——
        纯比例卡 0.5% 会让「有一对单侧」直接整篇拒收，太脆。"""
        pairs = _pairs(n=66)
        pairs[0] = PairDraft(seq=0, zh=ZH_SENT, en="", loc_page=1)
        rep = check(_res(pairs=pairs))
        assert rep.verdict is Verdict.DEGRADED
        assert "one_sided" in {i.code for i in rep.issues}

    def test_should_reject_one_sided_in_tiny_piece(self):
        """比例兜底：10 对里有 1 对单侧（10%）就该报。"""
        pairs = _pairs(n=10)
        pairs[0] = PairDraft(seq=0, zh=ZH_SENT, en="", loc_page=1)
        rep = check(_res(pairs=pairs))
        assert "one_sided" in {i.code for i in rep.issues}
        assert rep.issues[0].level == "error"


class Test降级但入库:
    def test_should_degrade_on_low_confidence(self):
        pairs = _pairs()
        pairs[4].needs_review = True
        pairs[4].confidence = 0.2
        rep = check(_res(pairs=pairs))
        assert rep.verdict is Verdict.DEGRADED
        assert rep.ok is True
        assert "low_confidence" in {i.code for i in rep.issues}

    def test_should_degrade_on_low_agreement(self):
        rep = check(_res(used_llm=True, agreement=0.5), min_agreement=0.8)
        assert rep.verdict is Verdict.DEGRADED
        assert "low_agreement" in {i.code for i in rep.issues}

    def test_should_pass_high_agreement(self):
        rep = check(_res(used_llm=True, agreement=0.95), min_agreement=0.8)
        assert "low_agreement" not in {i.code for i in rep.issues}

    def test_should_ignore_agreement_when_llm_unused(self):
        """只走通道 B 时 ``agreement`` 是 ``None``，不该报一致度告警。"""
        rep = check(_res(used_llm=False, agreement=None))
        assert "low_agreement" not in {i.code for i in rep.issues}

    def test_should_surface_align_warnings(self):
        rep = check(_res(warnings=["通道 A 降级为长度直配"]))
        assert "align" in {i.code for i in rep.issues}
        assert "降级" in rep.messages[0]

    def test_should_degrade_on_degenerate_length_distribution(self):
        """长度几乎全一样的退化分段。"""
        pairs = [PairDraft(seq=i, zh=ZH_SENT, en=EN_SENT, loc_page=1) for i in range(10)]
        rep = check(_res(pairs=pairs))
        assert rep.verdict is Verdict.DEGRADED
        assert "length_uniform" in {i.code for i in rep.issues}

    def test_should_degrade_on_length_spread(self):
        pairs = _pairs(n=12)
        pairs[0] = PairDraft(seq=0, zh="短。" * 2, en="Tin.", loc_page=1)
        rep = check(_res(pairs=pairs))
        assert "length_spread" in {i.code for i in rep.issues} or rep.verdict is Verdict.DEGRADED

    def test_should_degrade_on_short_pair(self):
        pairs = _pairs(n=8)
        pairs[0] = PairDraft(seq=0, zh="嗯。", en="Eh.", loc_page=1)
        rep = check(_res(pairs=pairs))
        assert "too_short" in {i.code for i in rep.issues}

    def test_should_degrade_on_sentence_spike(self):
        """一對里塞了 8 句英文而中文只有 1 句。"""
        pairs = _pairs(n=8)
        pairs[2] = PairDraft(seq=2, zh=ZH_SENT, en=" ".join(["Sentence here."] * 8), loc_page=1)
        rep = check(_res(pairs=pairs))
        assert "sentence_spike" in {i.code for i in rep.issues}

    def test_should_degrade_on_length_outlier(self):
        pairs = _pairs(n=8)
        pairs[2] = PairDraft(seq=2, zh=ZH_SENT, en=EN_SENT * 30, loc_page=1)
        rep = check(_res(pairs=pairs))
        assert "length_outlier" in {i.code for i in rep.issues}


class Test分数:
    def test_should_penalise_drop_rate(self):
        assert check(_res()).score > check(_res(dropped_zh=["x"])).score

    def test_should_penalise_disagreement(self):
        assert (
            check(_res(used_llm=True, agreement=1.0)).score
            > check(_res(used_llm=True, agreement=0.5)).score
        )

    def test_should_be_bounded_in_unit_range(self):
        for kw in ({}, {"used_llm": True, "agreement": 0.0}, {"dropped_zh": ["x"] * 9}):
            s = check(_res(**kw)).score
            assert 0.0 <= s <= 1.0, kw

    def test_should_be_zero_for_empty_result(self):
        assert check(AlignResult(pairs=[])).score == 0.0


class Test边界:
    def test_should_handle_non_finite_confidence(self):
        pairs = _pairs()
        pairs[0].confidence = float("nan")
        rep = check(_res(pairs=pairs))
        assert 0.0 <= rep.score <= 1.0

    def test_should_survive_few_pairs(self):
        """少于 5 对时分位数检查没有意义，必须直接跳过而不是崩。"""
        for n in (1, 2, 3, 4):
            rep = check(_res(pairs=_pairs(n=n)))
            assert rep.verdict is not None

    def test_should_not_flag_uniformity_on_tiny_sample(self):
        pairs = [PairDraft(seq=i, zh=ZH_SENT, en=EN_SENT, loc_page=1) for i in range(3)]
        rep = check(_res(pairs=pairs))
        assert "length_uniform" not in {i.code for i in rep.issues}

    def test_should_expose_one_sided_ratio_threshold(self):
        assert 0 < MAX_ONE_SIDED_RATIO < 0.1
        assert 0 < MAX_ONE_SIDED_ABS <= 5

    def test_report_is_dataclass_with_verdict(self):
        rep = QualityReport(verdict=Verdict.OK)
        assert rep.ok is True
        assert rep.to_dict()["verdict"] == "ok"
