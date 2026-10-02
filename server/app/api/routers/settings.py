"""设置中心（文档 F5/F6/F7）。

三块：
- **API Key**：密文入库，只回传掩码。连通性测试限时 10s（文档 F6 硬要求）。
- **模型档案**：文本/视觉两个能力位分别绑服务商+模型。
- **KV 设置**：打卡项定义、默认档案。

Key 存在**你自己的服务器**上，界面必须明示这一点（文档硬要求），
文案常量 ``STORAGE_DISCLOSURE`` 供前端直接引用，避免各端各写一遍。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import ORMModel
from app.core.deps import PASSWORD_HASH_KEY, get_db
from app.core.errors import ErrorCode, bad_request, conflict, not_found
from app.core.security import decrypt_secret, encrypt_secret, mask_secret
from app.db.models import ApiKey, ModelProfile, Setting, new_id, utcnow
from app.llm.registry import PROVIDERS, test_connection
from app.llm.registry import list_providers as list_providers_

router = APIRouter(prefix="/settings", tags=["设置"])

STORAGE_DISCLOSURE = "API Key 将加密存储在你自己的服务器上，Strayt 不上传到任何第三方。"

#: **从 ``registry.PROVIDERS`` 派生，不要手写。**
#: 之前这里手写过一份字面量 ``["openai","deepseek","moonshot",...]``，而注册表是
#: ``{deepseek, qwen, glm, openai, anthropic, gemini}`` —— 两边对不上，导致：
#: 存 anthropic/gemini 的 Key 被 422 挡在门外；反过来 moonshot/doubao 存得进去，
#: 却因为不在注册表里，要等真正调用时 ``get_spec()`` 才抛「未接入的服务商」。
#: 现在唯一真源是注册表，加一家服务商只需改 ``registry.py`` 一处。
Provider = Literal[tuple(PROVIDERS)]  # type: ignore[valid-type]

#: 只有这一家允许用户自带端点。固定端点的厂商填 ``base_url`` 一律 400，
#: 理由见 ``alembic/versions/b91c4e70d5a8_api_keys_base_url.py``。
_CUSTOM_ENDPOINT = "openai_compatible"


def _check_base_url(provider: str, base_url: str | None) -> str | None:
    """校验自定义端点地址。返回规范化后的值（去掉尾部斜杠）。"""
    if not base_url or not base_url.strip():
        return None
    url = base_url.strip().rstrip("/")
    if provider != _CUSTOM_ENDPOINT:
        raise bad_request(
            ErrorCode.VALIDATION,
            f"「{provider}」的接口地址是固定的，不接受自定义 Base URL。"
            f"只有「{_CUSTOM_ENDPOINT}」这一档可以自己填。",
        )
    if not url.startswith(("http://", "https://")):
        raise bad_request(ErrorCode.VALIDATION, f"Base URL 必须以 http:// 或 https:// 开头：{url}")
    if len(url) > 255:
        raise bad_request(ErrorCode.VALIDATION, "Base URL 太长")
    return url


# ==========================================================================
# API Key
# ==========================================================================
class ApiKeyIn(BaseModel):
    provider: Provider
    secret: str = Field(min_length=8, max_length=512)
    label: str = Field(default="", max_length=64)
    base_url: str | None = Field(default=None, max_length=255)


class ApiKeyOut(ORMModel):
    id: str
    provider: str
    base_url: str | None
    label: str
    secret_mask: str
    enabled: bool
    last_test_json: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


@router.get("/api-keys", response_model=list[ApiKeyOut], summary="Key 列表（只含掩码）")
def list_keys(db: Session = Depends(get_db)) -> list[ApiKey]:
    return list(db.scalars(select(ApiKey).order_by(ApiKey.created_at)))


@router.post("/api-keys", response_model=ApiKeyOut, status_code=201, summary="添加 Key")
def create_key(body: ApiKeyIn, db: Session = Depends(get_db)) -> ApiKey:
    secret = body.secret.strip()
    base_url = _check_base_url(body.provider, body.base_url)
    if body.provider == _CUSTOM_ENDPOINT and not base_url:
        raise bad_request(
            ErrorCode.VALIDATION,
            "自定义端点必须填 Base URL（形如 http://127.0.0.1:11434/v1）",
        )
    # 去重要把 base_url 算进身份：同一个 Key 打到两个不同端点是两件事，
    # 只按 (provider, mask) 判重会把第二个端点静默挡掉。
    dup = db.scalar(
        select(ApiKey).where(
            ApiKey.provider == body.provider,
            ApiKey.secret_mask == mask_secret(secret),
            ApiKey.base_url.is_(None) if base_url is None else ApiKey.base_url == base_url,
        )
    )
    if dup is not None:
        raise conflict(ErrorCode.CONFLICT, "同一服务商已存在相同 Key")

    k = ApiKey(
        id=new_id(),
        provider=body.provider,
        base_url=base_url,
        label=body.label.strip(),
        secret_enc=encrypt_secret(secret),
        secret_mask=mask_secret(secret),
    )
    db.add(k)
    db.commit()
    db.refresh(k)
    return k


@router.delete("/api-keys/{key_id}", status_code=204, summary="删除 Key")
def delete_key(key_id: str, db: Session = Depends(get_db)) -> None:
    k = db.get(ApiKey, key_id)
    if k is None:
        raise not_found("Key", key_id)
    db.delete(k)
    db.commit()


@router.get("/providers", summary="已接入的服务商与模型（给设置页下拉框）")
def list_providers() -> list[dict[str, Any]]:
    return list_providers_()


@router.post("/api-keys/{key_id}/test", response_model=ApiKeyOut, summary="连通性测试（≤10s，F6）")
async def test_key(key_id: str, db: Session = Depends(get_db)) -> ApiKey:
    """F6 硬要求：10s 内返回成功/失败 + 原因（Key 错误 / 网络不通 / 额度不足）。"""
    k = db.get(ApiKey, key_id)
    if k is None:
        raise not_found("Key", key_id)

    result = await test_connection(
        k.provider, decrypt_secret(k.secret_enc), None, base_url=k.base_url
    )
    k.last_test_json = {**result, "at": utcnow().isoformat()}
    db.commit()
    db.refresh(k)
    return k


class ApiKeyProbeIn(BaseModel):
    """不落库的试连：用户刚粘贴 Key 时先试，避免存一堆废 Key。"""

    provider: Provider
    secret: str = Field(min_length=8, max_length=512)
    model: str | None = Field(default=None, max_length=64)
    base_url: str | None = Field(default=None, max_length=255)


@router.post("/api-keys/probe", summary="试连（不落库，≤10s）")
async def probe_key(body: ApiKeyProbeIn) -> dict[str, Any]:
    base_url = _check_base_url(body.provider, body.base_url)
    return await test_connection(body.provider, body.secret.strip(), body.model, base_url=base_url)


# ==========================================================================
# 模型档案
# ==========================================================================
class ModelProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    text_key_id: str | None = None
    text_model: str | None = None
    vision_key_id: str | None = None
    vision_model: str | None = None


class ModelProfileOut(ORMModel):
    id: str
    name: str
    text_provider_id: str | None
    text_model: str | None
    vision_provider_id: str | None
    vision_model: str | None
    created_at: datetime
    updated_at: datetime


@router.get("/model-profiles", response_model=list[ModelProfileOut], summary="模型档案列表")
def list_profiles(db: Session = Depends(get_db)) -> list[ModelProfile]:
    return list(db.scalars(select(ModelProfile).order_by(ModelProfile.created_at)))


@router.post("/model-profiles", response_model=ModelProfileOut, status_code=201, summary="新建档案")
def create_profile(body: ModelProfileIn, db: Session = Depends(get_db)) -> ModelProfile:
    # 这条约束本来想写 DB CHECK，但 MySQL 禁止「CHECK 列 + ON DELETE 引用动作」，
    # 只能放这里（见 models.py ModelProfile 注释）
    if not (body.text_key_id or body.vision_key_id):
        raise bad_request(ErrorCode.VALIDATION, "至少绑定一个能力位（文本或视觉）")
    for kid in (body.text_key_id, body.vision_key_id):
        if kid and db.get(ApiKey, kid) is None:
            raise bad_request(ErrorCode.VALIDATION, f"Key 不存在：{kid}")

    p = ModelProfile(
        id=new_id(),
        name=body.name.strip(),
        text_provider_id=body.text_key_id,
        text_model=body.text_model,
        vision_provider_id=body.vision_key_id,
        vision_model=body.vision_model,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.delete("/model-profiles/{profile_id}", status_code=204, summary="删除档案")
def delete_profile(profile_id: str, db: Session = Depends(get_db)) -> None:
    p = db.get(ModelProfile, profile_id)
    if p is None:
        raise not_found("模型档案", profile_id)
    db.delete(p)
    db.commit()


# ==========================================================================
# KV 设置
# ==========================================================================
class KvIn(BaseModel):
    value: str


@router.get("/kv/{key}", summary="读 KV")
def get_kv(key: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.get(Setting, key)
    return {
        "key": key,
        "value": row.v if row else None,
        "updated_at": row.updated_at if row else None,
    }


@router.put("/kv/{key}", summary="写 KV")
def put_kv(key: str, body: KvIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    # 口令哈希是安全凭据，不允许经通用 KV 接口改写。
    # （原来这里挡的是早已废弃的 `access_token_hash`，配套提示还指向被删掉的
    #  `gen_token.py` —— 认证改造后这行等于既挡不住新键、报错信息也全是死链。）
    if key == PASSWORD_HASH_KEY:
        raise bad_request(
            ErrorCode.VALIDATION,
            "访问口令不能从这里改。请走设置页的「修改口令」，"
            "或服务端执行 `uv run python -m app.scripts.set_password --reset`。",
        )
    row = db.get(Setting, key)
    if row is None:
        row = Setting(k=key, v=body.value)
        db.add(row)
    else:
        row.v = body.value
    db.commit()
    return {"key": key, "value": row.v, "updated_at": row.updated_at}
