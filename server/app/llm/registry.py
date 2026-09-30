"""服务商注册表与工厂。

6 家来自文档 §10 的「API 抽象」表：DeepSeek / 阿里千问 / 智谱 / OpenAI /
Anthropic / Google。其中 4 家是 OpenAI 兼容格式，共用一个实现类。

新增服务商只需要在这里加一条 ``ProviderSpec``，管线代码不用动。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.llm.base import (
    _ERROR_HINTS,
    ChatMessage,
    ChatResult,
    LLMError,
    LLMErrorKind,
    LLMProvider,
)
from app.llm.native import AnthropicProvider, GeminiProvider
from app.llm.openai_compat import OpenAICompatProvider
from app.llm.pricing import estimate_cost


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    display_name: str
    base_url: str
    cls: type[LLMProvider]
    text_models: tuple[str, ...]
    vision_models: tuple[str, ...]
    #: 该实现是否把 system 并进首条 user
    system_in_user: bool = False
    notes: str = ""
    default_text: str = ""
    default_vision: str = ""

    @property
    def supports_vision(self) -> bool:
        return bool(self.vision_models)


def _spec(
    key: str,
    display: str,
    base_url: str,
    text: tuple[str, ...],
    vision: tuple[str, ...] = (),
    *,
    system_in_user: bool = False,
    notes: str = "",
) -> ProviderSpec:
    return ProviderSpec(
        key=key,
        display_name=display,
        base_url=base_url,
        cls=OpenAICompatProvider,
        text_models=text,
        vision_models=vision,
        system_in_user=system_in_user,
        notes=notes,
        default_text=text[0],
        default_vision=vision[0] if vision else "",
    )


#: 文档 §10 的 6 家。default_* 取该家的推荐款。
PROVIDERS: dict[str, ProviderSpec] = {
    s.key: s
    for s in [
        _spec(
            "deepseek",
            "DeepSeek",
            "https://api.deepseek.com/v1",
            ("deepseek-chat", "deepseek-reasoner"),
            notes="性价比最高，文本对齐首选（文档 §10）。无视觉能力。",
        ),
        _spec(
            "qwen",
            "阿里千问（百炼）",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ("qwen-max", "qwen-plus", "qwen-turbo"),
            ("qwen-vl-max", "qwen-vl-plus"),
            notes="图像识别强，扫描件首选（文档 §10）。",
        ),
        _spec(
            "glm",
            "智谱 GLM",
            "https://open.bigmodel.cn/api/paas/v4",
            ("glm-4-plus", "glm-4-air", "glm-4-flash"),
            ("glm-4v-plus",),
            notes="速度最快（文档 §10）。",
        ),
        _spec(
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            ("gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"),
            ("gpt-4o",),
            notes="质量稳定（文档 §10）。",
        ),
        ProviderSpec(
            key="anthropic",
            display_name="Anthropic Claude",
            base_url="https://api.anthropic.com/v1",
            cls=AnthropicProvider,
            text_models=("claude-sonnet-4", "claude-opus-4", "claude-haiku-4"),
            vision_models=("claude-sonnet-4", "claude-opus-4", "claude-haiku-4"),
            notes="质量稳定（文档 §10）。原生协议，非 OpenAI 兼容。",
            default_text="claude-sonnet-4",
            default_vision="claude-sonnet-4",
        ),
        ProviderSpec(
            key="gemini",
            display_name="Google Gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            cls=GeminiProvider,
            text_models=("gemini-2.5-pro", "gemini-2.5-flash"),
            vision_models=("gemini-2.5-pro", "gemini-2.5-flash"),
            notes="质量稳定（文档 §10）。原生协议，非 OpenAI 兼容。",
            default_text="gemini-2.5-pro",
            default_vision="gemini-2.5-pro",
        ),
    ]
}


def get_spec(provider: str) -> ProviderSpec:
    s = PROVIDERS.get(provider)
    if s is None:
        raise LLMError(LLMErrorKind.BAD_REQUEST, f"未接入的服务商：{provider}")
    return s


def list_providers() -> list[dict[str, Any]]:
    """给设置页下拉框用。附带价格基准，方便用户自己比价。"""
    out = []
    for s in PROVIDERS.values():
        pt = estimate_cost(s.key, s.default_text, 1_000_000, 0)
        out.append(
            {
                "key": s.key,
                "display_name": s.display_name,
                "text_models": list(s.text_models),
                "vision_models": list(s.vision_models),
                "default_text": s.default_text,
                "default_vision": s.default_vision,
                "supports_vision": s.supports_vision,
                "notes": s.notes,
                "price_per_1m_input_cny": pt.prompt_cny,
            }
        )
    return out


def build_provider(provider: str, api_key: str, model: str | None = None) -> LLMProvider:
    """按注册表造一个实例。``model`` 空则用该家推荐款。"""
    s = get_spec(provider)
    m = (model or s.default_text).strip()
    if not m:
        raise LLMError(LLMErrorKind.BAD_REQUEST, f"未指定模型：{provider}")
    known = s.text_models + s.vision_models
    if m not in known:
        # 不硬拦：厂商天天上新模型，写死白名单只会挡住用户刚充钱买的模型。
        # 但要留痕，方便设置页提示「不在已知列表里，可能是拼错了」。
        pass

    if s.cls is OpenAICompatProvider:
        inst = OpenAICompatProvider(
            api_key, m, timeout=get_settings().llm_timeout, base_url=s.base_url
        )
        inst.system_in_user = s.system_in_user
        return inst
    return s.cls(api_key, m, timeout=get_settings().llm_timeout)


# ==========================================================================
# 连通性测试（F6：10s 内返回成功/失败 + 原因）
# ==========================================================================
#: 测试用的最小提示词。够短就能验证链路，又能让模型回一句话。
_PROBE = "只回复两个字：正常"


async def test_connection(provider: str, api_key: str, model: str | None = None) -> dict[str, Any]:
    """F6 连通性测试。**硬性 10s 上限**，超时也算失败并如实标注。"""
    budget = get_settings().llm_probe_timeout
    spec = get_spec(provider)
    m = (model or spec.default_text).strip()
    p = build_provider(provider, api_key, m)
    p.timeout = budget

    started = asyncio.get_running_loop().time()
    try:
        res: ChatResult = await asyncio.wait_for(
            p.chat([ChatMessage.user(_PROBE)], max_tokens=16), timeout=budget
        )
    except TimeoutError:
        return _probe_result(
            False, provider, m, LLMErrorKind.NETWORK, f"超过 {budget}s 未响应", budget, started
        )
    except LLMError as e:
        return _probe_result(
            False, provider, m, e.kind, e.detail, budget, started, http_status=e.http_status
        )
    except Exception as e:  # 兜底：绝不让一个陌生异常把设置页搞崩
        return _probe_result(
            False,
            provider,
            m,
            LLMErrorKind.PROVIDER_ERROR,
            f"{type(e).__name__}: {e}",
            budget,
            started,
        )

    est = estimate_cost(provider, m, res.usage.prompt_tokens, res.usage.completion_tokens)
    return {
        "ok": True,
        "kind": "ok",
        "detail": res.text.strip()[:80],
        "hint": "Key 可用",
        "provider": provider,
        "model": res.model or m,
        "latency_ms": round((asyncio.get_running_loop().time() - started) * 1000),
        "usage": {
            "prompt_tokens": res.usage.prompt_tokens,
            "completion_tokens": res.usage.completion_tokens,
        },
        "cost": est.to_dict(),
        "http_status": 200,
    }


def _probe_result(
    ok: bool,
    provider: str,
    model: str,
    kind: str,
    detail: str,
    budget: float,
    started: float,
    *,
    http_status: int | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "kind": kind,
        "detail": detail,
        "hint": _ERROR_HINTS.get(kind, ""),
        "provider": provider,
        "model": model,
        "latency_ms": round((asyncio.get_running_loop().time() - started) * 1000),
        "budget_ms": int(budget * 1000),
        "http_status": http_status,
    }
