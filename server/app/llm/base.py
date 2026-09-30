"""LLM 调用抽象（文档 §10「API 抽象」）。

统一接口 ``chat`` / ``chat_vision`` 屏蔽 6 家服务商的报文差异。加工管线
（``app/align``、``app/jobs``）只认这里的 ``ChatResult``，不认任何厂商 SDK，
换服务商不需要改管线代码。

设计取舍：
- **只用 httpx 手写 HTTP**，不引 openai/anthropic 官方 SDK。6 家里有 4 家是
  OpenAI 兼容格式，一个 httpx 调用就够了；引 6 个 SDK 会把依赖树撑爆，
  换来的便利对单机自用没有价值。
- **重试在服务端做，不在客户端做**。加工任务可关窗，失败必须能续跑
  （见 ``jobs.checkpoint_json``），所以重试语义归服务端管线管。
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]
Channel = Literal["text", "vision"]


# ==========================================================================
# 数据结构
# ==========================================================================
@dataclass(frozen=True)
class TextPart:
    text: str
    kind: Literal["text"] = "text"


@dataclass(frozen=True)
class ImagePart:
    """图片内容。``data`` 是原始字节，服务端负责编码。"""

    data: bytes
    mime: str = "image/png"
    kind: Literal["image"] = "image"

    @property
    def b64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def approx_bytes(self) -> int:
        return len(self.data)


ContentPart = TextPart | ImagePart


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str | list[ContentPart] = ""

    @staticmethod
    def system(text: str) -> ChatMessage:
        return ChatMessage(role="system", content=text)

    @staticmethod
    def user(text: str) -> ChatMessage:
        return ChatMessage(role="user", content=text)

    @staticmethod
    def vision(text: str, images: list[ImagePart]) -> ChatMessage:
        parts: list[ContentPart] = [TextPart(text), *images]
        return ChatMessage(role="user", content=parts)


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ChatResult:
    text: str
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    raw_id: str | None = None


# ==========================================================================
# 错误分类（F6 要求把原因说清楚，不能只说「失败」）
# ==========================================================================
class LLMErrorKind:
    KEY_INVALID = "key_invalid"  # 401/403，Key 填错或已失效
    QUOTA_EXCEEDED = "quota_exceeded"  # 余额不足 / 额度耗尽
    RATE_LIMITED = "rate_limited"  # 429 触发限流，稍后重试可能成功
    NETWORK = "network"  # 连不上 / 超时 / DNS
    BAD_REQUEST = "bad_request"  # 报文不对，多半是模型名错了
    PROVIDER_ERROR = "provider_error"  # 5xx，服务商侧故障
    REFUSED = "refused"  # 模型拒绝回答（内容策略）


#: 面向用户的中文说明。F6 明确要求区分「Key 错误/网络不通/额度不足」
_ERROR_HINTS: dict[str, str] = {
    LLMErrorKind.KEY_INVALID: "Key 无效或已被撤销，请检查后重新填写",
    LLMErrorKind.QUOTA_EXCEEDED: "账户额度不足，请充值或换个 Key",
    LLMErrorKind.RATE_LIMITED: "被限流，稍后重试；可降低并发或换 Key",
    LLMErrorKind.NETWORK: "网络不通，请检查服务器网络与代理设置",
    LLMErrorKind.BAD_REQUEST: "请求被拒，通常是模型名写错或该模型无此能力",
    LLMErrorKind.PROVIDER_ERROR: "服务商侧故障，请稍后重试",
    LLMErrorKind.REFUSED: "模型拒绝回答，可能触发了内容策略",
}


class LLMError(Exception):
    def __init__(self, kind: str, detail: str, *, http_status: int | None = None) -> None:
        self.kind = kind
        self.detail = detail
        self.http_status = http_status
        super().__init__(f"[{kind}] {detail}")

    @property
    def hint(self) -> str:
        return _ERROR_HINTS.get(self.kind, "未知错误")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "kind": self.kind,
            "detail": self.detail,
            "hint": self.hint,
            "http_status": self.http_status,
        }


class BudgetExceeded(LLMError):
    """预估成本超上限。F8 要求「超预估 50% 时中断询问」，由管线抛出。"""


# ==========================================================================
# Provider 接口
# ==========================================================================
class LLMProvider(ABC):
    """一家服务商。``channel`` 区分文本通道与视觉通道
    （视觉通道可能不支持，DeepSeek 就只有文本）。"""

    name: str
    display_name: str
    base_url: str
    supports_vision: bool

    def __init__(self, api_key: str, model: str, *, timeout: float = 90.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @abstractmethod
    async def chat(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> ChatResult:
        """单轮对话。"""

    async def chat_vision(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> ChatResult:
        """多模态。默认不支持，调用方应先看 ``supports_vision``。"""
        raise LLMError(
            LLMErrorKind.BAD_REQUEST, f"{self.display_name} 未接入视觉能力，请换一家服务商"
        )
