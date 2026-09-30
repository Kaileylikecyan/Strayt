"""⑥ 通道 A（LLM 语义对齐）测试。

**不调外部 API**：用假 provider 顶掉 ``chat``，验证映射解析、校验、降级与
交叉校验。真实服务商的连通性由 ``tests/test_llm.py`` 负责。

重点守两条：

1. **模型碰不到正文**（AGENTS.md §7「卡片内容超出原文范围」是红线）。
   提示词只让模型输出编号，文本一律从原文取 —— 这里用「模型试图夹带改写文本」
   的输入来证明它无效。
2. **半对的映射比没对齐更糟**。宁可整块判失败降级到通道 B，也不放行。
"""

from __future__ import annotations

import json

import pytest
from app.align.base import LOW_CONFIDENCE, AlignError, AlignResult, PairDraft
from app.align.llm import (
    MAX_CHUNK,
    LlmAligner,
    LlmAlignerConfig,
    _agreement,
    _apply_agreement,
    _n_chunks,
    _parse_groups,
    _split_even,
    estimate_alignment_tokens,
)
from app.align.regular import RegularAligner
from app.llm.base import ChatMessage, ChatResult, LLMError, LLMErrorKind, LLMProvider, Usage

# ==========================================================================
# 假 provider
# ==========================================================================


class FakeProvider(LLMProvider):
    """按脚本回放模型输出。``script`` 用尽后回落到 ``default``。"""

    name = "deepseek"
    display_name = "假服务商"
    base_url = "https://example.invalid"
    supports_vision = False

    def __init__(self, script: list[str | Exception], default: str = "[]") -> None:
        super().__init__("sk-test", "deepseek-chat")
        self.script = list(script)
        self.default = default
        self.calls: list[list[ChatMessage]] = []
        self.prompts: list[str] = []

    async def chat(self, messages, *, max_tokens=None):
        self.calls.append(messages)
        self.prompts.append(messages[-1].content)
        item = self.script.pop(0) if self.script else self.default
        if isinstance(item, Exception):
            raise item
        return ChatResult(
            text=item, usage=Usage(prompt_tokens=100, completion_tokens=20), model=self.model
        )


def _perfect(n_zh: int, n_en: int) -> str:
    """生成一份合法的映射：两侧都尽量 1:1，短的一侧按连续块吸收。

    必须同时处理 ``英 > 中``（中文 1 组吃多句英文）和 ``中 > 英``
    （多句中文合成一组），否则会造出空组被校验拒掉。
    """
    out: list[dict[str, list[int]]] = []
    if n_en >= n_zh:
        base, extra = divmod(n_en, n_zh)
        e = 1
        for i in range(n_zh):
            take = base + (1 if i < extra else 0)
            out.append({"zh": [i + 1], "en": list(range(e, e + take))})
            e += take
    else:
        base, extra = divmod(n_zh, n_en)
        z = 1
        for i in range(n_en):
            take = base + (1 if i < extra else 0)
            out.append({"zh": list(range(z, z + take)), "en": [i + 1]})
            z += take
    return json.dumps(out, ensure_ascii=False)


