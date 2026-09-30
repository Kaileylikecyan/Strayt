"""加工任务引擎：⑥⑦⑧ 放服务端后台跑，支持断点续跑。

文档 §6 加工管线不变量第 7 条：加工任务在服务端后台跑，客户端可关窗；状态落
``jobs`` 表，支持断点续跑。一期只做 ``recite_align``（背诵项目的中英对齐入库）。

设计要点：

- **一篇 = 一个单元。** 单元粒度选篇目而不是整份文件：篇目之间没有依赖，一篇
  失败不该拖垮全篇；而对齐一次要花模型调用，按句攒检查点没意义。
- **每篇提交一次。** checkpoint 与该篇的入库数据在同一个事务里落地，崩了不会
  出现「checkpoint 说做完了但库里没有」的空洞。
- **心跳独立于单元。** 一篇的对齐可能要几十秒（多次模型调用），期间没有任何
  单元边界可写心跳，所以心跳跑在独立协程里，按固定间隔刷新并轮询取消请求。
- **取消只在单元边界生效。** 正在跑的对齐不会被硬打断 —— 强杀协程会留下半个
  事务在数据库里，代价远大于多等几十秒。这是有意的取舍，不是遗漏。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.align.base import Aligner, AlignError, AlignResult
from app.align.llm import estimate_alignment_tokens
from app.align.quality import check
from app.db.models import ApiKey, File, FileParse, Job, ModelProfile, Project, utcnow
from app.db.session import SessionLocal
from app.llm.base import Usage
from app.llm.pricing import estimate_cost
from app.parse import blocks as B
from app.parse.base import ParseError
from app.parse.pdf import PdfParser
from app.persist import SaveOutcome, SaveResult, save_piece
from app.storage.local import get_storage

log = logging.getLogger(__name__)

#: 心跳刷新间隔（秒）。太短白写库，太长客户端会以为任务死了。
HEARTBEAT_INTERVAL = 15.0
#: 单篇对齐的超时。卡死的模型调用不能无限挂着。
UNIT_TIMEOUT = 600.0
#: F8：实际花费超过预估的多少倍就中断任务（等用户确认后才继续）。
#: 预估按牌价 ±30% 误差设计，超出 50% 说明真金白银和预期对不上，该让用户拍板。
BUDGET_OVERAGE_RATIO = 1.5


@dataclass
class Unit:
    """一个加工单元 = 一篇范文。"""

    index: int
    title: str
    zh: str
    en: str
    page: int
    zh_page: int = 0
    en_page: int = 0

    @property
    def key(self) -> str:
        return _unit_key(self)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "index": self.index,
            "title": self.title,
            "page": self.page,
            "status": "pending",
        }


@dataclass
class UnitOutcome:
    outcome: str
    reason: str | None = None
    review: int = 0
    usage: Usage | None = None
    failure: dict | None = None


@dataclass
class Plan:
    """一份任务的加工计划。``units_json`` 直接给客户端显示用。"""

    file_id: str
    units: list[Unit] = field(default_factory=list)
    parser_version: str = "1"

    def to_json(self) -> list[dict[str, Any]]:
        return [u.to_json() for u in self.units]


# ==========================================================================
# 解析 → 切篇
# ==========================================================================
def _key_of_title(units: list[Unit], title: str) -> str | None:
    """按标题找回单元 key。

    旧 checkpoint 里只存了标题（``"unit": "上海概况·范文（一）"``），新格式存
    ``"0003:上海概况·范文（一）"``。同名的两篇（材料里很常见）会撞车，所以只认
    **唯一**匹配：认不出来宁可留旧格式，也不能把失败记到别的篇目头上。
    """
    hits = [u.key for u in units if u.title == title]
    return hits[0] if len(hits) == 1 else None


def _migrate_done(units: list[Unit], done: set[str]) -> set[str]:
    """把只存了标题的旧 ``done`` 补成稳定 key。"""
    out = set(done)
    for key in done:
        if ":" in key or _key_of_title(units, key) is None:
            continue  # 已是新格式，或标题对不上：原样留着，别猜
        out.discard(key)
        out.add(_key_of_title(units, key))  # type: ignore[arg-type]
    return out


def _backfill_failed_keys(failed: list[dict], done: set[str], units: list[Unit]) -> list[dict]:
    """给旧格式的失败明细补上 ``key``，否则同一次失败会被反复追加。

    ``_absorb`` 靠 key 覆盖去重。旧明细只有 ``unit``（标题）时，那个比较永远
    对不上 ``"0003:标题"``，于是**每续跑一次就多一条一模一样的红字** ——
    界面上同一个篇目挂五条重复失败。

    已经做完的（key 在 ``done`` 里）直接剔除：那一篇后来成功了，失败记录留着
    只会让人以为它还没过。
    """
    out: list[dict] = []
    for entry in failed:
        if entry.get("key"):
            if entry["key"] not in done:
                out.append(entry)
            continue
        key = _key_of_title(units, str(entry.get("unit", "")))
        if key is None or key in done:
            continue
        out.append({**entry, "key": key})
    return out


def build_plan(db: Session, file_row: File) -> Plan:
    """解析文件并切成篇目清单。

    顺带写一条 ``file_parses``：文档要求原文抽取结果可复查（② 的清洗规则以后
    会改，得能重跑对比）。``tokens_used`` 留 0 —— 解析本身不调模型。

    ``parse_channel`` 只有 ``text`` / ``vision`` 两种：``text`` 表示 PDF 有文本层，
    直接抽取；``vision`` 表示扫描件，必须走视觉 OCR，一期未实现，明确报错而不是
    静默产出 0 篇。
    """
    # 先看通道再看磁盘：扫描件没配 OCR 是"设置错了"，报这个比报"文件不在磁盘"
    # 更有指向性，用户一看就知道该去改设置而不是重传文件。
    if file_row.parse_channel != "text":
        raise NotImplementedError(
            "扫描件需要走视觉 OCR，一期未实现。请改走「文本层 PDF」并重新上传。"
        )

    path = get_storage().abs_path(file_row.server_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不在磁盘上：{file_row.server_path}")

    parser = PdfParser()
    doc = parser.parse(path)
    db.add(
        FileParse(
            file_id=file_row.id,
            channel=file_row.parse_channel,
            status="success",
            raw_text=doc.raw_text(),
            pages_json=doc.to_pages_json(),
            # 每次运行都留一行，便于规则改版后重跑对比（②的清洗规则会改）。
            # 靠 parser_version 区分是哪一版规则产出的，否则这些行无法分辨。
            parser_version=parser.version,
        )
    )
    db.flush()

    pieces = B.to_pieces(B.scan(doc.blocks))
    units = [
        Unit(
            index=i,
            title=p.title,
            zh=p.zh,
            en=p.en,
            page=p.page,
            zh_page=p.zh_page,
            en_page=p.en_page,
        )
        for i, p in enumerate(pieces)
    ]
    if not units:
        raise ParseError("no_pieces", "结构识别没有切出任何篇目，检查版面规则是否匹配该资料")
    return Plan(file_id=file_row.id, units=units)


# ==========================================================================
# 项目 → 对齐器
# ==========================================================================
@dataclass
class AlignerBundle:
    """对齐器 + 计价所需的 provider/model。

    ``provider`` 为 ``None`` 表示走了降级（纯长度直配），不花钱，计价字段留空。
    """

    aligner: Aligner
    mode: str
    provider: str | None = None
    model: str | None = None


def resolve_aligner(db: Session, project_id: str) -> AlignerBundle:
    """按项目绑定的模型档案构造对齐器。

    ``projects.api_profile_json = {"model_profile_id": ...}`` → ``model_profiles``
    → ``api_keys`` → 构造 provider。没绑档案、没绑 provider、或没有可用密钥时，
    **不报错**，退回纯长度直配（通道 B）：加工任务不能因为没配 API Key 就整体失败，
    这是设计上允许的降级（对齐质量会打折，但客户端仍能拿到可背诵的数据）。
    """
    from app.align.llm import LlmAligner, LlmAlignerConfig
    from app.align.regular import RegularAligner
    from app.core.security import decrypt_secret
    from app.llm.registry import build_provider

    fallback = AlignerBundle(RegularAligner(), "regular")
    project = db.get(Project, project_id)
    if project is None:
        return fallback
    profile_id = (project.api_profile_json or {}).get("model_profile_id")
    if not profile_id:
        return fallback
    profile = db.get(ModelProfile, profile_id)
    if profile is None or not profile.text_provider_id:
        return fallback
    key_row = db.get(ApiKey, profile.text_provider_id)
    if key_row is None or not key_row.enabled:
        return fallback
    try:
        provider = build_provider(
            key_row.provider, decrypt_secret(key_row.secret_enc), profile.text_model
        )
    except Exception as exc:  # 档案配错不该让整份资料加工失败
        log.warning("构造 provider 失败，降级为长度直配：%s", exc)
        return fallback
    return AlignerBundle(
        LlmAligner(provider, LlmAlignerConfig()), "llm", key_row.provider, provider.model
    )


# ==========================================================================
# 引擎
# ==========================================================================
class JobEngine:
    """跑一个 ``recite_align`` 任务。

    ``on_unit`` 是给测试用的钩子，生产路径不设。
    """

    def __init__(
        self,
        job_id: str,
        *,
        overwrite: bool = False,
        on_unit: Callable[[Unit, UnitOutcome], None] | None = None,
    ) -> None:
        self.job_id = job_id
        self.overwrite = overwrite
        self.on_unit = on_unit
        self._cancel = asyncio.Event()
        self._last: UnitOutcome | None = None
        # F8：本轮是否检查「实际 > 预估 50%」熔断。任务被熔断过一次后，用户点续跑
        # 就是拍板继续，不该再把同一个坑拦住不让跑完（见 run() 里的 budget_stop）。
        self._budget_guard = True

    def _absorb(self, ledger: _Ledger, unit: Unit, out: UnitOutcome, bundle: AlignerBundle) -> None:
        """把一个单元的结论并进账本。

        ``skipped``（篇目里有人工微调）算**已处理**：数据在库里、用户的选择被保留，
        不该拖住整个任务不放。但它要留在 ``units_json`` 里可查，不能混进 success。

        失败明细按单元 key **覆盖**而不是追加：续跑时账本是从 checkpoint 恢复的，
        同一篇重试再失败如果直接 append，每续跑一次就多一条重复记录，
        界面上会看到同一个篇目挂着好几条一模一样的红字。
        """
        if out.usage is not None:
            ledger.usage = _add_usage(ledger.usage, out.usage)
            # 钱要算：即使结果被跳过，模型照样跑过、照样花了
            ledger.cost.add(bundle, out.usage)
        if out.outcome in (
            str(SaveOutcome.CREATED),
            str(SaveOutcome.REPLACED),
        ) or out.outcome == str(SaveOutcome.SKIPPED):
            ledger.done.add(unit.key)
        elif out.failure is not None:
            entry = {**out.failure, "key": unit.key}
            for i, old in enumerate(ledger.failed):
                if old.get("key", old.get("unit")) == unit.key:
                    ledger.failed[i] = entry
                    break
            else:
                ledger.failed.append(entry)

    # -- 主流程 ---------------------------------------------------------
    async def run(self) -> str:
        """跑完返回终态 ``success`` / ``failed`` / ``cancelled`` / ``interrupted``。"""
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                raise KeyError(f"任务不存在：{self.job_id}")
            if job.status == "cancelled":
                return "cancelled"
            job.status = "running"
            job.error = None
            job.heartbeat_at = utcnow()
            project_id, file_id = job.project_id, (job.checkpoint_json or {}).get("file_id")
            if not file_id:
                job.status = "failed"
                job.error = "任务缺少 file_id"
                db.commit()
                return "failed"
            file_row = db.get(File, file_id)
            if file_row is None:
                job.status = "failed"
                job.error = f"文件不存在：{file_id}"
                db.commit()
                return "failed"
            bundle = resolve_aligner(db, project_id)
            try:
                plan = build_plan(db, file_row)
            except Exception as exc:
                job.status = "failed"
                job.error = f"解析失败：{exc}"
                db.commit()
                return "failed"

            # 续跑要拿回上一轮的账：已完成的篇目、失败明细、已花的 token 与钱。
            # 少了这一步，一次断点续跑就会把费用报成 0，比不续跑还误导。
            ckpt = dict(job.checkpoint_json or {})
            done = _migrate_done(plan.units, set(ckpt.get("done", [])))
            failed = list(ckpt.get("failed", []))
            usage = _split_tokens(job.tokens_used or 0)
            cost = _Cost.from_json(job.cost_estimate_json)
            failed = _backfill_failed_keys(failed, done, plan.units)
            # 熔断过一次后，用户点续跑 = 已确认继续，本轮不再拦截同一个坑
            self._budget_guard = not bool(ckpt.get("budget_stop"))

            job.total_units = len(plan.units)
            # 续跑时**不能**直接 ``job.units_json = plan.to_json()``：那会把上一轮
            # 已完成篇目的状态抹回 pending，而本轮不会再加工它们，于是永远停在
            # pending（客户端看到"这一篇还没做"，实际库里早有了）。
            # 所以只给没结论的单元填 pending，done / failed 里的保留原状态。
            prior = {r.get("key"): r for r in (job.units_json or [])}
            settled = done | {f.get("key") for f in failed if f.get("key")}
            job.units_json = [
                {
                    **row,
                    "status": prior[row["key"]]["status"]
                    if row.get("key") in settled and row.get("key") in prior
                    else "pending",
                }
                for row in plan.to_json()
            ]
            # 断点续跑的关键：**保留**已完成的 done / failed，只刷新本轮的路由信息。
            # 之前这里是重置成空 list，等于每次续跑都从第一篇重来。
            job.checkpoint_json = {
                **(job.checkpoint_json or {}),
                "file_id": file_id,
                "align_mode": bundle.mode,
                "done": sorted(done),
                "failed": failed,
            }
            job.done_units = len(done)
            job.tokens_used = _total_tokens(usage)
            if cost.estimated_cny <= 0:
                # F8：首次跑按整篇算一次性预估，之后每篇结算时拿实际花费比对
                cost.estimated_cny = _estimate_total_cost(bundle, plan.units)
            job.cost_estimate_json = cost.to_json()
            db.commit()
            units = plan.units
            # 旧 checkpoint 可能是按标题记的（早期版本），迁移到稳定 key
            done = {_unit_key(u) for u in units if u.title in done or _unit_key(u) in done}

        ledger = _Ledger(done=done, failed=failed, usage=usage, cost=cost)
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            for unit in units:
                if unit.key in ledger.done:
                    continue
                if self._cancel.is_set():
                    return self._finish_cancelled(ledger)
                await self._run_unit(unit, bundle, project_id, file_id, ledger)
                if self.on_unit is not None:
                    self.on_unit(unit, self._last)
                if self._budget_guard:
                    # F8：单元边界比对一次。只在这一篇**落库之后**才拦 ——
                    # 拆掉半个事务比多花几个 token 贵得多。
                    overage = self._budget_reason(ledger)
                    if overage is not None:
                        return self._finish_interrupted(ledger, overage)
            return self._finish_success(ledger)
        except Exception as exc:
            log.exception("任务 %s 失败", self.job_id)
            self._mark_failed(str(exc), ledger)
            return "failed"
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    # -- 单个单元 -------------------------------------------------------
    async def _run_unit(
        self, unit: Unit, bundle: AlignerBundle, project_id: str, file_id: str, ledger: _Ledger
    ) -> None:
        """跑一篇范文并落库。

        篇目数据与 checkpoint **在同一个事务里提交** —— 崩在中间也不会出现
        "篇目进了但 checkpoint 说没进"导致续跑重复加工的情况。
        """
        zh = B.split_segments(unit.zh)
        en = B.split_segments(unit.en)
        if not zh or not en:
            self._settle(
                unit,
                bundle,
                ledger,
                UnitOutcome(
                    outcome=SaveOutcome.REJECTED,
                    reason="单侧为空",
                    failure={
                        "unit": unit.title,
                        "reason": "align_empty",
                        "retryable": False,
                        "message": f"中文 {len(zh)} 段 / 英文 {len(en)} 段，缺一侧",
                    },
                ),
            )
            return
        try:
            res: AlignResult = await asyncio.wait_for(
                bundle.aligner.align_async(zh, en, loc_page=unit.zh_page), timeout=UNIT_TIMEOUT
            )
        except TimeoutError:
            self._settle(
                unit,
                bundle,
                ledger,
                UnitOutcome(
                    outcome=SaveOutcome.REJECTED,
                    reason="对齐超时",
                    failure={
                        "unit": unit.title,
                        "reason": "align_timeout",
                        "retryable": True,
                        "message": f"超过 {UNIT_TIMEOUT:.0f}s 未完成",
                    },
                ),
            )
            return
        except AlignError as exc:
            self._settle(
                unit,
                bundle,
                ledger,
                UnitOutcome(
                    outcome=SaveOutcome.REJECTED,
                    reason=str(exc),
                    failure={
                        "unit": unit.title,
                        "reason": exc.kind,
                        "retryable": True,
                        "message": exc.message,
                    },
                ),
            )
            return

        report = check(res)
        with SessionLocal() as db:
            saved = save_piece(
                db,
                project_id=project_id,
                file_id=file_id,
                title=unit.title,
                res=res,
                report=report,
                align_mode=bundle.mode,
                overwrite=self.overwrite,
                sort_order=unit.index,
            )
            self._settle(unit, bundle, ledger, _outcome_of(unit, saved, res.usage), db=db)

    def _settle(
        self,
        unit: Unit,
        bundle: AlignerBundle,
        ledger: _Ledger,
        out: UnitOutcome,
        *,
        db: Session | None = None,
    ) -> None:
        """记下一个单元的结论并落库。

        ``db`` 非空表示调用方已经开着事务（篇目行待提交），此时**借用**它一起提交，
        保证"篇目 + checkpoint"原子。``db`` 为空表示这一篇根本没写任何篇目行
        （对齐前就拒了），自己开一个事务记进度即可。

        统一走这里是有必要的：早期版本让失败路径直接 ``return``，结果
        ``failed_units_json`` 永远是空的 —— 用户看到"任务成功"，却不知道自己
        哪几篇根本没进去。
        """
        self._last = out
        self._absorb(ledger, unit, out, bundle)
        if db is not None:
            self._write_progress(db, unit, out, ledger)
            db.commit()
            return
        with SessionLocal() as own:
            self._write_progress(own, unit, out, ledger)
            own.commit()

    # -- 心跳 / 取消 ----------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        """定时刷心跳并轮询取消请求。

        取消是靠轮询实现的：HTTP 那边只把 ``jobs.status`` 改成 ``cancelled``，
        引擎没有别的方式收到通知（单机单进程，不值得为它引消息队列）。
        """
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            with SessionLocal() as db:
                job = db.get(Job, self.job_id)
                if job is None:
                    return
                if job.status == "cancelled":
                    self._cancel.set()
                    return
                job.heartbeat_at = utcnow()
                db.commit()

    def request_cancel(self) -> None:
        """外部（如测试）直接请求取消。"""
        self._cancel.set()

    # -- 收尾 -----------------------------------------------------------
    def _write_progress(self, db: Session, unit: Unit, out: UnitOutcome, ledger: _Ledger) -> None:
        """把进度/checkpoint 写进**调用方已经开着的事务**。

        故意不开自己的 session：篇目行和 checkpoint 必须是同一次提交，
        否则崩在中间会出现"篇目已入库、checkpoint 说没入"，
        续跑就会把这一篇重新加工一遍（多花钱且可能覆盖用户的编辑）。
        """
        job = db.get(Job, self.job_id)
        if job is None:
            return
        job.checkpoint_json = {
            **(job.checkpoint_json or {}),
            "done": sorted(ledger.done),
            "failed": ledger.failed,
        }
        job.failed_units_json = list(ledger.failed)
        job.tokens_used = _total_tokens(ledger.usage)
        job.cost_estimate_json = ledger.cost.to_json()
        job.done_units = len(ledger.done)
        total = job.total_units or (len(ledger.done) + len(ledger.failed))
        job.progress = int((len(ledger.done) + len(ledger.failed)) * 100 / total) if total else 100
        job.heartbeat_at = utcnow()
        # 必须**重建**列表而不是原地改 row：JSON 列（哪怕包了 MutableList）只在外层
        # list 被替换时标脏，改嵌套 dict 会被静默丢掉 —— 症状是任务跑完了
        # units_json 还全是 pending，客户端进度条永远不动。
        job.units_json = [
            (
                {
                    **row,
                    "status": (
                        "failed" if out.outcome == str(SaveOutcome.REJECTED) else str(out.outcome)
                    ),
                    "reason": out.reason,
                }
                if row.get("key") == unit.key
                else row
            )
            for row in (job.units_json or [])
        ]
        # 不 commit —— 留给调用方

    def _write_terminal(
        self,
        status: str,
        ledger: _Ledger,
        *,
        error: str | None = None,
        checkpoint_extra: dict | None = None,
    ) -> str:
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return "failed"
            job.status = status
            job.error = error
            job.done_units = len(ledger.done)
            job.failed_units_json = list(ledger.failed)
            job.tokens_used = _total_tokens(ledger.usage)
            job.cost_estimate_json = ledger.cost.to_json()
            job.checkpoint_json = {
                **(job.checkpoint_json or {}),
                **(checkpoint_extra or {}),
                "done": sorted(ledger.done),
                "failed": ledger.failed,
            }
            job.heartbeat_at = utcnow()
            if status == "success":
                job.progress = 100
            db.commit()
        return status

    def _finish_success(self, ledger: _Ledger) -> str:
        return self._write_terminal("success", ledger)

    def _finish_cancelled(self, ledger: _Ledger) -> str:
        return self._write_terminal("cancelled", ledger)

    def _budget_reason(self, ledger: _Ledger) -> str | None:
        """F8：实际花费超过预估 50% 时返回中断理由，否则 ``None``。"""
        est = ledger.cost.estimated_cny
        if est <= 0:
            return None  # 降级通道不花钱，无从超支
        actual = ledger.cost.total_cny()
        if actual > est * BUDGET_OVERAGE_RATIO:
            return (
                f"实际花费 {actual:.4f} 元（预估 {est:.4f} 元）已超过预估的 "
                f"{BUDGET_OVERAGE_RATIO:.0%}，加工任务已中断。确认费用没问题后"
                f"点「继续」可续跑（续跑不再触发本熔断）。"
            )
        return None

    def _finish_interrupted(self, ledger: _Ledger, message: str) -> str:
        """F8：把任务停在一个明确的 ``interrupted`` 终态，等用户拍板。

        ``budget_stop`` 写进 checkpoint：用户下一次点续跑就是「确认继续」，
        引擎据此关掉本轮熔断（见 run()），不会把同一个坑拦着不让跑完。
        """
        return self._write_terminal(
            "interrupted",
            ledger,
            error=message,
            checkpoint_extra={"budget_stop": True},
        )

    def _mark_failed(self, error: str, ledger: _Ledger) -> None:
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return
            job.status = "failed"
            job.error = error[:2000]
            job.done_units = len(ledger.done)
            job.failed_units_json = ledger.failed
            job.tokens_used = _total_tokens(ledger.usage)
            job.cost_estimate_json = ledger.cost.to_json()
            job.heartbeat_at = utcnow()
            db.commit()


def _unit_key(unit: Unit) -> str:
    """单元的稳定标识。

    不能只用标题：同一份资料里出现两个同名篇目时（③ 的结构规则偶尔会切错），
    单靠标题会让续跑把第二篇当成第一篇跳过 —— 少一篇数据，界面还看不出来。
    """
    return f"{unit.index:04d}:{unit.title}"


@dataclass
class _Ledger:
    """任务的一次性账本。``done`` 存单元 key，``failed`` 存可读的失败明细。"""

    done: set[str] = field(default_factory=set)
    failed: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost: _Cost = field(default_factory=lambda: _Cost())


def _outcome_of(unit: Unit, saved: SaveResult, usage: Usage | None) -> UnitOutcome:
    """把 ``save_piece`` 的结果翻译成单元结论。"""
    if saved.outcome is SaveOutcome.REJECTED:
        return UnitOutcome(
            outcome=str(SaveOutcome.REJECTED), reason=saved.reason, failure=saved.failure
        )
    return UnitOutcome(
        outcome=str(saved.outcome),
        reason=saved.reason,
        review=saved.review_count,
        usage=usage,
    )


def _total_tokens(usage: Usage) -> int:
    return usage.prompt_tokens + usage.completion_tokens


def _estimate_total_cost(bundle: AlignerBundle, units: list[Unit]) -> float:
    """F8：按整篇的块切与提示词布局预估总花费（元）。

    降级通道（``provider`` 为空）不花钱，返回 0。牌价表查不到时走兜底价，
    宁可比别的渠道高估，也不能把 0 当成「免费」放过去。
    """
    if not bundle.provider or not bundle.model:
        return 0.0
    prompt_total = 0
    completion_total = 0
    for u in units:
        zh = B.split_segments(u.zh)
        en = B.split_segments(u.en)
        if not zh or not en:
            continue
        p, c = estimate_alignment_tokens(zh, en)
        prompt_total += p
        completion_total += c
    try:
        return round(
            estimate_cost(bundle.provider, bundle.model, prompt_total, completion_total).total_cny,
            6,
        )
    except Exception as exc:  # 预估失败不该把任务打死，熔断退化为不开启
        log.debug("预估整篇费用失败：%s", exc)
        return 0.0


def _split_tokens(total: int) -> Usage:
    """续跑时只有 token 总量，prompt / completion 的拆分无从还原。

    全部记在 prompt 侧：展示与结算口径偏保守，宁可高估也别把花费报低。
    """
    return Usage(prompt_tokens=max(0, int(total)), completion_tokens=0)


def _add_usage(a: Usage, b: Usage) -> Usage:
    return Usage(
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
    )


@dataclass
class _Cost:
    """按 provider/model 累计牌价。降级路径下 ``by_model`` 为空。

    ``estimated_cny`` 是 F8 的一次性整篇预估（首次跑写入），续跑时原样恢复，
    不重算 —— 它代表的是「这份资料加工一遍」的参考值，与已经花掉多少无关。
    """

    by_model: dict[str, dict] = field(default_factory=dict)
    estimated_cny: float = 0.0

    @classmethod
    def from_json(cls, raw: dict | None) -> _Cost:
        """从 ``jobs.cost_estimate_json`` 恢复，续跑时不能把上轮花的钱抹成 0。"""
        cost = cls(estimated_cny=float((raw or {}).get("estimated_cny", 0.0)))
        for row in (raw or {}).get("by_model", []):
            key = f"{row.get('provider')}/{row.get('model')}"
            cost.by_model[key] = {
                "provider": row.get("provider", ""),
                "model": row.get("model", ""),
                "prompt_tokens": int(row.get("prompt_tokens", 0)),
                "completion_tokens": int(row.get("completion_tokens", 0)),
                "prompt_cny": float(row.get("prompt_cny", 0.0)),
                "completion_cny": float(row.get("completion_cny", 0.0)),
            }
        return cost

    def add(self, bundle: AlignerBundle, usage: Usage) -> None:
        if not bundle.provider or not bundle.model or not _total_tokens(usage):
            return
        try:
            est = estimate_cost(
                bundle.provider, bundle.model, usage.prompt_tokens, usage.completion_tokens
            )
        except Exception as exc:  # 牌价表缺这个模型不该让任务失败
            log.debug("无法估算 %s/%s 的费用：%s", bundle.provider, bundle.model, exc)
            return
        key = f"{bundle.provider}/{bundle.model}"
        slot = self.by_model.setdefault(
            key,
            {
                "provider": bundle.provider,
                "model": bundle.model,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "prompt_cny": 0.0,
                "completion_cny": 0.0,
            },
        )
        slot["prompt_tokens"] += est.prompt_tokens
        slot["completion_tokens"] += est.completion_tokens
        slot["prompt_cny"] = round(slot["prompt_cny"] + est.prompt_cny, 6)
        slot["completion_cny"] = round(slot["completion_cny"] + est.completion_cny, 6)

    def to_json(self) -> dict:
        return {
            "by_model": list(self.by_model.values()),
            "total_cny": round(
                sum(m["prompt_cny"] + m["completion_cny"] for m in self.by_model.values()), 6
            ),
            "estimated_cny": round(self.estimated_cny, 6),
        }

    def total_cny(self) -> float:
        """实际已累计的花费（不含预估）。"""
        return sum(m["prompt_cny"] + m["completion_cny"] for m in self.by_model.values())


# ==========================================================================
# 调度
# ==========================================================================
class RunManager:
    """进程内任务表。

    单用户自用，加工任务同时也就一两个，所以直接用 ``asyncio.create_task``
    挂在服务端进程上，不引 Celery/Redis。代价是**重启服务端会丢正在跑的任务** ——
    但 ``jobs`` 表里的状态和 checkpoint 都在，``resume`` 能接上。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[str]] = {}
        self._engines: dict[str, Any] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """记下主事件循环（应用启动时调）。

        ``/jobs`` 是同步路由，FastAPI 会丢到线程池里执行，那儿**没有**运行中的
        loop，``asyncio.create_task`` 直接 RuntimeError。没有这个兜底的话，
        真实环境下建任务就是 500（测试里都是替换掉的假 manager，测不出来）。
        """
        self._loop = loop or asyncio.get_running_loop()

    def submit(self, job_id: str, *, overwrite: bool = False) -> None:
        self.resume(job_id, overwrite=overwrite)

    def resume(self, job_id: str, *, overwrite: bool = False) -> None:
        if job_id in self._tasks and not self._tasks[job_id].done():
            return  # 已在跑，别重复起
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 调用方在同步路由的工作线程里：丢回主 loop 起，别在这里建 task
            loop = self._loop
            if loop is None or loop.is_closed():
                raise RuntimeError("RunManager 未绑定事件循环，无法起任务") from None
            loop.call_soon_threadsafe(self._start, job_id, overwrite)
            return
        self._start(job_id, overwrite)

    def _start(self, job_id: str, overwrite: bool) -> None:
        """必须在主事件循环上调用。"""
        if job_id in self._tasks and not self._tasks[job_id].done():
            return
        engine = self._build_engine(job_id, overwrite=overwrite)
        self._engines[job_id] = engine
        self._tasks[job_id] = asyncio.create_task(engine.run(), name=f"job-{job_id}")

    def _build_engine(self, job_id: str, *, overwrite: bool) -> Any:
        """按 ``jobs.type`` 选引擎。未知类型直接建不了任务（API 层已校验）。"""
        with SessionLocal() as db:
            job = db.get(Job, job_id)
        kind = job.type if job is not None else "recite_align"
        if kind == "graph_extract":
            from app.jobs.graph import GraphEngine

            return GraphEngine(job_id)
        return JobEngine(job_id, overwrite=overwrite)

    def request_cancel(self, job_id: str) -> None:
        eng = self._engines.get(job_id)
        if eng is not None:
            eng.request_cancel()

    def is_running(self, job_id: str) -> bool:
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        self._engines.clear()

    def active(self) -> list[str]:
        return [k for k, v in self._tasks.items() if not v.done()]


