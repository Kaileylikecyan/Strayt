"""F24 数据导出：一键 JSON 快照（防锁死备份）。

验证全量快照包含所有业务表、字段完整、跨表 id 一致，且不含泄露明文的字段。
"""

from __future__ import annotations

from datetime import date

import pytest
from app.db.models import (
    ApiKey,
    CardQuote,
    Category,
    Checkin,
    Edge,
    File,
    Flashcard,
    Goal,
    ModelProfile,
    Node,
    Piece,
    Plan,
    Project,
    utcnow,
)
from sqlalchemy.orm import Session


@pytest.fixture
def world(db_session: Session) -> dict:
    p = Project(name="图谱项目", type="graph")
    db_session.add(p)
    db_session.commit()
    pid = p.id

    f = File(
        project_id=pid,
        server_path="data/abc/raw.pdf",
        orig_name="raw.pdf",
        sha256="ab" * 32,
        size=1234,
        page_count=3,
    )
    db_session.add(f)
    db_session.flush()
    piece = Piece(project_id=pid, file_id=f.id, title="上海篇")
    db_session.add(piece)
    db_session.flush()
    cat = Category(project_id=pid, name="上海概况")
    db_session.add(cat)
    db_session.flush()
    node = Node(project_id=pid, category_id=cat.id, name="水网", mastery="mid")
    db_session.add(node)
    db_session.flush()

    goal = Goal(project_id=pid, exam_date=date(2026, 12, 1))
    plan = Plan(project_id=pid, title="过一轮", due_date=date(2026, 10, 1))
    checkin = Checkin(date=date(2026, 9, 28), items_json=["练口语"])
    edge = Edge(project_id=pid, from_node=node.id, to_node=node.id, reason="自环")
    quote = CardQuote(
        node_id=node.id, quote_text="水网密布", file_id=f.id, page=1, para=2, sort_order=0
    )
    flashcard = Flashcard(node_id=node.id, question="水网", answer="密布")
    key = ApiKey(provider="openai", label="主", secret_enc="cipher", secret_mask="sk-a***")
    profile = ModelProfile(name="默认", text_provider_id=key.id, text_model="gpt-4o-mini")
    db_session.add_all([goal, plan, checkin, edge, quote, flashcard, key, profile])
    db_session.commit()
    return {
        "pid": pid,
        "file": f.id,
        "piece": piece.id,
        "node": node.id,
        "category": cat.id,
        "key": key.id,
        "profile": profile.id,
    }


class Test导出:
    def test_snapshot_contains_all_sections(self, client, auth, world):
        r = client.get("/api/v1/export/snapshot", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["export_version"] == "1"
        assert body["schema_version"] >= 1
        assert body["exported_at"] is not None
        assert body["projects"][0]["name"] == "图谱项目"
        assert len(body["files"]) == 1
        assert body["files"][0]["server_path"] == "data/abc/raw.pdf"
        assert body["pieces"][0]["title"] == "上海篇"
        assert body["pieces"][0]["file_id"] == world["file"]
        assert body["pairs"] == []
        assert body["goals"][0]["exam_date"] == "2026-12-01"
        assert body["plans"][0]["status"] == "todo"
        assert body["checkins"][0]["items_json"] == ["练口语"]
        assert body["categories"][0]["id"] == world["category"]
        assert body["nodes"][0]["id"] == world["node"]
        assert body["nodes"][0]["mastery"] == "mid"
        assert body["edges"][0]["from_node"] == world["node"]
        assert body["card_quotes"][0]["page"] == 1
        assert body["flashcards"][0]["answer"] == "密布"
        assert body["api_keys"][0]["secret_enc"] == "cipher"
        assert body["api_keys"][0]["secret_mask"] == "sk-a***"
        assert body["model_profiles"][0]["text_model"] == "gpt-4o-mini"
        assert body["settings"][0]["k"]

    def test_snapshot_includes_soft_deleted_projects(self, client, auth, world, db_session):
        deleted = Project(name="已删", type="recite")
        db_session.add(deleted)
        db_session.commit()
        deleted.deleted_at = utcnow()
        db_session.commit()
        body = client.get("/api/v1/export/snapshot", headers=auth).json()
        assert {x["name"] for x in body["projects"]} == {"图谱项目", "已删"}

    def test_snapshot_requires_auth(self, client, world):
        r = client.get("/api/v1/export/snapshot")
        assert r.status_code == 401
