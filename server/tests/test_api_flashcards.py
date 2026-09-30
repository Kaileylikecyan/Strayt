"""F15 闪卡自测：重建卡组、按掌握度取卡、自测回写推进掌握度。"""

from __future__ import annotations

import pytest
from app.db.models import CardQuote, Category, File, Node, Project
from sqlalchemy.orm import Session

NODE_IDS = {"a": "a" * 32, "b": "b" * 32, "c": "c" * 32}
FILE_ID = "f" * 32


@pytest.fixture
def graph(db_session: Session) -> dict:
    p = Project(name="图谱", type="graph")
    db_session.add(p)
    db_session.commit()
    pid = p.id
    cat = Category(project_id=pid, name="上海概况", sort_order=0)
    db_session.add(cat)
    db_session.flush()

    db_session.add_all(
        [
            Node(
                id=NODE_IDS["a"],
                project_id=pid,
                category_id=cat.id,
                name="水网",
                card_summary="水网是城市命脉",
                mastery="no",
            ),
            # b 没有概要，靠摘录兜底
            Node(
                id=NODE_IDS["b"],
                project_id=pid,
                category_id=cat.id,
                name="航运",
                mastery="no",
            ),
            # c 既没有概要也没有摘录，生成时应跳过
            Node(
                id=NODE_IDS["c"],
                project_id=pid,
                category_id=cat.id,
                name="无名节点",
                mastery="yes",
            ),
        ]
    )
    db_session.add(
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
    db_session.flush()
    db_session.add(
        CardQuote(
            node_id=NODE_IDS["b"], quote_text="自开埠以来航运在此交汇", file_id=FILE_ID, page=4
        )
    )
    db_session.commit()
    return {"pid": pid, "cat": cat.id}


class Test生成:
    def test_should_generate_from_confirmed_nodes(self, client, auth, graph):
        r = client.post(f"/api/v1/projects/{graph['pid']}/flashcards/generate", headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["created"] == 2  # a=概要, b=摘录, c 跳过
        assert body["skipped"] == 1

    def test_should_generate_idempotently(self, client, auth, graph):
        for _ in range(2):
            client.post(f"/api/v1/projects/{graph['pid']}/flashcards/generate", headers=auth)
        r = client.get(f"/api/v1/projects/{graph['pid']}/flashcards", headers=auth)
        assert len(r.json()) == 2

    def test_should_404_missing_project(self, client, auth):
        r = client.post(f"/api/v1/projects/{'e' * 32}/flashcards/generate", headers=auth)
        assert r.status_code == 404


class Test卡组:
    def _seed(self, client, auth, graph):
        client.post(f"/api/v1/projects/{graph['pid']}/flashcards/generate", headers=auth)

    def test_should_list_with_question_answer_page_mastery(self, client, auth, graph):
        self._seed(client, auth, graph)
        r = client.get(f"/api/v1/projects/{graph['pid']}/flashcards", headers=auth)
        assert r.status_code == 200
        by_node = {x["node_id"]: x for x in r.json()}
        assert len(by_node) == 2
        assert by_node[NODE_IDS["a"]]["question"] == "水网"
        assert by_node[NODE_IDS["a"]]["answer"] == "水网是城市命脉"
        assert by_node[NODE_IDS["b"]]["answer"] == "自开埠以来航运在此交汇"
        assert by_node[NODE_IDS["b"]]["page"] == 4
        assert by_node[NODE_IDS["b"]]["mastery"] == "no"

    def test_should_filter_by_mastery(self, client, auth, graph, db_session):
        self._seed(client, auth, graph)
        db_session.query(Node).filter(Node.id == NODE_IDS["b"]).update({"mastery": "mid"})
        db_session.commit()
        r = client.get(f"/api/v1/projects/{graph['pid']}/flashcards?mastery=mid", headers=auth)
        assert [x["node_id"] for x in r.json()] == [NODE_IDS["b"]]


class Test回写:
    def test_correct_promotes_no_to_mid_then_yes(self, client, auth, graph):
        client.post(f"/api/v1/projects/{graph['pid']}/flashcards/generate", headers=auth)
        cards = client.get(f"/api/v1/projects/{graph['pid']}/flashcards", headers=auth).json()
        aid = next(x["id"] for x in cards if x["node_id"] == NODE_IDS["a"])

        r1 = client.post(f"/api/v1/flashcards/{aid}/result", json={"correct": True}, headers=auth)
        assert r1.json()["node_mastery"] == "mid"
        r2 = client.post(f"/api/v1/flashcards/{aid}/result", json={"correct": True}, headers=auth)
        assert r2.json()["node_mastery"] == "yes"
        # 到顶不再升
        r3 = client.post(f"/api/v1/flashcards/{aid}/result", json={"correct": True}, headers=auth)
        assert r3.json()["node_mastery"] == "yes"

    def test_wrong_demotes_yes_to_mid(self, client, auth, graph):
        client.post(f"/api/v1/projects/{graph['pid']}/flashcards/generate", headers=auth)
        cards = client.get(f"/api/v1/projects/{graph['pid']}/flashcards", headers=auth).json()
        aid = next(x["id"] for x in cards if x["node_id"] == NODE_IDS["a"])
        # 先顶到 yes 再打错
        for _ in range(2):
            client.post(f"/api/v1/flashcards/{aid}/result", json={"correct": True}, headers=auth)
        r = client.post(f"/api/v1/flashcards/{aid}/result", json={"correct": False}, headers=auth)
        assert r.json()["node_mastery"] == "mid"

    def test_should_404_missing_flashcard(self, client, auth):
        r = client.post(
            f"/api/v1/flashcards/{'b' * 32}/result", json={"correct": True}, headers=auth
        )
        assert r.status_code == 404
