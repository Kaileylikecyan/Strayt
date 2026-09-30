"""F11 加工引擎（``app.jobs.graph``）测试。

不打真 LLM：``build_plan`` 与 ``resolve_extractor`` 换假件，把引擎语义（草稿落
``results_json``、无模型即失败、续跑不重调、取消在单元边界）单独钉死。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.db.models import File, Job, Project
from app.graph.extract import GraphNodeDraft, GraphUnit
from app.jobs import engine as E
from app.jobs.graph import GraphEngine, GraphExtractorBundle, NoProviderError
from app.llm.base import Usage
from sqlalchemy.orm import Session

FILE_ID = "g" * 32


def _engine():
    from app.db.session import engine

    return engine


@pytest.fixture
def rig() -> Iterator[dict]:
    with Session(_engine()) as s:
        p = Project(name="图谱", type="graph")
        s.add(p)
        s.commit()
        pid = p.id
        s.add(
            File(
                id=FILE_ID,
                project_id=pid,
                server_path=f"{FILE_ID[:2]}/{FILE_ID}.pdf",
                orig_name="a.pdf",
                sha256=FILE_ID * 2,
                size=10,
                parse_channel="text",
            )
        )
        s.commit()
        yield {"project": pid}


def _unit(index: int, title: str) -> E.Unit:
    return E.Unit(
        index=index, title=title, zh=f"{title}正文", en="", page=index + 1, zh_page=index + 1
    )


class FakeExtractor:
    """按调用计数返回预置 GraphUnit，可抛异常。"""

    def __init__(self, unit: GraphUnit | None = None, *, raises: BaseException | None = None):
        self.unit = unit
        self.raises = raises
        self.calls: list[str] = []

    async def extract_async(self, text: str, *, title: str, loc_page: int) -> GraphUnit:
        self.calls.append(title)
        if self.raises is not None:
            raise self.raises
        if self.unit is not None:
            return self.unit
        return _graph_unit(title, loc_page)


def _graph_unit(title: str, loc_page: int) -> GraphUnit:
    return GraphUnit(
        title=title,
        loc_page=loc_page,
        categories=[f"{title}类"],
        nodes=[
            GraphNodeDraft(
                name=f"{title}#0", weight=2, category_index=0, summary=f"{title}概要", quote="原文"
            )
        ],
        usage=Usage(prompt_tokens=100, completion_tokens=20),
    )


def _make_bundle(fx: FakeExtractor) -> GraphExtractorBundle:
    return GraphExtractorBundle(fx, "fake", "fake-model")


def _patch(titles: list[str], bundle: GraphExtractorBundle) -> None:
    from app.jobs import graph as G

    units = [_unit(i, t) for i, t in enumerate(titles)]
    G.build_plan = lambda db, f: E.Plan(file_id=f.id, units=units)  # type: ignore[assignment]
    G.resolve_extractor = lambda db, pid: bundle  # type: ignore[assignment]


def _patch_plan(titles: list[str]) -> None:
    from app.jobs import graph as G

    units = [_unit(i, t) for i, t in enumerate(titles)]
    G.build_plan = lambda db, f: E.Plan(file_id=f.id, units=units)  # type: ignore[assignment]


def _new_job(pid: str) -> str:
    with Session(_engine()) as s:
        j = Job(
            project_id=pid,
            type="graph_extract",
            status="queued",
            checkpoint_json={"file_id": FILE_ID, "done": [], "failed": []},
        )
        s.add(j)
        s.commit()
        return j.id


def _job(jid: str) -> Job:
    with Session(_engine()) as s:
        j = s.get(Job, jid)
        s.expunge(j)
        return j


@pytest.fixture(autouse=True)
def _restore_globals() -> Iterator[None]:
    from app.jobs import graph as G

    real_build, real_resolve = G.build_plan, G.resolve_extractor
    yield
    G.build_plan, G.resolve_extractor = real_build, real_resolve


class Test正常流程:
    @pytest.mark.asyncio
    async def test_should_write_draft_and_finish_success(self, rig):
        fx = FakeExtractor()
        _patch(["甲", "乙"], _make_bundle(fx))
        jid = _new_job(rig["project"])
        assert await GraphEngine(jid).run() == "success"
        j = _job(jid)
        assert j.status == "success"
        assert j.progress == 100
        assert j.done_units == 2
        res = j.results_json
        assert set(res["units"]) == {"0000:甲", "0001:乙"}
        assert res["status"] == "pending_confirm"
        assert len(res["draft"]["nodes"]) == 2
        assert j.tokens_used == 240  # 100+20 每篇
        assert len(fx.calls) == 2

    @pytest.mark.asyncio
    async def test_should_fail_when_has_no_done_unit(self, rig):
        fx = FakeExtractor(unit=GraphUnit(title="空", loc_page=1, categories=[], nodes=[]))
        _patch(["甲"], _make_bundle(fx))
        jid = _new_job(rig["project"])
        assert await GraphEngine(jid).run() == "failed"
        j = _job(jid)
        assert j.status == "failed"
        assert "所有篇目" in j.error

    @pytest.mark.asyncio
    async def test_should_fail_job_without_llm(self, rig):
        from app.jobs import graph as G

        _patch_plan(["甲"])

        def _boom(db, pid):
            raise NoProviderError("项目未绑定模型档案")

        G.resolve_extractor = _boom  # type: ignore[assignment]
        jid = _new_job(rig["project"])
        assert await GraphEngine(jid).run() == "failed"
        assert "未绑定模型档案" in _job(jid).error
        assert _job(jid).done_units == 0

    @pytest.mark.asyncio
    async def test_should_cancel_at_unit_boundary(self, rig):
        fx = FakeExtractor()
        _patch(["甲", "乙"], _make_bundle(fx))
        eng = GraphEngine(_new_job(rig["project"]))
        eng.on_unit = lambda _u, _o: eng.request_cancel()
        assert await eng.run() == "cancelled"
        j = _job(eng.job_id)
        assert j.status == "cancelled"
        assert j.done_units == 1
        assert len(fx.calls) == 1
        assert "0000:甲" in j.results_json["units"]
        assert "draft" not in j.results_json

    @pytest.mark.asyncio
    async def test_should_empty_source_fail_unit(self, rig):
        from app.jobs import graph as G

        def _empty(db, f):
            return E.Plan(
                file_id=f.id, units=[E.Unit(index=0, title="无正文", zh="", en="", page=1)]
            )

        G.build_plan = _empty  # type: ignore[assignment]
        G.resolve_extractor = lambda db, pid: _make_bundle(FakeExtractor())  # type: ignore[assignment]
        jid = _new_job(rig["project"])
        assert await GraphEngine(jid).run() == "failed"
        j = _job(jid)
        assert j.failed_units_json[0]["reason"] == "empty_source"
        assert len(j.failed_units_json) == 1


class Test续跑:
    @pytest.mark.asyncio
    async def test_should_not_recall_llm_on_resume(self, rig):
        fx = FakeExtractor()
        _patch(["甲", "乙"], _make_bundle(fx))
        jid = _new_job(rig["project"])
        await GraphEngine(jid).run()
        assert len(fx.calls) == 2

        fx2 = FakeExtractor()
        _patch(["甲", "乙"], _make_bundle(fx2))
        with Session(_engine()) as s:
            s.get(Job, jid).status = "queued"
            s.commit()
        assert await GraphEngine(jid).run() == "success"
        assert fx2.calls == [], "完成过的篇不该再调模型"
        assert _job(jid).done_units == 2
        assert len(_job(jid).results_json["draft"]["nodes"]) == 2

    @pytest.mark.asyncio
    async def test_should_preserve_tokens_across_resume(self, rig):
        fx = FakeExtractor()
        _patch(["甲", "乙"], _make_bundle(fx))
        jid = _new_job(rig["project"])
        await GraphEngine(jid).run()
        first = _job(jid).tokens_used

        _patch(["甲", "乙"], _make_bundle(FakeExtractor()))
        with Session(_engine()) as s:
            s.get(Job, jid).status = "queued"
            s.commit()
        await GraphEngine(jid).run()
        assert _job(jid).tokens_used == first

    @pytest.mark.asyncio
    async def test_should_finalize_idempotently_from_results(self, rig):
        """收尾只读 results_json：崩在收尾前，续跑不会重调模型、草稿仍在。"""
        fx = FakeExtractor()
        _patch(["甲", "乙"], _make_bundle(fx))
        jid = _new_job(rig["project"])
        await GraphEngine(jid).run()
        # 模拟收尾前进程被杀
        with Session(_engine()) as s:
            s.get(Job, jid).status = "failed"
            s.commit()
        fx2 = FakeExtractor(raises=RuntimeError("收尾不该再调抽取"))
        _patch(["甲", "乙"], _make_bundle(fx2))
        assert await GraphEngine(jid).run() == "success"
        assert fx2.calls == []
        j = _job(jid)
        assert len(j.results_json["draft"]["nodes"]) == 2
        assert j.results_json["status"] == "pending_confirm"
