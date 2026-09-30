"""F11 合并/编辑层（``app.graph.merge``）测试。

验证“篇内坐标 → 项目全局坐标”的翻译：分类跨篇去重、节点稳定 key、
边双向去重与死边丢弃；以及预览确认时的三种手动调整。
"""

from __future__ import annotations

from app.graph.extract import GraphNodeDraft, GraphUnit
from app.graph.merge import (
    GraphDraft,
    GraphDraftCategory,
    GraphDraftNode,
    apply_edits,
    edges_from_nodes,
    merge_units,
)


def _unit(**kw) -> GraphUnit:
    data = {
        "title": "篇",
        "loc_page": 1,
        "categories": ["章"],
        "nodes": [_node(0)],
        "warnings": [],
    }
    data.update(kw)
    return GraphUnit(**data)


def _node(i: int, *, category_index: int = 0) -> GraphNodeDraft:
    return GraphNodeDraft(
        name=f"知识点{i}", weight=2, category_index=category_index, summary=f"概要{i}", quote="原文"
    )


def _draft(categories=("章甲",), nodes=("A",)) -> GraphDraft:
    return GraphDraft(
        categories=[GraphDraftCategory(f"c{i:03d}", c, i) for i, c in enumerate(categories)],
        nodes=[_dnode(k) for k in nodes],
    )


def _dnode(key: str) -> GraphDraftNode:
    return GraphDraftNode(
        key=key,
        name=key,
        weight=2,
        summary=f"{key}概要",
        quote="原文",
        loc_page=1,
        category_key="c000",
    )


class Test合并:
    def test_should_build_global_keys_in_unit_order(self):
        draft = merge_units(
            [
                (0, _unit(title="第一篇", loc_page=1, nodes=[_node(0), _node(1)])),
                (1, _unit(title="第二篇", loc_page=5, nodes=[_node(0)])),
            ]
        )
        assert [n.key for n in draft.nodes] == ["u000:n00", "u000:n01", "u001:n00"]
        assert draft.unit_count == 2
        assert [c.name for c in draft.categories] == ["章"]

    def test_should_dedupe_categories_across_units_by_name(self):
        draft = merge_units(
            [
                (0, _unit(title="一", categories=["上海概况"], nodes=[_node(0)])),
                (1, _unit(title="二", categories=["上海 概况"], nodes=[_node(0)])),
            ]
        )
        assert len(draft.categories) == 1, draft.categories
        assert draft.nodes[0].category_key == draft.nodes[1].category_key

    def test_should_pin_category_index_to_local_categories(self):
        draft = merge_units(
            [
                (
                    0,
                    _unit(
                        title="一",
                        categories=["甲", "乙"],
                        nodes=[_node(0, category_index=1), _node(1, category_index=0)],
                    ),
                )
            ]
        )
        keys = sorted({n.category_key for n in draft.nodes})
        assert len(keys) == 2

    def test_should_merge_duplicate_category_names_to_one(self):
        unit = _unit(title="一", categories=["甲", "甲"], nodes=[_node(0), _node(1)])
        draft = merge_units([(0, unit)])
        assert len(draft.categories) == 1, draft.categories
        assert draft.nodes[0].category_key == draft.nodes[1].category_key

    def test_should_drop_out_of_range_category_index(self):
        unit = _unit(title="一", categories=["甲"], nodes=[_node(0, category_index=5)])
        draft = merge_units([(0, unit)])
        assert draft.nodes[0].category_key == draft.categories[0].key

    def test_should_prune_empty_categories_after_edit(self):
        draft = _draft(categories=("甲", "乙"), nodes=("A",))  # A 在 c000
        out, _ = apply_edits(draft)
        assert [c.name for c in out.categories] == ["甲"]

    def test_merged_draft_serializes_roundtrip(self):
        draft = merge_units([(0, _unit(title="一", loc_page=2, nodes=[_node(0), _node(1)]))])
        again = GraphDraft.from_dict(draft.to_dict())
        assert again.to_dict() == draft.to_dict()
        assert again.nodes[0].loc_page == 2


