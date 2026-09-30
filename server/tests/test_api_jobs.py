"""``/api/v1/jobs`` 路由契约测试。

重点验"路由层"的职责：入队校验、状态机、以及**真的把任务交给引擎**。
引擎内部行为在 ``test_jobs.py`` 里单独验。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.core.errors import ErrorCode
from app.db.models import File, Job, Project
from app.jobs import engine as E
from sqlalchemy.orm import Session

FILE_ID = "f" * 32


def _engine():
    from app.db.session import engine

    return engine


@pytest.fixture
def rig(engine) -> Iterator[dict]:
    """一个背诵项目 + 一个挂着的文件，以及一个不启动后台的假 run_manager。"""
    with Session(_engine()) as s:
        recite = Project(name="背诵", type="recite")
        graph = Project(name="图谱", type="graph")
        s.add_all([recite, graph])
        s.commit()
        s.add(
            File(
                id=FILE_ID,
                project_id=recite.id,
                server_path=f"{FILE_ID[:2]}/{FILE_ID}.pdf",
                orig_name="a.pdf",
                sha256=FILE_ID * 2,
                size=10,
                parse_channel="text",
            )
        )
        s.commit()
        rid, gid = recite.id, graph.id

    submitted: list[tuple[str, bool]] = []
    resumed: list[str] = []
    cancelled: list[str] = []

    class FakeManager:
        def submit(self, job_id: str, *, overwrite: bool = False) -> None:
            submitted.append((job_id, overwrite))

        def resume(self, job_id: str, *, overwrite: bool = False) -> None:
            resumed.append(job_id)

        def request_cancel(self, job_id: str) -> None:
            cancelled.append(job_id)

    original = E.run_manager
    import app.api.routers.jobs as jobs_router

    jobs_router.run_manager = FakeManager()  # type: ignore[assignment]
    try:
        yield {
            "recite": rid,
            "graph": gid,
            "file": FILE_ID,
            "submitted": submitted,
            "resumed": resumed,
            "cancelled": cancelled,
        }
    finally:
        jobs_router.run_manager = original  # type: ignore[assignment]


class Test创建:
    def test_should_create_and_submit(self, client, auth, rig):
        r = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "queued"
        assert body["type"] == "recite_align"
        assert body["total_units"] == 0
        assert rig["submitted"] == [(body["id"], False)]

    def test_should_store_file_id_in_checkpoint(self, client, auth, rig):
        r = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        )
        jid = r.json()["id"]
        with Session(_engine()) as s:
            ckpt = s.get(Job, jid).checkpoint_json
        assert ckpt["file_id"] == rig["file"]
        assert ckpt["done"] == []
        assert ckpt["failed"] == []

    def test_should_pass_overwrite_flag_through(self, client, auth, rig):
        client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"], "overwrite": True},
            headers=auth,
        )
        assert rig["submitted"][0][1] is True

    def test_should_reject_graph_project(self, client, auth, rig):
        """图谱项目走的是知识图谱加工，不该被对齐任务收下。"""
        r = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["graph"], "file_id": rig["file"]},
            headers=auth,
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == ErrorCode.VALIDATION

    def test_should_reject_unknown_project(self, client, auth, rig):
        r = client.post(
            "/api/v1/jobs",
            json={"project_id": "0" * 32, "file_id": rig["file"]},
            headers=auth,
        )
        assert r.status_code == 404

    def test_should_reject_unknown_file(self, client, auth, rig):
        r = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": "0" * 32},
            headers=auth,
        )
        assert r.status_code == 404

    def test_should_reject_file_of_other_project(self, client, auth, rig):
        with Session(_engine()) as s:
            s.add(
                File(
                    id="9" * 32,
                    project_id=rig["graph"],
                    server_path="99/x.pdf",
                    orig_name="b.pdf",
                    sha256="9" * 64,
                    size=1,
                    parse_channel="text",
                )
            )
            s.commit()
        r = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": "9" * 32},
            headers=auth,
        )
        assert r.status_code == 409
        assert "不属于该项目" in r.json()["error"]["message"]

    def test_should_reject_unknown_type(self, client, auth, rig):
        r = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"], "type": "graph_build"},
            headers=auth,
        )
        assert r.status_code == 422

    def test_should_require_token(self, client, rig):
        r = client.post("/api/v1/jobs", json={"project_id": rig["recite"], "file_id": rig["file"]})
        assert r.status_code == 401
        assert rig["submitted"] == []


class Test查询:
    @pytest.fixture
    def jid(self, client, auth, rig) -> str:
        r = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        )
        return r.json()["id"]

    def test_should_list_by_project(self, client, auth, rig, jid):
        r = client.get("/api/v1/jobs", params={"project_id": rig["recite"]}, headers=auth)
        assert r.status_code == 200
        assert [j["id"] for j in r.json()] == [jid]

    def test_should_filter_by_status(self, client, auth, rig, jid):
        with Session(_engine()) as s:
            s.get(Job, jid).status = "success"
            s.commit()
        r = client.get(
            "/api/v1/jobs", params={"project_id": rig["recite"], "status": "queued"}, headers=auth
        )
        assert r.json() == []
        r = client.get(
            "/api/v1/jobs", params={"project_id": rig["recite"], "status": "success"}, headers=auth
        )
        assert [j["id"] for j in r.json()] == [jid]
        # F8 新增的熔断续跑状态进得了过滤（正则同步放行）
        with Session(_engine()) as s:
            s.get(Job, jid).status = "interrupted"
            s.commit()
        r = client.get(
            "/api/v1/jobs",
            params={"project_id": rig["recite"], "status": "interrupted"},
            headers=auth,
        )
        assert [j["id"] for j in r.json()] == [jid]

    def test_should_get_one_with_units(self, client, auth, rig, jid):
        with Session(_engine()) as s:
            j = s.get(Job, jid)
            j.total_units = 2
            j.units_json = [
                {"key": "0000:甲", "index": 0, "title": "甲", "page": 1, "status": "created"},
                {"key": "0001:乙", "index": 1, "title": "乙", "page": 2, "status": "failed"},
            ]
            j.progress = 100
            s.commit()
        body = client.get(f"/api/v1/jobs/{jid}", headers=auth).json()
        assert body["total_units"] == 2
        assert body["progress"] == 100
        assert [u["status"] for u in body["units_json"]] == ["created", "failed"]

    def test_should_404_unknown(self, client, auth, rig):
        assert client.get("/api/v1/jobs/" + "0" * 32, headers=auth).status_code == 404


class Test取消:
    def test_should_cancel_and_notify_engine(self, client, auth, rig):
        jid = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        ).json()["id"]
        r = client.post(f"/api/v1/jobs/{jid}/cancel", headers=auth)
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        assert rig["cancelled"] == [jid]

    def test_should_reject_cancel_of_finished(self, client, auth, rig):
        jid = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        ).json()["id"]
        with Session(_engine()) as s:
            s.get(Job, jid).status = "success"
            s.commit()
        r = client.post(f"/api/v1/jobs/{jid}/cancel", headers=auth)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == ErrorCode.JOB_NOT_RUNNABLE
        assert rig["cancelled"] == []

    def test_should_reject_double_cancel(self, client, auth, rig):
        jid = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        ).json()["id"]
        client.post(f"/api/v1/jobs/{jid}/cancel", headers=auth)
        assert client.post(f"/api/v1/jobs/{jid}/cancel", headers=auth).status_code == 409

    def test_should_cancel_interrupted(self, client, auth, rig):
        """熔断停下的任务还没跑完，用户也可以选择取消而不是继续。"""
        jid = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        ).json()["id"]
        with Session(_engine()) as s:
            s.get(Job, jid).status = "interrupted"
            s.commit()
        r = client.post(f"/api/v1/jobs/{jid}/cancel", headers=auth)
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"

    def test_should_404_unknown(self, client, auth, rig):
        assert client.post(f"/api/v1/jobs/{'0' * 32}/cancel", headers=auth).status_code == 404


class Test续跑:
    def _failed(self, client, auth, rig) -> str:
        jid = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        ).json()["id"]
        with Session(_engine()) as s:
            j = s.get(Job, jid)
            j.status = "failed"
            j.error = "进程被杀了"
            s.commit()
        return jid

    def test_should_requeue_and_resume(self, client, auth, rig):
        jid = self._failed(client, auth, rig)
        r = client.post(f"/api/v1/jobs/{jid}/resume", headers=auth)
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["error"] is None, "续跑要清掉上次的错误，否则界面一直显示红字"
        assert rig["resumed"] == [jid]

    def test_should_resume_cancelled(self, client, auth, rig):
        jid = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        ).json()["id"]
        client.post(f"/api/v1/jobs/{jid}/cancel", headers=auth)
        assert client.post(f"/api/v1/jobs/{jid}/resume", headers=auth).status_code == 200

    def test_should_resume_interrupted(self, client, auth, rig):
        """F8：熔断续跑 = 「用户确认继续」，清掉超支红字后重新入队。"""
        jid = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        ).json()["id"]
        with Session(_engine()) as s:
            j = s.get(Job, jid)
            j.status = "interrupted"
            j.error = "实际花费已超过预估 50%"
            s.commit()
        r = client.post(f"/api/v1/jobs/{jid}/resume", headers=auth)
        assert r.status_code == 200
        assert r.json()["status"] == "queued"
        assert r.json()["error"] is None, "续跑要清掉超支红字"
        assert rig["resumed"] == [jid]

    def test_should_reject_resume_of_running(self, client, auth, rig):
        jid = client.post(
            "/api/v1/jobs",
            json={"project_id": rig["recite"], "file_id": rig["file"]},
            headers=auth,
        ).json()["id"]
        with Session(_engine()) as s:
            s.get(Job, jid).status = "running"
            s.commit()
        assert client.post(f"/api/v1/jobs/{jid}/resume", headers=auth).status_code == 409
        assert rig["resumed"] == []

    def test_should_404_unknown(self, client, auth, rig):
        assert client.post(f"/api/v1/jobs/{'0' * 32}/resume", headers=auth).status_code == 404


class Test真实RunManager:
    """同步路由（FastAPI 线程池）里用真 ``RunManager`` 起任务。

    之前所有用例都把 ``run_manager`` 换成假对象，恰好绕开了真问题：
    线程池里没有运行中的事件循环，``asyncio.create_task`` 会 RuntimeError → 500。
    这里用真对象 + 已绑定的 loop，确保建任务不再炸。
    """

    def test_should_bind_loop_and_start_from_sync_route(self, client, auth, rig, monkeypatch):
        import asyncio
        import threading
        import time

        import app.api.routers.jobs as jobs_router

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        started: list[str] = []

        class RecordingManager(E.RunManager):
            def _start(self, job_id: str, overwrite: bool) -> None:
                started.append(job_id)

        mgr = RecordingManager()
        mgr.bind_loop(loop)
        monkeypatch.setattr(jobs_router, "run_manager", mgr)
        try:
            r = client.post(
                "/api/v1/jobs",
                json={"project_id": rig["recite"], "file_id": rig["file"]},
                headers=auth,
            )
            assert r.status_code == 201, r.text
            for _ in range(100):  # 等主 loop 把回调跑掉
                if started:
                    break
                time.sleep(0.02)
            assert started == [r.json()["id"]], "任务没有被交给引擎"
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

    def test_should_raise_when_no_loop_bound(self):
        """没绑 loop 就起任务要显式报错，不能静默丢任务。"""
        mgr = E.RunManager()
        with pytest.raises(RuntimeError, match="事件循环"):
            mgr.resume("f" * 32)


class Test重启恢复:
    def test_should_mark_orphaned_running_job_interrupted(self, engine):
        """服务端重启后没有协程在跑，任务不能永远卡 running（客户端点不了续跑）。"""
        with Session(_engine()) as s:
            p = Project(name="重启", type="recite")
            s.add(p)
            s.commit()
            s.add(
                File(
                    project_id=p.id,
                    server_path="aa/a.pdf",
                    orig_name="a.pdf",
                    sha256="a" * 64,
                    size=1,
                    parse_channel="text",
                )
            )
            s.commit()
            f = s.query(File).one()
            j = Job(project_id=p.id, type="recite_align", status="running")
            j.checkpoint_json = {"file_id": f.id, "done": [], "failed": []}
            s.add(j)
            s.commit()
            jid = j.id

        assert E.recover_orphaned_jobs() == 1
        with Session(_engine()) as s:
            j = s.get(Job, jid)
            assert j.status == "interrupted"
            assert "重启" in (j.error or "")
        # 恢复后不该再反复命中
        assert E.recover_orphaned_jobs() == 0
