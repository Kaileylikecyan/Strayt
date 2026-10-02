"""F12 一级分类规则：classify + merge 注入 + CRUD API + node_category 同步写。"""

from __future__ import annotations

import json
from datetime import timedelta

from app.core.errors import jsonable_errors
from app.db.models import Category, Node, utcnow
from app.graph.classify import CategoryRule, classify_categories
from app.graph.extract import GraphNodeDraft, GraphUnit
from app.graph.merge import merge_units
from sqlalchemy.orm import Session as S


def _rule(**kw) -> CategoryRule:
    data = {
        "id": "r1",
        "match_on": "file_name",
        "kind": "prefix",
        "pattern": "数学",
        "category": "高中数学",
        "enabled": True,
    }
    data.update(kw)
    return CategoryRule(**data)


def _unit(**kw) -> GraphUnit:
    data = {
        "title": "第一章",
        "loc_page": 1,
        "categories": ["默认章"],
        "nodes": [_node(0)],
        "warnings": [],
    }
    data.update(kw)
    return GraphUnit(**data)


def _node(i: int, *, category_index: int = 0) -> GraphNodeDraft:
    return GraphNodeDraft(
        name=f"知识点{i}", weight=2, category_index=category_index, summary=f"概要{i}", quote="原文"
    )


class TestClassify:
    def test_prefix_on_file_name(self):
        rules = [_rule()]
        assert classify_categories(rules, file_name="数学必修一.pdf", unit_title="第一章") == [
            "高中数学"
        ]
        assert classify_categories(rules, file_name="英语必修一.pdf", unit_title="第一章") is None

    def test_prefix_on_unit_title(self):
        rules = [_rule(match_on="unit_title", pattern="第")]
        assert classify_categories(rules, file_name="任意.pdf", unit_title="第一章") == ["高中数学"]
        assert classify_categories(rules, file_name="任意.pdf", unit_title="总纲") is None

    def test_disabled_rule_is_ignored(self):
        rules = [_rule(enabled=False)]
        assert classify_categories(rules, file_name="数学.pdf", unit_title="x") is None

    def test_first_match_wins(self):
        rules = [
            _rule(id="a", match_on="file_name", kind="contains", pattern="必修", category="甲"),
            _rule(id="b", match_on="file_name", kind="contains", pattern="数学", category="乙"),
        ]
        assert classify_categories(rules, file_name="数学必修一.pdf", unit_title="x") == ["甲"]

    def test_regex_bad_pattern_falls_back(self):
        rules = [_rule(kind="regex", pattern="([")]
        assert classify_categories(rules, file_name="数学.pdf", unit_title="x") is None

    def test_blank_pattern_matches_nothing(self):
        """空白 pattern 对三种 kind 都「命中一切」，一条脏规则能吞掉整个项目。

        ``"".startswith("")`` / ``"" in s`` / ``re.search("", s)`` 全为真，
        所以这里逐个 kind 钉死：空白一律不匹配。
        """
        for kind in ("prefix", "contains", "regex"):
            for blank in ("", "   ", "\t\n"):
                rules = [_rule(kind=kind, pattern=blank, category="吞掉一切")]
                assert (
                    classify_categories(rules, file_name="任意.pdf", unit_title="任意") is None
                ), f"{kind} + {blank!r} 不该命中"

    def test_blank_category_does_not_win_over_later_rule(self):
        """空白 pattern 的规则不该抢在真规则前面把分类定死。"""
        rules = [
            _rule(id="blank", kind="prefix", pattern="", category="脏"),
            _rule(id="real", kind="contains", pattern="数学", category="高中数学"),
        ]
        assert classify_categories(rules, file_name="数学.pdf", unit_title="x") == ["高中数学"]


class TestMergeInjection:
    def test_rule_applies_to_matching_unit(self):
        draft = merge_units(
            [(0, _unit())],
            rules=[_rule(pattern="数学")],
            file_name="数学必修一.pdf",
        )
        assert [c.name for c in draft.categories] == ["高中数学"]
        assert draft.nodes[0].category_key == "c000"

    def test_rule_collapses_multiple_categories_to_one(self):
        unit = _unit(categories=["章甲", "章乙"], nodes=[_node(0), _node(1, category_index=1)])
        draft = merge_units([(0, unit)], rules=[_rule()], file_name="数学.x.pdf")
        assert [n.category_key for n in draft.nodes] == ["c000", "c000"]
        assert [c.name for c in draft.categories] == ["高中数学"]

    def test_unmatched_unit_keeps_llm_categories(self):
        draft = merge_units([(0, _unit())], rules=[_rule()], file_name="英语.x.pdf")
        assert [c.name for c in draft.categories] == ["默认章"]