# ==========================================================================
# 正常路径
# ==========================================================================


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_should_build_pairs_from_valid_mapping(self):
        p = FakeProvider([_perfect(4, 4)])
        r = await LlmAligner(p).align_async(
            ["甲一。", "甲二。", "甲三。", "甲四。"], ["E1.", "E2.", "E3.", "E4."], loc_page=9
        )
        assert r.used_llm is True
        assert r.mode == "llm"
        assert len(r.pairs) == 4
        assert r.dropped_zh == []
        assert r.coverage == 1.0
        assert {x.loc_page for x in r.pairs} == {9}

    @pytest.mark.asyncio
    async def test_should_take_text_from_source_not_from_model(self):
        """红线：即使模型夹带改写文本，落库的也必须是原文。"""
        p = FakeProvider([_perfect(2, 2)])
        r = await LlmAligner(p).align_async(
            ["原文甲一。", "原文甲二。"], ["SRC-E1.", "SRC-E2."], loc_page=1
        )
        joined_zh = "".join(x.zh for x in r.pairs)
        joined_en = " ".join(x.en for x in r.pairs)
        assert joined_zh == "原文甲一。原文甲二。"
        assert joined_en == "SRC-E1. SRC-E2."
        # 模型输出里不含任何正文，绝无可能被写进结果
        assert "改写" not in joined_zh and "改写" not in joined_en

    @pytest.mark.asyncio
    async def test_should_ask_model_for_indices_only(self):
        p = FakeProvider([_perfect(3, 3)])
        await LlmAligner(p).align_async(["甲。", "乙。", "丙。"], ["A.", "B.", "C."], loc_page=1)
        sys_msg = p.calls[0][0].content
        assert "只输出编号" in sys_msg
        assert "JSON" in sys_msg
        # 提示词里带编号列表
        assert "[1] 甲。" in p.prompts[0]

    @pytest.mark.asyncio
    async def test_should_accumulate_usage(self):
        p = FakeProvider([_perfect(2, 2)])
        r = await LlmAligner(p).align_async(["甲。", "乙。"], ["A.", "B."], loc_page=1)
        assert r.usage is not None
        assert r.usage.prompt_tokens == 100
        assert r.usage.completion_tokens == 20

    @pytest.mark.asyncio
    async def test_should_merge_two_english_into_one_chinese(self):
        mapping = json.dumps([{"zh": 1, "en": [1, 2]}, {"zh": 2, "en": [3]}])
        p = FakeProvider([mapping])
        r = await LlmAligner(p).align_async(["甲。", "乙。"], ["E1.", "E2.", "E3."], loc_page=1)
        assert len(r.pairs) == 2
        assert r.pairs[0].en == "E1. E2."


# ==========================================================================
# 分块
# ==========================================================================


class TestChunking:
    def test_n_chunks_must_fit_both_sides(self):
        # 75 中 / 54 英，块大小 30：中文要 3 块、英文要 2 块 → 取小的 3 块，
        # 切 3 块后每块中 25 英 18，都不超限，且中文不会丢块。
        assert _n_chunks(75, 54, 30) == 3
        assert _n_chunks(100, 100, 30) == 4
        assert _n_chunks(10, 14, 30) == 1
        assert _n_chunks(0, 5, 30) == 1

    def test_split_even_covers_everything(self):
        for n, k in ((75, 3), (54, 3), (10, 3), (7, 3), (3, 3)):
            parts = _split_even(list(range(n)), k)
            flat = [i for part, _ in parts for i in part]
            assert flat == list(range(n)), f"n={n} k={k} 丢了下标"
            assert len(parts) == k

    def test_split_even_keeps_chunks_contiguous(self):
        for part, off in _split_even(list(range(75)), 3):
            assert part == list(range(off, off + len(part)))

    @pytest.mark.asyncio
    async def test_should_not_lose_sentences_when_chunks_differ(self):
        """回归：早期用 zip() 切块，块数不等时短侧多出的块被静默丢弃。"""
        zh = [f"中{i}。" for i in range(75)]
        en = [f"E{i}." for i in range(54)]
        # 75/3 = 25 句一块，54/3 = 18 句一块
        p = FakeProvider([_perfect(25, 18)] * 3)
        r = await LlmAligner(p, LlmAlignerConfig(max_chunk=30)).align_async(zh, en, loc_page=1)
        assert r.used_llm is True
        assert len(p.calls) == 3
        assert r.dropped_zh == []
        assert r.dropped_en == []
        assert "".join(x.zh for x in r.pairs) == "".join(zh)

    @pytest.mark.asyncio
    async def test_offset_should_shift_chunk_indices_back_to_global(self):
        zh = [f"中{i}。" for i in range(60)]
        en = [f"E{i}." for i in range(60)]
        p = FakeProvider([_perfect(30, 30)] * 2)
        r = await LlmAligner(p, LlmAlignerConfig(max_chunk=30)).align_async(zh, en, loc_page=1)
        assert r.pairs[0].zh == "中0。"
        assert r.pairs[-1].zh == "中59。"
        assert r.pairs[-1].en.startswith("E5")


