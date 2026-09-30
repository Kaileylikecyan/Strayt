import { useEffect, useState } from "react";
import { Button, Card, Form, Input, List, message, Modal, Popconfirm, Segmented, Select, Tag } from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import { useStraytTheme, MasteryDot, type MasteryLevel } from "@strayt/ui";
import type { ModelProfileOut } from "@strayt/api-client";
import { useApp } from "../store";

const STORAGE_DISCLOSURE =
  "API Key 将加密存储在你自己的服务器上（不留在客户端）。仅服务端在加工/对齐时调用对应模型厂商。";

export function SettingsPage() {
  const { config, api, snapshot, syncState, pending, disconnect, flushNow, refresh, lastError } = useApp();
  const { themeName, setThemeName } = useStraytTheme();

  const [keys, setKeys] = useState<Array<{ id: string; provider: string; label?: string | null }>>([]);
  const [providers, setProviders] = useState<string[]>([]);
  const [keyForm] = Form.useForm<{ provider: string; secret: string; label?: string }>();
  const [keyModal, setKeyModal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<ModelProfileOut[]>([]);
  const [profileModal, setProfileModal] = useState(false);
  const [profileForm] = Form.useForm<{
    name: string;
    text_key_id?: string;
    text_model?: string;
    vision_key_id?: string;
    vision_model?: string;
  }>();

  const reloadKeys = async () => {
    if (!api) return;
    try {
      const list = await api.domain.listApiKeys();
      setKeys(
        list.map((k) => ({
          id: k.id,
          provider: k.provider,
          label: (k as { label?: string | null }).label ?? null,
        })),
      );
      const prov = await api.domain.listProviders();
      setProviders((prov as Array<{ provider: string }>).map((p) => p.provider));
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

  useEffect(() => {
    void reloadKeys();
    void reloadProfiles();
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

  const submitKey = async (values: { provider: string; secret: string; label?: string }) => {
    if (!api) return;
    setBusy(true);
    try {
      await api.domain.createApiKey(values);
      setKeyModal(false);
      keyForm.resetFields();
      await reloadKeys();
    } catch (err) {
      void err;
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
          renderItem={(k) => (
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
              <Tag>{k.provider}</Tag>
              {k.label}
            </List.Item>
          )}
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

      <Card size="small" title="数据" style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        <p className="strayt-muted" style={{ margin: 0 }}>
          篇目 {snapshot?.pieces.length ?? 0} · 对句 {snapshot?.pairs.length ?? 0} · 打卡{" "}
          {(snapshot?.checkins ?? []).length} 天 · 计划 {(snapshot?.plans ?? []).length} 条
        </p>
      </Card>

      <Modal
        open={keyModal}
        title="新增 API Key"
        onCancel={() => setKeyModal(false)}
        onOk={() => keyForm.submit()}
        confirmLoading={busy}
        destroyOnClose
      >
        <Form form={keyForm} layout="vertical" onFinish={(v) => void submitKey(v)}>
          <Form.Item
            label="厂商"
            name="provider"
            rules={[{ required: true, message: "请选择或输入厂商" }]}
          >
            {providers.length > 0 ? (
              <Select
                showSearch
                options={providers.map((p) => ({ value: p, label: p }))}
                placeholder="openai / deepseek / ..."
              />
            ) : (
              <Input placeholder="openai / deepseek / ..." autoComplete="off" />
            )}
          </Form.Item>
          <Form.Item label="Key" name="secret" rules={[{ required: true, message: "请输入 Key" }]}>
            <Input.Password autoComplete="off" />
          </Form.Item>
          <Form.Item label="备注" name="label">
            <Input maxLength={60} />
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
              options={keys.map((k) => ({ value: k.id, label: `${k.provider}${k.label ? ` · ${k.label}` : ""}` }))}
            />
          </Form.Item>
          <Form.Item label="文本模型" name="text_model">
            <Input placeholder="留空 = 该厂商默认模型" />
          </Form.Item>
          <Form.Item label="视觉 Key（扫描件 OCR）" name="vision_key_id">
            <Select
              allowClear
              placeholder="留空 = 用文本 Key"
              options={keys.map((k) => ({ value: k.id, label: `${k.provider}${k.label ? ` · ${k.label}` : ""}` }))}
            />
          </Form.Item>
          <Form.Item label="视觉模型" name="vision_model">
            <Input placeholder="留空 = 该厂商默认模型" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}