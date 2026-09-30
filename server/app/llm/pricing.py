"""成本预估（文档 F8）。

**这里的数字是估算基准，不是报价。** 6 家都在频繁调价，写死一个「当前价格」
等于埋一个会悄悄变错的假设。所以：

- 价目表集中在本文件，带 ``VERIFIED_AT`` 标记来源与核对时间；
- 允许用 ``.env`` / 模型档案覆盖单个模型的价（``STRAYT_PRICE_<provider>_<model>``）；
- 预估只用于「调用前给用户看个量级」和「超 50% 中断询问」（F8），
  拿它当账单对账是不行的。

token 数是**估算**：真实用量以服务商返回的 ``usage`` 为准。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 各家 token 计价的核对日期。超过这个时间还没重新核对，就把数字当量级看。
VERIFIED_AT = "2026-09-27"

# 每 100 万 token 的价格，单位：人民币元
Price = tuple[float, float]  # (输入, 输出)


@dataclass(frozen=True)
class CostEstimate:
    prompt_tokens: int
    completion_tokens: int
    prompt_cny: float
    completion_cny: float

    @property
    def total_cny(self) -> float:
        return self.prompt_cny + self.completion_cny

    def to_dict(self) -> dict[str, float | int]:
        # 留 6 位小数：小额调用（探针十来 token ≈ ¥0.00002）若按 4 位取整会
        # 显示成 ¥0.0000，界面上看着像「成本没算出来」。
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "prompt_cny": round(self.prompt_cny, 6),
            "completion_cny": round(self.completion_cny, 6),
            "total_cny": round(self.total_cny, 6),
        }


#: 价目表。键为 (provider, model)，值 (输入价, 输出价) 元/百万 token。
#: 命不中就回落到 ``_FALLBACK``，宁可粗估也不要让调用崩掉。
_PRICES: dict[tuple[str, str], Price] = {
    # DeepSeek —— 文档 §10 标注「性价比最高，文本对齐首选」
    ("deepseek", "deepseek-chat"): (2.0, 8.0),
    ("deepseek", "deepseek-reasoner"): (4.0, 16.0),
    # 阿里千问 DashScope
    ("qwen", "qwen-max"): (2.4, 9.6),
    ("qwen", "qwen-plus"): (0.8, 2.0),
    ("qwen", "qwen-turbo"): (0.3, 0.6),
    ("qwen", "qwen-vl-max"): (3.0, 9.0),
    ("qwen", "qwen-vl-plus"): (1.6, 4.8),
    # 智谱 GLM —— 文档 §10 标注「速度最快」
    ("glm", "glm-4-plus"): (12.0, 12.0),
    ("glm", "glm-4-air"): (1.0, 1.0),
    ("glm", "glm-4-flash"): (0.5, 0.5),
    ("glm", "glm-4v-plus"): (12.0, 12.0),
    # OpenAI
    ("openai", "gpt-4o"): (18.0, 72.0),
    ("openai", "gpt-4o-mini"): (1.1, 4.3),
    ("openai", "gpt-4.1"): (18.0, 72.0),
    ("openai", "gpt-4.1-mini"): (2.8, 10.6),
    # Anthropic
    ("anthropic", "claude-sonnet-4"): (22.0, 110.0),
    ("anthropic", "claude-opus-4"): (108.0, 540.0),
    ("anthropic", "claude-haiku-4"): (7.2, 36.0),
    # Google
    ("gemini", "gemini-2.5-pro"): (7.7, 61.6),
    ("gemini", "gemini-2.5-flash"): (1.5, 6.0),
}

#: 命中不了价目表时用这档兜底（按中档估，宁可高估也不要让用户以为很便宜）
_FALLBACK: Price = (5.0, 20.0)

# --------------------------------------------------------------------------
# token 估算
# --------------------------------------------------------------------------
# CJK 一个字约 1 token；英文约 4 字符 1 token；数字/标点另算。
_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """粗估 token 数。误差在 ±30% 内，够用于「调用前给个量级」和超支熔断。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    rest = len(text) - cjk
    return cjk + (rest // 4) + 1


def estimate_tokens_messages(prompt: str, *, expected_output_chars: int = 0) -> int:
    """按「输入 prompt + 预期输出」估。输出量对齐任务通常远大于输入。"""
    return estimate_tokens(prompt) + max(1, expected_output_chars // 2)


def get_price(provider: str, model: str) -> Price:
    """取价。精确命中 → provider 档位 → 兜底档。"""
    p = _PRICES.get((provider, model))
    if p:
        return p
    # 同服务商任意模型的档位（取最便宜的那个，宁可说贵别说便宜）
    same = [v for (prov, _m), v in _PRICES.items() if prov == provider]
    return min(same) if same else _FALLBACK


def estimate_cost(
    provider: str, model: str, prompt_tokens: int, completion_tokens: int
) -> CostEstimate:
    pin, pout = get_price(provider, model)
    return CostEstimate(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_cny=prompt_tokens / 1_000_000 * pin,
        completion_cny=completion_tokens / 1_000_000 * pout,
    )
