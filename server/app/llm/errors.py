"""HTTP 状态码 → 错误分类。

F6 明确要求把失败原因说清楚（Key 错误 / 网络不通 / 额度不足），所以
**不允许把上游报错原样透传给用户** —— 上游的报文格式不统一，还常把 Key
片段回显在错误里。统一收敛成 ``LLMError``，原文只留一小段做排查线索。
"""

from __future__ import annotations

import re

from app.llm.base import LLMError, LLMErrorKind

_MAX_DETAIL = 300

# 各家对「额度不足」的措辞不统一，只能靠关键词识别
_QUOTA_HINTS = (
    "insufficient",
    "quota",
    "exceeded",
    "余额",
    "额度",
    "欠费",
    "arrearage",
    "billing",
    "out of credit",
)
_RATE_HINTS = ("rate limit", "too many requests", "qps", "限流", "rpm", "tpm")

_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|[A-Za-z0-9_\-]{32,})")


def _scrub(text: str) -> str:
    """抹掉疑似 Key 片段再截断，避免错误信息把凭据带进日志或 UI。"""
    return _SECRET.sub("***", text)[:_MAX_DETAIL]


def _looks_like(body: str, hints: tuple[str, ...]) -> bool:
    low = body.lower()
    return any(h in low for h in hints)


def classify_status(status: int, body: str) -> LLMError:
    detail = _scrub(body)
    low = body.lower()

    if status in (401, 403):
        # 403 在百炼上表示「没开通该模型」，和「Key 无效」是不同的建议话术
        if "not authorized" in low or "access denied" in low:
            return LLMError(
                LLMErrorKind.BAD_REQUEST,
                f"该 Key 无权访问此模型（{status}）：{detail}",
                http_status=status,
            )
        return LLMError(
            LLMErrorKind.KEY_INVALID, f"Key 被拒（{status}）：{detail}", http_status=status
        )

    if status == 429:
        kind = (
            LLMErrorKind.QUOTA_EXCEEDED
            if _looks_like(body, _QUOTA_HINTS)
            else LLMErrorKind.RATE_LIMITED
        )
        return LLMError(kind, f"{status}：{detail}", http_status=status)

    if status == 402:
        return LLMError(LLMErrorKind.QUOTA_EXCEEDED, f"欠费（402）：{detail}", http_status=status)

    if status == 404:
        return LLMError(
            LLMErrorKind.BAD_REQUEST,
            f"接口或模型不存在（404），检查 base_url 与模型名：{detail}",
            http_status=status,
        )

    if status in (400, 422):
        return LLMError(
            LLMErrorKind.BAD_REQUEST, f"请求被拒（{status}）：{detail}", http_status=status
        )

    if status == 413:
        return LLMError(
            LLMErrorKind.BAD_REQUEST, f"请求体过大（413）：{detail}", http_status=status
        )

    if status >= 500:
        return LLMError(
            LLMErrorKind.PROVIDER_ERROR, f"服务商故障（{status}）：{detail}", http_status=status
        )

    return LLMError(
        LLMErrorKind.PROVIDER_ERROR, f"未预期的状态码 {status}：{detail}", http_status=status
    )
