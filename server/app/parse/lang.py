"""④ 语言判别。

判据只有一条（AGENTS.md §6）：``zh_ratio < 0.3`` 判英文。

为什么用「比值」而不是「有没有汉字」：导游词材料里中英混排是常态（标题中文、
正文英文、结尾 `谢谢大家！`），「有没有汉字」会把「Thank you all!」这种
纯英文块里混进的单个汉字判成中文。

为什么阈值这么低：实测样板里中英段落都很干净，zh_ratio 要么接近 0 要么接近
1，0.3 这个中间地带几乎不会出现。留这么宽的余量是给**扫描件 OCR 噪声**兜底
—— OCR 出来的东西里混几个假汉字很常见。
"""

from __future__ import annotations

import re

from app.parse.base import LANG_ZH_THRESHOLD

#: CJK 统一表意文字 + 假名 + 谚文。中英对照材料里日韩字符也归这边。
_CJK = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af\u3000-\u303f\uff00-\uffef]"
)
#: 拉丁字母（含带重音的，英文材料里偶尔有）
_LATIN = re.compile(r"[A-Za-z\u00c0-\u024f]")


def zh_ratio(text: str) -> float:
    """中文字符占「中文 + 拉丁字母」的比例。

    刻意**不把数字和标点计入分母** —— 材料里页码、序号、年份很多，
    计入会让纯中文块被稀释，把 zh_ratio 拉低到阈值以下误判成英文。
    """
    if not text:
        return 0.0
    zh = len(_CJK.findall(text))
    en = len(_LATIN.findall(text))
    total = zh + en
    if total == 0:
        # 既无中文也无拉丁（纯数字/符号）：按中文侧的默认值处理，
        # 交给调用方按 kind 决定，不让语言判别在这里瞎猜。
        return 1.0
    return zh / total


def classify(text: str, threshold: float = LANG_ZH_THRESHOLD) -> str:
    """返回 ``"zh"`` 或 ``"en"``。"""
    return "zh" if zh_ratio(text) >= threshold else "en"


def annotate(text: str, threshold: float = LANG_ZH_THRESHOLD) -> float:
    """算出比例并夹到 [0,1]，供入库。"""
    return max(0.0, min(1.0, zh_ratio(text)))


#: ⑦ 质量自检用的更严阈值。中文字段应 ≈100% 中文，英文字段应 ≈0%。
QUALITY_ZH_MIN = 0.5
QUALITY_EN_MAX = 0.2


def cjk_ratio(text: str) -> float:
    """⑦ 自检口径：CJK 字符占**全部非空白字符**的比例。

    与 ``zh_ratio`` 分母不同 —— 自检要抓的是「中文字段里混进了大段英文」
    这种真问题，所以分母必须含标点、空格以外的一切。
    """
    stripped = [c for c in text if not c.isspace()]
    if not stripped:
        return 0.0
    return sum(1 for c in stripped if _CJK.match(c)) / len(stripped)
