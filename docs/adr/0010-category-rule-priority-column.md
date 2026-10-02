# ADR-0010 分类规则优先级由显式 priority 列承载

- 状态：已采纳
- 日期：2026-09-30
- 关联：`app/db/models.py`（`CategoryRule.priority`）、
  `app/api/routers/graph.py`（`list_category_rules` / `put_category_rules`）、
  `app/jobs/graph.py`（`merge_units` 前取规则）、
  `alembic/versions/d4f1a8b6c207_category_rules_priority.py`、
  `tests/test_category_rules.py::TestCategoryRuleApi`、`scripts/smoke_http.py --graph`

## 背景

F12 要求「一级分类可先用自定义规则覆盖 LLM 原判」。规则集合是**有序列表**，
语义是「自上而下匹配，第一条命中即采用」（`app/graph/classify.py` 一直是这么实现的）。

那么「顺序」存哪儿？最初的答案是：**`id` 升序**。`GET` 里写的是
`order_by(CategoryRule.id)`，`jobs/graph.py` 取规则时也是 `order_by(CategoryRule.id)`，
smoke 脚本的注释里还写着「按 id 升序即优先级」。

问题在于 `id` 是 `new_id()`（uuid4 hex）**随机**生成的。于是：

1. 用户在图谱页把「更精确的规则」拖到第一条，实际生效的却是随机那一条 —— 排序功能形同虚设，
   且错得没有规律，没法靠「重排一次」绕过去。
2. 更要命的是它**测不出来**。`smoke_http.py --graph` 里那条
   `got[0]["pattern"] == "高中数学"` 断言是在随机 id 上比顺序，本轮加口令登录后重跑就翻车了
   —— 一个「上轮绿、下轮红」的测试，等于没有测试。

上一轮把它记成「F12 规则优先级语义待定」的待办，方向是错的：那不是待定，是错的。

## 决策

加 `priority INT NOT NULL` 显式列，语义收敛为「**PUT 时客户端提交的数组下标即优先级**」：

- `put_category_rules` 用 `enumerate(body.rules)` 落 `priority`，一次写入的顺序就是语义。
- `list_category_rules` 与 `jobs/graph.py` 一律 `order_by(CategoryRule.priority, CategoryRule.id)`
  —— 第二排序键保留是因为同 `priority` 时仍需稳定序（理论上不会发生，但 `ORDER BY` 不给兜底
  就等于把不确定性留给将来）。
- `CategoryRuleOut` 回 `priority`，客户端回显排序结果时不必再猜。
- 联合索引 `ix_category_rules_project_priority (project_id, priority)`：列表和匹配都走这个前缀。

### 迁移里两个刻意的选择

- **存量行统一给 0**。原来就没有这个信息（随机 id 的顺序不是用户意图），无法还原，
  也不该假装能还原。用户下次打开管理面板保存一次即可恢复正确顺序。
- **`server_default` 加了又撤**。建列时带 `server_default='0'` 是为了让存量行能落库；
  随后立刻 `alter_column(server_default=None)`，让后续 INSERT 必须显式给值。
  否则「忘了写 `priority`」会退化成全 0，而全 0 时排序又变成按 id —— 正是刚修掉的 bug，
  不能留一个能把它悄悄请回来的入口。

## 后果

- `PUT` 的请求体**不接受** `priority` 字段（客户端传了也会被 `CategoryRuleIn` 忽略）。
  这是有意的：优先级只有一个来源，就是数组顺序，两处可写必然打架。
- 契约变了，`docs/api/openapi.yaml` 已重新导出（`CategoryRuleOut` 多一个只读字段）。
- 桌面端 `GraphPage.tsx` 用的就是数组顺序，本就无需改动。