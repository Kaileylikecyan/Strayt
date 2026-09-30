"""F11 入库（``app.graph.persist``）测试。

覆盖语义：确认即整份覆盖；空草稿拒收；一个事务四张表全进全出。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.db.models import CardQuote, Category, Edge, File, Node, Project
from app.db.session import SessionLocal
from app.graph.merge import GraphDraft, GraphDraftCategory, GraphDraftNode
from app.graph.persist import save_graph
from app.persist import SaveOutcome
from sqlalchemy import select

FILE_ID = "q" * 32


@pytest.fixture
def db_and_pid() -> Iterator[tuple]:
    with SessionLocal() as s:
        p = Project(name="图谱项目", type="graph")
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
    s = SessionLocal()
    try:
        yield s, pid
    finally:
        s.close()


def _draft_builder():
    return GraphDraft(
        categories=[
            GraphDraftCategory("c000", "上海概况", 0),
            GraphDraftCategory("c001", "经济", 1),
        ],
        nodes=[
            GraphDraftNode(
                key="u000:n00",
                name="水网",
                weight=3,
                summary="水网是城市命脉",
                quote="城市的淋巴系统和命脉，就是密布的水网。",
                loc_page=4,
                category_key="c000",
                links=[("u000:n01", "同属城市结构")],
            ),
            GraphDraftNode(
                key="u000:n01",
                name="航运",
                weight=1,
                summary="航运与贸易交汇",
                quote="自开埠以来，航运与贸易在此交汇。",
                loc_page=4,
                category_key="c001",
            ),
        ],
    )


def test_should_insert_all_rows(db_and_pid):
    s, pid = db_and_pid
    draft = _draft_builder()
    draft.edges = [("u000:n00", "u000:n01", "同属城市结构")]
    res = save_graph(s, project_id=pid, file_id=FILE_ID, draft=draft)
    s.commit()
    assert res.outcome == SaveOutcome.REPLACED
    assert s.scalars(select(Category).where(Category.project_id == pid)).all()
    nodes = s.scalars(select(Node).where(Node.project_id == pid).order_by(Node.name)).all()
    assert len(nodes) == 2
    assert nodes[0].category_id
    assert nodes[0].mastery  # 默认值存在
    quotes = s.scalars(select(CardQuote).where(CardQuote.node_id.in_([n.id for n in nodes]))).all()
    assert len(quotes) == 2
    assert quotes[0].page == 4  # 篇起始页
    assert quotes[0].para is None
    assert s.scalars(select(Edge).where(Edge.project_id == pid)).one().from_node != None  # noqa: E711


def test_should_reject_empty_draft(db_and_pid):
    s, pid = db_and_pid
    res = save_graph(s, project_id=pid, file_id=FILE_ID, draft=GraphDraft())
    s.commit()
    assert res.outcome == SaveOutcome.REJECTED


def test_should_reject_when_file_missing(db_and_pid):
    s, pid = db_and_pid
    res = save_graph(s, project_id=pid, file_id=None, draft=_draft_builder())
    s.commit()
    assert res.outcome == SaveOutcome.REJECTED


def test_should_wipe_old_graph_on_reconfirm(db_and_pid):
    s, pid = db_and_pid
    save_graph(s, project_id=pid, file_id=FILE_ID, draft=_draft_builder())
    s.commit()
    assert len(list(s.scalars(select(Node).where(Node.project_id == pid)))) == 2

    small = GraphDraft(
        categories=[GraphDraftCategory("c000", "上海概况", 0)],
        nodes=[
            GraphDraftNode(
                key="u000:n00",
                name="水网",
                weight=2,
                summary="s",
                quote="q",
                loc_page=1,
                category_key="c000",
            )
        ],
    )
    save_graph(s, project_id=pid, file_id=FILE_ID, draft=small)
    s.commit()
    assert len(list(s.scalars(select(Node).where(Node.project_id == pid)))) == 1
    assert len(list(s.scalars(select(Category).where(Category.project_id == pid)))) == 1
    assert not list(s.scalars(select(Edge).where(Edge.project_id == pid)))


def test_should_clamp_node_fields_on_save(db_and_pid):
    s, pid = db_and_pid
    draft = GraphDraft(
        categories=[GraphDraftCategory("c000", "章", 0)],
        nodes=[
            GraphDraftNode(
                key="u000:n00",
                name="超" * 300,
                weight=9,
                summary="概" * 1000,
                quote="引",
                loc_page=1,
                category_key="c000",
            )
        ],
    )
    save_graph(s, project_id=pid, file_id=FILE_ID, draft=draft)
    s.commit()
    n = s.scalars(select(Node).where(Node.project_id == pid)).one()
    assert len(n.name) == 200
    assert len(n.card_summary) == 600
    assert n.weight == 2  # 9 → 兜底 2


def test_should_dedupe_edges_on_save(db_and_pid):
    s, pid = db_and_pid
    draft = GraphDraft(
        categories=[GraphDraftCategory("c000", "章", 0)],
        nodes=[
            GraphDraftNode(
                key="a",
                name="A",
                weight=1,
                summary="s",
                quote="q",
                loc_page=1,
                category_key="c000",
                links=[("b", "r1")],
            ),
            GraphDraftNode(
                key="b",
                name="B",
                weight=1,
                summary="s",
                quote="q",
                loc_page=1,
                category_key="c000",
                links=[("a", "r2")],
            ),
        ],
        edges=[("a", "b", "r1"), ("b", "a", "r2"), ("a", "b", "重复")],
    )
    save_graph(s, project_id=pid, file_id=FILE_ID, draft=draft)
    s.commit()
    edges = s.scalars(select(Edge).where(Edge.project_id == pid)).all()
    assert len(edges) == 1, edges
