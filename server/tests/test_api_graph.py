"""图谱路由契约测试：草稿预览、确认入库、图谱拉取、创建校验。

引擎内部行为在 ``test_graph_jobs.py``。这里走 TestClient 验路由层：
入队校验（项目类型、LLM 配置）、确认的三种调整与覆盖语义。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.core.errors import ErrorCode
from app.db.models import File, Job, Project
from sqlalchemy.orm import Session

FILE_ID = "h" * 32

DRAFT = {
    "unit_count": 1,
    "warnings": [],
    "categories": [{"key": "c000", "name": "上海概况", "sort_order": 0}],
    "nodes": [
        {
            "key": "u000:n00",
            "name": "水网",
            "weight": 3,
            "summary": "水网是城市命脉",
            "quote": "城市的淋巴系统和命脉，就是密布的水网。",
            "loc_page": 4,
            "category_key": "c000",
            "links": [{"to": "u000:n01", "reason": "同为城市结构"}],
            "edited": False,
        },
        {
            "key": "u000:n01",
            "name": "航运",
            "weight": 1,
            "summary": "航运与贸易交汇",
            "quote": "自开埠以来，航运与贸易在此交汇。",
            "loc_page": 4,
            "category_key": "c000",
            "links": [],
            "edited": False,
        },
    ],
    "edges": [{"from": "u000:n00", "to": "u000:n01", "reason": "同为城市结构"}],
}


def _engine():
    from app.db.session import engine

    return engine


@pytest.fixture
def rig(client, auth, monkeypatch) -> Iterator[dict]:
    """graph/recite 项目 + 假 run_manager，避免在同步路由里起 asyncio 任务。"""
    with Session(_engine()) as s:
        recite = Project(name="背诵", type="recite")
        graph = Project(name="图谱", type="graph")
        s.add_all([recite, graph])
        s.commit()
        s.add(
            File(
                id=FILE_ID,
                project_id=graph.id,
                server_path=f"{FILE_ID[:2]}/{FILE_ID}.pdf",
                orig_name="a.pdf",
                sha256=FILE_ID * 2,
                size=10,
                parse_channel="text",
            )
        )
        s.commit()
        rid, gid = recite.id, graph.id

    submitted: list[str] = []

    class FakeManager:
        def submit(self, job_id: str, *, overwrite: bool = False) -> None:
            submitted.append(job_id)

        def request_cancel(self, job_id: str) -> None:
            pass

    import app.api.routers.jobs as jobs_router
    from app.jobs.graph import GraphExtractorBundle

    monkeypatch.setattr(jobs_router, "run_manager", FakeManager())
    monkeypatch.setattr(
        jobs_router,
        "resolve_extractor",
        lambda db, pid: GraphExtractorBundle(object(), "deepseek", "deepseek-chat"),
    )
    yield {"recite": rid, "graph": gid, "client": client, "auth": auth, "submitted": submitted}


def _mk_job(pid: str, *, type: str = "graph_extract", **kw) -> str:
    with Session(_engine()) as s:
        j = Job(
            project_id=pid,
            type=type,
            status=kw.pop("status", "success"),
            checkpoint_json=kw.pop("checkpoint_json", {"file_id": FILE_ID}),
        )
        if kw.pop("with_results", True):
            j.results_json = {"units": {}, "draft": DRAFT, "status": "pending_confirm"}
        s.add(j)
        s.commit()
        return j.id


class Test创建校验:
    def test_should_reject_wrong_type_for_project(self, rig):
        r = rig["client"].post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": FILE_ID, "type": "graph_extract"},
            headers=rig["auth"],
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == ErrorCode.VALIDATION

    def test_should_reject_graph_job_typed_as_recite(self, rig):
        r = rig["client"].post(
            "/api/v1/jobs",
            json={"project_id": rig["graph"], "file_id": FILE_ID, "type": "recite_align"},
            headers=rig["auth"],
        )
        assert r.status_code == 409

    def test_should_reject_unknown_type(self, rig):
        r = rig["client"].post(
            "/api/v1/jobs",
            json={"project_id": rig["graph"], "file_id": FILE_ID, "type": "bogus"},
            headers=rig["auth"],
        )
        assert r.status_code == 422  # Literal 校验直接拦

    def test_should_reject_when_no_llm_bound(self, rig, monkeypatch):
        import app.api.routers.jobs as jobs_router
        from app.jobs.graph import NoProviderError

        def _boom(db, pid):
            raise NoProviderError("项目未绑定模型档案")

        monkeypatch.setattr(jobs_router, "resolve_extractor", _boom)
        r = rig["client"].post(
            "/api/v1/jobs",
            json={"project_id": rig["graph"], "file_id": FILE_ID, "type": "graph_extract"},
            headers=rig["auth"],
        )
        assert r.status_code == 409
        assert "未绑定模型档案" in r.json()["error"]["message"]

    def test_should_create_and_submit_graph_job(self, rig):
        r = rig["client"].post(
            "/api/v1/jobs",
            json={"project_id": rig["graph"], "file_id": FILE_ID, "type": "graph_extract"},
            headers=rig["auth"],
        )
        assert r.status_code == 201
        assert r.json()["type"] == "graph_extract"
        assert rig["submitted"] == [r.json()["id"]]
        with Session(_engine()) as s:
            assert s.get(Job, r.json()["id"]).checkpoint_json["file_id"] == FILE_ID


class Test草稿:
    def test_should_return_draft(self, rig):
        jid = _mk_job(rig["graph"])
        r = rig["client"].get(f"/api/v1/jobs/{jid}/graph-draft", headers=rig["auth"])
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "pending_confirm"
        assert len(body["draft"]["nodes"]) == 2

    def test_should_404_unknown_job(self, rig):
        r = rig["client"].get(f"/api/v1/jobs/{'9' * 32}/graph-draft", headers=rig["auth"])
        assert r.status_code == 404

    def test_should_409_when_no_draft(self, rig):
        jid = _mk_job(rig["graph"], with_results=False)
        r = rig["client"].get(f"/api/v1/jobs/{jid}/graph-draft", headers=rig["auth"])
        assert r.status_code == 409
        assert r.json()["error"]["code"] == ErrorCode.GRAPH_NO_DRAFT


class Test确认:
    def test_should_confirm_and_persist(self, rig):
        jid = _mk_job(rig["graph"])
        r = rig["client"].post(
            f"/api/v1/jobs/{jid}/graph-confirm",
            json={"delete_keys": [], "reassign": {}, "edits": {}},
            headers=rig["auth"],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["node_count"] == 2
        assert body["category_count"] == 1
        assert body["edge_count"] == 1

        g = rig["client"].get(f"/api/v1/projects/{rig['graph']}/graph", headers=rig["auth"]).json()
        assert {n["name"] for n in g["nodes"]} == {"水网", "航运"}
        assert all(n["quote"]["text"] for n in g["nodes"])
        assert len(g["edges"]) == 1
        assert g["edges"][0]["from_node"] != g["edges"][0]["to_node"]

    def test_should_apply_edits_before_persist(self, rig):
        jid = _mk_job(rig["graph"])
        r = rig["client"].post(
            f"/api/v1/jobs/{jid}/graph-confirm",
            json={
                "delete_keys": ["u000:n01"],
                "reassign": {},
                "edits": {"u000:n00": {"name": "改名水网"}},
            },
            headers=rig["auth"],
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["node_count"] == 1
        assert body["edited_nodes"] == 1
        g = rig["client"].get(f"/api/v1/projects/{rig['graph']}/graph", headers=rig["auth"]).json()
        assert g["nodes"][0]["name"] == "改名水网"
        assert g["nodes"][0]["edited"] is True
        assert g["edges"] == []

    def test_should_reject_confirm_when_all_nodes_deleted(self, rig):
        jid = _mk_job(rig["graph"])
        r = rig["client"].post(
            f"/api/v1/jobs/{jid}/graph-confirm",
            json={"delete_keys": ["u000:n00", "u000:n01"]},
            headers=rig["auth"],
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == ErrorCode.GRAPH_REJECTED

    def test_should_409_when_job_not_success(self, rig):
        jid = _mk_job(rig["graph"], status="failed")
        r = rig["client"].post(
            f"/api/v1/jobs/{jid}/graph-confirm",
            json={},
            headers=rig["auth"],
        )
        assert r.status_code == 409

    def test_should_409_when_job_is_not_graph(self, rig):
        jid = _mk_job(rig["graph"], type="recite_align", with_results=False)
        r = rig["client"].post(f"/api/v1/jobs/{jid}/graph-confirm", json={}, headers=rig["auth"])
        assert r.status_code == 409


class Test图谱拉取:
    def test_should_404_missing_project(self, rig):
        r = rig["client"].get(f"/api/v1/projects/{'0' * 32}/graph", headers=rig["auth"])
        assert r.status_code == 404

    def test_should_return_empty_graph_before_confirm(self, rig):
        r = rig["client"].get(f"/api/v1/projects/{rig['graph']}/graph", headers=rig["auth"])
        assert r.status_code == 200
        assert r.json()["nodes"] == []
        assert r.json()["edges"] == []
