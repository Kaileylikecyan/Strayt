"""服务商注册表与工厂。

文档 §10 的「API 抽象」表要求接入 6 家：DeepSeek / 阿里千问 / 智谱 / OpenAI /
Anthropic / Google。其中 4 家是 OpenAI 兼容格式，共用一个实现类。

**在 6 家之外额外接了国内主流几家**（Kimi / 豆包 / 硅基流动 / MiniMax / 混元），
理由见下面 ``_DOMESTIC_*`` 段的注释 —— §10 那张表是写文档时的选型，不是限制清单，
个人自用场景下用户手里通常是这几家的 Key。

新增服务商只需要在这里加一条 ``ProviderSpec``，管线代码不用动。
``app/api/routers/settings.py`` 的 ``Provider`` 字面量**从本注册表派生**，
不要再手写 —— 之前就是手写两份导致的漏接（anthropic/gemini 存不进去）。
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
    #: 国内 / 海外。设置页按这个分组，个人自用时用户通常只看得到自己那组。
    region: str = "国内"
    #: 去哪拿 Key。填在 UI 上，省得用户去搜 —— 尤其国内各家控制台位置不统一。
    console_url: str = ""
    #: 覆盖 ``base_url``：用户自己填的端点（OneAPI / NewAPI / vLLM / Ollama 等）。
    #: 只有 ``base_url_editable=True`` 的项才允许填。
    base_url_editable: bool = False

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
    region: str = "国内",
    console_url: str = "",
    base_url_editable: bool = False,
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
        default_text=text[0] if text else "",
        default_vision=vision[0] if vision else "",
        region=region,
        console_url=console_url,
        base_url_editable=base_url_editable,
    )


#: 文档 §10 的 6 家 + 国内主流补充。default_* 取该家的推荐款。
#:
#: **模型名会过期，厂商天天上新/改名。** 这里的列表只用于给设置页做下拉候选，
#: 真正调用时 ``build_provider`` 不做白名单拦截（见那里的注释），所以列表过时
#: 不会挡住用户刚充钱买的模型，只会少几个候选项。
PROVIDERS: dict[str, ProviderSpec] = {
    s.key: s
    for s in [
        # ---- 国内：文档 §10 点名的 3 家 ----
        _spec(
            "deepseek",
            "DeepSeek 深度求索",
            "https://api.deepseek.com/v1",
            ("deepseek-chat", "deepseek-reasoner"),
            notes="性价比最高，文本对齐首选（文档 §10）。无视觉能力。",
            console_url="https://platform.deepseek.com/",
        ),
        _spec(
            "qwen",
            "阿里千问（百炼）",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ("qwen-max", "qwen-plus", "qwen-turbo"),
            ("qwen-vl-max", "qwen-vl-plus"),
            notes="图像识别强，扫描件首选（文档 §10）。",
            console_url="https://bailian.console.aliyun.com/",
        ),
        _spec(
            "glm",
            "智谱 GLM",
            "https://open.bigmodel.cn/api/paas/v4",
            ("glm-4-plus", "glm-4-air", "glm-4-flash"),
            ("glm-4v-plus",),
            notes="速度最快（文档 §10）。",
            console_url="https://open.bigmodel.cn/",
        ),
        # ---- 国内：§10 之外补的（个人自用最常撞到这几家）----
        _spec(
            "moonshot",
            "月之暗面 Kimi",
            "https://api.moonshot.cn/v1",
            ("kimi-k2-0905-preview", "moonshot-v1-128k", "moonshot-v1-32k"),
            ("moonshot-v1-8k-vision-preview", "kimi-k2-0905-preview"),
            notes="长文本强（128k 起步），整篇资料一次性喂进去时比别家省事。",
            console_url="https://platform.moonshot.cn/",
        ),
        _spec(
            "doubao",
            "字节豆包（火山方舟）",
            "https://ark.cn-beijing.volces.com/api/v3",
            ("doubao-1-5-pro-32k-250115", "doubao-1-5-lite-32k-250115"),
            ("doubao-1-5-vision-pro-250428",),
            notes="多模态性价比好。注意：控制台给的是**推理接入点 ID**（ep-xxx），"
            "不是模型名 —— 填 Key 之外还要在档案里把模型填成你的 ep- ID。",
            console_url="https://console.volcengine.com/ark",
        ),
        _spec(
            "siliconflow",
            "硅基流动 SiliconFlow",
            "https://api.siliconflow.cn/v1",
            ("Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"),
            ("Qwen/Qwen2.5-VL-72B-Instruct",),
            notes="聚合平台，一个 Key 打通 DeepSeek / Qwen / Llama 等几十个开源模型，"
            "适合只想充一个钱包的人。",
            console_url="https://cloud.siliconflow.cn/",
        ),
        _spec(
            "minimax",
            "MiniMax",
            "https://api.minimax.cn/v1",
            ("minimax-M2", "abab6.5s-chat", "MiniMax-Text-01"),
            notes="长文本与语音见长。",
            console_url="https://platform.minimaxi.com/",
        ),
        _spec(
            "hunyuan",
            "腾讯混元",
            "https://api.hunyuan.cloud.tencent.com/v1",
            ("hunyuan-turbos-latest", "hunyuan-large"),
            notes="腾讯云走内网出口，国内延迟低。",
            console_url="https://cloud.tencent.com/product/hunyuan",
        ),
        _spec(
            "openai_compatible",
            "自定义（OpenAI 兼容端点）",
            "",
            (),
            (),
            notes="本地 vLLM / Ollama / LM Studio，或 OneAPI、NewAPI 这类中转站。"
            "必须自己填 Base URL（要带 /v1），留空则填不了。模型名也自己填。",
            base_url_editable=True,
        ),
        # ---- 海外：文档 §10 点名的 3 家 ----
        _spec(
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            ("gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"),
            ("gpt-4o",),
            notes="质量稳定（文档 §10）。",
            region="海外",
            console_url="https://platform.openai.com/",
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
            region="海外",
            console_url="https://console.anthropic.com/",
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
            region="海外",
            console_url="https://aistudio.google.com/apikey",
        ),
    ]
}


def get_spec(provider: str) -> ProviderSpec:
    s = PROVIDERS.get(provider)
    if s is None:
        raise LLMError(LLMErrorKind.BAD_REQUEST, f"未接入的服务商：{provider}")
    return s


def list_providers() -> list[dict[str, Any]]:
    """给设置页下拉框用。附带价格基准，方便用户自己比价。

    **字段名以本函数为准**：设置页读的是 ``key`` / ``display_name``，
    不是 ``provider``（历史上就是这里叫 key、前端读 provider 才出过一版全
    undefined 的下拉框）。``base_url`` 仅在 ``base_url_editable`` 时下发，
    让 UI 知道该不该显示那个输入框。
    """
    out = []
    for s in PROVIDERS.values():
        pt = estimate_cost(s.key, s.default_text, 1_000_000, 0)
        out.append(
            {
                "key": s.key,
                "display_name": s.display_name,
                "region": s.region,
                "text_models": list(s.text_models),
                "vision_models": list(s.vision_models),
                "default_text": s.default_text,
                "default_vision": s.default_vision,
                "supports_vision": s.supports_vision,
                "notes": s.notes,
                "console_url": s.console_url,
                "base_url_editable": s.base_url_editable,
                "base_url": s.base_url if s.base_url_editable else "",
                "price_per_1m_input_cny": pt.prompt_cny,
            }
        )
    return out


def build_provider(
    provider: str, api_key: str, model: str | None = None, *, base_url: str | None = None
) -> LLMProvider:
    """按注册表造一个实例。``model`` 空则用该家推荐款。

    ``base_url`` 只对 ``base_url_editable`` 的项有意义（自定义兼容端点）；
    固定端点的厂商**不接受**覆盖 —— 否则用户填错一个地址就能把所有请求
    发到任意主机，而 Key 也会跟着过去。
    """
    s = get_spec(provider)
    m = (model or s.default_text).strip()
    if not m:
        raise LLMError(
            LLMErrorKind.BAD_REQUEST,
            f"未指定模型：{provider}。自定义端点必须自己填模型名。",
        )
    if base_url and base_url.strip() and not s.base_url_editable:
        raise LLMError(
            LLMErrorKind.BAD_REQUEST,
            f"{s.display_name} 的接口地址是固定的，不支持自定义 Base URL",
        )
    known = s.text_models + s.vision_models
    if m not in known:
        # 不硬拦：厂商天天上新模型，写死白名单只会挡住用户刚充钱买的模型。
        # 但要留痕，方便设置页提示「不在已知列表里，可能是拼错了」。
        pass

    if s.cls is OpenAICompatProvider:
        # 自定义端点：base_url 必填，注册表里是空串
        url = (base_url or s.base_url).strip().rstrip("/")
        if not url:
            raise LLMError(
                LLMErrorKind.BAD_REQUEST,
                "自定义端点必须填 Base URL（形如 http://127.0.0.1:11434/v1）",
            )
        if not url.startswith(("http://", "https://")):
            raise LLMError(
                LLMErrorKind.BAD_REQUEST, f"Base URL 必须以 http:// 或 https:// 开头：{url}"
            )
        inst = OpenAICompatProvider(api_key, m, timeout=get_settings().llm_timeout, base_url=url)
        inst.system_in_user = s.system_in_user
        return inst
    return s.cls(api_key, m, timeout=get_settings().llm_timeout)


# ==========================================================================
# 连通性测试（F6：10s 内返回成功/失败 + 原因）
# ==========================================================================
#: 测试用的最小提示词。够短就能验证链路，又能让模型回一句话。
_PROBE = "只回复两个字：正常"


async def test_connection(
    provider: str, api_key: str, model: str | None = None, *, base_url: str | None = None
) -> dict[str, Any]:
    """F6 连通性测试。**硬性 10s 上限**，超时也算失败并如实标注。"""
    budget = get_settings().llm_probe_timeout
    spec = get_spec(provider)
    m = (model or spec.default_text).strip()
    p = build_provider(provider, api_key, m, base_url=base_url)
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
