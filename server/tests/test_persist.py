"""⑧ 入库（``app.persist``）单元测试。

用真实 MySQL（``strayt_test``），因为要验 ``ON DELETE CASCADE`` 与 JSON 列的
真实行为 —— 手工微调保护依赖级联删干净。
"""

from __future__ import annotations

import pytest
from app.align.base import AlignResult, PairDraft
from app.align.quality import Verdict, check
from app.db.models import File, Pair, Piece, Project, new_id
from app.persist import SaveOutcome, find_piece, rejection_failure, save_piece
from sqlalchemy import select
from sqlalchemy.orm import Session

ZH = "上海的快速发展吸引了大批投资者前来兴业，产业升级持续推进。"
EN = "The rapid development of Shanghai has attracted investors."
FILE_A = "a" * 32
FILE_B = "b" * 32


def _engine():
    from app.db.session import engine

    return engine


def _make_file(fid: str, pid: str) -> str:
    with Session(_engine()) as s:
        s.add(
            File(
                id=fid,
                project_id=pid,
                server_path=f"{fid[:2]}/{fid}.pdf",
                orig_name="daoyouci.pdf",
                sha256=fid * 2,
                size=1024,
                parse_channel="text",
            )
        )
        s.commit()
    return fid


@pytest.fixture
def project_id() -> str:
    with Session(_engine()) as s:
        p = Project(name="背诵项目", type="recite")
        s.add(p)
        s.commit()
        pid = p.id
    # 真建 File 行：``pieces.file_id`` 上有真外键，假的 id 会被 MySQL 拒掉
    _make_file(FILE_A, pid)
    _make_file(FILE_B, pid)
    return pid


def _pairs(n: int = 6) -> AlignResult:
    out = []
    for i in range(n):
        out.append(
            PairDraft(
                seq=i,
                zh=ZH + f"第{i}段。" + "长" * (i * 9),
                en=EN + f" Part {i}." + "En" * (i * 20),
                loc_page=3,
                confidence=0.9,
                how="llm" if i else "direct",
            )
        )
    return AlignResult(pairs=out, mode="llm", used_llm=True, agreement=0.95)


def _save(db: Session, pid: str, title: str = "上海概况", **kw):
    res = _pairs()
    return save_piece(
        db,
        project_id=pid,
        file_id=kw.pop("file_id", FILE_A),
        title=title,
        res=res,
        report=check(res),
        align_mode="llm",
        **kw,
    )


class Test基本落库:
    def test_should_create_piece_with_pairs(self, project_id):
        with Session(_engine()) as s:
            r = _save(s, project_id)
            s.commit()
        assert r.outcome is SaveOutcome.CREATED
        assert r.piece_id is not None
        with Session(_engine()) as s:
            pairs = list(s.scalars(select(Pair).order_by(Pair.seq)))
            assert len(pairs) == 6
            assert pairs[0].loc_page == 3
            assert pairs[0].confidence == pytest.approx(0.9)
            assert pairs[0].how == "direct"
            assert pairs[0].manually_edited is False

    def test_should_stash_align_meta_on_piece(self, project_id):
        with Session(_engine()) as s:
            r = _save(s, project_id)
            s.commit()
            p = s.get(Piece, r.piece_id)
            s.refresh(p)
        assert p.align_mode == "llm"
        assert p.align_agreement == pytest.approx(0.95)
        assert p.align_warnings == []
        assert p.file_id == FILE_A

    def test_should_count_review_pairs(self, project_id):
        with Session(_engine()) as s:
            res = _pairs()
            res.pairs[2].needs_review = True
            r = save_piece(
                s,
                project_id=project_id,
                file_id=FILE_A,
                title="带待核",
                res=res,
                report=check(res),
                align_mode="llm",
            )
            s.commit()
        assert r.review_count == 1
        with Session(_engine()) as s:
            p = find_piece(s, project_id, FILE_A, "带待核")
            flagged = [x.needs_review for x in p.pairs]
            assert flagged.count(True) == 1

    def test_should_assign_sequential_sort_order(self, project_id):
        with Session(_engine()) as s:
            _save(s, project_id, title="甲")
            r2 = _save(s, project_id, title="乙")
            s.commit()
        assert _order(s, r2.piece_id) == 1

    def test_should_honour_explicit_sort_order(self, project_id):
        with Session(_engine()) as s:
            r = _save(s, project_id, title="指定", sort_order=7)
            s.commit()
        assert _order(s, r.piece_id) == 7

    def test_should_clamp_confidence_to_unit_range(self, project_id):
        with Session(_engine()) as s:
            res = _pairs()
            res.pairs[0].confidence = 5.0
            r = save_piece(
                s,
                project_id=project_id,
                file_id=FILE_A,
                title="越界",
                res=res,
                report=check(res),
                align_mode="llm",
            )
            s.commit()
        assert _conf(s, r.piece_id, 0) == 1.0


