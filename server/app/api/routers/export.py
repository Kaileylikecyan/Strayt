"""数据导出（文档 F24）：一键 JSON 快照。

「防锁死」备份：把全部项目数据导成一份自包含 JSON，客户端一键下载保存，
出问题时可人工恢复。覆盖所有业务表（含已软删项目与档案），**不导出**
加工任务（``jobs``）、上传会话（``upload_sessions``）、解析产物
（``file_parses``，可重跑再生）这类瞬时状态；``api_keys`` 存的是
Fernet 密文、``settings`` 存的是令牌哈希，均不泄露明文凭据。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import get_db
from app.db.models import (
    ApiKey,
    CardQuote,
    Category,
    Checkin,
    Edge,
    File,
    Flashcard,
    Goal,
    ModelProfile,
    Node,
    Pair,
    Piece,
    Plan,
    Project,
    Setting,
    utcnow,
)

router = APIRouter(prefix="/export", tags=["数据导出"])


def _proj(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "type": p.type,
        "api_profile_json": p.api_profile_json,
        "deleted_at": p.deleted_at,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _file(f: File) -> dict:
    return {
        "id": f.id,
        "project_id": f.project_id,
        "server_path": f.server_path,
        "orig_name": f.orig_name,
        "sha256": f.sha256,
        "size": f.size,
        "parse_channel": f.parse_channel,
        "page_count": f.page_count,
        "uploaded_at": f.uploaded_at,
        "deleted_at": f.deleted_at,
    }


def _piece(x: Piece) -> dict:
    return {
        "id": x.id,
        "project_id": x.project_id,
        "file_id": x.file_id,
        "title": x.title,
        "align_mode": x.align_mode,
        "align_warnings": x.align_warnings,
        "align_agreement": x.align_agreement,
        "recited": x.recited,
        "last_pos": x.last_pos,
        "sort_order": x.sort_order,
        "created_at": x.created_at,
        "updated_at": x.updated_at,
    }


def _pair(p: Pair) -> dict:
    return {
        "id": p.id,
        "pair_key": p.pair_key,
        "piece_id": p.piece_id,
        "seq": p.seq,
        "zh": p.zh,
        "en": p.en,
        "loc_page": p.loc_page,
        "needs_review": p.needs_review,
        "confidence": p.confidence,
        "how": p.how,
        "manually_edited": p.manually_edited,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _goal(g: Goal) -> dict:
    return {
        "id": g.id,
        "project_id": g.project_id,
        "exam_date": g.exam_date,
        "updated_at": g.updated_at,
    }


def _plan(pl: Plan) -> dict:
    return {
        "id": pl.id,
        "project_id": pl.project_id,
        "title": pl.title,
        "due_date": pl.due_date,
        "status": pl.status,
        "target_type": pl.target_type,
        "target_id": pl.target_id,
        "sort_order": pl.sort_order,
        "created_at": pl.created_at,
        "updated_at": pl.updated_at,
    }


def _checkin(c: Checkin) -> dict:
    return {"date": c.date, "items_json": c.items_json, "updated_at": c.updated_at}


def _node(n: Node) -> dict:
    return {
        "id": n.id,
        "project_id": n.project_id,
        "category_id": n.category_id,
        "name": n.name,
        "weight": n.weight,
        "card_summary": n.card_summary,
        "user_notes": n.user_notes,
        "edited": n.edited,
        "mastery": n.mastery,
        "created_at": n.created_at,
        "updated_at": n.updated_at,
    }


def _category(c: Category) -> dict:
    return {"id": c.id, "project_id": c.project_id, "name": c.name, "sort_order": c.sort_order}


def _edge(e: Edge) -> dict:
    return {
        "id": e.id,
        "project_id": e.project_id,
        "from_node": e.from_node,
        "to_node": e.to_node,
        "reason": e.reason,
    }


def _quote(q: CardQuote) -> dict:
    return {
        "id": q.id,
        "node_id": q.node_id,
        "quote_text": q.quote_text,
        "file_id": q.file_id,
        "page": q.page,
        "para": q.para,
        "sort_order": q.sort_order,
    }


def _flashcard(f: Flashcard) -> dict:
    return {
        "id": f.id,
        "node_id": f.node_id,
        "question": f.question,
        "answer": f.answer,
        "created_at": f.created_at,
        "updated_at": f.updated_at,
    }


@router.get("/snapshot", summary="全部项目数据一键导出 JSON 快照")
def export_snapshot(db: Session = Depends(get_db)) -> dict:
    def _all(model, conv):
        return [conv(x) for x in db.scalars(select(model).order_by(model.id))]

    return {
        "export_version": "1",
        "schema_version": get_settings().snapshot_schema_version,
        "exported_at": utcnow(),
        "projects": _all(Project, _proj),
        "files": _all(File, _file),
        "pieces": _all(Piece, _piece),
        "pairs": _all(Pair, _pair),
        "goals": _all(Goal, _goal),
        "plans": _all(Plan, _plan),
        "checkins": [_checkin(c) for c in db.scalars(select(Checkin).order_by(Checkin.date))],
        "categories": _all(Category, _category),
        "nodes": _all(Node, _node),
        "edges": _all(Edge, _edge),
        "card_quotes": _all(CardQuote, _quote),
        "flashcards": _all(Flashcard, _flashcard),
        "api_keys": [
            {
                "id": k.id,
                "provider": k.provider,
                "label": k.label,
                "secret_enc": k.secret_enc,
                "secret_mask": k.secret_mask,
                "enabled": k.enabled,
                "created_at": k.created_at,
                "updated_at": k.updated_at,
            }
            for k in db.scalars(select(ApiKey).order_by(ApiKey.id))
        ],
        "model_profiles": [
            {
                "id": m.id,
                "name": m.name,
                "text_provider_id": m.text_provider_id,
                "text_model": m.text_model,
                "vision_provider_id": m.vision_provider_id,
                "vision_model": m.vision_model,
                "created_at": m.created_at,
                "updated_at": m.updated_at,
            }
            for m in db.scalars(select(ModelProfile).order_by(ModelProfile.id))
        ],
        "settings": [
            {"k": s.k, "v": s.v, "updated_at": s.updated_at}
            for s in db.scalars(select(Setting).order_by(Setting.k))
        ],
    }