run_manager = RunManager()


def list_pending_job_ids(db: Session) -> list[str]:
    """启动时把中断的任务捞出来（状态还是 running/queued 却没有对应协程）。"""
    return list(
        db.scalars(
            select(Job.id).where(
                Job.type.in_(("recite_align", "graph_extract")),
                Job.status.in_(("running", "queued")),
            )
        )
    )


def recover_orphaned_jobs() -> int:
    """启动时把"没有协程在跑"的 running/queued 任务落成 ``interrupted``。

    服务端重启会丢掉所有后台协程（``RunManager`` 的注释里写明了这个取舍）。
    不处理的话，这些任务会永远卡在 ``running``：``resume`` 只接受
    failed/cancelled/interrupted，客户端既点不了续跑，也看不出它已经死了。
    checkpoint 还在，置成 interrupted 后用户点一次「续跑」就能接上。
    """
    with SessionLocal() as db:
        ids = list_pending_job_ids(db)
        for jid in ids:
            job = db.get(Job, jid)
            if job is None:
                continue
            job.status = "interrupted"
            job.error = "服务端重启，任务已中断（进度已保存，点「续跑」接着跑）。"
            job.heartbeat_at = utcnow()
        db.commit()
    return len(ids)


__all__ = [
    "HEARTBEAT_INTERVAL",
    "UNIT_TIMEOUT",
    "AlignerBundle",
    "JobEngine",
    "Plan",
    "RunManager",
    "Unit",
    "UnitOutcome",
    "build_plan",
    "list_pending_job_ids",
    "recover_orphaned_jobs",
    "resolve_aligner",
    "run_manager",
]
