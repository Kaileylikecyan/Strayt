import { useEffect, useMemo, useState } from "react";
import {
  AutoComplete,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  List,
  message,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Tag,
  Typography,
} from "antd";
import { DownloadOutlined, LinkOutlined, LockOutlined } from "@ant-design/icons";
import { useStraytTheme, MasteryDot, type MasteryLevel } from "@strayt/ui";
import type { ModelProfileOut, ProviderOut } from "@strayt/api-client";
import { useApp } from "../store";

const STORAGE_DISCLOSURE =
  "API Key 将加密存储在你自己的服务器上（不留在客户端）。仅服务端在加工/对齐时调用对应模型厂商。";

/** 背诵舱计时默认时长：走通用 KV（ADR-0012，计时状态本身不入库）。 */
const RECITE_TIMER_KEY = "recite.timer_default_seconds";
const RECITE_TIMER_MIN = 10;
const RECITE_TIMER_MAX = 3600;
const RECITE_TIMER_FALLBACK = 180;
const RECITE_TIMER_OPTIONS = [
  { value: 60, label: "1 分钟" },
  { value: 180, label: "3 分钟" },
  { value: 300, label: "5 分钟" },
];

function clampTimerSec(n: number): number {
  if (!Number.isFinite(n)) return RECITE_TIMER_FALLBACK;
  return Math.min(Math.max(Math.round(n), RECITE_TIMER_MIN), RECITE_TIMER_MAX);
}

/** 厂商下拉：按国内 / 海外分组，显示名 + 输入价。
 *
 *  注意读的是 `key` / `display_name`（服务端 `list_providers()` 的字段名），
 *  不是 `provider` —— 早期版本读错字段，下拉框渲染出一堆 undefined。
 */
function providerOptions(list: ProviderOut[]) {
  const groups: Record<string, typeof list> = {};
  for (const p of list) (groups[p.region] ??= []).push(p);
  return Object.entries(groups).map(([region, items]) => ({
    label: region,
    options: items.map((p) => ({
      value: p.key,
      label: `${p.display_name} · ¥${p.price_per_1m_input_cny}/百万输入`,
    })),
  }));
}

/** 模型下拉：优先给注册表里的候选，同时允许自由输入（厂商天天上新模型）。 */
function modelOptions(list: ProviderOut[], provider: string | undefined, kind: "text" | "vision") {
  const spec = list.find((p) => p.key === provider);
  if (!spec) return [];
  if (spec.base_url_editable) return [];
  const names = kind === "text" ? spec.text_models : spec.vision_models;
  return names.map((m) => ({ value: m, label: m }));
}

