"""访问口令登录（架构红线 5，决策 D-01 / ADR-0009）。

覆盖三层：纯函数（哈希与会话令牌）、HTTP 接口（state/setup/login/改密）、
以及准入门禁（AuthDep）。

**夹具陷阱**：``conftest.py`` 的 ``_clean`` 保留 ``settings`` 里的口令哈希，所以
本文件里真正调用 ``/auth/setup`` 的用例会**覆盖**那个哈希，让后续用例的 ``auth``
夹具失效。故 setup 路径靠「先备份哈希、测完还原」来测，不要用 ``monkeypatch`` 假设
它会自己回滚 —— DB 不是 fixture 作用域内的对象。
"""

from __future__ import annotations

import time

import pytest
from app.core.deps import PASSWORD_HASH_KEY
from app.core.security import (
    MIN_PASSWORD_LEN,
    hash_password,
    issue_session_token,
    verify_password,
    verify_session_token,
)
from app.db.models import Setting
from sqlalchemy import select
from sqlalchemy.orm import Session

# 夹具常量放 conftest 里，但那是 pytest 插件模块、不在 sys.path 上，
# 所以这里用 `tests.conftest` 这个包路径拿，而不是 `import conftest`。
from tests.conftest import TEST_PASSWORD, TEST_PASSWORD_HASH, TEST_SESSION_TOKEN


def _put_password_hash(engine) -> None:
    """把夹具的口令哈希写回去（行不存在就建）。

    不能无脑 ``row.v = ...`` —— ``no_password`` 夹具刚把整行删了，
    ``db.get`` 会返回 None，teardown 就会炸成
    ``AttributeError: 'NoneType' object has no attribute 'v'``，
    把真正的断言失败盖掉。
    """
    with Session(engine) as s:
        row = s.get(Setting, PASSWORD_HASH_KEY)
        if row is None:
            s.add(Setting(k=PASSWORD_HASH_KEY, v=TEST_PASSWORD_HASH))
        else:
            row.v = TEST_PASSWORD_HASH
        s.commit()


@pytest.fixture
def restore_password_hash(engine):
    """把 ``settings`` 里的口令哈希恢复成夹具种下的那个。

    必须用它护住会改口令的用例（setup / 改密），否则同一次 pytest 运行里
    后面的用例会突然全部 401。**不要**指望 ``monkeypatch`` 自动回滚 ——
    写库的是服务端自己的 Session，不是 fixture 能管的临时对象。
    """
    yield
    _put_password_hash(engine)


@pytest.fixture
def no_password(engine, restore_password_hash):
    """把口令哈希行删掉，模拟「服务端从未设置过口令」的全新安装。"""
    with Session(engine) as s:
        s.execute(Setting.__table__.delete())
        s.commit()
    yield


# ==========================================================================
# 纯函数层
# ==========================================================================
def test_hash_password_is_argon2_and_verifies() -> None:
    encoded = hash_password("correct-horse-battery")
    assert encoded.startswith("$argon2id$")
    assert "correct-horse-battery" not in encoded
    assert verify_password("correct-horse-battery", encoded) is True
    assert verify_password("wrong", encoded) is False


def test_hash_password_salted() -> None:
    """同口令两次哈希必须不同 —— 否则相同口令会互相印证。"""
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


def test_verify_password_tolerates_garbage_hash() -> None:
    """哈希列被写坏/为空时不能抛异常，直接判失败。"""
    assert verify_password("x", "") is False
    assert verify_password("", "$argon2id$broken") is False
    assert verify_password("x", "not-a-hash") is False


@pytest.mark.parametrize("pw", ["", "short", "a" * (MIN_PASSWORD_LEN - 1)])
def test_weak_password_rejected(pw: str) -> None:
    """长度下限以下必须拒。哈希函数抛 ValueError 而不是返回弱哈希。"""
    with pytest.raises(ValueError, match="口令"):
        hash_password(pw)


def test_long_password_rejected() -> None:
    """超长输入会让 Argon2 白烧内存，显式挡住。"""
    with pytest.raises(ValueError, match="过长"):
        hash_password("a" * 2000)


def test_session_token_roundtrip() -> None:
    ph = hash_password("pw-for-session")
    token = issue_session_token(ph)
    assert verify_session_token(token, ph) is True


def test_session_token_bound_to_password_hash() -> None:
    """**改口令即换密钥**：旧口令签发的令牌必须作废。这是 ADR-0009 的核心性质。"""
    old_hash = hash_password("old-password")
    token = issue_session_token(old_hash)
    new_hash = hash_password("new-password")
    assert verify_session_token(token, new_hash) is False