class Test边:
    def test_should_rebuild_edges_from_local_links(self):
        nodes = [
            GraphDraftNode(
                key="a",
                category_key="c",
                name="A",
                weight=1,
                summary="s",
                quote="q",
                loc_page=1,
                links=[("b", "r")],
            ),
            GraphDraftNode(
                key="b", category_key="c", name="B", weight=1, summary="s", quote="q", loc_page=1
            ),
        ]
        assert edges_from_nodes(nodes) == [("a", "b", "r")]

    def test_should_skip_reversed_duplicate(self):
        nodes = [
            GraphDraftNode(
                key="a",
                category_key="c",
                name="A",
                weight=1,
                summary="s",
                quote="q",
                loc_page=1,
                links=[("b", "r")],
            ),
            GraphDraftNode(
                key="b",
                category_key="c",
                name="B",
                weight=1,
                summary="s",
                quote="q",
                loc_page=1,
                links=[("a", "r2")],
            ),
        ]
        assert len(edges_from_nodes(nodes)) == 1

    def test_should_drop_self_loop_and_dead_link(self):
        nodes = [
            GraphDraftNode(
                key="a",
                category_key="c",
                name="A",
                weight=1,
                summary="s",
                quote="q",
                loc_page=1,
                links=[("a", "自环"), ("ghost", "死边")],
            ),
        ]
        assert edges_from_nodes(nodes) == []

    def test_merge_should_map_links_across_units_and_drop_dead(self):
        n0 = _node(0)
        n0.links = [(1, "与第一篇关联"), (0, "自环"), (5, "越界")]
        draft = merge_units([(0, _unit(title="一", nodes=[n0, _node(1)]))])
        assert draft.edges == [("u000:n00", "u000:n01", "与第一篇关联")], draft.edges


class Test编辑:
    def test_should_delete_node_and_its_edges(self):
        draft = GraphDraft(
            categories=[GraphDraftCategory("c0", "章", 0)],
            nodes=[
                _dnode("a"),
                GraphDraftNode(
                    key="b",
                    category_key="c0",
                    name="B",
                    weight=1,
                    summary="s",
                    quote="q",
                    loc_page=1,
                    links=[("a", "r")],
                ),
            ],
        )
        out, warns = apply_edits(draft, delete_keys={"a"})
        assert [n.key for n in out.nodes] == ["b"]
        assert out.edges == []
        assert any("已删除节点" in w for w in warns)

    def test_should_reassign_category(self):
        draft = _draft(categories=("甲", "乙"))
        out, _ = apply_edits(draft, reassign={"A": "c001"})
        assert out.nodes[0].category_key == "c001"

    def test_should_edit_name_and_mark_edited(self):
        draft = _draft()
        out, _ = apply_edits(draft, edits={"A": {"name": "新名字"}})
        assert out.nodes[0].name == "新名字"
        assert out.nodes[0].edited is True

    def test_should_clamp_edited_name(self):
        draft = _draft()
        out, warns = apply_edits(draft, edits={"A": {"name": "超" * 300}})
        assert len(out.nodes[0].name) <= 200
        assert any("截断" in w for w in warns)

    def test_should_delete_when_edit_empties_name(self):
        draft = _draft()
        out, warns = apply_edits(draft, edits={"A": {"name": "  "}})
        assert out.nodes == []
        assert any("被删除" in w for w in warns)

    def test_should_warn_and_ignore_unknown_keys(self):
        draft = _draft()
        out, warns = apply_edits(draft, delete_keys={"ghost"}, reassign={"ghost": "c0"})
        assert len(out.nodes) == 1
        assert any("不存在，已忽略" in w for w in warns)

    def test_should_rebuild_edges_after_edits(self):
        draft = GraphDraft(
            categories=[GraphDraftCategory("c0", "章", 0)],
            nodes=[
                GraphDraftNode(
                    key="a",
                    category_key="c0",
                    name="A",
                    weight=1,
                    summary="s",
                    quote="q",
                    loc_page=1,
                    links=[("b", "r")],
                ),
                _dnode("b"),
                _dnode("c"),
            ],
        )
        out, _ = apply_edits(draft, delete_keys={"c"})
        assert out.edges == [("a", "b", "r")]