export function SettingsPage() {
  const { config, api, snapshot, syncState, pending, disconnect, flushNow, refresh, lastError, changePassword } =
    useApp();
  const { themeName, setThemeName } = useStraytTheme();

  const [pwModal, setPwModal] = useState(false);
  const [pwBusy, setPwBusy] = useState(false);
  const [pwForm] = Form.useForm<{ old_password: string; new_password: string; confirm: string }>();

  const [keys, setKeys] = useState<
    Array<{ id: string; provider: string; label?: string | null; base_url?: string | null }>
  >([]);
  const [providers, setProviders] = useState<ProviderOut[]>([]);
  const [keyForm] = Form.useForm<{
    provider: string;
    secret: string;
    label?: string;
    base_url?: string;
  }>();
  const [keyModal, setKeyModal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<ModelProfileOut[]>([]);
  const [profileModal, setProfileModal] = useState(false);
  const [reciteTimerSec, setReciteTimerSec] = useState(RECITE_TIMER_FALLBACK);
  const [profileForm] = Form.useForm<{
    name: string;
    text_key_id?: string;
    text_model?: string;
    vision_key_id?: string;
    vision_model?: string;
  }>();

  // 档案里的模型下拉要跟着「选了哪个 Key」联动：Key 决定厂商，厂商决定候选模型。
  const [textKeyId, setTextKeyId] = useState<string | undefined>();
  const [visionKeyId, setVisionKeyId] = useState<string | undefined>();
  const [textKeyProvider, setTextKeyProvider] = useState<string | undefined>();
  const [visionKeyProvider, setVisionKeyProvider] = useState<string | undefined>();
  const [keyProvider, setKeyProvider] = useState<string | undefined>();

  const keyOptions = useMemo(
    () =>
      keys.map((k) => {
        const spec = providers.find((p) => p.key === k.provider);
        const bits = [spec?.display_name ?? k.provider];
        if (k.base_url) bits.push(k.base_url);
        if (k.label) bits.push(k.label);
        return { value: k.id, label: bits.join(" · ") };
      }),
    [keys, providers],
  );

  const reloadKeys = async () => {
    if (!api) return;
    try {
      const list = await api.domain.listApiKeys();
      setKeys(
        list.map((k) => ({
          id: k.id,
          provider: k.provider,
          label: (k as { label?: string | null }).label ?? null,
          base_url: (k as { base_url?: string | null }).base_url ?? null,
        })),
      );
    } catch (err) {
      void err;
    }
  };

  const reloadProviders = async () => {
    if (!api) return;
    try {
      setProviders(await api.domain.listProviders());
    } catch (err) {
      void err;
    }
  };

  const reloadProfiles = async () => {
    if (!api) return;
    try {
      setProfiles(await api.domain.listProfiles());
    } catch (err) {
      void err;
    }
  };

  const reloadReciteTimer = async () => {
    if (!api) return;
    try {
      const row = await api.domain.getKv(RECITE_TIMER_KEY);
      setReciteTimerSec(clampTimerSec(Number(row.value)));
    } catch (err) {
      void err;
    }
  };

  const saveReciteTimer = async (n: number) => {
    const next = clampTimerSec(n);
    setReciteTimerSec(next);
    if (!api) return;
    try {
      await api.domain.putKv(RECITE_TIMER_KEY, String(next));
    } catch {
      message.error("计时默认时长保存失败");
    }
  };

  useEffect(() => {
    void reloadKeys();
    void reloadProviders();
    void reloadProfiles();
    void reloadReciteTimer();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  const testKey = async (keyId: string) => {
    if (!api) return;
    setTesting(keyId);
    try {
      const k = await api.domain.testApiKey(keyId);
      const r = (k.last_test_json ?? {}) as { ok?: boolean; error?: string; latency_ms?: number };
      if (r.ok) message.success(`连通正常${r.latency_ms != null ? `（${r.latency_ms}ms）` : ""}`);
      else message.error(`连通失败：${r.error ?? "未知错误"}`);
      await reloadKeys();
    } catch (err) {
      message.error(`测试失败：${(err as Error).message}`);
    } finally {
      setTesting(null);
    }
  };

  const submitProfile = async (values: {
    name: string;
    text_key_id?: string;
    text_model?: string;
    vision_key_id?: string;
    vision_model?: string;
  }) => {
    if (!api) return;
    setBusy(true);
    try {
      await api.domain.createProfile({
        name: values.name,
        text_key_id: values.text_key_id || null,
        text_model: values.text_model || null,
        vision_key_id: values.vision_key_id || null,
        vision_model: values.vision_model || null,
      });
      setProfileModal(false);
      profileForm.resetFields();
      await reloadProfiles();
    } catch (err) {
      message.error(`保存失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const submitKey = async (values: {
    provider: string;
    secret: string;
    label?: string;
    base_url?: string;
  }) => {
    if (!api) return;
    setBusy(true);
    try {
      await api.domain.createApiKey({
        provider: values.provider,
        secret: values.secret,
        label: values.label,
        // 自定义端点必须带地址；固定端点的厂商把这一项清空再发，
        // 服务端会拒（而不是默默把 Key 发到注册表写死的地址上）。
        base_url: values.base_url?.trim() || null,
      });
      setKeyModal(false);
      keyForm.resetFields();
      setKeyProvider(undefined);
      await reloadKeys();
      message.success("已保存");
    } catch (err) {
      message.error(`保存失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const downloadExport = async () => {
    if (!api) return;
    const data = await api.domain.exportSnapshot();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `strayt-export-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const demoMastery: MasteryLevel = "yes";

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>设置</h3>

      <Card size="small" title="外观" style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        <div className="strayt-row">
          <span className="strayt-muted">主题</span>
          <Segmented
            value={themeName}
            options={[
              { value: "modern", label: "现代简约" },
              { value: "pixel", label: "像素风" },
            ]}
            onChange={(v) => setThemeName(v as "modern" | "pixel")}
          />
          <span className="strayt-row" title="掌握度三色取自当前主题 token">
            <MasteryDot level="no" />
            <MasteryDot level="mid" />
            <MasteryDot level={demoMastery} />
          </span>
        </div>
      </Card>

      <Card
        size="small"
        title="服务器"
        style={{ marginTop: "var(--tok-spaceMd-px)" }}
        extra={
          <Popconfirm title="断开并重新配置？" onConfirm={disconnect}>
            <Button size="small" danger>
              断开
            </Button>
          </Popconfirm>
        }
      >
        <p className="strayt-muted" style={{ marginTop: 0, marginBottom: "var(--tok-spaceSm-px)" }}>
          地址 {config?.baseUrl} · 同步状态 {syncState}{pending > 0 ? ` · 队列 ${pending} 条` : ""}
        </p>
        <div className="strayt-row">
          <Button size="small" onClick={() => void flushNow().then(refresh)}>
            强制同步
          </Button>
          <Button size="small" icon={<DownloadOutlined />} onClick={() => void downloadExport()}>
            导出全量数据
          </Button>
        </div>
        {lastError ? <p className="strayt-muted">{lastError}</p> : null}
      </Card>

      <Card
        size="small"
        title="访问口令"
        style={{ marginTop: "var(--tok-spaceMd-px)" }}
        extra={
          <Button size="small" icon={<LockOutlined />} onClick={() => setPwModal(true)}>
            修改口令
          </Button>
        }
      >
        <p className="strayt-muted" style={{ marginTop: 0 }}>
          服务端只存 Argon2id 哈希，不存明文。改完口令会立刻让当前所有登录会话失效，
          本机会自动用新令牌续上，不必重新登录。忘了口令只能在服务端执行
          <code> uv run python -m app.scripts.set_password --reset</code>。
        </p>
      </Card>

      <Card
        size="small"
        title="API Key（模型调用）"
        style={{ marginTop: "var(--tok-spaceMd-px)" }}
        extra={
          <Button size="small" type="primary" onClick={() => setKeyModal(true)}>
            新增
          </Button>
        }
      >
        <p className="strayt-muted" style={{ marginTop: 0 }}>
          {STORAGE_DISCLOSURE}
        </p>
        <List
          size="small"
          dataSource={keys}
          locale={{ emptyText: "尚未配置 API Key" }}
          renderItem={(k) => {
            const spec = providers.find((p) => p.key === k.provider);
            return (
              <List.Item
                actions={[
                  <Button
                    key="test"
                    size="small"
                    loading={testing === k.id}
                    onClick={() => void testKey(k.id)}
                  >
                    测试连通
                  </Button>,
                  <Popconfirm
                    key="del"
                    title="删除此 Key？"
                    onConfirm={() => {
                      if (api) void api.domain.deleteApiKey(k.id).then(reloadKeys);
                    }}
                  >
                    <Button size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>,
                ]}
              >
                <span className="strayt-row">
                  <Tag>{spec?.display_name ?? k.provider}</Tag>
                  {k.base_url ? <Typography.Text code>{k.base_url}</Typography.Text> : null}
                  {k.label ? <span className="strayt-muted">{k.label}</span> : null}
                </span>
              </List.Item>
            );
          }}
        />
      </Card>

      <Card
        size="small"
        title="模型档案（文本 / 视觉分别指定）"
        style={{ marginTop: "var(--tok-spaceMd-px)" }}
        extra={
          <Button size="small" type="primary" onClick={() => setProfileModal(true)}>
            新建
          </Button>
        }
      >
        <p className="strayt-muted" style={{ marginTop: 0 }}>
          对齐/图谱默认取「文本模型」；扫描件 OCR 走「视觉模型」，留空则回落文本。
        </p>
        <List
          size="small"
          dataSource={profiles}
          locale={{ emptyText: "还没有模型档案（加工会退回规则对齐）" }}
          renderItem={(p) => (
            <List.Item
              actions={[
                <Popconfirm
                  key="del"
                  title="删除此档案？"
                  onConfirm={() => {
                    if (api) void api.domain.deleteProfile(p.id).then(reloadProfiles);
                  }}
                >
                  <Button size="small" danger>
                    删除
                  </Button>
                </Popconfirm>,
              ]}
            >
              <span className="strayt-row">
                <strong>{p.name}</strong>
                <span className="strayt-muted">
                  文本 {p.text_model ?? "默认"} · 视觉 {p.vision_model ?? "—"}
                </span>
              </span>
            </List.Item>
          )}
        />
      </Card>

      <Card size="small" title="背诵舱" style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        <p className="strayt-muted" style={{ marginTop: 0 }}>
          「逐句递进」的默认计时。到每个背诵单元前都可以临时改，这里只定默认值。
        </p>
        <div className="strayt-row">
          <Select
            size="small"
            value={reciteTimerSec}
            options={RECITE_TIMER_OPTIONS}
            onChange={(v) => void saveReciteTimer(v)}
            style={{ minWidth: 0 }}
          />
          <InputNumber
            size="small"
            min={RECITE_TIMER_MIN}
            max={RECITE_TIMER_MAX}
            step={30}
            value={reciteTimerSec}
            onChange={(v) => void saveReciteTimer(Number(v))}
          />
          <span className="strayt-muted">秒</span>
        </div>
      </Card>

      <Card size="small" title="数据" style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        <p className="strayt-muted" style={{ margin: 0 }}>
          篇目 {snapshot?.pieces.length ?? 0} · 对句 {snapshot?.pairs.length ?? 0} · 打卡{" "}
          {(snapshot?.checkins ?? []).length} 天 · 计划 {(snapshot?.plans ?? []).length} 条
        </p>
      </Card>

      <Modal
        open={keyModal}
        title="新增 API Key"
        onCancel={() => {
          setKeyModal(false);
          setKeyProvider(undefined);
        }}
        onOk={() => keyForm.submit()}
        confirmLoading={busy}
        destroyOnClose
      >
        <Form form={keyForm} layout="vertical" onFinish={(v) => void submitKey(v)}>
          <Form.Item label="厂商" name="provider" rules={[{ required: true, message: "请选择厂商" }]}>
            <Select
              showSearch
              options={providerOptions(providers)}
              placeholder="DeepSeek / 阿里千问 / Kimi / ..."
              onChange={(v) => setKeyProvider(v)}
              optionFilterProp="label"
            />
          </Form.Item>
          {(() => {
            const spec = providers.find((p) => p.key === keyProvider);
            if (!spec) return null;
            return (
              <>
                {spec.notes ? (
                  <p className="strayt-muted" style={{ marginTop: "calc(-1 * var(--tok-spaceSm-px))" }}>
                    {spec.notes}
                  </p>
                ) : null}
                {spec.console_url ? (
                  <p className="strayt-muted" style={{ marginTop: 0 }}>
                    还没申请 Key？
                    <a href={spec.console_url} target="_blank" rel="noreferrer">
                      去{spec.display_name}控制台
                      <LinkOutlined />
                    </a>
                  </p>
                ) : null}
                {spec.base_url_editable ? (
                  <Form.Item
                    label="Base URL"
                    name="base_url"
                    rules={[
                      { required: true, message: "自定义端点必须填地址" },
                      {
                        validator: (_, v) =>
                          !v || /^https?:\/\/.+/.test(v.trim())
                            ? Promise.resolve()
                            : Promise.reject(new Error("要以 http:// 或 https:// 开头")),
                      },
                    ]}
                    extra="要带 /v1。例：http://127.0.0.1:11434/v1（Ollama）、https://your.newapi.site/v1"
                  >
                    <Input placeholder="http://127.0.0.1:11434/v1" autoComplete="off" />
                  </Form.Item>
                ) : null}
              </>
            );
          })()}
          <Form.Item label="Key" name="secret" rules={[{ required: true, message: "请输入 Key" }]}>
            <Input.Password autoComplete="off" placeholder="sk-…" />
          </Form.Item>
          <Form.Item label="备注" name="label">
            <Input maxLength={60} placeholder="如：主力 / 便宜那把" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        open={profileModal}
        title="新建模型档案"
        onCancel={() => setProfileModal(false)}
        onOk={() => profileForm.submit()}
        confirmLoading={busy}
        destroyOnClose
      >
        <Form form={profileForm} layout="vertical" onFinish={(v) => void submitProfile(v)}>
          <Form.Item label="名称" name="name" rules={[{ required: true, message: "请输入名称" }]}>
            <Input maxLength={64} placeholder="如：主力（便宜）" />
          </Form.Item>
          <Form.Item label="文本 Key" name="text_key_id">
            <Select
              allowClear
              placeholder="留空 = 任意可用 Key"
              options={keyOptions}
              optionFilterProp="label"
              onChange={(v) => {
                setTextKeyId(v);
                setTextKeyProvider(keys.find((k) => k.id === v)?.provider);
                // 换了 Key 就清掉上一个厂商的模型名，否则会存下一个不存在的组合
                profileForm.setFieldValue("text_model", undefined);
              }}
            />
          </Form.Item>
          <Form.Item label="文本模型" name="text_model">
            {/* AutoComplete 而非 Select：模型清单是注册表里的**快照**，厂商天天
                上新/改名，纯下拉会把用户挡在外面；AutoComplete 给候选又能手输。 */}
            <AutoComplete
              allowClear
              options={modelOptions(providers, textKeyProvider, "text")}
              placeholder={
                textKeyProvider ? "留空 = 该厂商默认模型" : "先选 Key 才能给候选，也可直接手输"
              }
              filterOption={(input, opt) =>
                String(opt?.label ?? "")
                  .toLowerCase()
                  .includes(input.toLowerCase())
              }
            />
          </Form.Item>
          <Form.Item label="视觉 Key（扫描件 OCR）" name="vision_key_id">
            <Select
              allowClear
              placeholder="留空 = 用文本 Key"
              options={keyOptions}
              optionFilterProp="label"
              onChange={(v) => {
                setVisionKeyId(v);
                setVisionKeyProvider(keys.find((k) => k.id === v)?.provider);
                profileForm.setFieldValue("vision_model", undefined);
              }}
            />
          </Form.Item>
          <Form.Item label="视觉模型" name="vision_model">
            <AutoComplete
              allowClear
              options={modelOptions(providers, visionKeyProvider, "vision")}
              placeholder={
                visionKeyProvider ? "留空 = 该厂商默认模型" : "先选 Key 才能给候选，也可直接手输"
              }
              filterOption={(input, opt) =>
                String(opt?.label ?? "")
                  .toLowerCase()
                  .includes(input.toLowerCase())
              }
            />
          </Form.Item>
          {textKeyId && visionKeyId && textKeyId !== visionKeyId ? (
            <p className="strayt-muted" style={{ marginTop: 0 }}>
              文本与视觉用了不同的 Key —— 加工时按各自通道分别取，正常。
            </p>
          ) : null}
        </Form>
      </Modal>

      <Modal
        title="修改访问口令"
        open={pwModal}
        onCancel={() => setPwModal(false)}
        onOk={() => pwForm.submit()}
        okText="保存"
        cancelText="取消"
        confirmLoading={pwBusy}
        destroyOnClose
      >
        <Form
          form={pwForm}
          layout="vertical"
          onFinish={async (v) => {
            setPwBusy(true);
            try {
              await changePassword(v.old_password, v.new_password);
              setPwModal(false);
              pwForm.resetFields();
              message.success("口令已修改，其他设备上的登录已失效");
            } catch (err) {
              message.error(err instanceof Error ? err.message : String(err));
            } finally {
              setPwBusy(false);
            }
          }}
        >
          <Form.Item
            label="当前口令"
            name="old_password"
            rules={[{ required: true, message: "请输入当前口令" }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item
            label="新口令"
            name="new_password"
            rules={[
              { required: true, message: "请输入新口令" },
              { min: 8, message: "口令至少 8 位" },
            ]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            label="再输一次"
            name="confirm"
            dependencies={["new_password"]}
            rules={[
              { required: true, message: "请再输入一次新口令" },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  if (!value || getFieldValue("new_password") === value) return Promise.resolve();
                  return Promise.reject(new Error("两次输入不一致"));
                },
              }),
            ]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}