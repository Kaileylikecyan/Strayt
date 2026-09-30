"""掌握度推进规则（F16）。

三级：``no < mid < yes``。闪卡自测答对往上顶一级，答错往回落一级；
直接手动设置（``PUT /nodes/{id}/mastery``、弱同步 ``node_mastery``）是绝对值，
不走这里的递进逻辑。
"""

from __future__ import annotations

from typing import Final, Literal

MASTERY_ORDER: Final[tuple[Literal["no", "mid", "yes"], ...]] = ("no", "mid", "yes")


def next_mastery(current: str, *, correct: bool) -> str:
    """自测后推进一档，两端钳位。"""
    idx = MASTERY_ORDER.index(current)  # type: ignore[arg-type]
    step = 1 if correct else -1
    return MASTERY_ORDER[max(0, min(len(MASTERY_ORDER) - 1, idx + step))]
