"""F11 图谱抽取器（``app.graph.extract``）测试。

**不调外部 API**：假 provider 回放模型 JSON。守住三条：

1. **引文可判**：模型给多长都不信，逐字核对原文，伪造引文直接弃节点。
2. **坏 JSON 回传重试**（解析必须在调用循环里）。
3. **长度守卫**：超长名称/概要/引文截断留告警，而不是在库里炸 1406。
"""

from __future__ import annotations

import json

import pytest
from app.graph.extract import (
    MAX_NAME_CHARS,
    MAX_PARAGRAPHS_PER_PROMPT,
    GraphExtractor,
    GraphNodeDraft,
    GraphUnit,
    parse_json,
)
from app.llm.base import ChatMessage, ChatResult, LLMError, LLMErrorKind, LLMProvider, Usage

SRC = (
    "上海是一座依水而兴的城市。黄浦江穿城而过，把市区一分为二。\n\n"
    "城市的淋巴系统和命脉，就是密布的水网。自开埠以来，航运与贸易在此交汇。"
)


class FakeProvider(LLMProvider):
    """按脚本回放模型输出，弹尽后回落到 ``default``。"""

    name = "deepseek"
    display_name = "假服务商"
    base_url = "https://example.invalid"
    supports_vision = False

    def __init__(self, script: list[str | Exception], default: str = "[]") -> None:
        super().__init__("sk-test", "deepseek-chat")
        self.script = list(script)
        self.default = default
        self.calls: list[list[ChatMessage]] = []

    async def chat(self, messages, *, max_tokens=None):
        self.calls.append(messages)
        item = self.script.pop(0) if self.script else self.default
        if isinstance(item, Exception):
            raise item
        return ChatResult(
            text=item, usage=Usage(prompt_tokens=100, completion_tokens=20), model=self.model
        )


def _node(name="水网", *, quote="城市的淋巴系统和命脉，就是密布的水网。"):
    return {
        "name": name,
        "weight": 3,
        "category_index": 0,
        "summary": "水网是城市命脉。",
        "quote": quote,
        "links": [],
    }


def _payload(**extra) -> str:
    return json.dumps(
        {"categories": ["上海概况"], "nodes": [_node()], "warnings": [], **extra},
        ensure_ascii=False,
    )


async def _extract(p, text: str = SRC, **kw) -> GraphUnit:
    return await GraphExtractor(p, **kw).extract_async(text, title="上海概况", loc_page=3)


class Test正常路径:
    @pytest.mark.asyncio
    async def test_should_parse_valid_payload(self):
        p = FakeProvider([_payload()])
        gu = await _extract(p)
        assert gu.title == "上海概况"
        assert gu.loc_page == 3
        assert len(gu.nodes) == 1
        assert gu.nodes[0].quote in SRC
        assert gu.categories == ["上海概况"]
        assert gu.usage.prompt_tokens == 100
        assert gu.usage.completion_tokens == 20

    @pytest.mark.asyncio
    async def test_should_tolerate_code_fence_and_prose(self):
        p = FakeProvider([f"```json\n{_payload()}\n```端到端"])
        gu = await _extract(p)
        assert len(gu.nodes) == 1

    @pytest.mark.asyncio
    async def test_should_drop_fabricated_quote(self):
        p = FakeProvider([_payload(nodes=[_node(quote="作者写的一句假话")])])
        gu = await _extract(p)
        assert gu.nodes == []
        assert any("引文未能在原文中找到" in w for w in gu.warnings)

    @pytest.mark.asyncio
    async def test_should_match_quote_with_whitespace_differences(self):
        """模型输出里夹了空格/换行也能对上原文。"""
        p = FakeProvider(
            [_payload(nodes=[_node(quote="城市的  淋巴 系统 和命脉 ，就是 密布的 水网。")])]
        )
        gu = await _extract(p)
        assert len(gu.nodes) == 1

    @pytest.mark.asyncio
    async def test_should_translate_links_to_final_indices(self):
        body = {
            "categories": ["水网"],
            "nodes": [
                _node(name="A", quote="黄浦江穿城而过，把市区一分为二。"),
                _node(name="B", quote="城市的淋巴系统和命脉，就是密布的水网。"),
                {"name": "假节点", "quote": "这句原文里没有", "summary": "x"},
            ],
            "warnings": [],
        }
        body["nodes"][0]["links"] = [
            {"index": 1, "reason": "都与水相关"},
            {"index": 2, "reason": "指向被丢弃的节点"},
        ]
        p = FakeProvider([json.dumps(body, ensure_ascii=False)])
        gu = await _extract(p)
        names = [n.name for n in gu.nodes]
        assert names == ["A", "B"], names
        a = gu.nodes[0]
        # 原始下标 1 → final 下标 1；指向原 2（被丢弃）的关联消失
        assert a.links == [(1, "都与水相关")], a.links

    @pytest.mark.asyncio
    async def test_should_clamp_oversized_fields(self):
        body = {
            "categories": ["上海" * 100],
            "nodes": [
                {
                    "name": "长" * 500,
                    "weight": 2,
                    "category_index": 0,
                    "summary": "概" * 1000,
                    "quote": "城市的淋巴系统和命脉，就是密布的水网。",
                }
            ],
        }
        p = FakeProvider([json.dumps(body, ensure_ascii=False)])
        gu = await _extract(p)
        assert len(gu.nodes) == 1
        assert len(gu.nodes[0].name) == MAX_NAME_CHARS
        assert len(gu.nodes[0].summary) == 600
        assert len(gu.categories[0]) == 120
        assert any("截断" in w for w in gu.warnings)

    @pytest.mark.asyncio
    async def test_should_accumulate_usage_across_retry(self):
        p = FakeProvider(["坏的", _payload()])
        gu = await _extract(p)
        assert gu.usage.prompt_tokens == 200
        assert gu.usage.completion_tokens == 40

    @pytest.mark.asyncio
    async def test_should_truncate_long_source_to_max_paragraphs(self):
        p = FakeProvider([_payload()])
        long_text = "\n\n".join(f"段落{i}" * 30 for i in range(200))
        await _extract(p, text=long_text)
        prompt = p.calls[0][-1].content
        assert f"共 {MAX_PARAGRAPHS_PER_PROMPT} 段" in prompt


