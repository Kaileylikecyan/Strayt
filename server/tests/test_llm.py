"""LLM 层：报文渲染、错误分类、成本预估、10s 预算。

**不真调任何外部 API。** 用 monkeypatch 替换 httpx 打桩，验证我们发出去的报文
对不对、回来的错误分不分得清。真实联调靠 ``/settings/api-keys/probe`` 端点
由用户在设置页手动触发。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from app.llm.base import (
    ChatMessage,
    ImagePart,
    LLMError,
    LLMErrorKind,
)
from app.llm.errors import classify_status
from app.llm.native import AnthropicProvider, GeminiProvider
from app.llm.openai_compat import OpenAICompatProvider
from app.llm.pricing import estimate_cost, estimate_tokens, get_price
from app.llm.registry import PROVIDERS, build_provider, list_providers
from app.llm.registry import test_connection as probe  # 别名：否则 pytest 会把真函数当测试收集

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _patch_post(monkeypatch, status: int, payload: dict, *, capture: dict | None = None):
    """把 httpx 的 POST 打桩，必要时把请求体记进 capture。"""

    async def handler(self, url, **kw):
        if capture is not None:
            capture["url"] = str(url)
            capture["headers"] = kw.get("headers") or {}
            capture["json"] = kw.get("json")
        return httpx.Response(status, json=payload, request=httpx.Request("POST", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "post", handler)


# ==========================================================================
# OpenAI 兼容
# ==========================================================================
def test_openai_payload_and_usage(monkeypatch) -> None:
    cap: dict = {}
    _patch_post(
        monkeypatch,
        200,
        {
            "id": "chatcmpl-1",
            "model": "deepseek-chat",
            "choices": [{"message": {"content": "正常"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 2},
        },
        capture=cap,
    )
    p = OpenAICompatProvider("sk-test", "deepseek-chat", base_url="https://api.deepseek.com/v1")
    r = asyncio.run(p.chat([ChatMessage.user("hi")], max_tokens=8))

    assert r.text == "正常"
    assert (r.usage.prompt_tokens, r.usage.completion_tokens) == (11, 2)
    assert cap["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer sk-test"
    assert cap["json"]["max_tokens"] == 8
    assert cap["json"]["messages"] == [{"role": "user", "content": "hi"}]


def test_system_stays_separate_by_default(monkeypatch) -> None:
    cap: dict = {}
    _patch_post(monkeypatch, 200, {"choices": [{"message": {"content": "ok"}}]}, capture=cap)
    p = OpenAICompatProvider("sk-t", "gpt-4o-mini", base_url="https://api.openai.com/v1")
    asyncio.run(p.chat([ChatMessage.system("你是助手"), ChatMessage.user("问题")]))
    assert [m["role"] for m in cap["json"]["messages"]] == ["system", "user"]


def test_system_in_user_flag_merges() -> None:
    """部分国产服务商不收 system 角色，要并进首条 user。"""
    p = OpenAICompatProvider("sk-t", "glm-4-air", base_url="https://x")
    p.system_in_user = True
    out = p._prepare([ChatMessage.system("规则A"), ChatMessage.user("问题B")])
    assert out == [{"role": "user", "content": "规则A\n\n问题B"}]


def test_vision_renders_data_uri(monkeypatch) -> None:
    cap: dict = {}
    _patch_post(monkeypatch, 200, {"choices": [{"message": {"content": "看图说话"}}]}, capture=cap)
    p = OpenAICompatProvider("sk-t", "qwen-vl-max", base_url="https://x")
    asyncio.run(
        p.chat_vision([ChatMessage.vision("描述", [ImagePart(data=PNG, mime="image/png")])])
    )

    parts = cap["json"]["messages"][0]["content"]
    assert isinstance(parts, list)
    assert parts[0] == {"type": "text", "text": "描述"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_chat_vision_without_image_short_circuits(monkeypatch) -> None:
    """没图就别浪费一次视觉调用，应走普通 chat。"""
    cap: dict = {}
    _patch_post(monkeypatch, 200, {"choices": [{"message": {"content": "ok"}}]}, capture=cap)
    p = OpenAICompatProvider("sk-t", "qwen-vl-max", base_url="https://x")
    asyncio.run(p.chat_vision([ChatMessage.user("纯文本")]))
    assert isinstance(cap["json"]["messages"][0]["content"], str)


def test_empty_choices_raises() -> None:
    p = OpenAICompatProvider("sk-t", "m", base_url="https://x")
    with pytest.raises(LLMError) as e:
        p._parse({"choices": []})
    assert e.value.kind == LLMErrorKind.PROVIDER_ERROR


def test_reasoning_only_response_raises() -> None:
    """推理模型可能只回 reasoning_content，静默返回空串会让下游拿到空段落。"""
    p = OpenAICompatProvider("sk-t", "m", base_url="https://x")
    with pytest.raises(LLMError) as e:
        p._parse({"choices": [{"message": {"content": None, "reasoning_content": "想了一下"}}]})
    assert e.value.kind == LLMErrorKind.REFUSED


# ==========================================================================
# Anthropic 原生
# ==========================================================================
def test_anthropic_system_is_top_level(monkeypatch) -> None:
    cap: dict = {}
    _patch_post(
        monkeypatch,
        200,
        {
            "id": "msg_1",
            "model": "claude-sonnet-4",
            "content": [{"type": "text", "text": "答案"}],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        },
        capture=cap,
    )
    p = AnthropicProvider("sk-ant-x", "claude-sonnet-4")
    r = asyncio.run(p.chat([ChatMessage.system("规则"), ChatMessage.user("问题")], max_tokens=64))

    assert r.text == "答案"
    assert (r.usage.prompt_tokens, r.usage.completion_tokens) == (5, 3)
    assert cap["headers"]["x-api-key"] == "sk-ant-x"
    assert "Authorization" not in cap["headers"], "Anthropic 不用 Bearer"
    assert cap["json"]["system"] == "规则"
    assert [m["role"] for m in cap["json"]["messages"]] == ["user"]
    assert cap["json"]["max_tokens"] == 64, "Anthropic 的 max_tokens 必填"


def test_anthropic_vision_block_shape(monkeypatch) -> None:
    cap: dict = {}
    _patch_post(monkeypatch, 200, {"content": [{"type": "text", "text": "x"}]}, capture=cap)
    p = AnthropicProvider("sk-ant-x", "claude-sonnet-4")
    asyncio.run(p.chat_vision([ChatMessage.vision("看", [ImagePart(data=PNG, mime="image/png")])]))
    block = cap["json"]["messages"][0]["content"][1]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"


# ==========================================================================
# Gemini 原生
# ==========================================================================
def test_gemini_key_in_query_and_roles(monkeypatch) -> None:
    cap: dict = {}
    _patch_post(
        monkeypatch,
        200,
        {
            "candidates": [{"content": {"parts": [{"text": "好"}]}}],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 1},
        },
        capture=cap,
    )
    p = GeminiProvider("AIza-x", "gemini-2.5-pro")
    r = asyncio.run(
        p.chat(
            [ChatMessage.system("规则"), ChatMessage.user("问题"), ChatMessage("assistant", "上句")]
        )
    )

    assert r.text == "好"
    assert r.usage.prompt_tokens == 7
    assert "key=AIza-x" in cap["url"]
    assert "systemInstruction" in cap["json"]
    assert [c["role"] for c in cap["json"]["contents"]] == ["user", "model"], (
        "assistant 要映射成 model"
    )


def test_gemini_blocked_prompt_raises() -> None:
    p = GeminiProvider("k", "gemini-2.5-pro")
    with pytest.raises(LLMError) as e:
        p._parse({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
    assert e.value.kind == LLMErrorKind.REFUSED
    assert "SAFETY" in e.value.detail


# ==========================================================================
# 错误分类（F6 核心）
# ==========================================================================
@pytest.mark.parametrize(
    ("status", "body", "expect"),
    [
        (401, '{"error":{"message":"invalid api key"}}', LLMErrorKind.KEY_INVALID),
        (403, '{"error":{"message":"not authorized for this model"}}', LLMErrorKind.BAD_REQUEST),
        (402, "payment required", LLMErrorKind.QUOTA_EXCEEDED),
        (429, "rate limit reached, please retry", LLMErrorKind.RATE_LIMITED),
        (429, "insufficient balance", LLMErrorKind.QUOTA_EXCEEDED),
        (404, "model not found", LLMErrorKind.BAD_REQUEST),
        (400, "bad json", LLMErrorKind.BAD_REQUEST),
        (500, "internal error", LLMErrorKind.PROVIDER_ERROR),
        (503, "overloaded", LLMErrorKind.PROVIDER_ERROR),
    ],
)
def test_status_classification(status, body, expect) -> None:
    err = classify_status(status, body)
    assert err.kind == expect
    assert err.http_status == status
    assert err.hint, "每种错误都必须有给用户看的中文提示"


def test_key_scrubbed_from_error_detail() -> None:
    """上游报错里常回显 Key 片段，不能带进日志或 UI。"""
    err = classify_status(401, '{"error":{"message":"invalid key sk-abcdefghijklmnop1234567890"}}')
    assert "sk-abcdefghijklmnop1234567890" not in err.detail
    assert "***" in err.detail


def test_network_error_kind(monkeypatch) -> None:
    async def boom(self, url, **kw):
        raise httpx.ConnectError("name resolution failed")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    p = OpenAICompatProvider("sk-t", "m", base_url="https://x")
    with pytest.raises(LLMError) as e:
        asyncio.run(p.chat([ChatMessage.user("hi")]))
    assert e.value.kind == LLMErrorKind.NETWORK


# ==========================================================================
# F6 的 10s 预算
# ==========================================================================
def test_probe_timeout_reports_network(monkeypatch) -> None:
    """超过 10s 预算必须返回失败并标注是超时，不能挂住设置页。"""
    from app.core.config import get_settings

    budget = get_settings().llm_probe_timeout
    assert budget <= 10, "F6 硬要求 10s 内返回"

    async def slow(self, url, **kw):
        raise httpx.TimeoutException("too slow")

    monkeypatch.setattr(httpx.AsyncClient, "post", slow)
    res = asyncio.run(probe("deepseek", "sk-bad"))
    assert res["ok"] is False
    assert res["kind"] == LLMErrorKind.NETWORK
    assert res["budget_ms"] <= 10_000
    assert res["hint"]


def test_probe_reports_key_invalid(monkeypatch) -> None:
    _patch_post(monkeypatch, 401, {"error": {"message": "Authentication Fails"}})
    res = asyncio.run(probe("openai", "sk-bad"))
    assert res["ok"] is False
    assert res["kind"] == LLMErrorKind.KEY_INVALID
    assert res["provider"] == "openai"


def test_probe_success_includes_cost(monkeypatch) -> None:
    _patch_post(
        monkeypatch,
        200,
        {
            "model": "deepseek-chat",
            "choices": [{"message": {"content": "正常"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    )
    res = asyncio.run(probe("deepseek", "sk-good"))
    assert res["ok"] is True
    assert res["cost"]["total_cny"] > 0
    assert res["latency_ms"] >= 0


def test_probe_never_raises(monkeypatch) -> None:
    """设置页不能被一个陌生异常搞崩。"""

    async def boom(self, url, **kw):
        raise ValueError(" totally unexpected ")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    res = asyncio.run(probe("openai", "sk-x"))
    assert res["ok"] is False
    assert "unexpected" in res["detail"]


# ==========================================================================
# 注册表与价目
# ==========================================================================
def test_registry_has_six_providers_from_doc() -> None:
    """文档 §10 点名 6 家，少一家就是功能缺失。"""
    assert set(PROVIDERS) == {"deepseek", "qwen", "glm", "openai", "anthropic", "gemini"}


def test_deepseek_has_no_vision() -> None:
    """DeepSeek 只有文本通道，调用方必须能提前知道。"""
    spec = PROVIDERS["deepseek"]
    assert spec.supports_vision is False
    assert spec.default_vision == ""
    p = list_providers()
    ds = next(x for x in p if x["key"] == "deepseek")
    assert ds["supports_vision"] is False
    assert ds["vision_models"] == []


def test_every_provider_has_default_and_price() -> None:
    for s in PROVIDERS.values():
        assert s.default_text, f"{s.key} 缺默认文本模型"
        if s.supports_vision:
            assert s.default_vision, f"{s.key} 标了视觉却没默认视觉模型"
        assert get_price(s.key, s.default_text)[0] > 0


def test_unknown_provider_rejected() -> None:
    with pytest.raises(LLMError) as e:
        build_provider("nope", "sk-x")
    assert e.value.kind == LLMErrorKind.BAD_REQUEST


def test_build_provider_applies_system_in_user() -> None:
    assert build_provider("deepseek", "sk-x").system_in_user is False


def test_cost_estimation() -> None:
    e = estimate_cost("deepseek", "deepseek-chat", 1_000_000, 1_000_000)
    assert e.prompt_cny == pytest.approx(2.0)
    assert e.completion_cny == pytest.approx(8.0)
    assert e.total_cny == pytest.approx(10.0)


def test_unknown_model_falls_back_not_crash() -> None:
    """厂商天天上新模型，价目表没收录也要能估。"""
    assert get_price("deepseek", "deepseek-chat-9999") == get_price("deepseek", "deepseek-chat")
    assert get_price("brand-new", "m")[0] > 0


def test_token_estimate_scales_with_language() -> None:
    zh = estimate_tokens("中英对照背诵材料" * 20)
    en = estimate_tokens("english alignment material " * 20)
    assert zh > en, "同样长度，中文应比英文更费 token"
    assert estimate_tokens("") == 0