class TestCostEstimate:
    """F8 预估：只数 token 不调模型，且与真实块切/提示词布局同源。"""

    def test_should_return_zero_when_either_side_empty(self):
        assert estimate_alignment_tokens([], ["x."]) == (0, 0)
        assert estimate_alignment_tokens(["中。"], []) == (0, 0)
        assert estimate_alignment_tokens([], []) == (0, 0)

    def test_should_grow_with_chunk_count(self):
        small = estimate_alignment_tokens(
            [f"中{i}。" for i in range(60)], [f"E{i}." for i in range(60)]
        )
        big = estimate_alignment_tokens(
            [f"中{i}。" for i in range(200)], [f"E{i}." for i in range(200)]
        )
        assert small[0] > 0 and small[1] > 0
        assert _n_chunks(200, 200, MAX_CHUNK) > _n_chunks(60, 60, MAX_CHUNK)
        assert big[0] > small[0]
        assert big[1] > small[1]

    def test_should_track_real_block_layout(self):
        """块数必须与 ``_ask_model`` 用同一个 ``_n_chunks`` 算出来的一致。"""
        zh = [f"中{i}。" for i in range(75)]
        en = [f"E{i}." for i in range(54)]
        k = _n_chunks(len(zh), len(en), MAX_CHUNK)
        assert k == 3  # 75/30→3 块、54/30→2 块，取大的 3
        prompt, _comp = estimate_alignment_tokens(zh, en)
        # 多块预估应明显大于单块（210 句分会切成更多块）
        many = estimate_alignment_tokens(
            [f"中{i}。" for i in range(210)], [f"E{i}." for i in range(210)]
        )
        assert prompt > 0
        assert many[0] > prompt


def _count_labels(msg: str, _prefix: str) -> int:  # pragma: no cover - 占位
    return 0


# ==========================================================================
# 映射解析与校验
# ==========================================================================


