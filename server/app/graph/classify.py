"""F12 一级分类规则：按文件名/篇章标题把抽取产物归入指定一级分类。

规则在「入库前」（``jobs/graph.py::_finish`` → ``merge_units``）应用：
命中规则的篇目标记到规则指定的类别，未命中保持 LLM 原判。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MATCH_OBJECTS = ("file_name", "unit_title")
MATCH_KINDS = ("prefix", "contains", "regex")


@dataclass(frozen=True)
class CategoryRule:
    """一条分类规则（与 ``category_rules`` 表一一对应）。"""

    id: str
    match_on: str  # file_name | unit_title
    kind: str  # prefix | contains | regex
    pattern: str
    category: str
    enabled: bool = True

    def match(self, file_name: str | None, unit_title: str) -> bool:
        if not self.enabled:
            return False
        # 空白 pattern 必须当不匹配。空串对三种 kind 全都「命中一切」
        # （startswith("")/"" in s/re.search("", s) 皆真），一条这样的脏规则会把
        # 整个项目倒进它指定的分类。API 层已挡（CategoryRuleIn._reject_blank），
        # 这里再兜一层是为了防改校验之前就入库的历史行。
        if not self.pattern.strip():
            return False
        haystack = unit_title if self.match_on == "unit_title" else (file_name or "")
        if self.kind == "prefix":
            return haystack.startswith(self.pattern)
        if self.kind == "contains":
            return self.pattern in haystack
        if self.kind == "regex":
            try:
                return re.search(self.pattern, haystack) is not None
            except re.error:
                # 静默不匹配（不让一条坏规则打断整条管线）。想在保存时就报错，
                # 见 CategoryRuleIn._reject_bad_regex。
                return False
        return False


def classify_categories(
    rules: list[CategoryRule],
    *,
    file_name: str | None,
    unit_title: str,
) -> list[str] | None:
    """取**第一条命中**规则指定的分类名；没命中返回 ``None``（保持 LLM 原判）。

    命中规则时，整篇统一归到规则指定的类别（覆盖这篇的 LLM 一级分类）。
    按 ``rules`` 传入顺序匹配（服务端按 id 升序排），第一条命中即止。
    """
    for rule in rules:
        if rule.match(file_name, unit_title):
            return [rule.category]
    return None


__all__ = ["MATCH_KINDS", "MATCH_OBJECTS", "CategoryRule", "classify_categories"]
