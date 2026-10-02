"""设置中心 API：API Key 的增删 / 连通性测试 / 自定义端点（`settings.py`）。

覆盖的行为边界：

- ``Provider`` 字面量与 ``PROVIDERS`` 注册表**同源**（漂移过一次，见 ``test_llm.py``）；
- ``base_url`` 只对 ``openai_compatible`` 开放，固定端点的厂商填了要 4xx
  —— 否则 Key 能被指使发往任意主机；
- 同一 Key 打到两个不同端点算两件事，去重不能只看 (provider, mask)；
- KV 通用写接口不得改写口令哈希。

这些都不是「顺手补的边角」：前两条在写这批用例之前是**真的能触发的**。
"""

from __future__ import annotations

import pytest
from app.api.routers.settings import STORAGE_DISCLOSURE  # noqa: F401  契约：文案必须存在
from app.llm.registry import PROVIDERS


def _key(client, auth, **body):
    payload = {"provider": "deepseek", "secret": "sk-test-abcdefgh", **body}
    return client.post("/api/v1/settings/api-keys", headers=auth, json=payload)


class TestProviderLiteral:
    def test_should_expose_every_registry_provider(self, client, auth) -> None:
        """下拉框里必须能选到注册表里的每一家。

        旧版这里读的是 ``provider`` 字段，而 ``/providers`` 返回 ``key``，
        于是下拉框全是 ``undefined`` —— 用户根本没法选厂商。
        """
        r = client.get("/api/v1/settings/providers", headers=auth)
        assert r.status_code == 200
        got = {row["key"] for row in r.json()}
        assert got == set(PROVIDERS)
        for row in r.json():
            assert row["display_name"], f"{row['key']} 缺显示名"
            assert row["region"] in ("国内", "海外")

    def test_should_include_mainland_providers(self, client, auth) -> None:
        got = {row["key"] for row in client.get("/api/v1/settings/providers", headers=auth).json()}
        for k in (
            "deepseek",
            "qwen",
            "glm",
            "moonshot",
            "doubao",
            "siliconflow",
            "minimax",
            "hunyuan",
        ):
            assert k in got, f"国内主流厂商 {k} 没出现在设置页下拉里"

    @pytest.mark.parametrize("provider", ["anthropic", "gemini", "openai"])
    def test_should_accept_key_for_every_registry_provider(self, client, auth, provider) -> None:
        """注册表里有、界面就要能存。反之也要能存但调用时报「未接入」——
        这两个方向历史上各错过一次。"""
        r = _key(client, auth, provider=provider)
        assert r.status_code == 201, f"{provider} 存不进 Key：{r.status_code} {r.text[:200]}"
        assert r.json()["provider"] == provider

    def test_should_reject_provider_outside_registry(self, client, auth) -> None:
        assert _key(client, auth, provider="not-a-vendor").status_code == 422


class TestCustomEndpoint:
    def test_should_accept_base_url_for_custom_endpoint(self, client, auth) -> None:
        r = _key(
            client,
            auth,
            provider="openai_compatible",
            secret="sk-local-abcdefgh",
            base_url="http://127.0.0.1:11434/v1/",
        )
        assert r.status_code == 201
        # 尾部斜杠要规整掉，否则调用时会拼成 /v1//chat/completions
        assert r.json()["base_url"] == "http://127.0.0.1:11434/v1"

    def test_should_reject_base_url_for_fixed_endpoint_provider(self, client, auth) -> None:
        """**安全边界**：固定端点的厂商不能改地址。

        放开的话，用户（或误操作）把 DeepSeek 的 Key 指到别的主机，
        ``Authorization`` 头就会跟着发过去。
        """
        r = _key(client, auth, provider="deepseek", base_url="http://evil.example.com/v1")
        assert r.status_code == 400
        assert "Base URL" in r.text

    def test_should_require_base_url_for_custom_endpoint(self, client, auth) -> None:
        r = _key(client, auth, provider="openai_compatible", secret="sk-local-abcdefgh")
        assert r.status_code == 400
        assert "Base URL" in r.text

    @pytest.mark.parametrize(
        "bad", ["file:///etc/passwd", "127.0.0.1:11434/v1", "ftp://x/v1", "  "]
    )
    def test_should_reject_malformed_base_url(self, client, auth, bad) -> None:
        r = _key(
            client,
            auth,
            provider="openai_compatible",
            secret="sk-local-abcdefgh",
            base_url=bad,
        )
        # 纯空白视作「没填」，也要挡
        assert r.status_code == 400

    def test_same_key_on_two_endpoints_is_not_a_duplicate(self, client, auth) -> None:
        """同一串 Key 打到两个不同端点是两件事。

        只按 (provider, mask) 判重会把第二个端点静默挡掉，用户以为存上了。
        """
        common = {"provider": "openai_compatible", "secret": "sk-same-abcdefgh"}
        a = _key(client, auth, **common, base_url="http://127.0.0.1:11434/v1")
        b = _key(client, auth, **common, base_url="http://127.0.0.1:8000/v1")
        assert a.status_code == 201
        assert b.status_code == 201
        assert a.json()["id"] != b.json()["id"]

    def test_identical_provider_endpoint_key_is_still_deduped(self, client, auth) -> None:
        common = {"provider": "openai_compatible", "secret": "sk-dup-abcdefgh"}
        a = _key(client, auth, **common, base_url="http://127.0.0.1:11434/v1")
        b = _key(client, auth, **common, base_url="http://127.0.0.1:11434/v1")
        assert a.status_code == 201
        assert b.status_code == 409


class TestApiKeyBasics:
    def test_should_never_return_plaintext_secret(self, client, auth) -> None:
        r = _key(client, auth, secret="sk-supersecret-123456")
        assert r.status_code == 201
        assert "sk-supersecret-123456" not in r.text
        assert r.json()["secret_mask"]

    def test_should_404_on_unknown_key_delete(self, client, auth) -> None:
        assert client.delete("/api/v1/settings/api-keys/nope", headers=auth).status_code == 404


class TestKvGuard:
    def test_should_refuse_writing_password_hash(self, client, auth) -> None:
        """通用 KV 写接口不得改写口令哈希。

        旧代码挡的是早已废弃的 ``access_token_hash``，认证改造后这行等于
        形同虚设 —— 真正的口令键反而没挡。
        """
        from app.core.deps import PASSWORD_HASH_KEY

        r = client.put(
            f"/api/v1/settings/kv/{PASSWORD_HASH_KEY}", headers=auth, json={"value": "x"}
        )
        assert r.status_code == 400
        assert "set_password" in r.text

    def test_should_allow_ordinary_kv(self, client, auth) -> None:
        r = client.put("/api/v1/settings/kv/checkin_items", headers=auth, json={"value": "[]"})
        assert r.status_code == 200
        assert r.json()["value"] == "[]"
