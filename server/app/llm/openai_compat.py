"""OpenAI 兼容协议实现。

6 家里 **4 家**（DeepSeek / 阿里千问 DashScope / 智谱 / OpenAI 本身）都吃
``POST {base_url}/chat/completions`` 这一套，只是 base_url 和模型名不同。
所以这里写一份，靠 ``base_url`` + ``model`` 参数化。

报文细节各家仍有差异，都在这一层抹平：
- 百炼（DashScope）的 ``qwen-vl-*`` 用 ``image_url`` 字段，值是 data URI；
- 部分国产服务商的 ``usage`` 可能缺字段，要兜底成 0；
- 部分服务商的 system 消息不接受，只能并进首条 user —— 用 ``system_in_user`` 开关。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.llm.base import (
    ChatMessage,
    ChatResult,
    ContentPart,
    ImagePart,
    LLMError,
    LLMErrorKind,
    LLMProvider,
    TextPart,
    Usage,
)
from app.llm.errors import classify_status

_JSON_HEADERS = {"Content-Type": "application/json"}


def _render_part(part: ContentPart) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    if isinstance(part, ImagePart):
        return {"type": "image_url", "image_url": {"url": f"data:{part.mime};base64,{part.b64}"}}
    raise TypeError(f"未知内容片段：{type(part)!r}")


def render_message(m: ChatMessage) -> dict[str, Any]:
    if isinstance(m.content, str):
        return {"role": m.role, "content": m.content}
    return {"role": m.role, "content": [_render_part(p) for p in m.content]}


class OpenAICompatProvider(LLMProvider):
    """DeepSeek / 千问 / 智谱 / OpenAI / 任意兼容端点。"""

    #: 部分国产服务商不接受 system 角色，需要并进首条 user
    system_in_user = False
    #: 部分服务商的视觉模型字段名不是 image_url（如百炼旧版用 image）
    vision_field = "image_url"

    def __init__(
        self, api_key: str, model: str, *, timeout: float = 90.0, base_url: str = ""
    ) -> None:
        super().__init__(api_key, model, timeout=timeout)
        if base_url:
            self.base_url = base_url

    def _endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {**_JSON_HEADERS, "Authorization": f"Bearer {self.api_key}"}

    def _prepare(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        if not self.system_in_user:
            return [render_message(m) for m in messages]
        # 把 system 并进第一条 user 前面
        sys_text = "\n\n".join(
            m.content for m in messages if m.role == "system" and isinstance(m.content, str)
        )
        rest = [m for m in messages if m.role != "system"]
        out: list[dict[str, Any]] = []
        for m in rest:
            if not out and m.role == "user":
                merged = f"{sys_text}\n\n{m.content}" if isinstance(m.content, str) else m.content
                out.append({"role": "user", "content": merged})
            else:
                out.append(render_message(m))
        return out

    def _parse(self, data: dict[str, Any]) -> ChatResult:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(LLMErrorKind.PROVIDER_ERROR, f"响应里没有 choices：{str(data)[:200]}")
        msg = choices[0].get("message") or {}
        text = msg.get("content")
        if text is None:
            # 有些推理模型把内容放在 reasoning_content，正文为空要提示而不是静默返回空串
            raise LLMError(
                LLMErrorKind.REFUSED,
                f"模型未返回正文（reasoning={str(msg.get('reasoning_content'))[:80]}）",
            )
        u = data.get("usage") or {}
        return ChatResult(
            text=text,
            usage=Usage(
                prompt_tokens=int(u.get("prompt_tokens") or 0),
                completion_tokens=int(u.get("completion_tokens") or 0),
            ),
            model=str(data.get("model") or self.model),
            raw_id=data.get("id"),
        )

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self._endpoint(), headers=self._headers(), json=payload)
        except httpx.TimeoutException as e:
            raise LLMError(LLMErrorKind.NETWORK, f"超时（{self.timeout}s）") from e
        except httpx.HTTPError as e:
            raise LLMError(LLMErrorKind.NETWORK, f"连接失败：{e}") from e

        if r.status_code >= 400:
            raise classify_status(r.status_code, r.text)
        try:
            data: dict[str, Any] = r.json()
        except ValueError as e:
            raise LLMError(LLMErrorKind.PROVIDER_ERROR, f"响应不是 JSON：{r.text[:200]}") from e
        return data

    async def chat(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._prepare(messages),
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        return self._parse(await self._post(payload))

    async def chat_vision(
        self, messages: list[ChatMessage], *, max_tokens: int | None = None
    ) -> ChatResult:
        if not any(
            isinstance(p, ImagePart)
            for m in messages
            if isinstance(m.content, list)
            for p in m.content
        ):
            # 没图就别浪费一次视觉调用
            return await self.chat(messages, max_tokens=max_tokens)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._prepare(messages),
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        return self._parse(await self._post(payload))
