"""准入与统一错误体（架构红线 5）。"""

from __future__ import annotations


def test_health_needs_no_token(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_missing_token_401(client) -> None:
    r = client.get("/api/v1/projects")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] in {"token_missing", "token_invalid"}
    assert "error" in body and "code" in body["error"]


def test_wrong_token_401(client) -> None:
    r = client.get("/api/v1/projects", headers={"Authorization": "Bearer stt_nope"})
    assert r.status_code == 401


def test_valid_token_ok(client, auth) -> None:
    assert client.get("/api/v1/projects", headers=auth).status_code == 200


def test_token_hashed_not_plaintext(client, engine) -> None:
    """库里绝不能出现明文令牌。"""
    from app.core.deps import TOKEN_HASH_KEY
    from app.db.models import Setting
    from sqlalchemy import select

    with engine.connect() as c:
        row = c.execute(select(Setting.v).where(Setting.k == TOKEN_HASH_KEY)).scalar_one()
    assert row is not None
    assert not row.startswith("stt_")
    assert row.startswith("pbkdf2$")


def test_validation_error_body(client, auth) -> None:
    """参数错误也必须收敛到同一形状，且 detail 带字段名。"""
    r = client.post("/api/v1/projects", headers=auth, json={"name": "x", "type": "bogus"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation"
    assert any("type" in str(d) for d in err["detail"])
