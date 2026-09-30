"""HTTP 级真实数据 E2E：对着**跑着的服务端**把客户端要走的路走一遍。

和 ``server/var_test/job_e2e.py`` 的区别：那个在进程内直接调引擎，验证加工质量；
这个只发 HTTP，验证**客户端实际会踩的协议面**——鉴权、bootstrap、上传、建任务、
轮询、以及 F18 的弱同步写通道（``pair_edit`` 用 ``pair_key`` 寻址）。

它抓到的都是单测抓不到的 bug：
  * 同步路由是 ``def``（线程池里跑）→ ``asyncio.create_task`` 无运行事件循环 → 500
  * ``files.sha256`` 全局唯一 → 同一份资料传进第二个项目被判「文件不属于该项目」
  * 浏览器 ``toISOString()`` 发带 ``Z`` 的 ``client_ts``，库里是 naive → LWW 比较 TypeError

用法（服务端已在 8000 端口跑着）::

    $env:STRAYT_TOKEN = "<访问令牌>"          # 不要把令牌写进文件/提交
    uv run --project server python scripts\\smoke_http.py
    uv run --project server python scripts\\smoke_http.py --keep   # 保留项目便于人工检查

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PDF = REPO / "server" / "fixtures" / "daoyouci.pdf"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--url", default=os.environ.get("STRAYT_URL", "http://127.0.0.1:8000")
    )
    ap.add_argument("--token", default=os.environ.get("STRAYT_TOKEN", ""))
    ap.add_argument("--keep", action="store_true", help="结束后不删项目（留给人看）")
    args = ap.parse_args()

    if not args.token:
        print("缺访问令牌：设环境变量 STRAYT_TOKEN 或传 --token", file=sys.stderr)
        return 2
    if not PDF.exists():
        print(f"样板 PDF 不在：{PDF}（被 .gitignore 排除，需自备）", file=sys.stderr)
        return 2

    base = args.url.rstrip("/")
    auth = {"Authorization": f"Bearer {args.token}"}

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
