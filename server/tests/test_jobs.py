"""加工任务引擎（``app.jobs.engine``）测试。

不打真实 LLM，也不解析真实 PDF：``build_plan`` 与 ``resolve_aligner`` 两个入口
被替换成可控的假实现，把引擎本身的语义（事务边界、checkpoint、取消、续跑、计费）
单独钉死。真实链路另有 ``var_test/quality_e2e.py``。
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.align.base import Aligner, AlignError, AlignResult, PairDraft
from app.db.models import File, FileParse, Job, Pair, Piece, Project, new_id
from app.db.session import SessionLocal
from app.jobs import engine as E
from app.llm.base import Usage
from app.parse.base import ParseError
from app.persist import SaveOutcome
from app.storage.local import get_storage
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
FILE_ID = "e" * 32

ZH = "上海的快速发展吸引了大批投资者前来兴业，产业升级持续推进。"


def _engine():
    from app.db.session import engine

    return engine


# --------------------------------------------------------------------------
# 假件
# --------------------------------------------------------------------------
class FakeAligner(Aligner):
    """按段数一一对应的假对齐器。"""

    def __init__(self, *, usage: Usage | None = None) -> None:
        self.calls: list[int] = []
        self._usage = usage
        self.raises: BaseException | None = None
        self.gate = asyncio.Event()
        self.gate.set()

    @property
    def provider(self) -> str | None:
        return "fake"

    @property
    def model(self) -> str | None:
        return "fake-model"

    def _result(self, zh_segments: list[str], en_segments: list[str], loc_page: int) -> AlignResult:
        n = min(len(zh_segments), len(en_segments))
        pairs = [
            PairDraft(
                seq=i,
                zh=zh_segments[i] + "补长" * (i * 5),
                en=en_segments[i] + " pad " * (i * 12),
                loc_page=loc_page,
                confidence=0.9,
                how="llm",
            )
            for i in range(n)
        ]
        return AlignResult(pairs=pairs, mode="llm", used_llm=True, agreement=0.9, usage=self._usage)

    def align(
        self, zh_segments: list[str], en_segments: list[str], *, loc_page: int = 0
    ) -> AlignResult:
        """同步入口。引擎只走异步版，这里只是为了满足抽象基类。"""
        return self._result(zh_segments, en_segments, loc_page)

    async def align_async(
        self, zh_segments: list[str], en_segments: list[str], *, loc_page: int = 0
    ) -> AlignResult:
        self.calls.append(len(zh_segments))
        await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        return self._result(zh_segments, en_segments, loc_page)


def _unit(index: int, title: str, *, zh: str | None = None, en: str | None = None) -> E.Unit:
    return E.Unit(
        index=index,
        title=title,
        zh=zh if zh is not None else f"{title}。{ZH}",
        en=en if en is not None else f"{title} in English. " * 6,
        page=index + 1,
        zh_page=index + 1,
        en_page=index + 1,
    )


def _patch(titles: list[str], aligner: FakeAligner, mode: str = "llm") -> None:
    """把解析与对齐都换成假件。"""
    E.build_plan = _plan_of(*titles)  # type: ignore[assignment]
    E.resolve_aligner = lambda db, pid: E.AlignerBundle(  # type: ignore[assignment]
        aligner, mode, aligner.provider, aligner.model
    )


def _u(index: int, title: str) -> E.Unit:
    return _unit(index, title)


def _plan_of(*titles: str):
    """造一个返回固定篇目清单的 ``build_plan``。"""
    units = [_unit(i, t) for i, t in enumerate(titles)]

    def _build(db, file_row) -> E.Plan:
        return E.Plan(file_id=file_row.id, units=units)

    return _build


class _AlwaysFail(Aligner):
    """每篇都过不了质量门控。"""

    provider = "fake"
    model = "fake-model"

    def align(self, zh_segments, en_segments, loc_page):
        return AlignResult(pairs=[], warnings=["质量未过"])


@pytest.fixture
def project_id() -> str:
    with Session(_engine()) as s:
        p = Project(name="任务项目", type="recite")
        s.add(p)
        s.commit()
        pid = p.id
    with Session(_engine()) as s:
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
    return pid


def _new_job(pid: str, ckpt: dict | None = None, **kw) -> str:
    with Session(_engine()) as s:
        j = Job(
            project_id=pid,
            type="recite_align",
            status="queued",
            checkpoint_json=ckpt
            if ckpt is not None
            else {"file_id": FILE_ID, "done": [], "failed": []},
            **kw,
        )
        s.add(j)
        s.commit()
        return j.id


def _job(jid: str) -> Job:
    with Session(_engine()) as s:
        j = s.get(Job, jid)
        s.expunge(j)
        return j


def _pieces(pid: str) -> list[Piece]:
    with Session(_engine()) as s:
        rows = list(
            s.scalars(select(Piece).where(Piece.project_id == pid).order_by(Piece.sort_order))
        )
        return [(r.id, r.title) for r in rows]  # type: ignore[misc]


def _pair_count(pid: str) -> int:
    with Session(_engine()) as s:
        ids = select(Pair.piece_id).where(Pair.piece_id.in_(select(Piece.id)))
        return len(list(s.scalars(select(Pair.id).where(Pair.piece_id.in_(ids)))))


# --------------------------------------------------------------------------
class Test主流程:
    @pytest.mark.asyncio
    async def test_should_write_all_pieces_and_finish_success(self, project_id):
        al = FakeAligner()
        _patch(["甲", "乙", "丙"], al)
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "success"
        j = _job(jid)
        assert j.status == "success"
        assert j.progress == 100
        assert j.total_units == 3
        assert j.done_units == 3
        assert [t for _, t in _pieces(project_id)] == ["甲", "乙", "丙"]
        assert _pair_count(project_id) == 6
        assert len(al.calls) == 3

    @pytest.mark.asyncio
    async def test_should_mark_pending_units_in_units_json(self, project_id):
        _patch(["甲", "乙"], FakeAligner())
        jid = _new_job(project_id)
        await E.JobEngine(jid).run()
        j = _job(jid)
        assert [u["status"] for u in j.units_json] == ["created", "created"]
        assert all(u["key"] for u in j.units_json)
        assert all(u["page"] >= 1 for u in j.units_json)

    @pytest.mark.asyncio
    async def test_should_not_start_when_already_cancelled(self, project_id):
        al = FakeAligner()
        _patch(["甲"], al)
        jid = _new_job(project_id, ckpt={"file_id": FILE_ID, "done": [], "failed": []})
        with Session(_engine()) as s:
            j = s.get(Job, jid)
            j.status = "cancelled"
            s.commit()
        assert await E.JobEngine(jid).run() == "cancelled"
        assert al.calls == []
        assert _pieces(project_id) == []

    @pytest.mark.asyncio
    async def test_should_fail_when_file_id_missing(self, project_id):
        _patch(["甲"], FakeAligner())
        jid = _new_job(project_id, ckpt={"done": [], "failed": []})
        assert await E.JobEngine(jid).run() == "failed"
        assert "file_id" in _job(jid).error

    @pytest.mark.asyncio
    async def test_should_fail_when_parse_raises(self, project_id):
        _patch(["甲"], FakeAligner())

        def boom(db, file_row):
            raise ParseError("no_pieces", "结构识别没切出任何篇目")

        E.build_plan = boom  # type: ignore[assignment]
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "failed"
        j = _job(jid)
        assert j.status == "failed"
        assert "结构识别没切出任何篇目" in j.error
        assert j.done_units == 0


class Test单侧为空:
    @pytest.mark.asyncio
    async def test_should_record_failure_without_persisting(self, project_id):
        _patch(["有中文"], FakeAligner())
        # 把英文抽空 -> 单侧为空
        E.build_plan = lambda db, f: E.Plan(  # type: ignore[assignment]
            file_id=f.id, units=[_unit(0, "有中文", en="")]
        )
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "success"
        j = _job(jid)
        assert j.done_units == 0
        assert j.failed_units_json[0]["reason"] == "align_empty"
        assert _pieces(project_id) == []

    @pytest.mark.asyncio
    async def test_should_finish_success_even_with_failed_units(self, project_id):
        """单篇拒收不该把整份资料判死刑 —— 其余篇目照常入库。"""
        _patch(["好的一篇"], FakeAligner())
        E.build_plan = lambda db, f: E.Plan(  # type: ignore[assignment]
            file_id=f.id, units=[_unit(0, "好的"), _unit(1, "坏的", en="")]
        )
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "success"
        j = _job(jid)
        assert j.done_units == 1
        assert len(j.failed_units_json) == 1
        assert [t for _, t in _pieces(project_id)] == ["好的"]


class Test对齐异常:
    @pytest.mark.asyncio
    async def test_should_record_align_error_as_retryable(self, project_id):
        al = FakeAligner()
        al.raises = AlignError("budget_exhausted", "预算上限 2.00 元已用尽")
        _patch(["甲"], al)
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "success"
        j = _job(jid)
        assert j.failed_units_json[0]["reason"] == "budget_exhausted"
        assert j.failed_units_json[0]["retryable"] is True
        assert _pieces(project_id) == []

    @pytest.mark.asyncio
    async def test_should_fail_whole_job_on_unexpected_error(self, project_id):
        al = FakeAligner()
        al.raises = RuntimeError("provider 炸了")
        _patch(["甲", "乙"], al)
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "failed"
        j = _job(jid)
        assert j.status == "failed"
        assert "provider 炸了" in j.error
        # 未预期的异常中断整份资料，已入库的篇目仍在（可续跑）
        assert j.done_units == 0


class Test人工微调保护:
    @pytest.mark.asyncio
    async def test_should_skip_piece_with_manual_edits(self, project_id):
        _patch(["甲"], FakeAligner())
        jid = _new_job(project_id)
        await E.JobEngine(jid).run()
        pid = _pieces(project_id)[0][0]
        with Session(_engine()) as s:
            pair = s.scalars(select(Pair).where(Pair.piece_id == pid).limit(1)).one()
            pair.manually_edited = True
            s.commit()

        jid2 = _new_job(project_id)
        assert await E.JobEngine(jid2).run() == "success"
        j = _job(jid2)
        # 跳过也算处理完：数据在、用户的选择被保留
        assert j.done_units == 1
        assert j.units_json[0]["status"] == str(SaveOutcome.SKIPPED)
        assert "人工微调" in j.units_json[0]["reason"]
        with Session(_engine()) as s:
            p = s.get(Piece, pid)
            s.refresh(p)
            assert any(x.manually_edited for x in p.pairs)

    @pytest.mark.asyncio
    async def test_should_overwrite_when_flagged(self, project_id):
        _patch(["甲"], FakeAligner())
        jid = _new_job(project_id)
        await E.JobEngine(jid).run()
        pid = _pieces(project_id)[0][0]
        with Session(_engine()) as s:
            pair = s.scalars(select(Pair).where(Pair.piece_id == pid).limit(1)).one()
            pair.manually_edited = True
            s.commit()
        jid2 = _new_job(project_id)
        assert await E.JobEngine(jid2, overwrite=True).run() == "success"
        assert _job(jid2).units_json[0]["status"] == str(SaveOutcome.REPLACED)


class Test断点续跑:
    @pytest.mark.asyncio
    async def test_should_skip_done_units(self, project_id):
        al = FakeAligner()
        _patch(["甲", "乙", "丙"], al)
        jid = _new_job(project_id)
        await E.JobEngine(jid).run()
        assert len(al.calls) == 3

        al2 = FakeAligner()
        _patch(["甲", "乙", "丙"], al2)
        await E.JobEngine(jid).run()
        assert al2.calls == [], "已完成的篇目不该再调一次模型（那是真金白银）"
        assert _job(jid).done_units == 3

    @pytest.mark.asyncio
    async def test_should_preserve_tokens_and_cost_across_resume(self, project_id):
        al = FakeAligner(usage=Usage(prompt_tokens=100, completion_tokens=20))
        _patch(["甲", "乙"], al)
        jid = _new_job(project_id)
        await E.JobEngine(jid).run()
        first = _job(jid)
        assert first.tokens_used == 240
        assert first.cost_estimate_json["total_cny"] > 0
        assert first.cost_estimate_json["estimated_cny"] > 0, "F8 首跑要写整篇预估"

        al2 = FakeAligner(usage=Usage(prompt_tokens=100, completion_tokens=20))
        _patch(["甲", "乙"], al2)
        await E.JobEngine(jid).run()
        second = _job(jid)
        assert second.tokens_used == 240
        assert second.cost_estimate_json["total_cny"] == pytest.approx(
            first.cost_estimate_json["total_cny"]
        )
        assert second.cost_estimate_json["estimated_cny"] == pytest.approx(
            first.cost_estimate_json["estimated_cny"]
        ), "预估必须原样保留，续跑不能重算"

    @pytest.mark.asyncio
    async def test_should_only_process_units_left_after_crash(self, project_id):
        """模拟崩在第二篇：checkpoint 里有第一篇，续跑只做第二篇。"""
        al = FakeAligner()
        _patch(["甲", "乙"], al)
        jid = _new_job(project_id)
        # 手工造一个"跑完第一篇"的状态
        with Session(_engine()) as s:
            j = s.get(Job, jid)
            j.checkpoint_json = {"file_id": FILE_ID, "done": ["0000:甲"], "failed": []}
            j.done_units = 1
            j.total_units = 2
            j.status = "failed"
            j.error = "进程被杀了"
            s.commit()
        al2 = FakeAligner()
        _patch(["甲", "乙"], al2)
        assert await E.JobEngine(jid).run() == "success"
        assert len(al2.calls) == 1, "只应加工没做过的那一篇"
        assert [t for _, t in _pieces(project_id)] == ["乙"]

    @pytest.mark.asyncio
    async def test_should_migrate_legacy_title_only_checkpoint(self, project_id):
        """老 checkpoint 只存了标题，也要认。"""
        with Session(_engine()) as s:
            j = Job(
                project_id=project_id,
                type="recite_align",
                status="failed",
                checkpoint_json={"file_id": FILE_ID, "done": ["甲"], "failed": []},
            )
            s.add(j)
            s.commit()
            jid = j.id
        al = FakeAligner()
        _patch(["甲", "乙"], al)
        assert await E.JobEngine(jid).run() == "success"
        assert len(al.calls) == 1


class Test成本熔断:
    """F8：预估与实际差异 >50% 中断（任务级）。

    预估在首跑按整篇算一次写进 ``cost_estimate_json.estimated_cny``；每篇落库后
    拿累计实际花费比对。超过预估 1.5 倍（预算比预估保守算出的上界）就把任务停在
    明确的 ``interrupted`` 终态，等用户拍板 —— 用户点续跑即视为确认继续，
    引擎关掉熔断把任务跑完。
    """

    def _set_queued(self, jid: str) -> None:
        with Session(_engine()) as s:
            s.get(Job, jid).status = "queued"
            s.commit()

    @pytest.mark.asyncio
    async def test_should_store_fresh_estimate_and_pass_within_budget(self, project_id):
        _patch(["甲"], FakeAligner())
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "success"
        cost = _job(jid).cost_estimate_json
        assert cost["estimated_cny"] > 0
        assert cost["total_cny"] == 0  # fake 没报 usage，实际花费 0，无从超支

    @pytest.mark.asyncio
    async def test_should_interrupt_when_actual_over_50pct(self, project_id):
        al = FakeAligner(usage=Usage(prompt_tokens=500_000, completion_tokens=500_000))
        _patch(["甲", "乙", "丙"], al)
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "interrupted"
        j = _job(jid)
        assert j.status == "interrupted"
        assert "50%" in j.error
        # 第一篇落库之后才拦：数据在库里，可续跑
        assert j.done_units == 1
        assert [t for _, t in _pieces(project_id)] == ["甲"]
        assert j.checkpoint_json["budget_stop"] is True

    @pytest.mark.asyncio
    async def test_should_resume_after_interrupt_without_guarding_again(self, project_id):
        al = FakeAligner(usage=Usage(prompt_tokens=500_000, completion_tokens=500_000))
        _patch(["甲", "乙"], al)
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "interrupted"
        assert _job(jid).status == "interrupted"

        self._set_queued(jid)
        al2 = FakeAligner(usage=Usage(prompt_tokens=500_000, completion_tokens=500_000))
        _patch(["甲", "乙"], al2)
        assert await E.JobEngine(jid).run() == "success"
        j = _job(jid)
        assert j.done_units == 2
        assert [t for _, t in _pieces(project_id)] == ["甲", "乙"]
        assert len(al2.calls) == 1, "只剩乙没做，且续跑不再触发熔断"

    @pytest.mark.asyncio
    async def test_should_keep_estimate_across_interrupt_and_resume(self, project_id):
        _patch(
            ["甲", "乙"],
            FakeAligner(usage=Usage(prompt_tokens=500_000, completion_tokens=500_000)),
        )
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "interrupted"
        est = _job(jid).cost_estimate_json["estimated_cny"]
        assert est > 0

        self._set_queued(jid)
        _patch(["甲", "乙"], FakeAligner())
        assert await E.JobEngine(jid).run() == "success"
        assert _job(jid).cost_estimate_json["estimated_cny"] == pytest.approx(est)

    @pytest.mark.asyncio
    async def test_no_interrupt_when_regular_fallback(self, project_id):
        """降级通道不花钱，不应被熔断拦住（provider 为空，预估恒 0）。"""
        _patch(["甲", "乙"], FakeAligner(usage=Usage(500_000, 500_000)))
        E.resolve_aligner = (  # type: ignore[assignment]
            lambda db, pid: E.AlignerBundle(FakeAligner(), "regular", None, None)
        )
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "success"
        assert _job(jid).cost_estimate_json["estimated_cny"] == 0
        assert _job(jid).cost_estimate_json["total_cny"] == 0

    @pytest.mark.asyncio
    async def test_should_retry_failed_units_on_resume(self, project_id):
        """失败明细不该让失败篇目永远重试不了。"""
        al = FakeAligner()
        E.build_plan = lambda db, f: E.Plan(  # type: ignore[assignment]
            file_id=f.id, units=[_unit(0, "坏的", en=""), _unit(1, "好的")]
        )
        E.resolve_aligner = lambda db, pid: E.AlignerBundle(  # type: ignore[assignment]
            al, "llm", al.provider, al.model
        )
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "success"
        assert len(al.calls) == 1
        assert [t for _, t in _pieces(project_id)] == ["好的"]

        # 同一份资料，续跑时 plan 不变（真实场景就是这样）
        al2 = FakeAligner()
        E.resolve_aligner = lambda db, pid: E.AlignerBundle(  # type: ignore[assignment]
            al2, "llm", al2.provider, al2.model
        )
        with Session(_engine()) as s:
            j = s.get(Job, jid)
            j.status = "failed"
            s.commit()
        assert await E.JobEngine(jid).run() == "success"
        assert al2.calls == [], "单侧为空在调模型前就拒了，不该再花钱"
        assert [t for _, t in _pieces(project_id)] == ["好的"]
        # 但失败明细要重记一遍，界面才知道它还没过
        assert len(_job(jid).failed_units_json) == 1
        assert _job(jid).status == "success"


class Test取消:
    @pytest.mark.asyncio
    async def test_should_stop_at_unit_boundary(self, project_id):
        al = FakeAligner()
        _patch(["甲", "乙", "丙"], al)
        eng = E.JobEngine(_new_job(project_id))

        # ``on_unit`` 是同步回调（``Callable[..., None]``），不是协程
        eng.on_unit = lambda _u, _o: eng.request_cancel()
        assert await eng.run() == "cancelled"
        j = _job(eng.job_id)
        assert j.status == "cancelled"
        assert j.done_units == 1
        assert len(al.calls) == 1, "取消后不该再开工新的一篇"
        assert [t for _, t in _pieces(project_id)] == ["甲"]

    @pytest.mark.asyncio
    async def test_should_keep_checkpoint_after_cancel(self, project_id):
        al = FakeAligner()
        _patch(["甲", "乙"], al)
        eng = E.JobEngine(_new_job(project_id))
        eng.on_unit = lambda _u, _o: eng.request_cancel()
        await eng.run()
        j = _job(eng.job_id)
        assert j.checkpoint_json["done"] == ["0000:甲"]

    @pytest.mark.asyncio
    async def test_should_resume_after_cancel(self, project_id):
        al = FakeAligner()
        _patch(["甲", "乙"], al)
        eng = E.JobEngine(_new_job(project_id))
        eng.on_unit = lambda _u, _o: eng.request_cancel()
        await eng.run()
        al2 = FakeAligner()
        _patch(["甲", "乙"], al2)
        with Session(_engine()) as s:
            j = s.get(Job, eng.job_id)
            # 路由的 resume 会先把状态改回 queued，引擎才肯接手
            j.status = "queued"
            s.commit()
        assert await E.JobEngine(eng.job_id).run() == "success"
        assert len(al2.calls) == 1
        assert [t for _, t in _pieces(project_id)] == ["甲", "乙"]


class Test心跳:
    @pytest.mark.asyncio
    async def test_heartbeat_polls_cancel_flag(self, project_id, monkeypatch):
        """心跳看到 jobs.status=cancelled 就要把引擎叫停。"""
        al = FakeAligner()
        al.gate = asyncio.Event()  # 先卡住，给心跳留出轮询窗口
        _patch(["甲", "乙"], al)
        monkeypatch.setattr(E, "HEARTBEAT_INTERVAL", 0.01)
        eng = E.JobEngine(_new_job(project_id))
        task = asyncio.create_task(eng.run())
        await asyncio.sleep(0.05)
        with Session(_engine()) as s:
            j = s.get(Job, eng.job_id)
            j.status = "cancelled"
            s.commit()
        await asyncio.sleep(0.05)  # 等心跳轮询到
        al.gate.set()
        assert await task == "cancelled"
        assert _job(eng.job_id).done_units == 1


class Test事务原子性:
    @pytest.mark.asyncio
    async def test_piece_and_checkpoint_commit_together(self, project_id):
        """篇目与 checkpoint 必须是**同一次** commit，不能分两次。

        分两次的后果：崩在中间会留下"篇目已入库、checkpoint 说没入"的状态，
        续跑会把这一篇重新加工一遍 —— 多花一次模型的钱。
        """
        _patch(["甲"], FakeAligner())
        jid = _new_job(project_id)
        commits: list[str] = []
        original = E.SessionLocal

        class CountingSession(Session):
            def commit(self) -> None:
                commits.append("commit")
                super().commit()

        # 必须是绑好 engine 的 sessionmaker：直接拿 Session 类是没有 bind 的
        E.SessionLocal = sessionmaker(  # type: ignore[assignment]
            bind=_engine(), class_=CountingSession, expire_on_commit=False
        )
        try:
            await E.JobEngine(jid).run()
        finally:
            E.SessionLocal = original  # type: ignore[assignment]

        # 1 篇任务共 3 次提交：开场（置 running + 计划）、单元（篇目+checkpoint）、终态
        assert len(commits) == 3, f"单元写入和 checkpoint 被拆成了两次提交：{commits}"
        with Session(_engine()) as s:
            n = len(list(s.scalars(select(Piece).where(Piece.project_id == project_id))))
        assert n == 1
        assert _job(jid).checkpoint_json["done"] == ["0000:甲"]

    @pytest.mark.asyncio
    async def test_failed_unit_leaves_no_piece(self, project_id):
        """拒收的篇目一行都不许写，但失败明细必须落库。"""
        _patch(["甲"], FakeAligner())
        E.build_plan = lambda db, f: E.Plan(  # type: ignore[assignment]
            file_id=f.id, units=[_unit(0, "坏的", en="")]
        )
        jid = _new_job(project_id)
        assert await E.JobEngine(jid).run() == "success"
        assert _pieces(project_id) == []
        j = _job(jid)
        assert j.failed_units_json[0]["reason"] == "align_empty"
        assert j.units_json[0]["status"] == "failed"
        assert j.units_json[0]["reason"] == "单侧为空"


class TestRunManager:
    @pytest.mark.asyncio
    async def test_should_dedupe_concurrent_submit(self, project_id):
        _patch(["甲"], FakeAligner())
        mgr = E.RunManager()
        jid = _new_job(project_id)
        mgr.submit(jid)
        mgr.submit(jid)
        assert len(mgr.active()) == 1
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_should_report_running(self, project_id):
        al = FakeAligner()
        al.gate = asyncio.Event()
        _patch(["甲"], al)
        mgr = E.RunManager()
        jid = _new_job(project_id)
        mgr.submit(jid)
        assert mgr.is_running(jid)
        mgr.request_cancel(jid)
        al.gate.set()
        await mgr.shutdown()
        assert not mgr.is_running(jid)

    @pytest.mark.asyncio
    async def test_shutdown_should_be_idempotent(self, project_id):
        _patch(["甲"], FakeAligner())
        mgr = E.RunManager()
        mgr.submit(_new_job(project_id))
        await mgr.shutdown()
        await mgr.shutdown()
        assert mgr.active() == []

    @pytest.mark.asyncio
    async def test_should_run_to_completion_via_manager(self, project_id):
        _patch(["甲", "乙"], FakeAligner())
        mgr = E.RunManager()
        jid = _new_job(project_id)
        mgr.submit(jid)
        task = mgr._tasks[jid]
        assert await task == "success"
        assert mgr.active() == []
        await mgr.shutdown()


class Test待处理查询:
    def test_should_list_runnable_jobs(self, project_id):
        j1 = _new_job(project_id)
        j2 = _new_job(project_id)
        _new_job(project_id)  # 默认 queued
        with Session(_engine()) as s:
            s.get(Job, j2).status = "success"
            s.commit()
            ids = set(E.list_pending_job_ids(s))
        assert j1 in ids
        assert j2 not in ids


class Test辅助:
    def test_split_tokens_is_conservative(self):
        u = E._split_tokens(500)
        assert E._total_tokens(u) == 500
        assert u.completion_tokens == 0

    def test_split_tokens_never_negative(self):
        assert E._total_tokens(E._split_tokens(-3)) == 0

    def test_unit_key_distinguishes_duplicate_titles(self):
        a, b = _unit(0, "同名"), _unit(1, "同名")
        assert a.key != b.key
        assert a.key == "0000:同名"

    def test_cost_roundtrip(self):
        c = E._Cost()
        c.add(E.AlignerBundle(FakeAligner(), "llm", "fake", "fake-model"), Usage(10, 5))
        again = E._Cost.from_json(c.to_json())
        assert again.to_json() == c.to_json()

    def test_cost_uses_fallback_price_for_unknown_model(self):
        """牌价表没有这个模型时按兜底价算，而不是不记账。

        报 0 元会让人以为免费，从而毫无节制地重跑；兜底价至少是诚实的上界。
        """
        c = E._Cost()
        c.add(E.AlignerBundle(FakeAligner(), "llm", "某厂", "某模型"), Usage(1_000_000, 0))
        assert len(c.by_model) == 1
        assert c.to_json()["total_cny"] > 0

    def test_cost_ignores_zero_usage(self):
        c = E._Cost()
        c.add(E.AlignerBundle(FakeAligner(), "llm", "fake", "fake-model"), Usage(0, 0))
        assert c.by_model == {}

    def test_plan_to_json_shape(self):
        plan = E.Plan(file_id=FILE_ID, units=[_unit(0, "甲")])
        row = plan.to_json()[0]
        assert set(row) == {"key", "index", "title", "page", "status"}
        assert row["status"] == "pending"

    def test_ledger_defaults_are_independent(self):
        a, b = E._Ledger(), E._Ledger()
        a.done.add("x")
        assert b.done == set()

    def test_add_usage(self):
        u = E._add_usage(Usage(1, 2), Usage(10, 20))
        assert (u.prompt_tokens, u.completion_tokens) == (11, 22)


class Test解析前置条件:
    def test_should_reject_vision_channel(self, project_id):
        with Session(_engine()) as s:
            s.get(File, FILE_ID).parse_channel = "vision"
            s.commit()
            f = s.get(File, FILE_ID)
            with pytest.raises(NotImplementedError, match="OCR"):
                E.build_plan(s, f)

    def test_should_reject_missing_file_on_disk(self, project_id):
        with Session(_engine()) as s:
            f = s.get(File, FILE_ID)
            f.server_path = "ff/does-not-exist.pdf"
            s.commit()
            s.refresh(f)
            with pytest.raises(FileNotFoundError):
                E.build_plan(s, f)


FIXTURE_PDF = Path(__file__).resolve().parents[1] / "fixtures" / "daoyouci.pdf"


class Test续跑状态保持:
    def test_should_keep_settled_status_on_resume(self, project_id):
        """续跑不能把已完成篇目的状态抹回 pending。

        踩过的坑：``run()`` 开头无条件 ``job.units_json = plan.to_json()``，
        续跑时把上一轮 done 的篇目全刷成 pending，而本轮不会再加工它们 ——
        于是它们永远停在 pending，客户端以为"还没做"，库里其实早有了。
        """
        _patch(["一", "二"], FakeAligner())
        jid = _new_job(project_id)
        eng = E.JobEngine(jid)
        eng.on_unit = lambda u, _o: u.key == "0000:一" and eng.request_cancel()
        assert asyncio.run(eng.run()) == "cancelled"
        with Session(_engine()) as s:
            first = _job(jid)
            statuses = {u["key"]: u["status"] for u in first.units_json}
        assert statuses["0000:一"] != "pending", "取消前完成的那篇应已落状态"
        assert statuses["0001:二"] == "pending", "没做的那篇应是 pending"

        with Session(_engine()) as s:
            j = s.get(Job, jid)
            j.status = "queued"
            s.commit()
        assert asyncio.run(E.JobEngine(jid).run()) == "success"
        with Session(_engine()) as s:
            after = {u["key"]: u["status"] for u in _job(jid).units_json}
        assert after["0000:一"] != "pending", f"续跑把已完成篇目抹回 pending 了：{after}"
        assert set(after.values()) == {"created"}, after


class Test旧Checkpoint迁移:
    """旧 checkpoint 的 ``done`` / ``failed`` 只存了标题，没有稳定 key。"""

    def test_should_migrate_title_only_done(self, project_id):
        _patch(["一", "二"], FakeAligner())
        jid = _new_job(project_id)
        assert asyncio.run(E.JobEngine(jid).run()) == "success"
        with Session(_engine()) as s:
            j = s.get(Job, jid)
            j.checkpoint_json = {**j.checkpoint_json, "done": ["二"]}  # 旧格式
            j.status = "queued"
            s.commit()
        E.build_plan = _plan_of("一", "二", "三")
        E.resolve_aligner = lambda db, pid: E.AlignerBundle(FakeAligner(), "llm")
        assert asyncio.run(E.JobEngine(jid).run()) == "success"
        with Session(_engine()) as s:
            done = s.get(Job, jid).checkpoint_json["done"]
        assert "0001:二" in done, done
        assert "二" not in done, f"旧标题没被换掉：{done}"
        assert "0000:一" in done and "0002:三" in done, done

    def test_should_not_guess_ambiguous_title(self, project_id):
        """同名两篇（材料里很常见）不能瞎认，否则失败会记到别的篇目头上。"""
        E.build_plan = _plan_of("同名", "同名", "别的")
        assert E._migrate_done(["同名"], {"同名"}) == {"同名"}
        assert E._key_of_title([_u(0, "同名"), _u(1, "同名")], "同名") is None
        assert E._key_of_title([_u(0, "甲"), _u(1, "乙")], "乙") == "0001:乙"
        assert E._key_of_title([_u(0, "甲")], "查无此篇") is None

    def test_should_backfill_failed_key_and_dedupe(self, project_id):
        """旧失败明细（只有 ``unit``）必须补 key，否则每续跑一次多一条重复红字。"""
        _patch(["一", "二"], FakeAligner())
        jid = _new_job(project_id)
        E.build_plan = _plan_of("一", "二")
        E.resolve_aligner = lambda db, pid: E.AlignerBundle(_AlwaysFail(), "llm")
        assert asyncio.run(E.JobEngine(jid).run()) == "success"
        with Session(_engine()) as s:
            j = s.get(Job, jid)
            assert len(j.failed_units_json) == 2, j.failed_units_json
            # 模拟旧数据：抹掉 key，只留标题
            j.failed_units_json = [
                {"unit": e["key"].split(":", 1)[1], "reason": e["reason"]}
                for e in j.failed_units_json
            ]
            j.checkpoint_json = {**j.checkpoint_json, "failed": j.failed_units_json}
            j.status = "queued"
            s.commit()
        assert asyncio.run(E.JobEngine(jid).run()) == "success"
        with Session(_engine()) as s:
            failed = s.get(Job, jid).failed_units_json
        assert len(failed) == 2, f"重复累积了：{failed}"
        assert all(e.get("key") for e in failed), failed
        assert {e["key"] for e in failed} == {"0000:一", "0001:二"}, failed

    def test_should_drop_failed_entry_that_later_succeeded(self, project_id):
        """失败明细对应的篇目后来做成功了，就不该再挂着红字。"""
        units = [_u(0, "一"), _u(1, "二")]
        out = E._backfill_failed_keys(
            [
                {"unit": "一", "reason": "质量未过"},  # 一会成功
                {"unit": "二", "reason": "质量未过"},
                {"unit": "查无此篇", "reason": "对不上"},
            ],
            {"0000:一"},
            units,
        )
        assert out == [{"unit": "二", "reason": "质量未过", "key": "0001:二"}], out

    def test_should_keep_one_parse_row_per_run_with_version(self, project_id):
        """每次运行留一行 ``file_parses``，且带真实 ``parser_version``。

        ②的清洗规则会改，留历史才能重跑对比（文档要求原文抽取可复查）。
        但恒为 ``"1"`` 的版本号让这些行无法分辨是哪版规则产出的，等于
        白占空间（样板每行 ~340KB）还对不上账。
        """
        from app.parse.pdf import PdfParser

        real = E.__dict__["build_plan"]  # autouse fixture 跑完会还原这两处
        E.resolve_aligner = lambda db, pid: E.AlignerBundle(  # type: ignore[assignment]
            FakeAligner(), "llm", "fake", "fake-model"
        )
        # 按线上的路径形态把样板 PDF 放进 storage。别用 ../.. 逃逸到仓库里，
        # storage 的根和测试临时目录不是一回事，会报"文件不在磁盘上"。
        rel = f"realparse/{FIXTURE_PDF.name}"
        dest = get_storage().abs_path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(FIXTURE_PDF, dest)
        try:
            with Session(_engine()) as s:
                f = s.get(File, FILE_ID)
                f.server_path = rel
                s.commit()
            E.build_plan = real
            for expected_rows in (1, 2):
                jid = _new_job(project_id)
                assert asyncio.run(E.JobEngine(jid).run()) == "success", _job(jid).error
                with Session(_engine()) as s:
                    rows = s.scalars(select(FileParse).where(FileParse.file_id == FILE_ID)).all()
                assert len(rows) == expected_rows, f"第 {expected_rows} 次应留 {expected_rows} 行"
                assert all(r.parser_version == PdfParser.version for r in rows)
                assert all(r.raw_text for r in rows)
        finally:
            shutil.rmtree(dest.parent, ignore_errors=True)


class Test真实PDF落库:
    """``file_parses.raw_text`` 曾经是 MySQL ``TEXT``（上限 65,535 字节）。

    样板 PDF 清洗后 159,817 字节，**任何真实 PDF 都会在这一步报
    ``DataError 1406``**，整条链路根本走不完。之前没暴露是因为单元测试的
    解析样本都是几十字节的小段，真实数据又只走了不落库的纯函数。
    所以这条必须用真 PDF 跑，且必须真的 insert。
    """

    @pytest.mark.skipif(
        not FIXTURE_PDF.exists(), reason="样板 PDF 不在仓库里（被 .gitignore 排除）"
    )
    def test_should_store_real_pdf_parse_result(self, project_id, tmp_path):
        import shutil

        fid = new_id()
        rel = f"{fid[:2]}/{fid}.pdf"
        dest = get_storage().abs_path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(FIXTURE_PDF, dest)
        try:
            with Session(_engine()) as s:
                s.add(
                    File(
                        id=fid,
                        project_id=project_id,
                        server_path=rel,
                        orig_name="daoyouci.pdf",
                        sha256=fid * 2,
                        size=FIXTURE_PDF.stat().st_size,
                        parse_channel="text",
                    )
                )
                s.commit()
                plan = E.build_plan(s, s.get(File, fid))
                s.commit()  # 真正写进 MySQL —— 上限就在这一步炸

            assert len(plan.units) == 12
            with Session(_engine()) as s:
                row = s.scalars(select(FileParse).where(FileParse.file_id == fid)).one()
                assert len(row.raw_text.encode("utf-8")) > 65535, (
                    "样本不再复现原来的越界，这条测试就失去意义了"
                )
                assert row.pages_json
        finally:
            shutil.rmtree(get_storage().abs_path(rel).parent, ignore_errors=True)

    def test_text_columns_must_be_longtext(self):
        """把 MySQL 列类型钉死：这三列一旦退回 TEXT，真实资料就写不进去。

        直接查 ``information_schema``，比"插一条刚好越界的数据"更稳 ——
        后者会随 fixture 变化而失效，而且绕过了质量门控。
        """
        from sqlalchemy import text as sql_text

        with _engine().connect() as conn:
            rows = conn.execute(
                sql_text(
                    "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM information_schema"
                    ".COLUMNS WHERE TABLE_SCHEMA = :db AND ("
                    "(TABLE_NAME='file_parses' AND COLUMN_NAME='raw_text') OR"
                    "(TABLE_NAME='pairs' AND COLUMN_NAME IN ('zh','en')))"
                ),
                {"db": _engine().url.database},
            ).all()
        assert len(rows) == 3, [tuple(r) for r in rows]
        for table, col, dtype in rows:
            assert dtype == "longtext", f"{table}.{col} 是 {dtype}，真实资料存不进去"

    def test_long_pair_text_survives_insert(self, project_id):
        """``pairs.zh`` / ``pairs.en`` 装得下整页长度的一段。"""
        long_zh = "上海欢迎你。" * 12_000  # ~72KB，超过 TEXT 上限
        long_en = "Shanghai welcomes you. " * 6_000
        assert len(long_zh.encode("utf-8")) > 65535

        from app.align.base import PairDraft  # noqa: F401

        piece = Piece(id=new_id(), project_id=project_id, title="超长", sort_order=0)
        with Session(_engine()) as s:
            s.add(piece)
            s.commit()
            piece_id = piece.id
            s.add(Pair(piece_id=piece_id, seq=0, zh=long_zh, en=long_en, loc_page=1))
            s.commit()  # 真正写进 MySQL
        with Session(_engine()) as s:
            got = s.scalars(select(Pair).where(Pair.piece_id == piece_id)).one()
            assert got.zh == long_zh
            assert got.en == long_en


# 防止 pytest 收集期就污染全局（_patch 是全局替换）
@pytest.fixture(autouse=True)
def _restore_module_globals() -> Iterator[None]:
    real_build, real_resolve = E.build_plan, E.resolve_aligner
    yield
    E.build_plan, E.resolve_aligner = real_build, real_resolve
    with contextlib.suppress(Exception):
        SessionLocal().close()
