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
from app.core.deps import get_db
from app.core.errors import ErrorCode, bad_request, conflict, not_found
from app.core.security import decrypt_secret, encrypt_secret, mask_secret
from app.db.models import ApiKey, ModelProfile, Setting, new_id, utcnow
from app.llm.registry import list_providers as list_providers_
from app.llm.registry import test_connection

router = APIRouter(prefix="/settings", tags=["设置"])

STORAGE_DISCLOSURE = "API Key 将加密存储在你自己的服务器上，Strayt 不上传到任何第三方。"

Provider = Literal["openai", "deepseek", "moonshot", "qwen", "doubao", "glm", "openai_compatible"]


# ==========================================================================
# API Key
# ==========================================================================
class ApiKeyIn(BaseModel):
    provider: Provider
    secret: str = Field(min_length=8, max_length=512)
    label: str = Field(default="", max_length=64)


class ApiKeyOut(ORMModel):
    id: str
    provider: str
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
    dup = db.scalar(
        select(ApiKey).where(
            ApiKey.provider == body.provider, ApiKey.secret_mask == mask_secret(secret)
        )
    )
    if dup is not None:
        raise conflict(ErrorCode.CONFLICT, "同一服务商已存在相同 Key")

    k = ApiKey(
        id=new_id(),
        provider=body.provider,
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

    # 探针本身也读 DB 外的配置，所以这之前先把行锁住语义简化：测试期间不写 enabled
    result = await test_connection(k.provider, decrypt_secret(k.secret_enc))
    k.last_test_json = {**result, "at": utcnow().isoformat()}
    db.commit()
    db.refresh(k)
    return k


class ApiKeyProbeIn(BaseModel):
    """不落库的试连：用户刚粘贴 Key 时先试，避免存一堆废 Key。"""

    provider: Provider
    secret: str = Field(min_length=8, max_length=512)
    model: str | None = Field(default=None, max_length=64)


@router.post("/api-keys/probe", summary="试连（不落库，≤10s）")
async def probe_key(body: ApiKeyProbeIn) -> dict[str, Any]:
    return await test_connection(body.provider, body.secret.strip(), body.model)


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
    # 访问令牌哈希是安全凭据，不允许经通用 KV 接口改写
    if key == "access_token_hash":
        raise bad_request(
            ErrorCode.VALIDATION, "访问令牌只能用 `uv run python -m app.scripts.gen_token` 生成"
        )
    row = db.get(Setting, key)
    if row is None:
        row = Setting(k=key, v=body.value)
        db.add(row)
    else:
        row.v = body.value
    db.commit()
    return {"key": key, "value": row.v, "updated_at": row.updated_at}
