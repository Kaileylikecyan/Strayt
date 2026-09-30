"""F11 加工任务引擎：LLM 抽取知识点 → 预览草稿（``jobs.results_json``）。

与背诵管线（``engine.py``）同一条纪律：

- **一篇 = 一个单元**，篇际独立，一篇失败不拖垮全篇；
- **进度/草稿/checkpoint 同一个事务提交**，崩了不出现「checkpoint 说做完了
  但草稿里没有」；
- **心跳独立于单元**，取消只在单元边界生效（不硬杀协程）；
- **续跑省模型费**：已 done 的篇不重调 LLM，草稿从 ``results_json`` 还原，
  所有篇结束后一次性合成项目级草稿；
- **图谱加工必须有 LLM，没有降级**（背诵可以退回长度直配，图谱没有模型就没有
  知识点）。所以 provider 缺席时任务直接失败并给出原因，而不是静默产出空图。

结果**不回写正式表**：只存进 ``jobs.results_json``（预览草稿），等客户端
「预览确认」后由 ``graph-confirm`` 接口一次性原子入库。加工任务的目标状态是
「草稿就绪可确认」，不是「已入库」。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from app.core.security import decrypt_secret
from app.db.models import ApiKey, File, Job, ModelProfile, Project, utcnow
from app.db.session import SessionLocal
from app.graph.extract import GraphError, GraphUnit
from app.graph.merge import merge_units
from app.jobs.engine import (
    HEARTBEAT_INTERVAL,
    UNIT_TIMEOUT,
    Unit,
    UnitOutcome,
    _add_usage,
    _backfill_failed_keys,
    _Cost,
    _Ledger,
    _migrate_done,
    _split_tokens,
    _total_tokens,
    _unit_key,
    build_plan,
)
from app.llm.base import LLMError, LLMProvider
from app.llm.registry import build_provider
from app.persist import SaveOutcome

log = logging.getLogger(__name__)


class NoProviderError(Exception):
    """图谱加工必须有 LLM，但没有可用的 provider。"""


@dataclass
class GraphExtractorBundle:
    """抽取器 + 计价所需的 provider/model。"""

    extractor: object
    provider: str | None = None
    model: str | None = None


def resolve_extractor(db, project_id: str) -> GraphExtractorBundle:
    """按项目绑定的模型档案构造抽取器。

    图谱加工**不能没有模型**（背诵可以降级为长度直配，抽取降不了级），
    所以这里是硬性抛出而不是静默降级 —— 缺配置要让用户看到原因去设置，
    而不是等任务跑完拿一张空图。
    """
    from app.graph.extract import GraphExtractor

    project = db.get(Project, project_id)
    if project is None:
        raise NoProviderError("项目不存在，无法解析模型档案")
    profile_id = (project.api_profile_json or {}).get("model_profile_id")
    if not profile_id:
        raise NoProviderError("项目未绑定模型档案，请先在设置里配置 LLM 档案")
    profile = db.get(ModelProfile, profile_id)
    if profile is None or not profile.text_provider_id:
        raise NoProviderError(f"模型档案 {profile_id} 不存在或未配置文本模型")
    key_row = db.get(ApiKey, profile.text_provider_id)
    if key_row is None or not key_row.enabled:
        raise NoProviderError("模型档案绑定的 API Key 无效或已停用")
    provider: LLMProvider = build_provider(
        key_row.provider, decrypt_secret(key_row.secret_enc), profile.text_model
    )
    return GraphExtractorBundle(GraphExtractor(provider), key_row.provider, provider.model)


class GraphEngine:
    """跑一个 ``graph_extract`` 任务。结果落草稿，不写正式表。"""

    def __init__(self, job_id: str, *, on_unit=None) -> None:
        self.job_id = job_id
        self.on_unit = on_unit
        self._cancel = asyncio.Event()
        self._last: UnitOutcome | None = None

    # -- 主流程 ---------------------------------------------------------
    async def run(self) -> str:
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
            try:
                bundle = resolve_extractor(db, project_id)
            except NoProviderError as exc:
                job.status = "failed"
                job.error = str(exc)
                db.commit()
                return "failed"
            try:
                plan = build_plan(db, file_row)
            except Exception as exc:
                job.status = "failed"
                job.error = f"解析失败：{exc}"
                db.commit()
                return "failed"

            ckpt = dict(job.checkpoint_json or {})
            done = _migrate_done(plan.units, set(ckpt.get("done", [])))
            failed = list(ckpt.get("failed", []))
            usage = _split_tokens(job.tokens_used or 0)
            cost = _Cost.from_json(job.cost_estimate_json)
            failed = _backfill_failed_keys(failed, done, plan.units)

            job.total_units = len(plan.units)
            prior = {r.get("key"): r for r in (job.units_json or [])}
            settled = done | {f.get("key") for f in failed if f.get("key")}
            job.units_json = [
                {
                    **row,
                    "status": (
                        prior[row["key"]]["status"]
                        if row.get("key") in settled and row.get("key") in prior
                        else "pending"
                    ),
                }
                for row in plan.to_json()
            ]
            job.checkpoint_json = {
                **(job.checkpoint_json or {}),
                "file_id": file_id,
                "done": sorted(done),
                "failed": failed,
            }
            job.done_units = len(done)
            job.tokens_used = _total_tokens(usage)
            job.cost_estimate_json = cost.to_json()
            db.commit()
            units = plan.units
            done = {_unit_key(u) for u in units if u.title in done or _unit_key(u) in done}

        ledger = _Ledger(done=done, failed=failed, usage=usage, cost=cost)
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            for unit in units:
                if unit.key in ledger.done:
                    continue  # 上一轮已完成，草稿已在 results_json，续跑不再调 LLM
                if self._cancel.is_set():
                    return self._finish_cancelled(ledger)
                await self._run_unit(unit, bundle, ledger)
                if self.on_unit is not None:
                    self.on_unit(unit, self._last)
            return self._finish(ledger)
        except Exception as exc:
            log.exception("图谱任务 %s 失败", self.job_id)
            self._mark_failed(str(exc), ledger)
            return "failed"
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    # -- 单个单元 -------------------------------------------------------
    async def _run_unit(self, unit: Unit, bundle: GraphExtractorBundle, ledger: _Ledger) -> None:
        text = unit.zh.strip() or unit.en.strip()
        if not text:
            self._settle(
                unit,
                bundle,
                ledger,
                UnitOutcome(
                    outcome=str(SaveOutcome.REJECTED),
                    reason="单侧为空",
                    failure={
                        "unit": unit.title,
                        "reason": "empty_source",
                        "retryable": False,
                        "message": "该篇正文为空，没有可抽取的内容",
                    },
                ),
            )
            return
        try:
            gu: GraphUnit = await asyncio.wait_for(
                bundle.extractor.extract_async(
                    text, title=unit.title, loc_page=unit.zh_page or unit.page
                ),
                timeout=UNIT_TIMEOUT,
            )
        except TimeoutError:
            self._settle(
                unit,
                bundle,
                ledger,
                self._failed(
                    unit,
                    "抽取超时",
                    "extract_timeout",
                    True,
                    f"超过 {UNIT_TIMEOUT:.0f}s 未完成",
                ),
            )
            return
        except (LLMError, GraphError) as exc:
            kind = getattr(exc, "kind", "llm_failed")
            detail = getattr(exc, "detail", None) or getattr(exc, "message", None) or str(exc)
            retryable = kind in ("rate_limited", "provider_error", "network", "extract_timeout")
            self._settle(
                unit, bundle, ledger, self._failed(unit, "抽取失败", kind, retryable, detail)
            )
            return

        if not gu.nodes:
            self._settle(
                unit,
                bundle,
                ledger,
                self._failed(
                    unit,
                    "本篇未抽取到可用知识点",
                    "graph_empty",
                    True,
                    " ".join(gu.warnings) or "模型没有给出可用知识点，可重试",
                ),
            )
            return
        self._settle(
            unit,
            bundle,
            ledger,
            UnitOutcome(
                outcome=str(SaveOutcome.CREATED),
                reason=f"抽取 {len(gu.nodes)} 个知识点、{len(gu.categories)} 个分类",
                review=len(gu.nodes),
                usage=gu.usage,
            ),
            gu=gu,
        )

    @staticmethod
    def _failed(unit: Unit, reason: str, kind: str, retryable: bool, message: str) -> UnitOutcome:
        return UnitOutcome(
            outcome=str(SaveOutcome.REJECTED),
            reason=reason,
            failure={
                "unit": unit.title,
                "reason": kind,
                "retryable": retryable,
                "message": message,
            },
        )

    def _settle(
        self,
        unit: Unit,
        bundle: GraphExtractorBundle,
        ledger: _Ledger,
        out: UnitOutcome,
        *,
        gu: GraphUnit | None = None,
    ) -> None:
        """记下一个单元的结论。``gu`` 非空表示该篇抽取成功，草稿一起落盘。

        草稿和 checkpoint **在同一个事务里提交** —— 另一处（``confirm``）原子
        入库前，这半个事务不能和进度脱节。
        """
        self._last = out
        if out.usage is not None:
            ledger.usage = _add_usage(ledger.usage, out.usage)
            ledger.cost.add(bundle, out.usage)
        if out.outcome in (
            str(SaveOutcome.CREATED),
            str(SaveOutcome.REPLACED),
            str(SaveOutcome.SKIPPED),
        ):
            ledger.done.add(unit.key)
        elif out.failure is not None:
            entry = {**out.failure, "key": unit.key}
            for i, old in enumerate(ledger.failed):
                if old.get("key", old.get("unit")) == unit.key:
                    ledger.failed[i] = entry
                    break
            else:
                ledger.failed.append(entry)
        with SessionLocal() as db:
            self._write_progress(db, unit, out, ledger)
            if gu is not None:
                job = db.get(Job, self.job_id)
                if job is not None:
                    results = dict(job.results_json or {})
                    units = dict(results.get("units", {}))
                    units[unit.key] = gu.to_dict()
                    job.results_json = {**results, "units": units}
            db.commit()

    # -- 进度 / 收尾 ----------------------------------------------------
    def _write_progress(self, db, unit: Unit, out: UnitOutcome, ledger: _Ledger) -> None:
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
        # 必须重建整个列表而不是原地改行：MutableList 只在外层 list 被替换时标脏
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

    def _finish(self, ledger: _Ledger) -> str:
        """收尾。有可确认草稿才 success；素材不合格视为失败（可续跑重试）。"""
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return "failed"
            if not ledger.done:
                job.status = "failed"
                job.error = "所有篇目均未抽取到可用知识点"
                self._apply_terminal(job, ledger)
                db.commit()
                return "failed"
            units_json = dict((job.results_json or {}).get("units", {}))

            # 按篇的稳定 key 前缀（``0003:标题`` → 3）保持篇序，合成项目级草稿。
            def _sort_key(kv) -> int:
                key = kv[0]
                try:
                    return int(key.split(":", 1)[0])
                except ValueError:
                    return 9999

            ordered = [
                (int(k.split(":", 1)[0]), GraphUnit.from_dict(v))
                for k, v in sorted(units_json.items(), key=_sort_key)
            ]
            draft = merge_units(ordered)
            job.results_json = {
                **(job.results_json or {}),
                "units": units_json,
                "draft": draft.to_dict(),
                "status": "pending_confirm",
            }
            job.status = "success"
            job.error = None
            self._apply_terminal(job, ledger)
            db.commit()
            return "success"

    def _apply_terminal(self, job: Job, ledger: _Ledger) -> None:
        job.done_units = len(ledger.done)
        job.failed_units_json = list(ledger.failed)
        job.tokens_used = _total_tokens(ledger.usage)
        job.cost_estimate_json = ledger.cost.to_json()
        job.checkpoint_json = {
            **(job.checkpoint_json or {}),
            "done": sorted(ledger.done),
            "failed": ledger.failed,
        }
        job.heartbeat_at = utcnow()

    def _finish_cancelled(self, ledger: _Ledger) -> str:
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return "failed"
            job.status = "cancelled"
            self._apply_terminal(job, ledger)
            db.commit()
        return "cancelled"

    def _mark_failed(self, error: str, ledger: _Ledger) -> None:
        with SessionLocal() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return
            job.status = "failed"
            job.error = error[:2000]
            self._apply_terminal(job, ledger)
            db.commit()

    # -- 心跳 / 取消 ----------------------------------------------------
    async def _heartbeat_loop(self) -> None:
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
        self._cancel.set()


__all__ = ["GraphEngine", "GraphExtractorBundle", "NoProviderError", "resolve_extractor"]
