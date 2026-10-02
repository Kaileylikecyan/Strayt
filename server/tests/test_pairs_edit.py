"""F18 结构化编辑：拆分 / 合并段落对（HTTP 层）。"""

from __future__ import annotations

from app.db.models import Pair, Piece
from sqlalchemy.orm import Session as S


def _digest(pairs: list[dict]) -> list[dict]:
    """抽出对断言有用的字段，多个元素时可比对。"""
    return [
        {"seq": p["seq"], "zh": p["zh"], "en": p["en"], "manually_edited": p["manually_edited"]}
        for p in pairs
    ]


def _seed(client, auth, engine, *, n=2, blocks: list[int | None] | None = None) -> dict:
    """建一个背诵项目（HTTP）+ 一篇目 + n 个段落对，返回 {project_id, piece_id, keys}。

    ``blocks`` 给定时按它写 ``block_no``，用来测 ADR-0012 的块号在
    拆分/合并后是否还在 —— 丢了块号，「按段」会在用户手动编辑一次之后静默失效。
    """
    proj_id = client.post(
        "/api/v1/projects", headers=auth, json={"name": "背诵", "type": "recite"}
    ).json()["id"]
    piece_id = "split-piece"
    with S(engine) as s:
        piece = Piece(id=piece_id, project_id=proj_id, title="篇")
        s.add(piece)
        s.flush()
        keys = []
        for i in range(n):
            key = f"k{i}"
            keys.append(key)
            s.add(
                Pair(
                    piece_id=piece.id,
                    pair_key=key,
                    seq=i,
                    zh=f"中{i}",
                    en=f"en{i}",
                    block_no=blocks[i] if blocks else None,
                )
            )
        s.commit()
    return {"project_id": proj_id, "piece_id": piece_id, "keys": keys}


class Test拆分:
    def test_should_split_one_pair_into_two(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=1)
        r = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/split",
            headers=auth,
            json={
                "pair_key": seed["keys"][0],
                "zh_a": "中a",
                "en_a": "enA",
                "zh_b": "中b",
                "en_b": "enB",
            },
        )
        assert r.status_code == 200, r.text
        got = r.json()
        assert len(got) == 2  # 原来 1 条，拆成 2 条
        assert {p["zh"] for p in got} == {"中a", "中b"}
        assert all(p["manually_edited"] for p in got)
        # seq 稠密连续
        assert [p["seq"] for p in got] == [0, 1]
        assert len({p["pair_key"] for p in got}) == 2

    def test_should_renumber_rest_when_splitting_middle(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=3)
        r = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/split",
            headers=auth,
            json={
                "pair_key": seed["keys"][1],
                "zh_a": "中1a",
                "en_a": "en1A",
                "zh_b": "中1b",
                "en_b": "en1B",
            },
        )
        assert r.status_code == 200, r.text
        got = r.json()
        assert len(got) == 4
        assert [p["seq"] for p in got] == [0, 1, 2, 3]
        assert got[0]["zh"] == "中0"
        assert got[3]["zh"] == "中2"

    def test_should_reject_empty_half(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=1)
        r = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/split",
            headers=auth,
            json={
                "pair_key": seed["keys"][0],
                "zh_a": "中a",
                "en_a": "enA",
                "zh_b": "  ",
                "en_b": "enB",
            },
        )
        assert r.status_code == 400, r.text

    def test_should_404_on_unknown_pair_key(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=1)
        r = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/split",
            headers=auth,
            json={"pair_key": "nope", "zh_a": "a", "en_a": "b", "zh_b": "c", "en_b": "d"},
        )
        assert r.status_code == 404, r.text


class Test合并:
    def test_should_merge_two_adjacent_pairs(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=2)
        r = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/merge",
            headers=auth,
            json={"pair_key": seed["keys"][0], "with_key": seed["keys"][1]},
        )
        assert r.status_code == 200, r.text
        got = r.json()
        assert len(got) == 1
        assert got[0]["zh"] == "中0中1"
        assert got[0]["en"] == "en0 en1"
        assert got[0]["manually_edited"] is True
        assert got[0]["seq"] == 0

    def test_should_reject_non_adjacent_merge(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=3)
        r = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/merge",
            headers=auth,
            json={"pair_key": seed["keys"][0], "with_key": seed["keys"][2]},
        )
        assert r.status_code == 400, r.text

    def test_should_reject_merge_with_itself(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=2)
        r = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/merge",
            headers=auth,
            json={"pair_key": seed["keys"][0], "with_key": seed["keys"][0]},
        )
        assert r.status_code == 400, r.text

    def test_should_preserve_adjacency_across_middle(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=3)
        got = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/merge",
            headers=auth,
            json={"pair_key": seed["keys"][1], "with_key": seed["keys"][2]},
        ).json()
        assert len(got) == 2
        assert [p["seq"] for p in got] == [0, 1]
        assert got[1]["zh"] == "中1中2"


class Test块号随编辑保留:
    """ADR-0012：手动编辑不能让「按段」静默失效。

    ``block_no`` 丢了不会报错，只表现为「我明明按段分好了，改一句就全变一句一格」。
    """

    def test_拆分后两半沿用原块号(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=1, blocks=[3])
        got = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/split",
            headers=auth,
            json={
                "pair_key": seed["keys"][0],
                "zh_a": "中a",
                "en_a": "enA",
                "zh_b": "中b",
                "en_b": "enB",
            },
        ).json()
        assert [p["block_no"] for p in got] == [3, 3], "拆开的两半本就在同一段里"

    def test_拆分后不存在的块号保持null(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=1, blocks=[None])
        got = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/split",
            headers=auth,
            json={
                "pair_key": seed["keys"][0],
                "zh_a": "中a",
                "en_a": "enA",
                "zh_b": "中b",
                "en_b": "enB",
            },
        ).json()
        assert [p["block_no"] for p in got] == [None, None], "不能凭空给块号"

    def test_同块内合并保留块号(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=2, blocks=[5, 5])
        got = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/merge",
            headers=auth,
            json={"pair_key": seed["keys"][0], "with_key": seed["keys"][1]},
        ).json()
        assert [p["block_no"] for p in got] == [5]

    def test_跨块合并取靠前的块号(self, client, auth, engine):
        seed = _seed(client, auth, engine, n=2, blocks=[5, 9])
        got = client.post(
            f"/api/v1/projects/{seed['project_id']}/pieces/{seed['piece_id']}/pairs/merge",
            headers=auth,
            json={"pair_key": seed["keys"][0], "with_key": seed["keys"][1]},
        ).json()
        assert [p["block_no"] for p in got] == [5], "按首字符所在块，与解析层口径一致"