class TestParseGroups:
    def test_should_strip_code_fence(self):
        raw = '```json\n[{"zh": 1, "en": [1]}]\n```'
        assert _parse_groups(raw, n_zh=1, n_en=1, block=1) == [([0], [0])]

    def test_should_tolerate_prose_around_json(self):
        raw = '好的，结果如下：\n[{"zh": 1, "en": [1]}]\n希望有帮助。'
        assert _parse_groups(raw, n_zh=1, n_en=1, block=1) == [([0], [0])]

    def test_should_accept_two_element_array_form(self):
        assert _parse_groups("[[1, [1, 2]]]", n_zh=1, n_en=2, block=1) == [([0], [0, 1])]

    def test_should_accept_scalar_english_index(self):
        assert _parse_groups('[{"zh": 1, "en": 1}]', n_zh=1, n_en=1, block=1) == [([0], [0])]

    def test_should_accept_alt_key_names(self):
        assert _parse_groups(
            '[{"zh_indices": [1], "en_indices": [1]}]', n_zh=1, n_en=1, block=1
        ) == [([0], [0])]

    @pytest.mark.parametrize(
        "raw,kind",
        [
            ('[{"zh": 1, "en": [3]}]', "llm_out_of_range"),  # 英文越界（n_en=2）
            ('[{"zh": 3, "en": [1]}]', "llm_out_of_range"),  # 中文越界（n_zh=2）
            ('[{"zh": 2, "en": [1]}]', "llm_incomplete"),  # 漏了中文 1
            ('[{"zh": 1, "en": [1]}, {"zh": 1, "en": [2]}]', "llm_not_monotonic"),  # 中文重复
            ('[{"zh": 1, "en": [2]}, {"zh": 2, "en": [1]}]', "llm_not_monotonic"),  # 英文乱序
            ('[{"zh": 1, "en": [1]}, {"zh": 2, "en": [1]}]', "llm_not_monotonic"),  # 英文复用
            ('[{"zh": 1, "en": []}]', "llm_bad_shape"),  # 空组
            ('["随便"]', "llm_bad_shape"),
            ("不是 JSON", "llm_bad_json"),
            ("{", "llm_bad_json"),
            ("[]", "llm_empty"),
        ],
    )
    def test_should_reject_broken_mapping(self, raw: str, kind: str):
        with pytest.raises(AlignError) as ei:
            _parse_groups(raw, n_zh=2, n_en=2, block=1)
        assert ei.value.kind == kind

    def test_should_reject_non_contiguous_group(self):
        with pytest.raises(AlignError) as ei:
            _parse_groups('[{"zh": [1, 3], "en": [1, 2]}]', n_zh=3, n_en=2, block=1)
        assert ei.value.kind in ("llm_not_contiguous", "llm_incomplete")

    def test_should_reject_oversize_group(self):
        raw = json.dumps([{"zh": [1, 2, 3, 4, 5], "en": [1, 2, 3, 4, 5]}])
        with pytest.raises(AlignError) as ei:
            _parse_groups(raw, n_zh=5, n_en=5, block=1)
        assert ei.value.kind == "llm_oversize"

    def test_should_accept_contiguous_merge(self):
        raw = json.dumps([{"zh": [1, 2], "en": [1]}])
        assert _parse_groups(raw, n_zh=2, n_en=1, block=1) == [([0, 1], [0])]


# ==========================================================================
# 降级
# ==========================================================================


class TestFallback:
    @pytest.mark.asyncio
    async def test_should_fall_back_to_regular_on_provider_error(self):
        p = FakeProvider([LLMError(LLMErrorKind.KEY_INVALID, "401")])
        r = await LlmAligner(p).align_async(["甲一。", "甲二。"], ["E1.", "E2."], loc_page=1)
        assert r.used_llm is False
        assert r.mode == "regular"
        assert len(r.pairs) == 2
        assert "降级" in r.warnings[0]

    @pytest.mark.asyncio
    async def test_should_fall_back_on_broken_json(self):
        p = FakeProvider(["完全不是 JSON"])
        r = await LlmAligner(p, LlmAlignerConfig(max_retries=0)).align_async(
            ["甲一。", "甲二。"], ["E1.", "E2."], loc_page=1
        )
        assert r.used_llm is False
        assert r.dropped_zh == []

    @pytest.mark.asyncio
    async def test_should_keep_partial_usage_after_fallback(self):
        p = FakeProvider([LLMError(LLMErrorKind.QUOTA_EXCEEDED, "欠费")])
        r = await LlmAligner(p).align_async(["甲。"], ["E1."], loc_page=1)
        assert r.usage is not None
        assert r.usage.total == 0  # 请求就失败了，没消耗

    @pytest.mark.asyncio
    async def test_should_raise_on_both_sides_empty(self):
        p = FakeProvider([])
        with pytest.raises(AlignError) as ei:
            await LlmAligner(p).align_async(["  "], ["\n"], loc_page=1)
        assert ei.value.kind == "empty"

    def test_sync_align_should_refuse(self):
        p = FakeProvider([])
        with pytest.raises(AlignError) as ei:
            LlmAligner(p).align(["甲。"], ["E1."], loc_page=1)
        assert ei.value.kind == "needs_async"

    @pytest.mark.asyncio
    async def test_should_retry_transient_error_once(self):
        p = FakeProvider(
            [
                LLMError(LLMErrorKind.RATE_LIMITED, "429"),
                _perfect(2, 2),
            ]
        )
        r = await LlmAligner(p, LlmAlignerConfig(max_retries=1)).align_async(
            ["甲。", "乙。"], ["E1.", "E2."], loc_page=1
        )
        assert r.used_llm is True
        assert len(p.calls) == 2

    @pytest.mark.asyncio
    async def test_should_not_retry_permanent_error(self):
        p = FakeProvider([LLMError(LLMErrorKind.KEY_INVALID, "401")] * 3)
        r = await LlmAligner(p, LlmAlignerConfig(max_retries=2)).align_async(
            ["甲。"], ["E1."], loc_page=1
        )
        assert r.used_llm is False
        assert len(p.calls) == 1  # Key 错重试无意义，不该烧请求

    @pytest.mark.asyncio
    async def test_should_retry_after_json_parse_failure(self):
        p = FakeProvider(["坏的", _perfect(2, 2)])
        r = await LlmAligner(p, LlmAlignerConfig(max_retries=1)).align_async(
            ["甲。", "乙。"], ["E1.", "E2."], loc_page=1
        )
        assert r.used_llm is True
        assert "无法解析" in p.calls[1][-1].content