def test_session_token_expiry() -> None:
    ph = hash_password("pw-for-expiry")
    token = issue_session_token(ph, ttl_seconds=-1)
    assert verify_session_token(token, ph) is False
    # 显式给「当前时间晚于过期点」也判失败
    assert (
        verify_session_token(issue_session_token(ph, ttl_seconds=1), ph, now=time.time() + 10)
        is False
    )


@pytest.mark.parametrize("bad", ["", "nodot", "a.b", "!!!.???", "MQ==.MQ=="])
def test_session_token_rejects_garbage(bad: str) -> None:
    assert verify_session_token(bad, hash_password("pw-garbage")) is False


# ==========================================================================
# HTTP 层
# ==========================================================================
def test_health_needs_no_auth(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_state_is_public(client) -> None:
    """/auth/state 必须免鉴权 —— 客户端还没登录就要靠它决定显示哪个表单。"""
    r = client.get("/api/v1/auth/state")
    assert r.status_code == 200
    assert r.json() == {"password_set": True}


def test_login_returns_usable_session(client) -> None:
    """口令换来的令牌要真能过 AuthDep，否则等于没登录。"""
    r = client.post("/api/v1/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 200
    token = r.json()["session_token"]
    assert (
        client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )


def test_login_wrong_password_401(client) -> None:
    r = client.post("/api/v1/auth/login", json={"password": "definitely-wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "password_wrong"


def test_setup_refuses_when_password_already_set(client) -> None:
    """已设过口令时 /auth/setup 必须 409 —— 不给静默覆盖留口子。"""
    r = client.post("/api/v1/auth/setup", json={"password": "brand-new-password"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "password_already_set"


def test_setup_creates_when_absent(client, engine, no_password) -> None:
    assert client.get("/api/v1/auth/state").json() == {"password_set": False}

    r = client.post("/api/v1/auth/setup", json={"password": "first-password"})
    assert r.status_code == 200
    token = r.json()["session_token"]
    assert (
        client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )

    with Session(engine) as s:
        assert verify_password("first-password", s.get(Setting, PASSWORD_HASH_KEY).v)


def test_login_before_password_set_409(client, no_password) -> None:
    r = client.post("/api/v1/auth/login", json={"password": "anything-long"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "password_not_set"


def test_setup_weak_password_422(client, no_password) -> None:
    """太弱的口令要走 422 而不是 500 —— pydantic 校验器抛的 ValueError
    被 ``jsonable_errors`` 接住了（错误体序列化的坑，见 rtm 缺口 11）。"""
    r = client.post("/api/v1/auth/setup", json={"password": "abc"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation"


# ==========================================================================
# 改密
# ==========================================================================
def test_change_password_invalidates_old_session(client, restore_password_hash) -> None:
    """改密的核心断言：旧令牌当场作废，新令牌可用，且**不需要重新输口令**。

    **必须挂 ``restore_password_hash``**：这个用例会真的把库里的口令哈希换成新值，
    不还原的话同一次运行里后续所有用 ``auth`` 夹具的用例全部 401，而且报的
    是 ``unauthorized``，看不出是夹具被污染（这类失败最难查）。
    """
    old_headers = {"Authorization": f"Bearer {TEST_SESSION_TOKEN}"}
    assert client.get("/api/v1/projects", headers=old_headers).status_code == 200

    r = client.post(
        "/api/v1/auth/password",
        headers=old_headers,
        json={"old_password": TEST_PASSWORD, "new_password": "brand-new-password"},
    )
    assert r.status_code == 200
    new_token = r.json()["session_token"]
    assert new_token != TEST_SESSION_TOKEN

    # 旧令牌死
    assert client.get("/api/v1/projects", headers=old_headers).status_code == 401
    # 新令牌活
    assert (
        client.get("/api/v1/projects", headers={"Authorization": f"Bearer {new_token}"}).status_code
        == 200
    )
    # 旧口令登不上了
    assert client.post("/api/v1/auth/login", json={"password": TEST_PASSWORD}).status_code == 401
    # 新口令能登
    assert (
        client.post("/api/v1/auth/login", json={"password": "brand-new-password"}).status_code
        == 200
    )


def test_change_password_weak_new_rejected(client, engine, restore_password_hash) -> None:
    """改密同样要挡弱口令，且失败时**不能动**原哈希 —— 否则用户被锁在外面。

    弱口令的 422 来自 pydantic 请求体校验（早于路由与 AuthDep），
    所以这里必须同时带上有效令牌，才能证明是「口令弱」而不是「没登录」被拒。
    """
    with Session(engine) as s:
        before = s.get(Setting, PASSWORD_HASH_KEY).v
    r = client.post(
        "/api/v1/auth/password",
        headers={"Authorization": f"Bearer {TEST_SESSION_TOKEN}"},
        json={"old_password": TEST_PASSWORD, "new_password": "abc"},
    )
    assert r.status_code == 422, r.text
    with Session(engine) as s:
        assert s.get(Setting, PASSWORD_HASH_KEY).v == before


def test_change_password_requires_auth(client) -> None:
    """改密必须鉴权（router2 带 AuthDep）—— 不能靠知道旧口令就改。"""
    r = client.post(
        "/api/v1/auth/password",
        json={"old_password": TEST_PASSWORD, "new_password": "a-fine-new-password"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_change_password_wrong_old_401(client, auth) -> None:
    r = client.post(
        "/api/v1/auth/password",
        headers=auth,
        json={"old_password": "nope", "new_password": "another-password"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "password_wrong"


# ==========================================================================
# 准入门禁
# ==========================================================================
def test_missing_session_401(client) -> None:
    r = client.get("/api/v1/projects")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_bad_session_401(client) -> None:
    r = client.get("/api/v1/projects", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_valid_session_ok(client, auth) -> None:
    assert client.get("/api/v1/projects", headers=auth).status_code == 200


def test_password_hash_stored_argon2_not_plaintext(engine) -> None:
    with engine.connect() as c:
        row = c.execute(select(Setting.v).where(Setting.k == PASSWORD_HASH_KEY)).scalar_one()
    assert row.startswith("$argon2id$")
    assert TEST_PASSWORD not in row


def test_validation_error_body(client, auth) -> None:
    """校验失败也得是同一形状的错误体。"""
    r = client.post("/api/v1/projects", headers=auth, json={"name": "x", "type": "bogus"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation"
    assert any("type" in str(d) for d in err["detail"])


def test_export_redacts_password_hash(client, auth) -> None:
    """**F24 导出不能带口令哈希**：备份是用户会下载、转发、长期存盘的，
    Argon2id 哈希是离线爆破的直接靶子。原实现会把它一并导出去。"""
    from app.api.routers.export import REDACTED_SETTINGS

    assert PASSWORD_HASH_KEY in REDACTED_SETTINGS
    r = client.get("/api/v1/export/snapshot", headers=auth)
    assert r.status_code == 200
    keys = {row["k"] for row in r.json()["settings"]}
    assert PASSWORD_HASH_KEY not in keys


# ---- CORS：桌面端能不能连上服务端 ----------------------------------------
#
# 这组用例是被一次真实的 `failed to fetch` 逼出来的：CORS 正则只写了
# `tauri://localhost`，而 **Windows 的 WebView2 发的是 `http://tauri.localhost`**
# （用 http 模拟自定义协议来满足同源策略）。少了它，桌面端所有请求在浏览器层
# 就被拦掉 —— 服务端日志一片干净，只有前端报 `failed to fetch`，极易误判成
# 「服务没起 / 端口不对」。所以这里把三个平台各自的 origin 都钉住。


@pytest.mark.parametrize(
    "origin",
    [
        "http://tauri.localhost",  # Windows WebView2
        "tauri://localhost",  # macOS / Linux
        "http://localhost:5173",  # Vite dev（网页端）
        "http://127.0.0.1:5174",  # Vite dev（Tauri devUrl）
    ],
)
def test_cors_allows_real_client_origins(client, origin: str) -> None:
    r = client.get("/api/v1/auth/state", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize(
    "origin",
    ["http://evil.example.com", "https://evil.example.com", "http://tauri.localhost.evil.com"],
)
def test_cors_rejects_foreign_origins(client, origin: str) -> None:
    """放行必须精确到 host，不能用前缀/包含匹配 —— 否则等于对全网开放。"""
    r = client.get("/api/v1/auth/state", headers={"Origin": origin})
    assert "access-control-allow-origin" not in r.headers


def test_cors_preflight_from_desktop_app_is_allowed(client) -> None:
    """带 ``Authorization`` 的实际请求会先发 preflight；OPTIONS 也必须放行，
    否则光放行 GET 不够，桌面端第一个要鉴权的接口照样连不上。"""
    r = client.options(
        "/api/v1/projects",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://tauri.localhost"
    allowed_headers = (r.headers.get("access-control-allow-headers") or "").lower()
    assert "authorization" in allowed_headers
