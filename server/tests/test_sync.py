"""弱同步：bootstrap 全量 + batch 重放 + LWW 裁决（架构红线 6）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.models import Pair, Piece, Plan, utcnow


def _proj(client, auth, name="同步") -> str:
    return client.post(
        "/api/v1/projects", headers=auth, json={"name": name, "type": "recite"}
    ).json()["id"]


def test_bootstrap_shape(client, auth) -> None:
    p = _proj(client, auth)
    r = client.get("/api/v1/sync/bootstrap", headers=auth)
    assert r.status_code == 200
    b = r.json()
    assert b["schema_version"] >= 1
    assert b["server_time"] is not None
    # 一期客户端要消费的实体集合，少一个就是协议断裂
    for k in ("projects", "files", "pieces", "pairs", "goals", "plans", "checkins"):
        assert k in b, f"snapshot 缺 {k}"
    assert p in {x["id"] for x in b["projects"]}


def test_bootstrap_excludes_deleted(client, auth) -> None:
    p = _proj(client, auth)
    client.delete(f"/api/v1/projects/{p}", headers=auth)
    b = client.get("/api/v1/sync/bootstrap", headers=auth).json()
    assert p not in {x["id"] for x in b["projects"]}


def test_batch_applies_and_sorts_by_client_ts(client, auth, engine) -> None:
    """同批按 client_ts 升序重放，「先写的先落」。"""
    p = _proj(client, auth)
    from sqlalchemy.orm import Session as S

    with S(engine) as s:
        piece = Piece(id="piece1", project_id=p, title="篇一", sort_order=0)
        s.add(piece)
        s.commit()
    now = utcnow()
    r = client.post(
        "/api/v1/sync/batch",
        headers=auth,
        json={
            "ops": [
                {
                    "op_id": "b",
                    "entity": "piece_progress",
                    "entity_id": "piece1",
                    "client_ts": (now + timedelta(seconds=1)).isoformat(),
                    "patch": {"last_pos": 7},
                },
                {
                    "op_id": "a",
                    "entity": "piece_progress",
                    "entity_id": "piece1",
                    "client_ts": (now + timedelta(seconds=2)).isoformat(),
                    "patch": {"last_pos": 9},
                },
            ]
        },
    )
    assert r.status_code == 200, r.text
    results = {x["op_id"]: x for x in r.json()["results"]}
    # 两条都 applied，最终值取 client_ts 更晚的那条
    assert results["a"]["status"] == "applied"
    assert results["b"]["status"] == "applied"

    from sqlalchemy.orm import Session as S

    with S(engine) as s:
        assert s.get(Piece, "piece1").last_pos == 9


def test_lww_drops_stale_write(client, auth, engine) -> None:
    """客户端那条比服务端旧 → 丢弃，不回退进度。"""
    p = _proj(client, auth)
    from sqlalchemy.orm import Session as S

    fresh = utcnow() + timedelta(seconds=10)
    with S(engine) as s:
        s.add(Piece(id="piece2", project_id=p, title="篇二", last_pos=5, updated_at=fresh))
        s.commit()

    r = client.post(
        "/api/v1/sync/batch",
        headers=auth,
        json={
            "ops": [
                {
                    "op_id": "stale",
                    "entity": "piece_progress",
                    "entity_id": "piece2",
                    "client_ts": utcnow().isoformat(),  # 比服务端旧
                    "patch": {"last_pos": 0},
                }
            ]
        },
    )
    res = r.json()["results"][0]
    assert res["status"] == "conflict"

    with S(engine) as s:
        assert s.get(Piece, "piece2").last_pos == 5, "陈旧写入不得回退进度"


def test_replay_has_no_double_effect(client, auth, engine) -> None:
    """同一条 op 重放两次，不得产生额外副作用。

    这里刻意**不断言第二次的 status**：LWW 比较的是 client_ts 与服务端
    updated_at，而本测试的 client_ts 领先服务端时钟，所以第二次仍会 applied
    —— 这符合语义，真正要保证的是「值没有被改坏 / 没有叠加」。
    真正会挡成 conflict 的场景见 ``test_lww_drops_stale_write``。
    """
    p = _proj(client, auth)
    from sqlalchemy.orm import Session as S

    with S(engine) as s:
        s.add(Piece(id="piece3", project_id=p, title="篇三"))
        s.commit()

    ts = (utcnow() + timedelta(seconds=5)).isoformat()
    op = {
        "ops": [
            {
                "op_id": "same",
                "entity": "piece_progress",
                "entity_id": "piece3",
                "client_ts": ts,
                "patch": {"last_pos": 3, "recited": True},
            }
        ]
    }
    r1 = client.post("/api/v1/sync/batch", headers=auth, json=op).json()["results"][0]
    r2 = client.post("/api/v1/sync/batch", headers=auth, json=op).json()["results"][0]
    assert r1["status"] == "applied"
    assert r2["status"] in ("applied", "conflict")

    with S(engine) as s:
        piece = s.get(Piece, "piece3")
        assert (piece.last_pos, piece.recited) == (3, True), "重放不得叠加或回退"


def test_replay_with_backdated_client_ts_is_skipped(client, auth, engine) -> None:
    """真实离线场景：重放时补一个陈旧 client_ts → 必须挡成 conflict。"""
    p = _proj(client, auth)
    from sqlalchemy.orm import Session as S

    with S(engine) as s:
        s.add(Piece(id="piece6", project_id=p, title="篇六", last_pos=1))
        s.commit()

    op = {
        "ops": [
            {
                "op_id": "back",
                "entity": "piece_progress",
                "entity_id": "piece6",
                "client_ts": (utcnow() - timedelta(days=1)).isoformat(),
                "patch": {"last_pos": 99},
            }
        ]
    }
    r1 = client.post("/api/v1/sync/batch", headers=auth, json=op).json()["results"][0]
    r2 = client.post("/api/v1/sync/batch", headers=auth, json=op).json()["results"][0]
    assert r1["status"] == "conflict"
    assert r2["status"] == "conflict"

    with S(engine) as s:
        assert s.get(Piece, "piece6").last_pos == 1


def test_one_bad_op_does_not_rollback_batch(client, auth, engine) -> None:
    """离线队列里常有已删实体，单条失败不能拖垮整批。"""
    p = _proj(client, auth)
    from sqlalchemy.orm import Session as S

    with S(engine) as s:
        s.add(Piece(id="piece4", project_id=p, title="篇四"))
        s.add(Plan(id="plan4", project_id=p, title="计划四"))
        s.commit()

    now = (utcnow() + timedelta(seconds=5)).isoformat()
    r = client.post(
        "/api/v1/sync/batch",
        headers=auth,
        json={
            "ops": [
                {
                    "op_id": "gone",
                    "entity": "piece_progress",
                    "entity_id": "no-such",
                    "client_ts": now,
                    "patch": {"last_pos": 1},
                },
                {
                    "op_id": "ok",
                    "entity": "plan",
                    "entity_id": "plan4",
                    "client_ts": now,
                    "patch": {"status": "doing"},
                },
            ]
        },
    )
    results = {x["op_id"]: x for x in r.json()["results"]}
    assert results["gone"]["status"] == "skipped"
    assert results["ok"]["status"] == "applied"

    with S(engine) as s:
        assert s.get(Plan, "plan4").status == "doing"


def test_forbidden_entity_and_field_rejected(client, auth, engine) -> None:
    """白名单：不在表内的实体、越权字段一律拒。"""
    p = _proj(client, auth)
    now = utcnow().isoformat()
    r = client.post(
        "/api/v1/sync/batch",
        headers=auth,
        json={
            "ops": [
                {
                    "op_id": "x",
                    "entity": "project",
                    "entity_id": p,
                    "client_ts": now,
                    "patch": {"name": "hacked"},
                }
            ]
        },
    )
    assert r.json()["results"][0]["status"] == "error"

    from sqlalchemy.orm import Session as S

    with S(engine) as s:
        s.add(Plan(id="plan5", project_id=p, title="计划五"))
        s.commit()
    r2 = client.post(
        "/api/v1/sync/batch",
        headers=auth,
        json={
            "ops": [
                {
                    "op_id": "y",
                    "entity": "plan",
                    "entity_id": "plan5",
                    "client_ts": now,
                    "patch": {"project_id": "other"},
                }
            ]
        },
    )
    assert r2.json()["results"][0]["status"] == "error"
    assert "project_id" in r2.json()["results"][0]["reason"]

    with S(engine) as s:
        assert s.get(Plan, "plan5").project_id == p


def test_pair_edit_accepts_stable_pair_key(client, auth, engine) -> None:
    """F18 手动编辑：客户端只用 pair_key 寻址，不放自增主键。"""
    p = _proj(client, auth)
    from sqlalchemy.orm import Session as S

    with S(engine) as s:
        piece = Piece(id="piece-pk", project_id=p, title="篇")
        s.add(piece)
        s.flush()
        s.add(
            Pair(
                id="pair-row-1",
                piece_id=piece.id,
                pair_key="k-abcd",
                seq=1,
                zh="你好",
                en="hello",
            )
        )
        s.commit()

    r = client.post(
        "/api/v1/sync/batch",
        headers=auth,
        json={
            "ops": [
                {
                    "op_id": "pair-edit",
                    "entity": "pair_edit",
                    "entity_id": "k-abcd",  # 不是主键 id
                    "client_ts": (utcnow() + timedelta(seconds=5)).isoformat(),
                    "patch": {"en": "hi there", "manually_edited": True},
                }
            ]
        },
    )
    res = r.json()["results"][0]
    assert res["status"] == "applied", res

    with S(engine) as s:
        pair = s.get(Pair, "pair-row-1")
        assert pair.en == "hi there"
        assert pair.manually_edited is True


def test_batch_accepts_timezone_aware_client_ts(client, auth, engine) -> None:
    """浏览器 ``toISOString()`` 发的是带 ``Z`` 的 aware 时间戳。

    库里 ``updated_at`` 是 naive，直接比较会 ``TypeError`` → 整批 500。
    服务端要把它折成 naive UTC 再参与 LWW 裁决。
    """
    p = _proj(client, auth)
    from sqlalchemy.orm import Session as S

    with S(engine) as s:
        piece = Piece(id="piece-tz", project_id=p, title="篇", sort_order=0)
        s.add(piece)
        s.commit()

    future = datetime.now(UTC) + timedelta(seconds=30)
    r = client.post(
        "/api/v1/sync/batch",
        headers=auth,
        json={
            "ops": [
                {
                    "op_id": "tz-1",
                    "entity": "piece_progress",
                    "entity_id": "piece-tz",
                    "client_ts": future.isoformat(),  # 带 +00:00
                    "patch": {"last_pos": 5},
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["status"] == "applied", r.json()

    with S(engine) as s:
        assert s.get(Piece, "piece-tz").last_pos == 5

    # aware 的旧时间同样要能判成 conflict（不能因为时区而误判成"更新"）
    stale = datetime.now(UTC) - timedelta(hours=1)
    r2 = client.post(
        "/api/v1/sync/batch",
        headers=auth,
        json={
            "ops": [
                {
                    "op_id": "tz-2",
                    "entity": "piece_progress",
                    "entity_id": "piece-tz",
                    "client_ts": stale.isoformat(),
                    "patch": {"last_pos": 99},
                }
            ]
        },
    )
    assert r2.json()["results"][0]["status"] == "conflict", r2.json()
    with S(engine) as s:
        assert s.get(Piece, "piece-tz").last_pos == 5, "旧写入不该生效"


def test_batch_limit_enforced(client, auth) -> None:
    from app.core.config import get_settings

    limit = get_settings().sync_batch_limit
    now = utcnow().isoformat()
    ops = [
        {"op_id": str(i), "entity": "plan", "entity_id": "x", "client_ts": now, "patch": {}}
        for i in range(limit + 1)
    ]
    r = client.post("/api/v1/sync/batch", headers=auth, json={"ops": ops})
    # limit=200，请求体本身可能先被 pydantic 的 max_length 拦下，两种都算拒
    assert r.status_code in (400, 422)
    assert r.json()["error"]["code"] in {"sync_stale", "validation"}
