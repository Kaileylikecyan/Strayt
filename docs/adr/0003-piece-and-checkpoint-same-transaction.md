# ADR-0003 篇目写入与 checkpoint 同事务提交

- 状态：已采纳
- 日期：2026-09-27
- 关联：`app/jobs/engine.py`（`_settle`、`_write_progress`）、`app/persist.py`（`save_piece`）

## 背景

加工任务的产物有两样：入库的篇目/段落对（`pieces` / `pairs`），和表示"做到哪儿了"的
`jobs.checkpoint_json`。两者必须一致，否则：

- **先入库、后写 checkpoint**，崩在中间 → 篇目已在库，checkpoint 说没做 → 续跑重做
  这一篇。重复调模型（多花钱），而且可能覆盖用户已经做过的微调。
- **先写 checkpoint、后入库** → checkpoint 说做了，库里没有 → 这一篇永远缺失，而
  任务显示 100% 完成。**这个更糟，是静默的数据丢失。**

## 决策

`_write_progress` 接受调用方**已经开着**的 `Session`，不开自己的 session。`_settle` 在
同一个事务里依次做 `save_piece` 和 `_write_progress`，最后由 `_settle` 统一 `commit()`。

## 理由

- 两次写入必须原子，这是"崩了能不能接上"的唯一保证。分两次提交就没有 ADR 可写了。
- 质量门控拒收时（`SaveOutcome.REJECTED`）`save_piece` 不写篇目，但 checkpoint 仍要记
  下"这篇试过了、为什么不行"。这也是必须在同一事务里统一提交的原因之一：拒收这一步
  本身要落两样状态。

## 后果

- **commit 次数是测试断言**。`test_should_commit_once_per_unit` 钉住"一个单元 = 一次
  commit"（外加开始和收尾各一次）。将来谁在单元中间偷偷 `commit()`，测试会红。
- `jobs` 的 JSON 列（`units_json` / `failed_units_json`）必须是**整体替换**而不是原地
  改嵌套 dict。SQLAlchemy 的 JSON 列即使包了 `MutableList`，改嵌套 dict 也会被静默
  丢掉 —— 症状是"任务跑完了 `units_json` 还全 pending，客户端进度条永远不动"。
  `_write_progress` 因此重建整个列表。
- `SaveResult` 返回 `piece_id`（标量）而不是 ORM `Piece` 对象，避免把脱离 Session 的
  实例传给调用方（`DetachedInstanceError`）。这个坑在测试里踩过一次。
