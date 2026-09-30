# ADR-0004 续跑时从 checkpoint 恢复单元状态，不重置为 pending

- 状态：已采纳
- 日期：2026-09-27
- 关联：`app/jobs/engine.py`（`run()` 内 `job.units_json` 赋值）

## 背景

`run()` 开头会用本次 `build_plan()` 的结果重建 `units_json`（篇目清单、状态、页码），
因为 `units_json` 兼作**客户端进度条的数据源**（AGENTS.md §5：直接供客户端渲染）。

早期实现是无条件 `job.units_json = plan.to_json()`。

## 问题

`plan.to_json()` 把所有单元状态置为 `pending`。续跑时：

1. 第一篇在上一轮已成功，key 在 `checkpoint_json.done` 里，本轮会被跳过
2. 但它的 `units_json.status` 刚被重置成 `pending`
3. 本轮不会再加工它 → 它的状态**永远**停在 `pending`
4. `done_units = 12`、进度 100%，但客户端看到"第 1 篇还没做"

数据是对的，界面是错的 —— 而且是那种用户会反复刷新、反复怀疑自己网络的错。

## 决策

只给**没有结论**的单元填 `pending`；key 在 `done` 或 `failed` 里的，保留 `units_json`
里的原状态（`prior[row["key"]]["status"]`）。

## 理由

`units_json` 是"计划"和"结果"的混合体：计划部分每轮重算（切分可能变），结果部分必须
持久。混在一起整体覆盖，等于用新的计划把旧的结果擦掉。

分开处理后，"切分规则改了导致某一篇的页码变了"和"这一篇已经做完了"能同时成立 ——
前者更新，后者保留。

## 后果

- 回归测试 `test_should_keep_settled_status_on_resume`：取消在第一篇 → 断言第一篇状态
  非 pending、第二篇是 pending → 续跑 → 断言**所有**状态都非 pending。
- 同类的 `failed_units_json` 也要注意：`_backfill_failed_keys` 会剔除"后来成功了"的
  失败记录，避免界面上一个篇目既显示已处理又挂着红字。

## 教训

`pending` / `created` / `replaced` / `skipped` 这类状态是**给用户看的**，不是内部实现
细节。数据一致但界面误导，比数据不一致更难排查 —— 因为数据层怎么看都自洽。
