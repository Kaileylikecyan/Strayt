"""F16 掌握度：聚合统计、直接设置、弱同步回写。

数据直接造（跳过确认流程），因为统计接口只关心库里的最终态。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from app.db.models import Category, Node, Project, utcnow
from sqlalchemy.orm import Session

NODE_IDS = {"a": "a" * 32, "b": "b" * 32, "c": "c" * 32, "d": "d" * 32}


@pytest.fixture
def master(db_session: Session) -> dict:
    p = Project(name="图谱", type="graph")
    db_session.add(p)
    db_session.commit()
    pid = p.id

    c1 = Category(project_id=pid, name="上海概况", sort_order=0)
    c2 = Category(project_id=pid, name="外滩", sort_order=1)
    db_session.add_all([c1, c2])
    db_session.flush()

    db_session.add_all(
        [
            Node(id=NODE_IDS["a"], project_id=pid, category_id=c1.id, name="水网", mastery="no"),
            Node(id=NODE_IDS["b"], project_id=pid, category_id=c1.id, name="航运", mastery="mid"),
            Node(
                id=NODE_IDS["c"], project_id=pid, category_id=c2.id, name="万国建筑", mastery="yes"
            ),
            Node(id=NODE_IDS["d"], project_id=pid, category_id=None, name="无主节点", mastery="no"),
        ]
    )
    db_session.commit()
    return {"pid": pid, "c1": c1.id, "c2": c2.id}


class Test统计:
    def test_should_aggregate_overall_and_by_category(self, client, auth, master):
        r = client.get(f"/api/v1/projects/{master['pid']}/mastery-stats", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 4
        assert body["by_mastery"] == {"no": 2, "mid": 1, "yes": 1}
        assert body["mastery_ratio"] == 0.5
        names = {x["name"]: x for x in body["by_category"]}
        assert names["上海概况"]["total"] == 2
        assert names["上海概况"]["no"] == 1
        assert names["上海概况"]["mid"] == 1
        assert names["上海概况"]["yes"] == 0
        assert names["外滩"]["yes"] == 1
        assert names["未分类"]["total"] == 1

    def test_should_return_zero_on_empty_project(self, client, auth, db_session):
        p = Project(name="空", type="graph")
        db_session.add(p)
        db_session.commit()
        body = client.get(f"/api/v1/projects/{p.id}/mastery-stats", headers=auth).json()
        assert body["total"] == 0
        assert body["by_mastery"] == {"no": 0, "mid": 0, "yes": 0}
        assert body["mastery_ratio"] == 0.0

    def test_should_404_missing_project(self, client, auth):
        r = client.get(f"/api/v1/projects/{'e' * 32}/mastery-stats", headers=auth)
        assert r.status_code == 404


class Test直接设置:
    def _read_mastery(self, engine, node_id: str) -> str:
        with Session(engine) as s:
            n = s.get(Node, node_id)
            return n.mastery if n else "<none>"

    def test_should_set_mastery(self, client, auth, master, engine):
        r = client.put(
            f"/api/v1/nodes/{NODE_IDS['a']}/mastery", json={"mastery": "yes"}, headers=auth
        )
        assert r.status_code == 200
        assert r.json()["mastery"] == "yes"
        assert self._read_mastery(engine, NODE_IDS["a"]) == "yes"

    def test_should_reject_bad_value(self, client, auth, master):
        r = client.put(
            f"/api/v1/nodes/{NODE_IDS['a']}/mastery", json={"mastery": "huge"}, headers=auth
        )
        assert r.status_code == 422

    def test_should_404_missing_node(self, client, auth, master):
        r = client.put(f"/api/v1/nodes/{'f' * 32}/mastery", json={"mastery": "mid"}, headers=auth)
        assert r.status_code == 404


class Test弱同步:
    def test_should_apply_node_mastery_entity(self, client, auth, master):
        r = client.post(
            "/api/v1/sync/batch",
            json={
                "ops": [
                    {
                        "op_id": "op-1",
                        "entity": "node_mastery",
                        "entity_id": NODE_IDS["a"],
                        "client_ts": (utcnow() + timedelta(seconds=5)).isoformat(),
                        "patch": {"mastery": "mid"},
                    }
                ]
            },
            headers=auth,
        )
        assert r.status_code == 200
        assert r.json()["results"][0]["status"] == "applied"

    def test_should_reject_unknown_field_for_node_mastery(self, client, auth, master):
        r = client.post(
            "/api/v1/sync/batch",
            json={
                "ops": [
                    {
                        "op_id": "op-2",
                        "entity": "node_mastery",
                        "entity_id": NODE_IDS["a"],
                        "client_ts": (utcnow() + timedelta(seconds=5)).isoformat(),
                        "patch": {"name": "改名"},
                    }
                ]
            },
            headers=auth,
        )
        assert r.json()["results"][0]["status"] == "error"

    def test_should_lww_conflict_on_stale_write(self, client, auth, master):
        old = datetime(2020, 1, 1)
        r = client.post(
            "/api/v1/sync/batch",
            json={
                "ops": [
                    {
                        "op_id": "op-3",
                        "entity": "node_mastery",
                        "entity_id": NODE_IDS["a"],
                        "client_ts": old.isoformat(),
                        "patch": {"mastery": "no"},
                    }
                ]
            },
            headers=auth,
        )
        assert r.json()["results"][0]["status"] == "conflict"