class Test重试:
    @pytest.mark.asyncio
    async def test_should_retry_bad_json_with_feedback(self):
        p = FakeProvider(["这不是 JSON", _payload()])
        gu = await _extract(p)
        assert len(gu.nodes) == 1
        assert len(p.calls) == 2
        assert "无法解析" in p.calls[1][-1].content

    @pytest.mark.asyncio
    async def test_should_retry_transient_llm_error(self):
        p = FakeProvider([LLMError(LLMErrorKind.RATE_LIMITED, "429"), _payload()])
        gu = await _extract(p)
        assert len(gu.nodes) == 1
        assert len(p.calls) == 2

    @pytest.mark.asyncio
    async def test_should_not_retry_permanent_error(self):
        p = FakeProvider([LLMError(LLMErrorKind.KEY_INVALID, "401")] * 3)
        with pytest.raises(LLMError):
            await _extract(p)
        assert len(p.calls) == 1

    @pytest.mark.asyncio
    async def test_should_give_up_after_max_retries(self):
        from app.graph.extract import GraphError

        p = FakeProvider(["坏1", "坏2", _payload()])
        with pytest.raises(GraphError) as ei:
            await GraphExtractor(p, max_retries=1).extract_async(SRC, title="t", loc_page=1)
        assert ei.value.kind == "llm_bad_json"
        assert len(p.calls) == 2


class Test解析辅助:
    def test_parse_json_strips_fence(self):
        assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_parse_json_rejects_non_object(self):
        with pytest.raises(Exception, match="找不到 JSON 对象"):
            parse_json("[1, 2]")

    @pytest.mark.asyncio
    async def test_empty_nodes_returns_empty_unit(self):
        """空 nodes 不是异常：走「本篇无可用知识点」的上层逻辑去标记失败。"""
        p = FakeProvider([json.dumps({"categories": ["未分类"], "nodes": []})])
        gu = await _extract(p)
        assert gu.nodes == []
        assert gu.categories == ["未分类"]


class Test图节点序列化:
    def test_graph_unit_roundtrip(self):
        gu = GraphUnit(
            title="t",
            loc_page=2,
            categories=["a"],
            nodes=[GraphNodeDraft(name="n", weight=3, category_index=0, summary="s", quote="q")],
            usage=Usage(prompt_tokens=1, completion_tokens=2),
        )
        again = GraphUnit.from_dict(gu.to_dict())
        assert again.title == gu.title
        assert again.loc_page == 2
        assert again.nodes[0].name == "n"
        assert again.usage.prompt_tokens == 1
        assert again.usage.completion_tokens == 2