def _create_graph_project(client, auth) -> str:
    """建一个图谱项目（HTTP）。"""
    return client.post(
        "/api/v1/projects", headers=auth, json={"name": "图谱", "type": "graph"}
    ).json()["id"]


class TestCategoryRuleApi:
    def test_put_and_get_roundtrip(self, client, auth):
        pid = _create_graph_project(client, auth)
        rules = [
            {"match_on": "file_name", "kind": "prefix", "pattern": "数学", "category": "高中数学"},
            {
                "match_on": "unit_title",
                "kind": "contains",
                "pattern": "向量",
                "category": "向量专题",
            },
        ]
        r = client.put(
            f"/api/v1/projects/{pid}/category-rules", headers=auth, json={"rules": rules}
        )
        assert r.status_code == 200, r.text
        assert len(r.json()) == 2

        got = client.get(f"/api/v1/projects/{pid}/category-rules", headers=auth).json()
        assert {x["pattern"] for x in got} == {"数学", "向量"}
        assert all(x["enabled"] for x in got)

    def test_replace_is_whole_table(self, client, auth):
        pid = _create_graph_project(client, auth)
        client.put(
            f"/api/v1/projects/{pid}/category-rules",
            headers=auth,
            json={
                "rules": [
                    {"match_on": "file_name", "kind": "prefix", "pattern": "A", "category": "甲"}
                ]
            },
        )
        client.put(
            f"/api/v1/projects/{pid}/category-rules",
            headers=auth,
            json={
                "rules": [
                    {"match_on": "file_name", "kind": "prefix", "pattern": "B", "category": "乙"}
                ]
            },
        )
        got = client.get(f"/api/v1/projects/{pid}/category-rules", headers=auth).json()
        assert [x["pattern"] for x in got] == ["B"]

    def test_should_404_on_missing_project(self, client, auth):
        r = client.get("/api/v1/projects/nope/category-rules", headers=auth)
        assert r.status_code == 404, r.text

    def test_should_422_on_blank_pattern(self, client, auth):
        """``min_length=1`` 放得过「  」，strip 后就是空 pattern，会吞掉整个项目。"""
        pid = _create_graph_project(client, auth)
        for blank in ("", "   ", "\t"):
            r = client.put(
                f"/api/v1/projects/{pid}/category-rules",
                headers=auth,
                json={
                    "rules": [
                        {
                            "match_on": "file_name",
                            "kind": "prefix",
                            "pattern": blank,
                            "category": "X",
                        }
                    ]
                },
            )
            assert r.status_code == 422, f"pattern={blank!r} 应被挡：{r.text}"
        assert client.get(f"/api/v1/projects/{pid}/category-rules", headers=auth).json() == []

    def test_should_422_on_blank_category(self, client, auth):
        pid = _create_graph_project(client, auth)
        r = client.put(
            f"/api/v1/projects/{pid}/category-rules",
            headers=auth,
            json={
                "rules": [
                    {"match_on": "file_name", "kind": "prefix", "pattern": "a", "category": "  "}
                ]
            },
        )
        assert r.status_code == 422, r.text

    def test_should_422_on_bad_regex(self, client, auth):
        """保存时就该报错 —— ``match`` 对 ``re.error`` 是静默不匹配的。"""
        pid = _create_graph_project(client, auth)
        r = client.put(
            f"/api/v1/projects/{pid}/category-rules",
            headers=auth,
            json={
                "rules": [
                    {"match_on": "file_name", "kind": "regex", "pattern": "([", "category": "X"}
                ]
            },
        )
        assert r.status_code == 422, r.text
        assert client.get(f"/api/v1/projects/{pid}/category-rules", headers=auth).json() == []

    def test_should_strip_whitespace_on_save(self, client, auth):
        pid = _create_graph_project(client, auth)
        client.put(
            f"/api/v1/projects/{pid}/category-rules",
            headers=auth,
            json={
                "rules": [
                    {
                        "match_on": "file_name",
                        "kind": "prefix",
                        "pattern": "  数学  ",
                        "category": "  高中数学  ",
                    }
                ]
            },
        )
        got = client.get(f"/api/v1/projects/{pid}/category-rules", headers=auth).json()
        assert got[0]["pattern"] == "数学"
        assert got[0]["category"] == "高中数学"

    def test_rejected_batch_must_not_wipe_existing_rules(self, client, auth):
        """整表覆盖 + 校验：一条非法规则不能让整表被清空。"""
        pid = _create_graph_project(client, auth)
        client.put(
            f"/api/v1/projects/{pid}/category-rules",
            headers=auth,
            json={
                "rules": [
                    {"match_on": "file_name", "kind": "prefix", "pattern": "A", "category": "甲"}
                ]
            },
        )
        client.put(
            f"/api/v1/projects/{pid}/category-rules",
            headers=auth,
            json={
                "rules": [
                    {"match_on": "file_name", "kind": "prefix", "pattern": "B", "category": "乙"},
                    {"match_on": "file_name", "kind": "prefix", "pattern": " ", "category": "丙"},
                ]
            },
        )
        got = client.get(f"/api/v1/projects/{pid}/category-rules", headers=auth).json()
        assert [x["pattern"] for x in got] == ["A"], "校验失败时不应先删表"


