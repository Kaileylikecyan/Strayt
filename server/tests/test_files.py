"""上传：直传、SHA-256 去重、分块续传、损坏检测（文档 F9）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "daoyouci.pdf"


def _proj(client, auth) -> str:
    return client.post(
        "/api/v1/projects", headers=auth, json={"name": "上传测试", "type": "recite"}
    ).json()["id"]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_direct_upload_real_pdf(client, auth) -> None:
    p = _proj(client, auth)
    data = FIXTURE.read_bytes()
    r = client.post(
        f"/api/v1/files?project_id={p}",
        headers=auth,
        files={"file": ("daoyouci.pdf", data, "application/pdf")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sha256"] == _sha(data)
    assert body["size"] == len(data)


def test_sha_dedup_reuses_row(client, auth) -> None:
    """同内容重复上传不产生第二行、不占第二份磁盘。"""
    p = _proj(client, auth)
    data = b"%PDF-1.4\nfake but valid bytes\n%%EOF\n"
    a = client.post(
        f"/api/v1/files?project_id={p}",
        headers=auth,
        files={"file": ("a.pdf", data, "application/pdf")},
    ).json()

    b = client.post(
        f"/api/v1/files?project_id={p}",
        headers=auth,
        files={"file": ("b.pdf", data, "application/pdf")},
    ).json()
    assert a["id"] == b["id"], "SHA-256 相同应复用同一 File 行"

    listed = client.get("/api/v1/files", headers=auth, params={"project_id": p}).json()
    assert len(listed) == 1


def test_sha_dedup_is_per_project(client, auth) -> None:
    """同一份资料要能挂到多个项目：磁盘实体复用，但各项目各有一行。

    早期版本 ``files.sha256`` 是全局唯一，第二个项目再传同一份资料会拿到
    **别的项目**的行，随后建任务就 409「文件不属于该项目」。
    """
    data = FIXTURE.read_bytes()
    p1 = _proj(client, auth)
    p2 = _proj(client, auth)

    a = client.post(
        f"/api/v1/files?project_id={p1}",
        headers=auth,
        files={"file": ("daoyouci.pdf", data, "application/pdf")},
    ).json()
    b = client.post(
        f"/api/v1/files?project_id={p2}",
        headers=auth,
        files={"file": ("daoyouci.pdf", data, "application/pdf")},
    ).json()

    assert b["project_id"] == p2
    assert a["id"] != b["id"], "不同项目应是不同的 File 行"
    assert a["sha256"] == b["sha256"], "同一份内容一个 sha256（磁盘实体复用）"

    for pid, fid in ((p1, a["id"]), (p2, b["id"])):
        listed = client.get("/api/v1/files", headers=auth, params={"project_id": pid}).json()
        assert [f["id"] for f in listed] == [fid], f"项目 {pid} 的资料列表不对"

    # 两个项目都能拿它建任务（409 才是回归信号）
    for pid, fid in ((p1, a["id"]), (p2, b["id"])):
        r = client.post(
            "/api/v1/jobs",
            headers=auth,
            json={"project_id": pid, "file_id": fid, "type": "recite_align"},
        )
        assert r.status_code == 201, r.text


def test_reject_unsupported_extension(client, auth) -> None:
    p = _proj(client, auth)
    r = client.post(
        f"/api/v1/files?project_id={p}",
        headers=auth,
        files={"file": ("x.exe", b"MZ", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation"


def test_chunked_upload_resume_and_verify(client, auth) -> None:
    """分块 + 断点续传 + 最终 SHA 校验。"""
    p = _proj(client, auth)
    # 每单元 24 字节；300000 个 ≈ 7.2MB，配 5MB chunk 切成 2 块
    blob = b"".join(b"STRAYT-CHUNK-UNIT-%06d\n" % i for i in range(300_000))
    sha = _sha(blob)

    s = client.post(
        "/api/v1/upload-sessions",
        headers=auth,
        params={"project_id": p},
        json={"orig_name": "chunky.pdf", "size": len(blob), "sha256": sha},
    )
    assert s.status_code == 201, s.text
    sess = s.json()
    cs, n = sess["chunk_size"], sess["total_chunks"]
    assert n == 2, "7.2MB 配 5MB chunk 应有 2 块"

    # 只传前 n-1 块就中断
    for i in range(n - 1):
        r = client.put(
            f"/api/v1/upload-sessions/{sess['id']}/chunks/{i}",
            headers=auth,
            content=blob[i * cs : (i + 1) * cs],
        )
        assert r.status_code == 204

    # 续传：GET 会话拿已收块
    again = client.get(f"/api/v1/upload-sessions/{sess['id']}", headers=auth).json()
    assert set(again["received_json"]) == {str(i) for i in range(n - 1)}

    # 未传完就 complete → 明确报错并列出缺块
    bad = client.post(f"/api/v1/upload-sessions/{sess['id']}/complete", headers=auth)
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "upload_chunk_out_of_order"
    assert str(n - 1) in str(bad.json()["error"]["detail"]["missing"])

    # 补最后一块 → 合并成功
    r = client.put(
        f"/api/v1/upload-sessions/{sess['id']}/chunks/{n - 1}",
        headers=auth,
        content=blob[(n - 1) * cs :],
    )
    assert r.status_code == 204
    done = client.post(f"/api/v1/upload-sessions/{sess['id']}/complete", headers=auth)
    assert done.status_code == 200, done.text
    assert done.json()["sha256"] == sha
    assert done.json()["size"] == len(blob)


def test_chunk_checksum_mismatch_rejected(client, auth) -> None:
    """声明的 SHA 与实传内容不符 → 拒收，不入库、不留产物。"""
    p = _proj(client, auth)
    blob = b"A" * (6 * 1024 * 1024)  # 超过 5MB chunk → 至少 2 块
    lie = _sha(b"something else entirely")

    s = client.post(
        "/api/v1/upload-sessions",
        headers=auth,
        params={"project_id": p},
        json={"orig_name": "lie.pdf", "size": len(blob), "sha256": lie},
    ).json()

    for i in range(s["total_chunks"]):
        client.put(
            f"/api/v1/upload-sessions/{s['id']}/chunks/{i}",
            headers=auth,
            content=blob[i * s["chunk_size"] : (i + 1) * s["chunk_size"]],
        )

    r = client.post(f"/api/v1/upload-sessions/{s['id']}/complete", headers=auth)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "upload_checksum_mismatch"
    assert client.get("/api/v1/files", headers=auth, params={"project_id": p}).json() == []


def test_chunk_index_out_of_range(client, auth) -> None:
    p = _proj(client, auth)
    s = client.post(
        "/api/v1/upload-sessions",
        headers=auth,
        params={"project_id": p},
        json={"orig_name": "x.pdf", "size": 6 * 1024 * 1024, "sha256": _sha(b"whatever")},
    ).json()
    r = client.put(f"/api/v1/upload-sessions/{s['id']}/chunks/9999", headers=auth, content=b"z")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "upload_chunk_out_of_order"
