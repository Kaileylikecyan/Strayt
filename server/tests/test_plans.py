"""F22 学习路径闭环：阶段计划列表与每日待办聚合（读取侧）。

改写入口走弱同步（``POST /sync/batch`` 的 ``plan`` / ``goal`` 实体），
这里只验证读取聚合与排序；写通道另有 ``test_sync.py`` 覆盖。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from app.db.models import Plan, Project
from sqlalchemy.orm import Session

REF = date(2026, 9, 28)


@pytest.fixture
def plans(
    db_session: Session,
) -> dict:
    p = Project(name="备考", type="recite")
    db_session.add(p)
    db_session.commit()
    pid = p.id

    def add(title, *, due, status="todo", sort=0):
        db_session.add(
            Plan(
                project_id=pid,
                title=title,
                due_date=due,
                status=status,
                sort_order=sort,
            )
        )
        db_session.flush()

    add("逾期-欠了", due=REF - timedelta(days=2), sort=5)
    add("今天-正在做", due=REF, status="doing", sort=1)
    add("后三天", due=REF + timedelta(days=3), sort=2)
    add("未排期", due=None, sort=9)
    add("已完成-旧", due=REF - timedelta(days=5), status="done", sort=0)
    db_session.commit()
    return {"pid": pid}


class Test阶段计划列表:
    def test_lists_in_agenda_order(self, client, auth, plans):
        r = client.get("/api/v1/plans", params={"ref_date": REF.isoformat()}, headers=auth)
        assert r.status_code == 200
        titles = [x["title"] for x in r.json()]
        assert titles == ["逾期-欠了", "今天-正在做", "后三天", "未排期", "已完成-旧"]

        first = r.json()[0]
        assert first["due_date"] == (REF - timedelta(days=2)).isoformat()
        assert first["days_until"] == -2
        assert first["overdue"] is True
        assert r.json()[1]["overdue"] is False
        assert r.json()[2]["days_until"] == 3
        assert r.json()[3]["days_until"] is None

    def test_filters_by_project_and_status(self, client, auth, plans, db_session):
        other = Project(name="另一个", type="recite")
        db_session.add(other)
        db_session.commit()
        db_session.add(
            Plan(project_id=other.id, title="他组逾期", due_date=REF - timedelta(days=1))
        )
        db_session.commit()

        r = client.get(
            "/api/v1/plans",
            params={"project_id": other.id, "ref_date": REF.isoformat()},
            headers=auth,
        )
        assert [x["title"] for x in r.json()] == ["他组逾期"]

        r = client.get(
            "/api/v1/plans",
            params={"status": "doing", "ref_date": REF.isoformat()},
            headers=auth,
        )
        assert [x["title"] for x in r.json()] == ["今天-正在做"]

    def test_rejects_bad_status_value(self, client, auth, plans):
        r = client.get("/api/v1/plans", params={"status": "later"}, headers=auth)
        assert r.status_code == 422


class Test每日待办聚合:
    def test_buckets_by_due_date(self, client, auth, plans):
        r = client.get("/api/v1/plans/daily", params={"day": REF.isoformat()}, headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["date"] == REF.isoformat()
        assert body["stats"] == {"total": 5, "open": 4, "done": 1}
        assert [x["title"] for x in body["overdue"]] == ["逾期-欠了"]
        assert [x["title"] for x in body["today"]] == ["今天-正在做"]
        assert [x["title"] for x in body["later"]] == ["后三天"]
        assert [x["title"] for x in body["unscheduled"]] == ["未排期"]
        assert [x["title"] for x in body["done"]] == ["已完成-旧"]

    def test_overdue_criteria_follows_day_param(self, client, auth, plans):
        # 参考日提前到「逾期-欠了」的到期日当天 → 它变为今天待办，今天的任务则排到之后
        shifted = (REF - timedelta(days=2)).isoformat()
        body = client.get("/api/v1/plans/daily", params={"day": shifted}, headers=auth).json()
        assert body["overdue"] == []
        assert [x["title"] for x in body["today"]] == ["逾期-欠了"]
        assert [x["title"] for x in body["later"]] == ["今天-正在做", "后三天"]

    def test_empty_project_returns_empty_buckets(self, client, auth):
        body = client.get(
            "/api/v1/plans/daily", params={"day": REF.isoformat()}, headers=auth
        ).json()
        assert body["stats"] == {"total": 0, "open": 0, "done": 0}
        for k in ("overdue", "today", "later", "unscheduled", "done"):
            assert body[k] == []
