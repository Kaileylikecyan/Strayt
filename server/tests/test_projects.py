"""项目 CRUD（文档 F4）：type 不可改、软删可恢复。"""

from __future__ import annotations


def _mk(client, auth, name="导游词", type_="recite") -> dict:
    r = client.post("/api/v1/projects", headers=auth, json={"name": name, "type": type_})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_and_list(client, auth) -> None:
    a = _mk(client, auth, "A")
    b = _mk(client, auth, "B", "graph")
    got = client.get("/api/v1/projects", headers=auth).json()
    assert {p["id"] for p in got} == {a["id"], b["id"]}


def test_type_cannot_change(client, auth) -> None:
    """type 决定加工管线，创建后必须锁死。PATCH 里根本没这个字段。"""
    p = _mk(client, auth, type_="recite")
    r = client.patch(f"/api/v1/projects/{p['id']}", headers=auth, params={"name": "改名"})
    assert r.status_code == 200
    assert r.json()["name"] == "改名"
    assert r.json()["type"] == "recite"

    # 试图改 type → Pydantic 拒绝（路由不提供该参数）
    r2 = client.patch(f"/api/v1/projects/{p['id']}", headers=auth, params={"type": "graph"})
    assert r2.status_code in (422, 405)


def test_soft_delete_then_restore(client, auth) -> None:
    p = _mk(client, auth)
    assert client.delete(f"/api/v1/projects/{p['id']}", headers=auth).status_code == 204

    live = client.get("/api/v1/projects", headers=auth).json()
    assert p["id"] not in {x["id"] for x in live}

    trash = client.get("/api/v1/projects", headers=auth, params={"include_deleted": "true"}).json()
    assert p["id"] in {x["id"] for x in trash}

    r = client.post(f"/api/v1/projects/{p['id']}/restore", headers=auth)
    assert r.status_code == 200
    assert r.json()["deleted_at"] is None


def test_404_body_shape(client, auth) -> None:
    r = client.get("/api/v1/projects/deadbeef", headers=auth)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_goal_upsert(client, auth) -> None:
    p = _mk(client, auth)
    r1 = client.put(
        f"/api/v1/projects/{p['id']}/goal", headers=auth, json={"exam_date": "2026-12-20"}
    )
    assert r1.status_code == 200 and r1.json()["exam_date"] == "2026-12-20"

    r2 = client.put(
        f"/api/v1/projects/{p['id']}/goal", headers=auth, json={"exam_date": "2027-01-05"}
    )
    assert r2.json()["id"] == r1.json()["id"], "同一项目只能一个 goal"
    assert r2.json()["exam_date"] == "2027-01-05"
