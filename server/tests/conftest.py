"""pytest 公共夹具。

测试跑**真实 MySQL**（``strayt_test`` 库），不用 SQLite。理由：schema 里有
``json_valid`` CHECK、``ON DELETE SET NULL``、utf8mb4 等 MySQL 特有约束，
拿 SQLite 测等于测了个假的 —— 那些约束恰恰是这版 schema 最容易出错的地方。

每个测试前清空全部业务表（保留 settings 里的口令哈希），测试间互不污染，
也绝不碰开发库 ``strayt``。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# 必须在导入 app.* 之前设置：Settings 是 lru_cache 的
os.environ.setdefault(
    "STRAYT_DB_URL", "mysql+pymysql://strayt:strayt@127.0.0.1:3306/strayt_test?charset=utf8mb4"
)
os.environ.setdefault("STRAYT_SECRET_KEY", "dGVzdC1vbmx5LWtleS1ub3QtcmVhbC1zZWNyZXQtdGVzdA==")
os.environ.setdefault("STRAYT_DATA_DIR", "var_test")

from app.core.config import get_settings
from app.core.deps import PASSWORD_HASH_KEY
from app.core.security import hash_password, issue_session_token
from app.db.models import Base, Setting
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

TEST_PASSWORD = "test-password-1234"


def _assert_test_db(url: str) -> str:
    """从连接串里取出库名，确认是测试库。

    下面 ``_ensure_schema`` 会 **drop 全部表**，万一 ``STRAYT_DB_URL`` 被指向了
    开发库就会把数据全删掉。``os.environ.setdefault`` 尊重已存在的环境变量，
    所以这个保护不是多余的。
    """
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        raise RuntimeError(
            f"拒绝在非测试库上重建 schema：当前库名 {name!r}，必须以 '_test' 结尾。"
            "pytest 只允许操作测试库。"
        )
    return name


def _ensure_schema(eng) -> None:
    """让测试库的 schema 与 models 对齐。

    ``create_all`` 对**已存在**的表是 no-op —— 加了列也不会补。所以模型改了字段
    而测试库是旧 schema 时，症状是莫名其妙的
    ``Unknown column 'pieces.file_id' in 'field list'``。这里主动比对并重建。

    比对的不只是列名，还有**列类型**。只比列名漏过一次：``raw_text`` 从 ``Text``
    升到 ``LONGTEXT`` 后列名没变，于是测试库悄悄留在 TEXT 上，真实 PDF 一存就
    ``DataError 1406``，而全是小样本的单元测试照样全绿。
    """
    insp = inspect(eng)
    stale = False
    for name, table in Base.metadata.tables.items():
        if name not in insp.get_table_names():
            continue  # 缺表，交给 create_all 建
        have = {c["name"]: c for c in insp.get_columns(name)}
        want = {c.name: c for c in table.columns}
        if set(want) - set(have):
            stale = True
            break
        if any(_type_mismatch(want[c], have[c]) for c in want):
            stale = True
            break
    if stale:
        _assert_test_db(str(eng.url))
        Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)


#: MySQL 里 ``TEXT`` / ``LONGTEXT`` / ``VARCHAR(n)`` 在不同方言下渲染成不同类型串。
#: 反射出来的是 ``LONGTEXT()`` / ``VARCHAR(n)``，跟列声明的编译结果比类型名和长度。
_TYPE_OK: dict[str, int | None] = {"LONGTEXT": None, "TEXT": 65535, "MEDIUMTEXT": 16_777_215}


def _type_mismatch(want, have) -> bool:
    """列类型是否与模型声明不一致（只看长度/大���这一维，够抓 TEXT→LONGTEXT）。"""
    w, h = str(want.type).upper(), str(have["type"]).upper()
    base = lambda s: s.split("(")[0]  # noqa: E731
    if base(w) != base(h):
        return True
    if base(w) in _TYPE_OK:
        # 家族相同，比长度：TEXT(65535) vs LONGTEXT(4G) 判为不一致
        want_len = want.type.length if base(w) == "VARCHAR" else _TYPE_OK[base(w)]
        have_len = have["type"].length if base(w) == "VARCHAR" else _TYPE_OK[base(h)]
        return want_len != have_len
    return False


#: 夹具里种进 DB 的口令哈希原样。改口令用例要靠它拿旧哈希做对比。
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)
#: 与 TEST_PASSWORD_HASH 配套的会话令牌（HMAC 签名，免去每个用例跑一次 Argon2 登录）
TEST_SESSION_TOKEN = issue_session_token(TEST_PASSWORD_HASH)


@pytest.fixture(scope="session")
def engine() -> Iterator:
    eng = create_engine(get_settings().db_url)
    _assert_test_db(get_settings().db_url)
    _ensure_schema(eng)

    # 口令哈希种一次（幂等：上次跑留下的行直接覆盖，settings 表不被 _clean 清空）
    with Session(eng) as s:
        row = s.get(Setting, PASSWORD_HASH_KEY)
        if row is None:
            s.add(Setting(k=PASSWORD_HASH_KEY, v=TEST_PASSWORD_HASH))
        else:
            row.v = TEST_PASSWORD_HASH
        s.commit()

    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean(engine) -> Iterator[None]:
    """每个测试前清空业务表（保留 settings 里的口令哈希）。

    注意：**不要**在测试里真的调用 `/auth/setup` —— 那会覆盖这里种下的哈希，
    把后续用例的 ``auth`` 夹具全部作废。要测 setup 语义的用例请自己造隔离夹具，
    或直接测 ``hash_password`` / ``verify_password`` 这层纯函数。
    """
    with Session(engine) as s:
        for tbl in reversed(Base.metadata.sorted_tables):
            if tbl.name != "settings":
                s.execute(tbl.delete())
        s.commit()
    yield


@pytest.fixture
def client(engine, monkeypatch) -> Iterator[TestClient]:
    from app.core import deps as deps_mod

    # 必须和 app/db/session.py 的 SessionLocal **逐项对齐**。
    # 曾经这里写的是裸 `sessionmaker(bind=engine)`，autoflush 默认 True 而生产是
    # False，于是「先 add 再 select 查不到新行」这类 bug 在单测里全绿、只在线上炸：
    # 拆分对句时新行没 flush，重编号漏掉它们，seq 直接撞车。
    # 凡是生产 session 的配置项变了，这里要一起改，别让两边漂移。
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _get_db() -> Iterator[Session]:
        s = factory()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(deps_mod, "get_db", _get_db)

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def db_session(engine) -> Iterator[Session]:
    """直连测试库的 Session，供测试造数据 / 断言（与 client 同引擎）。"""
    s = Session(engine)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_SESSION_TOKEN}"}
