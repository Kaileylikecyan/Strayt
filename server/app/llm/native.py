"""Anthropic 与 Google Gemini 的原生协议。

这两家**不是** OpenAI 兼容格式，要单独写。差异点：

Anthropic
- ``x-api-key`` 头 + ``anthropic-version`` 头，不是 Bearer
- system 是**顶层字段**，不放进 messages
- 图片必须是 base64 且限定 jpeg/png/gif/webp
- usage 字段是 ``input_tokens`` / ``output_tokens``

Gemini
- Key 走 query 参数 ``?key=``，不是头
- 角色叫 ``user`` / ``model``（不是 assistant）
- 图片是 inlineData ``{mimeType, data}``
- usage 字段是 ``promptTokenCount`` / ``candidatesTokenCount``
"""

from __future__ import annotations

from typing import Any

import httpx

from app.llm.base import (
    ChatMessage,
    ChatResult,
    ImagePart,
    LLMError,
    LLMErrorKind,
    LLMProvider,
    TextPart,
    Usage,
)
from app.llm.errors import classify_status

# Anthropic 只收这几种图片格式，别的要先转
_ANTHROPIC_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    display_name = "Anthropic Claude"
    base_url = "https://api.anthropic.com/v1"
    supports_vision = True

    API_VERSION = "2023-06-01"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.API_VERSION,
        }

    def _payload(self, messages: list[ChatMessage], max_tokens: int | None) -> dict[str, Any]:
        sys_text = "\n\n".join(
            m.content for m in messages if m.role == "system" and isinstance(m.content, str)
        )
        # Anthropic 的 max_tokens 是必填
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or 4096,
            "messages": [
                {
                    "role": "user" if m.role != "assistant" else "assistant",
                    "content": (
                        m.content
                        if isinstance(m.content, str)
                        else [
                            {"type": "text", "text": p.text}
                            if isinstance(p, TextPart)
                            else {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": p.mime
                                    if p.mime in _ANTHROPIC_MIME
                                    else "image/png",
                                    "data": p.b64,
                                },
                            }
                            for p in m.content
                        ]
                    ),
                }
                for m in messages
                if m.role != "system"
            ],
        }
        if sys_text:
            payload["system"] = sys_text
        return payload

    def _parse(self, data: dict[str, Any]) -> ChatResult:
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text:
            stop = data.get("stop_reason")
            raise LLMError(LLMErrorKind.REFUSED, f"模型未返回正文（stop_reason={stop}）")
        u = data.get("usage") or {}
        return ChatResult(
            text=text,
            usage=Usage(
                prompt_tokens=int(u.get("input_tokens") or 0),
                completion_tokens=int(u.get("output_tokens") or 0),
            ),
            model=str(data.get("model") or self.model),
            raw_id=data.get("id"),
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(
                    f"{self.base_url.rstrip('/')}/messages", headers=self._headers(), json=payload
                )
        except httpx.TimeoutException as e:
            raise LLMError(LLMErrorKind.NETWORK, f"超时（{self.timeout}s）") from e
        except httpx.HTTPError as e:
            raise LLMError(LLMErrorKind.NETWORK, f"连接失败：{e}") from e
        if r.status_code >= 400:
            raise classify_status(r.status_code, r.text)
        try:
            return dict(r.json())
        except ValueError as e:
            raise LLMError(LLMErrorKind.PROVIDER_ERROR, f"响应不是 JSON：{r.text[:200]}") from e

    async def chat(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> ChatResult:
        return self._parse(await self._post(self._payload(messages, max_tokens)))

    async def chat_vision(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> ChatResult:
        has_img = any(
            isinstance(p, ImagePart)
            for m in messages
            if isinstance(m.content, list)
            for p in m.content
        )
        if not has_img:
            return await self.chat(messages, max_tokens=max_tokens)
        return self._parse(await self._post(self._payload(messages, max_tokens)))


class GeminiProvider(LLMProvider):
    name = "gemini"
    display_name = "Google Gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    supports_vision = True

    def _url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models/{self.model}:generateContent?key={self.api_key}"

    def _payload(self, messages: list[ChatMessage], max_tokens: int | None) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        system_parts: list[dict[str, str]] = []
        for m in messages:
            parts: list[dict[str, Any]] = []
            if isinstance(m.content, str):
                parts.append({"text": m.content})
            else:
                for p in m.content:
                    if isinstance(p, TextPart):
                        parts.append({"text": p.text})
                    else:
                        parts.append({"inlineData": {"mimeType": p.mime, "data": p.b64}})
            if m.role == "system":
                # Gemini 用 systemInstruction，不进 contents
                system_parts.extend(parts)
                continue
            contents.append({"role": "model" if m.role == "assistant" else "user", "parts": parts})

        payload: dict[str, Any] = {"contents": contents}
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        if max_tokens:
            payload["generationConfig"] = {"maxOutputTokens": max_tokens}
        return payload

    def _parse(self, data: dict[str, Any]) -> ChatResult:
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError(LLMErrorKind.REFUSED, f"无候选回复：{str(data)[:200]}")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text:
            # 安全拦截会返回 promptFeedback 而没有内容
            block = (data.get("promptFeedback") or {}).get("blockReason")
            raise LLMError(LLMErrorKind.REFUSED, f"模型未返回正文（blockReason={block}）")
        u = data.get("usageMetadata") or {}
        return ChatResult(
            text=text,
            usage=Usage(
                prompt_tokens=int(u.get("promptTokenCount") or 0),
                completion_tokens=int(u.get("candidatesTokenCount") or 0),
            ),
            model=self.model,
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self._url(), json=payload)
        except httpx.TimeoutException as e:
            raise LLMError(LLMErrorKind.NETWORK, f"超时（{self.timeout}s）") from e
        except httpx.HTTPError as e:
            raise LLMError(LLMErrorKind.NETWORK, f"连接失败：{e}") from e
        if r.status_code >= 400:
            raise classify_status(r.status_code, r.text)
        try:
            return dict(r.json())
        except ValueError as e:
            raise LLMError(LLMErrorKind.PROVIDER_ERROR, f"响应不是 JSON：{r.text[:200]}") from e

    async def chat(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> ChatResult:
        return self._parse(await self._post(self._payload(messages, max_tokens)))

    async def chat_vision(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> ChatResult:
        has_img = any(
            isinstance(p, ImagePart)
            for m in messages
            if isinstance(m.content, list)
            for p in m.content
        )
        if not has_img:
            return await self.chat(messages, max_tokens=max_tokens)
        return self._parse(await self._post(self._payload(messages, max_tokens)))
