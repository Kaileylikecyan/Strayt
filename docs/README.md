# 文档索引

| 路径 | 内容 |
|---|---|
| [`rtm.md`](rtm.md) | 需求追踪矩阵 F1~F24 ↔ 实现 ↔ 测试 ↔ 状态 |
| [`api/openapi.yaml`](api/openapi.yaml) | REST 契约（**生成物**，勿手改） |
| [`adr/`](adr/) | 架构决策记录 |
| `../学习工作台产品说明文档.md` | 产品需求真源 |
| `../AGENTS.md` | 环境陷阱、架构红线、管线不变量 |

## 常用命令

```powershell
# 全量自检（lint + 格式 + 迁移 + 测试 + 真实数据 E2E）
.\scripts\check.ps1

# 改完服务端接口后重新导出契约
uv run --project server python scripts\export_openapi.py
```

`check.ps1` 若报 `PSSecurityException`，见脚本头部注释的两种放行方式。

## 什么时候该写 ADR

出现下面任一情况就写一条，别只留在 commit message 里：

- 推翻了自己之前的做法（尤其是"当时看起来对的"那种）
- 有两个都说得通的方案，选了一个，理由半年后可能被人质疑
- 踩了坑，且坑的**根因是设计而不是笔误**（例：测试只比列名不比列类型）
- 引入了会让未来的人踩第二次的约定（例：单元 key 的格式与迁移规则）

ADR 只记**决策与理由**，不记过程。当前：

- [0001 正文列用 LONGTEXT](adr/0001-longtext-for-body-columns.md)
- [0002 单元身份用稳定 key](adr/0002-stable-unit-key-for-checkpoint.md)
- [0003 篇目与 checkpoint 同事务](adr/0003-piece-and-checkpoint-same-transaction.md)
- [0004 续跑保留单元状态](adr/0004-preserve-unit-status-on-resume.md)
- [0005 质量门控入库前拦截](adr/0005-quality-gate-blocks-persistence.md)
- [0006 图谱草稿存 results_json、确认后擦除重建](adr/0006-graph-draft-in-results-json-and-wipe-rebuild.md)
- [0007 文件去重以 (project_id, sha256) 为界](adr/0007-file-dedup-scope-per-project.md)
- [0008 加工任务绑主事件循环调度、启动回收孤儿任务](adr/0008-bind-main-loop-and-recover-orphan-jobs.md)
