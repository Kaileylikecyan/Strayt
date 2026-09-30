import { useMemo, useState } from "react";
import { Button, Card, Form, Input, Modal, Popconfirm, Radio } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { ProjectTypeTag } from "@strayt/ui";
import { useApp } from "../store";
import type { SyncSnapshot } from "@strayt/sync-engine";

function pieceStats(snapshot: SyncSnapshot | null, projectId: string) {
  const pieces = (snapshot?.pieces ?? []).filter((p) => p.project_id === projectId);
  const recited = pieces.filter((p) => p.recited).length;
  const lastPosTotal = pieces.reduce((acc, p) => acc + p.last_pos, 0);
  const studied = pieces.filter((p) => p.recited || p.last_pos > 0);
  const lastStudyAt = studied.reduce<string | null>(
    (acc, p) => (acc === null || p.updated_at > acc ? p.updated_at : acc),
    null,
  );
  return { total: pieces.length, recited, avgPos: pieces.length ? Math.round(lastPosTotal / pieces.length) : 0, lastStudyAt };
}

function relTime(iso: string | null): string {
  if (!iso) return "还没开始";
  const t = dayjs(iso);
  const mins = dayjs().diff(t, "minute");
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins} 分钟前`;
  const hours = dayjs().diff(t, "hour");
  if (hours < 24) return `${hours} 小时前`;
  const days = dayjs().diff(t, "day");
  if (days < 30) return `${days} 天前`;
  return t.format("YYYY-MM-DD");
}

export function Projects({
  onOpenRecite,
  onOpenGraph,
}: {
  onOpenRecite: (projectId: string) => void;
  onOpenGraph: (projectId: string) => void;
}) {
  const { snapshot, domain, refresh, lastError, clearError } = useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const [modalType, setModalType] = useState<"create" | "rename">("create");
  const [targetId, setTargetId] = useState<string | null>(null);
  const [form] = Form.useForm<{ name: string; type: "recite" | "graph" }>();
  const [busy, setBusy] = useState(false);

  const projects = useMemo(() => [...(snapshot?.projects ?? [])], [snapshot]);

  if (!domain) return null;

  const openModal = (type: "create" | "rename", id?: string) => {
    setModalType(type);
    setTargetId(id ?? null);
    form.resetFields();
    if (type === "rename" && id) {
      const p = snapshot?.projects.find((x) => x.id === id);
      form.setFieldsValue({ name: p?.name ?? "" });
    } else {
      form.setFieldsValue({ name: "", type: "recite" });
    }
    setModalOpen(true);
  };

  const submit = async (values: { name: string; type?: "recite" | "graph" }) => {
    setBusy(true);
    clearError();
    try {
      if (modalType === "create") {
        await domain.createProject({ name: values.name, type: values.type ?? "recite" });
      } else if (modalType === "rename" && targetId) {
        await domain.renameProject(targetId, values.name);
      }
      setModalOpen(false);
      await refresh();
    } catch (err) {
      void err;
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    try {
      await domain.deleteProject(id);
      await refresh();
    } catch (err) {
      void err;
    }
  };

  const empty = projects.length === 0;

  return (
    <div>
      <div className="strayt-row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>项目列表</h3>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => openModal("create")}>
          新建项目
        </Button>
      </div>
      {lastError ? <p className="strayt-muted">{lastError}</p> : null}

      {empty ? (
        <Card style={{ marginTop: "var(--tok-spaceMd-px)" }}>
          还没有项目。点右上角「新建项目」，命名后选择类型，然后到桌面端上传资料加工。
        </Card>
      ) : (
        projects.map((p) => {
          const stats = pieceStats(snapshot, p.id);
          return (
            <Card
              key={p.id}
              size="small"
              style={{ marginTop: "var(--tok-spaceMd-px)" }}
              title={
                <span className="strayt-row">
                  {p.name}
                  <ProjectTypeTag type={p.type} />
                </span>
              }
              extra={
                <span className="strayt-row">
                  <Button size="small" onClick={() => openModal("rename", p.id)}>
                    改名
                  </Button>
                  <Popconfirm
                    title="删除项目？"
                    description="将移入服务端回收站，资料与进度不会立即丢失。"
                    onConfirm={() => void remove(p.id)}
                  >
                    <Button size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </span>
              }
            >
              <p className="strayt-muted" style={{ marginTop: 0 }}>
                {dayjs(p.created_at).format("YYYY-MM-DD")} 创建
                {p.type === "recite" ? ` · 篇目 ${stats.total} · 已背 ${stats.recited}` : ""}
                {` · 最近学习 ${relTime(stats.lastStudyAt)}`}
              </p>
              {p.type === "recite" ? (
                <Button type="primary" size="small" onClick={() => onOpenRecite(p.id)}>
                  进入背诵舱
                </Button>
              ) : (
                <Button type="primary" size="small" onClick={() => onOpenGraph(p.id)}>
                  进入知识图谱
                </Button>
              )}
            </Card>
          );
        })
      )}

      <Modal
        open={modalOpen}
        title={modalType === "create" ? "新建项目" : "重命名"}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={busy}
        destroyOnClose
      >
        <Form form={form} layout="vertical" onFinish={(v) => void submit(v)}>
          <Form.Item label="名称" name="name" rules={[{ required: true, message: "请输入名称" }]}>
            <Input maxLength={120} placeholder="如：导游证考试 · 上海导游词" />
          </Form.Item>
          {modalType === "create" ? (
            <Form.Item label="类型" name="type">
              <Radio.Group optionType="button" buttonStyle="solid">
                <Radio value="recite">背诵型</Radio>
                <Radio value="graph">图谱型</Radio>
              </Radio.Group>
            </Form.Item>
          ) : null}
          {modalType === "create" ? (
            <p className="strayt-muted">
              背诵型 = 中英对齐 + 背诵舱；图谱型 = 概念图谱（加工需配置文本模型 API Key）。类型创建后不可改。
            </p>
          ) : null}
        </Form>
      </Modal>
    </div>
  );
}