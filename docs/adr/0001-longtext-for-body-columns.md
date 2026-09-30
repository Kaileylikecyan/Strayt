# ADR-0001 正文列用 LONGTEXT，不靠应用层截断

- 状态：已采纳
- 日期：2026-09-27
- 关联：`alembic/versions/3e8b71d4c902_raw_text_longtext.py`、`app/db/models.py`

## 背景

T2 收尾时用真实样板 PDF（`fixtures/daoyouci.pdf`，50 页）跑通了完整加工链路，第一次
就撞上 `DataError 1406 Data too long`：`file_parses.raw_text` 是 MySQL `TEXT`，上限
65,535 字节，而样板清洗后是 159,817 字节。

也就是说**任何真实 PDF 都存不进去**，整条 ③④⑤⑥⑦⑧ 链路在落库那一步就走不完。此前
一直没暴露，是因为单元测试的解析样本都是几十字节的小段，而真实数据只走过
`var_test/quality_e2e.py` —— 那个脚本只验纯函数，不落库。

## 决策

把三列提到 `LONGTEXT`：

| 列 | 内容 | 样板实测 |
|---|---|---|
| `file_parses.raw_text` | 清洗后全文 | 159,817 字节（超 2.4 倍） |
| `pairs.zh` / `pairs.en` | 单个段落对 | 最长 8,461 字节（未超，但无余量） |

模型侧写成 `Text().with_variant(mysql.LONGTEXT(), "mysql")`，让 SQLite 等方言仍用
`TEXT`。

## 理由

- **不在应用层截断。** 截断会让 `raw_text` 与 `pages_json` 不一致 —— 后者没截，于是
  "清洗后全文"这个字段名在说谎。`file_parses` 的用途就是原文可复查（②的清洗规则会
  改，要能重跑对比），截断直接毁掉这个用途。
- **不用 `MEDIUMTEXT`。** 它够大，但同样是"猜一个够用的大小"。`LONGTEXT` 4GB 的代价在
  MySQL 里只是行外存储指针，而 160KB 相对 `max_allowed_packet`（8.0 默认 64MB）微不足道。
- **一起改 `pairs.zh/en`。** 虽然实测没超，但版式差的资料可能把整页塞进一段。与其等
  线上 `DataError` 再排查，不如同批解决 —— 迁移成本一样（一次 `ALTER`）。

## 后果

- `alembic/versions/3e8b71d4c902_raw_text_longtext.py` 需要在开发库和测试库都 upgrade。
  `ALTER TABLE ... MODIFY` 会重建表，50 万行级别的库要挑低峰期。
- 新增 `tests/test_jobs.py::Test真实PDF落库`，用真 PDF 真 insert 钉住这个回归。
  光靠小样本测试永远发现不了这类问题。

## 顺带修的测试基础设施漏洞

`tests/conftest.py::_ensure_schema` 原本只比对**列名**，不比列类型。列名没变而类型从
`TEXT` 升到 `LONGTEXT`，测试库会悄悄留在旧 schema 上 —— 于是这个 bug 在测试环境里
还多藏了一层。现在 `_ensure_schema` 也会比对列类型的长度/族别，漂移即重建。

这一条比 LONGTEXT 本身更重要：它是**上一个 bug 能藏这么久的直接原因**。
