"""next_mastery 推进规则（F16 自测回写）。"""

from __future__ import annotations

import pytest
from app.graph.progress import next_mastery


class Test自测推进:
    @pytest.mark.parametrize(
        "current,correct,expected",
        [
            ("no", True, "mid"),
            ("mid", True, "yes"),
            ("yes", True, "yes"),
            ("no", False, "no"),
            ("mid", False, "no"),
            ("yes", False, "mid"),
        ],
    )
    def test_progression(self, current, correct, expected):
        assert next_mastery(current, correct=correct) == expected