class Test拒收:
    def test_should_reject_empty_result(self, project_id):
        with Session(_engine()) as s:
            r = save_piece(
                s,
                project_id=project_id,
                file_id=FILE_A,
                title="空",
                res=AlignResult(pairs=[]),
                report=check(AlignResult(pairs=[])),
                align_mode="llm",
            )
            s.commit()
        assert r.outcome is SaveOutcome.REJECTED
        assert r.failure["reason"] == "no_pairs"
        assert s_no_piece(project_id, "空") is False

    def test_should_reject_swapped_fields(self, project_id):
        res = _pairs()
        res.pairs[0] = PairDraft(seq=0, zh=EN, en=ZH, loc_page=3)
        rep = check(res)
        assert rep.verdict is Verdict.REJECTED
        with Session(_engine()) as s:
            r = save_piece(
                s,
                project_id=project_id,
                file_id=FILE_A,
                title="写反",
                res=res,
                report=rep,
                align_mode="llm",
            )
            s.commit()
        assert r.outcome is SaveOutcome.REJECTED
        assert r.failure["reason"] == "quality_gate"
        assert r.failure["retryable"] is False
        assert s_no_piece(project_id, "写反") is False

    def test_rejection_failure_shape(self, project_id):
        res = _pairs()
        res.pairs[0] = PairDraft(seq=0, zh=EN, en=ZH, loc_page=3)
        f = rejection_failure("某篇", check(res))
        assert f["unit"] == "某篇"
        assert f["reason"] == "quality_gate"
        assert f["issues"]


def s_no_piece(pid: str, title: str) -> bool:
    with Session(_engine()) as s:
        return find_piece(s, pid, FILE_A, title) is not None


def _order(s: Session, piece_id: str) -> int:
    return s.get(Piece, piece_id).sort_order


def _file_of(s: Session, piece_id: str) -> str | None:
    return s.get(Piece, piece_id).file_id


def _conf(s: Session, piece_id: str, seq: int) -> float:
    return s.scalars(
        select(Pair.confidence).where(Pair.piece_id == piece_id, Pair.seq == seq)
    ).one()


def _set_edited(s: Session, piece_id: str, seq: int) -> None:
    pair = s.scalars(select(Pair).where(Pair.piece_id == piece_id, Pair.seq == seq)).one()
    pair.manually_edited = True
    s.commit()


class Test幂等与覆盖:
    def test_should_replace_when_no_manual_edits(self, project_id):
        with Session(_engine()) as s:
            _save(s, project_id, title="幂等")
            s.commit()
            first = find_piece(s, project_id, FILE_A, "幂等").id
            r = _save(s, project_id, title="幂等")
            s.commit()
        assert r.outcome is SaveOutcome.REPLACED
        assert r.piece_id != first
        with Session(_engine()) as s:
            n = len(list(s.scalars(select(Piece).where(Piece.title == "幂等"))))
        assert n == 1

    def test_should_skip_piece_with_manual_edits(self, project_id):
        """F18 的人工微调不能被静默冲掉。"""
        with Session(_engine()) as s:
            r = _save(s, project_id, title="有微调")
            s.commit()
            _set_edited(s, r.piece_id, 3)
            again = _save(s, project_id, title="有微调")
            s.commit()
        assert again.outcome is SaveOutcome.SKIPPED
        assert again.wiped_edits == 1
        assert "人工微调" in again.reason
        # 原文必须一字未动
        with Session(_engine()) as s:
            p = find_piece(s, project_id, FILE_A, "有微调")
            assert p.pairs[3].manually_edited is True
            assert len(p.pairs) == 6

    def test_should_overwrite_when_explicitly_asked(self, project_id):
        with Session(_engine()) as s:
            r = _save(s, project_id, title="强刷")
            s.commit()
            _set_edited(s, r.piece_id, 1)
            s.commit()
            again = _save(s, project_id, title="强刷", overwrite=True)
            s.commit()
        assert again.outcome is SaveOutcome.REPLACED
        assert again.wiped_edits == 1
        with Session(_engine()) as s:
            p = find_piece(s, project_id, FILE_A, "强刷")
            assert all(x.manually_edited is False for x in p.pairs)

    def test_should_keep_pieces_of_other_files(self, project_id):
        with Session(_engine()) as s:
            _save(s, project_id, title="同名", file_id=FILE_A)
            _save(s, project_id, title="同名", file_id=FILE_B)
            s.commit()
        with Session(_engine()) as s:
            n = len(list(s.scalars(select(Piece).where(Piece.title == "同名"))))
        assert n == 2

    def test_should_ignore_other_projects(self, project_id):
        with Session(_engine()) as s:
            other = Project(name="别的项目", type="recite")
            s.add(other)
            s.commit()
            _save(s, project_id, title="同题")
            _save(s, other.id, title="同题")
            s.commit()
        with Session(_engine()) as s:
            n = len(list(s.scalars(select(Piece).where(Piece.title == "同题"))))
        assert n == 2

    def test_should_allow_piece_without_file(self, project_id):
        """手工新建的篇目没有来源文件。"""
        with Session(_engine()) as s:
            res = _pairs(3)
            r = save_piece(
                s,
                project_id=project_id,
                file_id=None,
                title="手工",
                res=res,
                report=check(res),
                align_mode="regular",
            )
            s.commit()
        assert r.outcome is SaveOutcome.CREATED
        assert _file_of(s, r.piece_id) is None

    def test_find_piece_returns_none_when_absent(self, project_id):
        with Session(_engine()) as s:
            assert find_piece(s, project_id, FILE_A, "根本没有") is None


class Test级联:
    def test_should_cascade_delete_pairs_with_piece(self, project_id):
        with Session(_engine()) as s:
            r = _save(s, project_id, title="级联")
            s.commit()
            pid = r.piece_id
            s.delete(s.get(Piece, r.piece_id))
            s.commit()
            n = len(list(s.scalars(select(Pair).where(Pair.piece_id == pid))))
        assert n == 0


class Test返回值:
    def test_result_is_json_friendly(self, project_id):
        with Session(_engine()) as s:
            r = _save(s, project_id)
            s.commit()
        assert str(r.outcome) == "created"
        assert isinstance(r.wiped_edits, int)
        assert isinstance(r.review_count, int)
        assert r.piece_id is not None

    def test_new_id_is_32_chars(self):
        assert len(new_id()) == 32