class TestValidationErrorBody:
    """自定义校验器失败必须回 422 且**带可读 detail**，不能变成 500。

    pydantic 会把校验器抛的 ``ValueError`` 原样留在 ``errors()`` 的 ``ctx`` 里，
    直接丢给 ``JSONResponse`` 会在序列化时炸掉。症状是「请求体非法 → 500」，
    跟真正的服务端崩了长得一样，极难定位。
    """

    def test_validator_error_returns_422_with_readable_detail(self, client, auth):
        pid = _create_graph_project(client, auth)
        r = client.put(
            f"/api/v1/projects/{pid}/category-rules",
            headers=auth,
            json={
                "rules": [
                    {"match_on": "file_name", "kind": "prefix", "pattern": "  ", "category": "X"}
                ]
            },
        )
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["error"]["code"] == "validation"
        blob = json.dumps(body, ensure_ascii=False)
        assert "不能为空或纯空白" in blob, blob

    def test_jsonable_errors_survives_live_exceptions(self):
        """直接钉住清洗函数本身：喂一个活的 ValueError 进去。"""
        raw = [
            {
                "type": "value_error",
                "loc": ("body", "rules", 0, "pattern"),
                "msg": "Value error, 不能为空或纯空白",
                "input": "  ",
                "ctx": {"error": ValueError("不能为空或纯空白")},
            }
        ]
        out = jsonable_errors(raw)
        # 能 dumps 就是过了；再确认没把信息洗没
        text = json.dumps(out, ensure_ascii=False)
        assert "不能为空或纯空白" in text
        assert out[0]["loc"] == ["body", "rules", 0, "pattern"]


class TestNodeCategorySync:
    def _seed(self, client, auth, engine) -> dict:
        pid = _create_graph_project(client, auth)
        with S(engine) as s:
            cat = Category(id="cat-a", project_id=pid, name="甲", sort_order=0)
            s.add(cat)
            node = Node(id="node-1", project_id=pid, category_id=cat.id, name="n", weight=2)
            s.add(node)
            s.commit()
        return {"project_id": pid, "node_id": "node-1"}

    def test_move_node_to_another_category(self, client, auth, engine):
        pid = self._seed(client, auth, engine)["project_id"]
        with S(engine) as s:
            cat2 = Category(id="cat-b", project_id=pid, name="乙", sort_order=1)
            s.add(cat2)
            s.commit()
        now = utcnow() + timedelta(seconds=5)
        r = client.post(
            "/api/v1/sync/batch",
            headers=auth,
            json={
                "ops": [
                    {
                        "op_id": "nc-1",
                        "entity": "node_category",
                        "entity_id": "node-1",
                        "client_ts": now.isoformat(),
                        "patch": {"category_id": "cat-b"},
                    }
                ]
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["results"][0]["status"] == "applied"
        with S(engine) as s:
            node = s.get(Node, "node-1")
            assert node.category_id == "cat-b"

    def test_reject_cross_project_category(self, client, auth, engine):
        self._seed(client, auth, engine)
        other = _create_graph_project(client, auth)
        with S(engine) as s:
            cat = Category(id="cat-other", project_id=other, name="丙", sort_order=0)
            s.add(cat)
            s.commit()
        now = utcnow() + timedelta(seconds=5)
        r = client.post(
            "/api/v1/sync/batch",
            headers=auth,
            json={
                "ops": [
                    {
                        "op_id": "nc-bad",
                        "entity": "node_category",
                        "entity_id": "node-1",
                        "client_ts": now.isoformat(),
                        "patch": {"category_id": "cat-other"},
                    }
                ]
            },
        )
        body = r.json()
        assert body["results"][0]["status"] == "error"