# ==========================================================================
# 预算
# ==========================================================================


class TestBudget:
    @pytest.mark.asyncio
    async def test_should_refuse_when_budget_exceeded(self):
        p = FakeProvider([_perfect(2, 2)])
        cfg = LlmAlignerConfig(budget_cny=0.0000001)
        r = await LlmAligner(p, cfg).align_async(["甲。" * 400], ["E." * 700], loc_page=1)
        assert r.used_llm is False  # 超预算 → 降级，不烧额度
        assert len(p.calls) == 0

    @pytest.mark.asyncio
    async def test_should_call_model_when_budget_is_generous(self):
        p = FakeProvider([_perfect(2, 2)])
        cfg = LlmAlignerConfig(budget_cny=10.0)
        r = await LlmAligner(p, cfg).align_async(["甲。", "乙。"], ["E1.", "E2."], loc_page=1)
        assert r.used_llm is True
        assert len(p.calls) == 1


# ==========================================================================
# 交叉校验
# ==========================================================================


def _pairs(*specs: tuple[int, int]) -> list[PairDraft]:
    return [PairDraft(seq=i, zh="。" * z, en="." * e, loc_page=1) for i, (z, e) in enumerate(specs)]


class TestAgreement:
    def test_identical_structure_scores_one(self):
        a = _pairs((1, 1), (1, 1), (1, 1))
        assert _agreement(a, a) == 1.0

    def test_disjoint_boundaries_score_zero(self):
        a = _pairs((1, 1), (1, 1), (1, 1))  # 切点 {1,2,3}
        b = _pairs((7, 1), (7, 1))  # 切点 {7,14}
        assert _agreement(a, b) == 0.0

    def test_should_penalise_under_segmentation(self):
        """回归：只算「A 的切点有多少落在 B 里」的话，切得稀反而拿满分。

        A 切点 {2,4,6} 全落在 B 的 {1..6} 里 → 覆盖率 1.0，等于奖励漏切。
        F1 要把 B 多出来的 3 个切点算进分母。
        """
        coarse = _pairs((2, 1), (2, 1), (2, 1))  # 切点 {2,4,6}
        fine = _pairs((1, 1), (1, 1), (1, 1), (1, 1), (1, 1), (1, 1))  # 切点 {1..6}
        v = _agreement(coarse, fine)
        assert 0.0 < v < 1.0
        assert v == pytest.approx(2 * 1.0 * 0.5 / 1.5)

    def test_should_penalise_over_segmentation(self):
        fine = _pairs((1, 1), (1, 1), (1, 1), (1, 1), (1, 1), (1, 1))
        coarse = _pairs((2, 1), (2, 1), (2, 1))
        assert _agreement(fine, coarse) == pytest.approx(_agreement(coarse, fine))

    def test_partial_overlap_is_between(self):
        a = _pairs((1, 1), (1, 1), (1, 1), (1, 1))
        b = _pairs((1, 1), (1, 1), (2, 1), (2, 1))
        v = _agreement(a, b)
        assert 0.0 < v < 1.0

    def test_boundary_metric_is_offset_invariant(self):
        """回归：早期按同下标比分组签名，对数差一个就整体归零。"""
        a = _pairs((1, 1), (1, 1), (1, 1), (1, 1))
        b = _pairs((1, 1), (1, 1), (1, 1), (1, 1), (1, 1))  # 通道 B 多切一刀
        assert _agreement(a, b) > 0.8  # 4 个切点里 3 个对得上

    def test_empty_input_scores_zero(self):
        assert _agreement([], _pairs((1, 1))) == 0.0
        assert _agreement(_pairs((1, 1)), []) == 0.0

    def test_full_agreement_gives_full_confidence(self):
        res = AlignResult(pairs=_pairs((1, 1), (1, 1)))
        _apply_agreement(res, 1.0, 0.6)
        assert all(p.confidence == 1.0 for p in res.pairs)

    def test_low_agreement_drops_below_review_threshold(self):
        res = AlignResult(pairs=_pairs((1, 1), (1, 1)))
        _apply_agreement(res, 0.2, 0.6)
        assert all(p.confidence < LOW_CONFIDENCE for p in res.pairs)

    def test_mid_agreement_is_monotonic(self):
        prev = -1.0
        for a in (0.6, 0.7, 0.8, 0.9, 1.0):
            res = AlignResult(pairs=_pairs((1, 1)))
            _apply_agreement(res, a, 0.6)
            assert res.pairs[0].confidence > prev
            prev = res.pairs[0].confidence

    @pytest.mark.asyncio
    async def test_should_flag_disagreement_with_regular(self):
        """模型把中文两两合并（合法映射），但等长输入下通道 B 必然给 1:1。

        一致度不会掉到 0 —— 通道 A 的 3 个切点 ``{2,4,6}`` 全落在通道 B 的
        ``{1..6}`` 里，只是 B 多切了 3 刀。F1 ≈ 0.67，低于下限 0.8，仍要报出来。
        """
        two_by_two = json.dumps(
            [
                {"zh": [1, 2], "en": [1, 2]},
                {"zh": [3, 4], "en": [3, 4]},
                {"zh": [5, 6], "en": [5, 6]},
            ]
        )
        p = FakeProvider([two_by_two])
        r = await LlmAligner(p).align_async(
            [f"中{i}。" for i in range(6)], [f"E{i}." for i in range(6)], loc_page=1
        )
        assert r.used_llm is True  # 映射合法，不该降级
        assert len(r.pairs) == 3
        assert r.agreement is not None
        assert r.agreement < LlmAlignerConfig().min_agreement
        assert any("一致度" in w for w in r.warnings)
        assert all(x.confidence < LOW_CONFIDENCE for x in r.pairs)

    @pytest.mark.asyncio
    async def test_should_agree_with_regular_on_clean_input(self):
        zs = ["甲" * 40 + "。"] * 6
        es = ["En" * 69 + "."] * 6  # 只有一个句号 = 1 句
        p = FakeProvider([_perfect(6, 6)])
        r = await LlmAligner(p).align_async(zs, es, loc_page=1)
        assert r.agreement == 1.0
        assert all(x.confidence >= LOW_CONFIDENCE for x in r.pairs)
        assert not any(x.needs_review for x in r.pairs)


class TestAgreementWithRealAligner:
    @pytest.mark.asyncio
    async def test_regular_still_works_as_fallback(self):
        """降级路径必须给出与通道 A 同形状的结果，否则 ⑦ 口径不一致。"""
        r = RegularAligner().align(["甲。" * 20] * 3, ["En." * 70] * 3, loc_page=1)
        assert len(r.pairs) == 3
        assert all(p.loc_page == 1 for p in r.pairs)
