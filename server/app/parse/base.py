"""解析层公共数据结构与协议。

管线（AGENTS.md §6）：

    ①版面提取 → ②页级清洗 → ③块结构识别 → ④语言判别 → ⑤段落切分
                                                              ↓
                        ⑥对齐(A:LLM / B:直配) → ⑦质量自检 → ⑧入库

本文件只定义 ①②④⑤ 产物的形状与解析器协议；③ 在 ``blocks.py``，
⑥⑦⑧ 在 ``app/align`` 与 ``app/jobs``。

一条硬约束贯穿全文件：**``loc_page``（0 起页号）必须一路带下去**。
出处回看是二期/三期能力，页号丢了只能重跑全部加工（AGENTS.md §6）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

# 解析器版本号写进 file_parses.parser_version。改了解析逻辑要 bump，
# 否则「保留原始解析结果换模型重试」会混进不同口径的数据。
PARSER_VERSION = "1"


@dataclass(frozen=True)
class Line:
    """一行原始文本 + 它在页面上的纵向位置（用于判断块间距）。"""

    text: str
    top: float = 0.0
    height: float = 0.0


@dataclass
class TextBlock:
    """一段连续文本，尚未判定中英。

    ``page`` 是 0 起页号 —— 二期出处回看的唯一依据，**不可省略**。
    """

    text: str
    page: int
    order: int = 0
    kind: str = "body"  # body | title | note | watermark
    #: 块内语言判别结果，⑤ 之后才有意义
    zh_ratio: float = 0.0

    @property
    def is_chinese(self) -> bool:
        return self.zh_ratio >= LANG_ZH_THRESHOLD

    @property
    def is_english(self) -> bool:
        return self.zh_ratio < LANG_ZH_THRESHOLD


@dataclass
class ParsedDoc:
    """一个文件的解析产物。

    ``pages_json`` 落库到 ``file_parses``（文档 §7 流程 2：全部失败时保留原始
    解析结果换模型重试）。页号信息在这里，不能丢。
    """

    channel: str  # text | vision
    page_count: int
    blocks: list[TextBlock] = field(default_factory=list)
    parser_version: str = PARSER_VERSION
    warnings: list[str] = field(default_factory=list)
    #: 视觉通道产物：每页图片的相对路径（扫描件 OCR 用）
    page_images: list[str] = field(default_factory=list)

    def to_pages_json(self) -> list[dict]:
        """落库形状：按页聚合，便于二期按页回看。"""
        by_page: dict[int, list[dict]] = {}
        for b in self.blocks:
            by_page.setdefault(b.page, []).append(
                {"order": b.order, "kind": b.kind, "text": b.text, "zh_ratio": round(b.zh_ratio, 4)}
            )
        return [{"page": p, "blocks": by_page[p]} for p in sorted(by_page)]

    def raw_text(self) -> str:
        return "\n".join(b.text for b in self.blocks if b.kind != "watermark")


# ④语言判别阈值：zh_ratio < 0.3 判为英文（AGENTS.md §6）
LANG_ZH_THRESHOLD = 0.3


class ParseError(Exception):
    """解析失败。``kind`` 对应 ErrorCode，客户端据此提示。"""

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)


class Parser(ABC):
    """一个文件类型对应一个解析器。"""

    name: str
    #: 支持的扩展名（含点，小写）
    extensions: tuple[str, ...]
    #: 版面/清洗规则版本。②页级清洗、③结构识别、⑤分段任一处规则改动都要
    #: **手动 +1** —— ``file_parses.parser_version`` 靠它区分历史解析结果。
    #: 没有版本号就没法判断"重跑结果变了"是规则变了还是文件变了。
    version: str = "1"

    @abstractmethod
    def parse(self, path: Path) -> ParsedDoc:
        """解析文件。失败抛 ``ParseError``，不要抛裸异常。"""
