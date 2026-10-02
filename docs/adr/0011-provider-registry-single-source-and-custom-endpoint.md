# ADR-0011 服务商注册表为唯一真源 + 自定义端点仅对专用 provider 开放

- 状态：已采纳
- 日期：2026-09-30
- 关联：`app/llm/registry.py`（`PROVIDERS` / `ProviderSpec` / `list_providers` / `build_provider`）、
  `app/core/config.py`（`Provider = Literal[tuple(PROVIDERS)]`）、
  `app/api/routers/settings.py`（`ApiKeyIn.base_url` 与 `_check_base_url`）、
  `app/db/models.py`（`ApiKey.base_url`）、
  `alembic/versions/b91c4e70d5a8_api_keys_base_url.py`、
  `app/jobs/engine.py`、`app/jobs/graph.py`（透传 `key_row.base_url`）、
  `tests/test_llm.py::TestProviderRegistry`、`tests/test_settings_api.py`

## 背景

两个独立的问题，一起改。

### 一、设置页的厂商下拉框读错了字段名

`/settings/providers` 返回的每一项，标识字段一直叫 **`key`**（`list_providers()` 从
`ProviderSpec.key` 取的）。而 `SettingsPage.tsx` 写的是 `p.provider`，于是下拉框里每个
选项的 value 都是 `undefined`：用户看到一列可选厂商，点下去存 Key 必然 422。

它长期没被发现，因为服务端**同时**存在一份写死的
`Literal["openai", "deepseek", "qwen", "glm", "anthropic", "gemini"]`。校验只认这六家，
UI 却想让人手输任意字符串 —— 两边都没有覆盖「注册表里有但字面量里没有」这一类，
测试自然也测不到。

### 二、国内主流厂商缺席

注册表只有 6 家，其中 4 家海外。个人自用在国内，实际能拿到的 Key 大多来自 Kimi、
豆包（火山方舟）、SiliconFlow、MiniMax、腾讯混元；另有一批用户是拿 OneAPI / NewAPI /
Ollama 这类**自建 OpenAI 兼容网关**，此前完全没有入口。

## 决策

### 1. `Provider` 字面量从 `PROVIDERS` 派生

```python
Provider = Literal[tuple(PROVIDERS)]
```

删掉手写列表。**校验通过即意味着「这家在注册表里有实现」**，不再需要两处同步，
也就不会再出现「能存进库但调用时才发现未接入」的反向漏网
（`tests/test_settings_api.py::TestProviderLiteral` 双向都钉住了）。

### 2. `ProviderSpec` 扩展承载「怎么展示、怎么拿 Key」

新增四个字段，都只服务设置页展示，不影响调用路径：

- `region`：下拉框按 `国内` / `海外` 分组（`tests/test_settings_api.py` 断言只能是这两值，
  前端直接拿它当分组键，改字面量会漏到这里）；
- `display_name`：UI 显示名，与 `key` 解耦；
- `console_url`：申请 Key 的入口。**每家真实厂商必填** —— 百炼 / 方舟 / SiliconFlow 的控制台
  差着好几个层级，让用户自己搜是纯摩擦；
- `base_url_editable`：是否允许用户改地址，只有 `openai_compatible` 为 `True`。

同时给八家国内厂商补齐 `text_models` / `default_text`，价格表补对应条目。

### 3. `base_url` 落库，但**固定端点不可覆盖**

`api_keys` 加可空 `base_url` 列（迁移 `b91c4e70d5a8`），随 Key 一起存、随 Key 一起走
（`build_provider` 收到 `key_row.base_url`，`jobs/engine.py` 与 `jobs/graph.py` 两处都已透传）。

`_check_base_url` 划一条硬边界：

- `base_url_editable=False` 的厂商**填了就 400**；
- `openai_compatible`**必须填**，且要与注册表的占位地址不同，否则等于没填。

这条不是洁癖。`Authorization` 头是跟着 `base_url` 走的：放开「改地址」意味着一条
DeepSeek 的 Key 可以被指使发往任意主机，而用户界面看起来完全正常。注册表里写死的
地址之所以写死，就是因为它是**信任边界的一部分**，不是省事。

### 4. 前端读 `key`，模型名按 Key 联动

- 下拉框读 `key` / `display_name`，按 `region` 分组；
- 选中 `openai_compatible` 时才渲染 Base URL 输入框，并提示要带 `/v1`；
- 模型档案的模型名从「纯自由输入」改成 `AutoComplete`：候选来自所选 Key 所属厂商的
  `text_models` / `vision_models`，但仍可手输 —— 模型清单是**快照**，厂商天天上新，
  纯下拉会把用户挡在外面。切 Key 时清掉模型字段，否则会存下一个不存在的「厂商 × 模型」组合。

## 后果

- **去重口径变了**：`(provider, mask)` 判重对自定义端点不再适用 —— 同一串 Key 打到
  两个不同网关是两件事，否则第二个会被静默挡掉、用户以为存上了。固定端点仍按
  `(provider, mask)` 判重，行为不变（两条都有用例）。
- 契约变了：`ApiKeyIn` / `ApiKeyOut` / `ApiKeyProbeIn` 多 `base_url`，`ApiKeyIn.provider`
  的 enum 从 6 项扩到 12 项。`docs/api/openapi.yaml` 已重新导出。
- 新增八家的**价格是估算值**，没有逐项实时核验；代码里已注明，且支持
  `STRAYT_PRICE_<provider>_<model>=输入,输出` 覆盖。真接 Key 后应逐家核对。
- 模型清单同理，快照性质；改清单只需动 `registry.py`，不需要动前端。
- `openai_compatible` 参与 `test_every_provider_has_default_and_price` 时**豁免默认模型与价格**
  —— 模型和地址都是用户填的，没有「推荐款」可言。