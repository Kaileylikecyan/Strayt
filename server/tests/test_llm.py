"""LLM 层：报文渲染、错误分类、成本预估、10s 预算。

**不真调任何外部 API。** 用 monkeypatch 替换 httpx 打桩，验证我们发出去的报文
对不对、回来的错误分不分得清。真实联调靠 ``/settings/api-keys/probe`` 端点
由用户在设置页手动触发。
"""

from __future__ import annotations

import asyncio
from typing import get_args

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
def test_registry_keeps_the_six_providers_from_doc() -> None:
    """文档 §10 点名 6 家，少一家就是功能缺失。

    在这 6 家**之外**补了国内主流几家（月之暗面 / 豆包 / 硅基流动 / MiniMax / 混元）：
    §10 那张表是写文档时的选型基线，不是限制清单，个人自用时用户手里通常
    就是这几家的 Key。见 ``registry.py`` 模块注释。
    """
    for k in ("deepseek", "qwen", "glm", "openai", "anthropic", "gemini"):
        assert k in PROVIDERS, f"§10 点名的 {k} 不在注册表里"


def test_registry_covers_mainland_china_providers() -> None:
    """国内主流几家必须在，且都复用 OpenAI 兼容实现（否则要单独写协议适配）。

    缺一家就是「用户手里有 Key 但界面里选不到」。这种缺失在旧版真实发生过：
    ``settings.py`` 手写的 Provider 字面量与注册表不同步（anthropic/gemini
    存不进去、moonshot/doubao 存得进却调不通）。
    """
    for k in ("deepseek", "qwen", "glm", "moonshot", "doubao", "siliconflow", "minimax", "hunyuan"):
        assert k in PROVIDERS, f"国内主流厂商 {k} 缺接入"
        assert PROVIDERS[k].cls is OpenAICompatProvider, f"{k} 应复用 OpenAI 兼容实现"
        assert PROVIDERS[k].region == "国内", f"{k} 应归到国内分组"


def test_api_key_provider_literal_is_derived_from_registry() -> None:
    """设置页的 Provider 字面量必须**从注册表派生**，不能手写第二份。

    手写导致过两边漂移，且漂移方向很坏：真要用 Key 的两家存不进去，
    存得进去的两家调用时才炸。这个用例让漂移立刻失败。
    """
    from app.api.routers.settings import Provider

    assert set(get_args(Provider)) == set(PROVIDERS)


def test_custom_endpoint_is_the_only_editable_base_url() -> None:
    """只有自定义端点允许自带 base_url。

    放开给固定端点的风险：把某个 Key 的地址指到别的主机，
    ``Authorization`` 头就会跟着一起发过去。
    """
    editable = {k for k, s in PROVIDERS.items() if s.base_url_editable}
    assert editable == {"openai_compatible"}
    for k, s in PROVIDERS.items():
        if k not in editable:
            assert s.base_url, f"{k} 是固定端点厂商，base_url 不能为空"


def test_custom_endpoint_requires_base_url() -> None:
    """自定义端点漏填地址要明确报错，不能拿空串拼出 `/chat/completions`。"""
    with pytest.raises(LLMError) as e:
        build_provider("openai_compatible", "sk-x", "my-model")
    assert e.value.kind == LLMErrorKind.BAD_REQUEST
    assert "Base URL" in str(e.value.detail)

    p = build_provider(
        "openai_compatible", "sk-x", "my-model", base_url="http://127.0.0.1:11434/v1/"
    )
    # 尾部斜杠必须被规整掉，否则会拼成 /v1//chat/completions
    assert p.base_url == "http://127.0.0.1:11434/v1"
    assert p.model == "my-model"


def test_fixed_endpoint_provider_refuses_custom_base_url() -> None:
    """固定端点的厂商必须拒绝覆盖 base_url。"""
    with pytest.raises(LLMError) as e:
        build_provider("deepseek", "sk-x", None, base_url="http://evil.example.com/v1")
    assert e.value.kind == LLMErrorKind.BAD_REQUEST


def test_custom_endpoint_rejects_non_http_url() -> None:
    with pytest.raises(LLMError) as e:
        build_provider("openai_compatible", "sk-x", "m", base_url="file:///etc/passwd")
    assert e.value.kind == LLMErrorKind.BAD_REQUEST


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
        # 自定义端点例外：模型和地址都是用户填的，没有「推荐款」可言。
        if s.base_url_editable:
            assert not s.text_models, f"{s.key} 不该预置模型清单（用户自己填）"
            continue
        assert s.default_text, f"{s.key} 缺默认文本模型"
        if s.supports_vision:
            assert s.default_vision, f"{s.key} 标了视觉却没默认视觉模型"
        assert get_price(s.key, s.default_text)[0] > 0


def test_every_real_provider_has_console_url() -> None:
    """每家真实厂商都要给「去哪拿 Key」的入口。

    国内几家控制台位置很不统一（百炼、方舟、SiliconFlow 差着好几个层级），
    让用户自己搜是纯粹的摩擦。自定义端点没有控制台，跳过。
    """
    for k, s in PROVIDERS.items():
        if s.base_url_editable:
            continue
        assert s.console_url.startswith("https://"), f"{k} 缺控制台链接"


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
