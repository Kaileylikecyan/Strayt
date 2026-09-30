# 样板资料目录

**这里不放任何真实资料。** 真实教材/试题有版权，不进仓库（`.gitignore` 排除了
`*.pdf` / `*.docx` / `*.json`）。

## 跑测试需要什么

`tests/test_files.py::test_direct_upload_real_pdf` 会读 `daoyouci.pdf`。
本地自备一份放这里即可（文件名固定）：

```
server/fixtures/daoyouci.pdf
```

一份中英对照的学习材料即可，测试只断言「上传成功 / SHA-256 与字节一致 /
分块协议正确」，不校验内容。没有这个文件时该用例会失败，其余用例不受影响。

## 为什么要留真实样本

管线不变量（`AGENTS.md` §6）里几条只有拿真实 PDF 才发现得了：

- 样板 PDF 零空行全硬换行，`extraction_mode="plain"` 拿不到段落边界，必须 `layout`；
- 但 `layout` 会吞掉英文单词间的空格，所以正文取 `plain`、边界取 `layout`，两路混合；
- 中英句数天然不等（实测 49 vs 55），按句索引配对必然错位。

这些结论写进了 `app/parse/` 的实现前提，替换材料时值得重跑一次确认。
