"""HTTP 级真实数据 E2E：对着**跑着的服务端**把客户端要走的路走一遍。

和 ``server/var_test/job_e2e.py`` 的区别：那个在进程内直接调引擎，验证加工质量；
这个只发 HTTP，验证**客户端实际会踩的协议面**——鉴权、bootstrap、上传、建任务、
轮询、以及 F18 的弱同步写通道（``pair_edit`` 用 ``pair_key`` 寻址）。

它抓到的都是单测抓不到的 bug：
  * 同步路由是 ``def``（线程池里跑）→ ``asyncio.create_task`` 无运行事件循环 → 500
  * ``files.sha256`` 全局唯一 → 同一份资料传进第二个项目被判「文件不属于该项目」
  * 浏览器 ``toISOString()`` 发带 ``Z`` 的 ``client_ts``，库里是 naive → LWW 比较 TypeError

用法（服务端已在 8000 端口跑着）::

    $env:STRAYT_PASSWORD = "<访问口令>"       # 不要把口令写进文件/提交
    uv run --project server python scripts\\smoke_http.py
    uv run --project server python scripts\\smoke_http.py --keep   # 保留项目便于人工检查

鉴权跟客户端走同一条路：``/auth/login`` 换会话令牌，再拿它当 Bearer 用（ADR-0009）。
所以这个脚本顺带也验了「口令能换到可用会话」这条链路。

会在本地 dev 库里建一个临时项目并在结束时删掉（进回收站）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
PDF = REPO / "server" / "fixtures" / "daoyouci.pdf"


def run_graph_cases(
    call: Callable[..., Any],
    check: Callable[[bool, str], None],
) -> None:
    """F12 图谱与一级分类规则的 HTTP 级验证（不调 LLM，不跑抽取任务）。

    只打协议面：分类规则 CRUD（含空 pattern 挡回、非法 regex 挡回）、以及
    ``node_category`` 弱同步写实体能不能按 ``node_id`` 定位。

    **覆盖边界**：这里建不出真节点（节点要 LLM 抽取才产生），所以
    ``node_category`` 只能验到「节点不存在」这一道闸。分类归属闸的真覆盖在
    ``tests/test_category_rules.py::TestNodeCategorySync``（那里直接种节点）。
    """
    gid = call(
        "POST",
        "/api/v1/projects",
        {
            "name": f"冒烟·图谱E2E {datetime.now(timezone.utc).strftime('%m%d-%H%M%S')}",
            "type": "graph",
        },
    )["id"]
    print(f"\n[G1] 建图谱项目 {gid[:8]}…")

    try:
        call(
            "PUT",
            f"/api/v1/projects/{gid}/category-rules",
            {
                "rules": [
                    {
                        "match_on": "file_name",
                        "kind": "prefix",
                        "pattern": "高中数学",
                        "category": "高中数学",
                        "enabled": True,
                    },
                    {
                        "match_on": "unit_title",
                        "kind": "regex",
                        "pattern": "^函数",
                        "category": "高中数学",
                        "enabled": True,
                    },
                ]
            },
        )
        got = call("GET", f"/api/v1/projects/{gid}/category-rules")
        check(len(got) == 2, f"规则应有 2 条：{got}")
        check(all(r["id"] for r in got), "规则没落 id")
        check(
            got[0]["pattern"] == "高中数学" and got[0]["enabled"] is True,
            f"规则回读不对：{got[0]}",
        )
        print(f"[G2] 规则整表写入并回读 {len(got)} 条（含 id/优先级）")

        # 整表覆盖：再存一条应该只剩一条
        call(
            "PUT",
            f"/api/v1/projects/{gid}/category-rules",
            {
                "rules": [
                    {
                        "match_on": "file_name",
                        "kind": "contains",
                        "pattern": "英语",
                        "category": "英语",
                    }
                ]
            },
        )
        got2 = call("GET", f"/api/v1/projects/{gid}/category-rules")
        check(len(got2) == 1, f"整表覆盖语义不对，剩 {len(got2)} 条")
        print("[G3] 整表覆盖语义正确（旧规则被清掉）")

        for bad, why in (
            (
                {
                    "match_on": "file_name",
                    "kind": "prefix",
                    "pattern": "  ",
                    "category": "X",
                },
                "空 pattern",
            ),
            (
                {
                    "match_on": "file_name",
                    "kind": "regex",
                    "pattern": "([",
                    "category": "X",
                },
                "非法 regex",
            ),
            (
                {
                    "match_on": "file_name",
                    "kind": "prefix",
                    "pattern": "a",
                    "category": " ",
                },
                "空 category",
            ),
            (
                {"match_on": "nope", "kind": "prefix", "pattern": "a", "category": "X"},
                "非法 match_on",
            ),
        ):
            rejected = False
            try:
                call("PUT", f"/api/v1/projects/{gid}/category-rules", {"rules": [bad]})
            except SystemExit:
                rejected = True
            check(rejected, f"{why} 居然没被挡下")
        print("[G4] 空 pattern / 非法 regex / 空分类名 / 非法 match_on 全被挡（4xx）")

        call("PUT", f"/api/v1/projects/{gid}/category-rules", {"rules": []})
        empty = call("GET", f"/api/v1/projects/{gid}/category-rules")
        check(empty == [], f"清空规则应得空列表：{empty}")
        print("[G5] 规则可清空（回到纯模型判定）")

        # node_category 弱同步写实体：目标节点不存在 / 分类不存在都得被挡，
        # 不能让 FK 把别的项目的分类拉进来（sync.py 的两道闸）
        def push_node_category(entity_id: str, category_id: str) -> dict:
            return call(
                "POST",
                "/api/v1/sync/batch",
                {
                    "ops": [
                        {
                            "op_id": str(uuid.uuid4()),
                            "entity": "node_category",
                            "entity_id": entity_id,
                            "patch": {"category_id": category_id},
                            "client_ts": (
                                datetime.now(timezone.utc) + timedelta(seconds=5)
                            ).isoformat(),
                        }
                    ]
                },
            )["results"][0]

        r_missing_node = push_node_category("does-not-exist", "whatever")
        check(
            r_missing_node["status"] == "skipped",
            f"给不存在的节点改分类应 skipped：{r_missing_node}",
        )
        print(f"[G6] node_category 对未知 node_id → {r_missing_node['status']}")

        # 这条**不是**分类闸的覆盖：图谱临时项目里没有节点（建节点要走 LLM），
        # 所以 entity_id 只能是假的，服务端先在节点闸上就 skipped 了，分类闸根本没走到。
        # 分类闸（尤其「别把别的项目的分类拉进来」）的真覆盖在
        # tests/test_category_rules.py::TestNodeCategorySync::test_reject_cross_project_category，
        # 那里能直接种真节点。这里保留它只是确认两道闸的顺序：节点不存在时
        # 不该因为分类也不存在就报成别的错。
        r_both_missing = push_node_category("also-missing", "no-such-category")
        check(
            r_both_missing["status"] == "skipped",
            f"节点与分类都不存在时应 skipped（节点闸先拦）：{r_both_missing}",
        )
        print(f"[G7] node_category 节点不存在时先在节点闸 skipped → {r_both_missing['status']}")

        # 字段白名单：node_category 不许改别的字段
        r_bad_field = call(
            "POST",
            "/api/v1/sync/batch",
            {
                "ops": [
                    {
                        "op_id": str(uuid.uuid4()),
                        "entity": "node_category",
                        "entity_id": "any",
                        "patch": {"name": "偷偷改名"},
                        "client_ts": (
                            datetime.now(timezone.utc) + timedelta(seconds=5)
                        ).isoformat(),
                    }
                ]
            },
        )["results"][0]
        check(r_bad_field["status"] == "error", f"越权字段应被拒：{r_bad_field}")
        print(f"[G8] node_category 改非白名单字段 → {r_bad_field['status']}")
    finally:
        call("DELETE", f"/api/v1/projects/{gid}")
        print(f"[G9] 清理图谱临时项目 {gid[:8]}…")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--url", default=os.environ.get("STRAYT_URL", "http://127.0.0.1:8000")
    )
    ap.add_argument("--password", default=os.environ.get("STRAYT_PASSWORD", ""))
    ap.add_argument("--keep", action="store_true", help="结束后不删项目（留给人看）")
    ap.add_argument(
        "--graph",
        action="store_true",
        help="额外跑 F12 图谱用例（建图谱项目 + 分类规则 CRUD + node_category 弱同步写）",
    )
    args = ap.parse_args()

    if not args.password:
        print(
            "缺访问口令：设环境变量 STRAYT_PASSWORD 或传 --password", file=sys.stderr
        )
        return 2
    if not PDF.exists():
        print(f"样板 PDF 不在：{PDF}（被 .gitignore 排除，需自备）", file=sys.stderr)
        return 2

    base = args.url.rstrip("/")

    # 免鉴权客户端：探活 + 换会话令牌。login 失败要报得清楚 —— 最常见的原因是
    # 服务端还没设过口令（首次接入要走 /auth/setup），而这里拿不到明文口令。
    def call_public(method: str, path: str, body: object | None = None) -> object:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                payload = res.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            raise SystemExit(
                f"{path} 返回 {e.code}：{e.read().decode(errors='replace')[:300]}"
            ) from e

    call_public("GET", "/api/v1/auth/state")
    session = call_public("POST", "/api/v1/auth/login", {"password": args.password})
    token = session["session_token"] if isinstance(session, dict) else ""
    if not token:
        raise SystemExit("登录成功但没拿到 session_token，服务端行为异常")
    auth = {"Authorization": f"Bearer {token}"}

    def call(
        method: str,
        path: str,
        body: object | None = None,
        raw: bytes | None = None,
        ctype: str | None = None,
    ) -> object:
        data = (
            raw
            if raw is not None
            else (json.dumps(body).encode() if body is not None else None)
        )
        headers = dict(auth)
        if ctype:
            headers["Content-Type"] = ctype
        elif body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                payload = res.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            raise SystemExit(
                f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')[:400]}"
            ) from e
        except urllib.error.URLError as e:
            raise SystemExit(f"连不上 {base}：{e.reason}（服务端没起？）") from e

    def upload(path: Path, project_id: str) -> dict:
        boundary = "----strayt" + uuid.uuid4().hex
        body = (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + path.read_bytes()
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return call(
            "POST",
            f"/api/v1/files?project_id={project_id}",
            raw=body,
            ctype=f"multipart/form-data; boundary={boundary}",
        )

    def check(cond: bool, msg: str) -> None:
        if not cond:
            raise SystemExit(f"断言失败：{msg}")

    boot = call("GET", "/api/v1/sync/bootstrap")
    check(isinstance(boot, dict) and boot.get("schema_version") == 1, "bootstrap 异常")
    print(
        f"[1] bootstrap ok：schema={boot['schema_version']} 项目 {len(boot['projects'])} 个"
    )

    project = call(
        "POST",
        "/api/v1/projects",
        {
            "name": f"冒烟·背诵E2E {datetime.now(timezone.utc).strftime('%m%d-%H%M%S')}",
            "type": "recite",
        },
    )
    pid = project["id"]
    print(f"[2] 建项目 {pid[:8]}… type={project['type']}")

    try:
        f = upload(PDF, pid)
        print(f"[3] 上传 {f['orig_name']} {f['size']}B channel={f['parse_channel']}")

        job = call(
            "POST",
            "/api/v1/jobs",
            {
                "project_id": pid,
                "file_id": f["id"],
                "type": "recite_align",
                "overwrite": True,
            },
        )
        print(f"[4] 建任务 {job['id'][:8]}… 状态 {job['status']}")

        deadline, last, j = time.time() + 900, "", {}
        while time.time() < deadline:
            j = call("GET", f"/api/v1/jobs/{job['id']}")
            line = f"    status={j['status']} {j['done_units']}/{j['total_units']} {j['progress']}%"
            if line != last:
                print(line)
                last = line
            if j["status"] in {"success", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(3)
        else:
            raise SystemExit("任务超时未到终态")

        check(j["status"] == "success", f"任务终态 {j['status']}：{j.get('error')}")
        cost = (j.get("cost_estimate_json") or {}).get("estimated_cny", 0)
        print(f"[5] 任务成功；通道 B（无 Key）预估花费 ¥{cost}")

        pieces = call("GET", f"/api/v1/projects/{pid}/pieces")
        check(bool(pieces), "没有篇目")
        print(f"[6] 篇目 {len(pieces)} 个：{pieces[0]['title'][:24]}…")

        p0 = pieces[0]
        pairs = call("GET", f"/api/v1/projects/{pid}/pieces/{p0['id']}/pairs")
        check(bool(pairs), "没有对句")
        with_page = sum(1 for x in pairs if x.get("loc_page") is not None)
        print(f"[7] 首篇对句 {len(pairs)} 组，带 loc_page {with_page} 组（页码保留）")
        check(with_page == len(pairs), "有对句丢了 loc_page —— 二期出处回看会瞎")

        # F18：客户端只看得见稳定 key（pair_key），拿不到自增主键
        target = pairs[0]
        ts = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        ops = {
            "ops": [
                {
                    "op_id": str(uuid.uuid4()),
                    "entity": "pair_edit",
                    "entity_id": target["pair_key"],
                    "patch": {
                        "zh": "【人工校订】" + target["zh"],
                        "en": target["en"],
                        "manually_edited": True,
                    },
                    "client_ts": ts,
                }
            ]
        }
        res = call("POST", "/api/v1/sync/batch", ops)
        print(f"[8] pair_edit(pair_key) 裁决：{res['results'][0]['status']}")
        check(res["results"][0]["status"] == "applied", str(res["results"][0]))

        again = call("GET", f"/api/v1/projects/{pid}/pieces/{p0['id']}/pairs")
        edited = next(x for x in again if x["pair_key"] == target["pair_key"])
        check(edited["zh"].startswith("【人工校订】"), str(edited))
        print(f"[9] 复读确认改句生效：{edited['zh'][:30]}…")

        # LWW：更旧的 client_ts 必须被判 conflict，且不能覆盖新值
        stale = {
            "ops": [
                dict(
                    ops["ops"][0],
                    op_id="stale-1",
                    client_ts=(
                        datetime.now(timezone.utc) - timedelta(hours=1)
                    ).isoformat(),
                    patch={"zh": "不该生效的改写", "manually_edited": True},
                )
            ]
        }
        replay = call("POST", "/api/v1/sync/batch", stale)
        print(
            f"[10] 旧 client_ts 重放：{replay['results'][0]['status']}（期望 conflict）"
        )
        check(replay["results"][0]["status"] == "conflict", str(replay))

        final = call("GET", f"/api/v1/projects/{pid}/pieces/{p0['id']}/pairs")
        kept = next(x for x in final if x["pair_key"] == target["pair_key"])
        check(
            kept["zh"].startswith("【人工校订】"),
            f"LWW 失败，旧写入覆盖了新值：{kept['zh']}",
        )
        print(f"[11] 旧写入未覆盖：{kept['zh'][:30]}…")

        # F18 的 split / merge：两者都会换掉 pair_key，客户端得能按新 key 找到原句
        if len(pairs) >= 2:
            a, b = pairs[0], pairs[1]
            check(
                abs(a["seq"] - b["seq"]) == 1,
                f"[12] 前两对不相邻，无法测合并：{a['seq']},{b['seq']}",
            )

            def cut(s: str) -> tuple[str, str]:
                """从中点切成两半，保证两半都非空（服务端对空半边是 422）。"""
                s = (s or "").strip()
                if len(s) < 2:
                    return s, s
                m = len(s) // 2
                return s[:m], s[m:]

            zh_a, zh_b = cut(a["zh"])
            en_a, en_b = cut(a["en"])
            orig_keys = {x["pair_key"] for x in pairs}
            # 端点返回的是「整篇重编号后的列表」，不是只有拆出来那两句
            split_res = call(
                "POST",
                f"/api/v1/projects/{pid}/pieces/{p0['id']}/pairs/split",
                {
                    "pair_key": a["pair_key"],
                    "zh_a": zh_a,
                    "en_a": en_a,
                    "zh_b": zh_b,
                    "en_b": en_b,
                },
            )
            check(
                len(split_res) == len(pairs) + 1,
                f"拆一句应让整篇 +1 行：{len(pairs)} → {len(split_res)}",
            )
            check(
                [x["seq"] for x in split_res] == list(range(len(split_res))),
                "拆句后 seq 必须稠密唯一（撞 seq 会让背诵舱排序错位）："
                f"{[x['seq'] for x in split_res]}",
            )
            new_rows = [x for x in split_res if x["pair_key"] not in orig_keys]
            check(len(new_rows) == 2, f"应有 2 个新对：{new_rows}")
            check(
                a["pair_key"] not in {x["pair_key"] for x in split_res},
                "原 key 还在（会重影）",
            )
            check(
                all(x["manually_edited"] for x in new_rows),
                "拆出来的新对没标人工编辑 → 覆盖重跑会被冲掉",
            )
            check(
                all(x["loc_page"] == a["loc_page"] for x in new_rows),
                f"拆分丢了 loc_page（出处回看会瞎）：{new_rows}",
            )
            check(
                {x["zh"] for x in new_rows} == {zh_a, zh_b},
                f"拆出来的两半内容不对：{new_rows}",
            )
            new_keys = [x["pair_key"] for x in new_rows]
            print(
                "[12] F18 split：整篇 +1 行，seq 稠密唯一，新对 key 全换且继承 loc_page"
            )

            after_split = call("GET", f"/api/v1/projects/{pid}/pieces/{p0['id']}/pairs")
            check(
                len(after_split) == len(pairs) + 1,
                f"拆一句应 +1 行：{len(pairs)} → {len(after_split)}",
            )
            seqs = [x["seq"] for x in after_split]
            check(
                seqs == list(range(len(after_split))),
                f"拆句后段序号不连续（应 0..n-1）：{seqs}",
            )
            print(
                f"[13] 拆后 {len(after_split)} 行，seq 0..{len(after_split) - 1} 连续"
            )

            merged = call(
                "POST",
                f"/api/v1/projects/{pid}/pieces/{p0['id']}/pairs/merge",
                {"pair_key": new_keys[0], "with_key": new_keys[1]},
            )
            check(
                len(merged) == len(pairs),
                f"拆(+1)再合(-1) 应回到原行数 {len(pairs)}，实得 {len(merged)}",
            )
            check(
                [x["seq"] for x in merged] == list(range(len(merged))),
                f"合并后 seq 必须稠密唯一：{[x['seq'] for x in merged]}",
            )
            new_merged = next(x for x in merged if x["pair_key"] not in orig_keys)
            check(
                new_merged["zh"] == (zh_a + zh_b).strip()
                and new_merged["en"] == (en_a + " " + en_b).strip(),
                f"合并后内容不是两段拼接：{new_merged}",
            )
            check(
                new_merged["manually_edited"],
                "合并出来的新句没标人工编辑 → 覆盖重跑会被冲掉",
            )
            print(
                "[14] F18 merge：整篇 -1 行，seq 仍稠密唯一，中文首尾相接、英文空格相连"
            )

            final_pairs = call("GET", f"/api/v1/projects/{pid}/pieces/{p0['id']}/pairs")
            check(
                len(final_pairs) == len(pairs),
                f"拆→合应回到原行数：{len(final_pairs)} vs {len(pairs)}",
            )
            seqs2 = [x["seq"] for x in final_pairs]
            check(
                seqs2 == list(range(len(final_pairs))),
                f"合并后 seq 不连续：{seqs2}",
            )
            check(
                new_merged["loc_page"] == a["loc_page"],
                "合并丢了 loc_page（出处回看会瞎）",
            )
            print(
                f"[15] 拆→合回到 {len(final_pairs)} 行，seq 0..{len(final_pairs) - 1} 连续，loc_page 保住"
            )

            # 不相邻的两个不许合并，得挡掉而不是静默改数据
            near = final_pairs[0]
            far_pair = next(
                (x for x in final_pairs if abs(x["seq"] - near["seq"]) > 1), None
            )
            if far_pair:
                rejected = False
                try:
                    call(
                        "POST",
                        f"/api/v1/projects/{pid}/pieces/{p0['id']}/pairs/merge",
                        {
                            "pair_key": near["pair_key"],
                            "with_key": far_pair["pair_key"],
                        },
                    )
                except SystemExit:
                    rejected = True
                check(rejected, "合并不相邻的两句居然没报错")
                print("[16] 合并不相邻两句被挡（4xx）")
        else:
            print("[12~16] 对句不足 2 组，跳过 F18 split/merge 用例")

        if args.graph:
            run_graph_cases(call, check)
    finally:
        if args.keep:
            print(f"\n--keep：项目 {pid} 保留，可去网页端查看")
        else:
            call("DELETE", f"/api/v1/projects/{pid}")
            print(f"\n[12] 清理临时项目 {pid[:8]}…（软删，在回收站）")

    print("E2E 全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
