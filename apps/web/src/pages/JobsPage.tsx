import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Badge, Button, Card, Empty, Modal, Progress, Select, Spin, Tag, message } from "antd";
import {
  ArrowRightOutlined,
  ExclamationCircleOutlined,
  PauseCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import type { FileOut, JobOut } from "@strayt/api-client";
import { useApp } from "../store";

const JOB_TYPE_LABEL: Record<string, string> = {
  recite_align: "背诵对齐",
  graph_extract: "图谱抽取",
};

const STATUS_TAG: Record<string, { color: string; label: string }> = {
  queued: { color: "var(--tok-colorInfo)", label: "排队中" },
  running: { color: "var(--tok-colorPrimary)", label: "加工中" },
  success: { color: "var(--tok-colorSuccess)", label: "成功" },
  failed: { color: "var(--tok-colorError)", label: "失败" },
  cancelled: { color: "", label: "已取消" },
  interrupted: { color: "var(--tok-colorWarning)", label: "已中断" },
};

const POLL_MS = 2500;

export function JobsPage({ prefillProjectId }: { prefillProjectId?: string }) {
  const { domain, lastError, clearError } = useApp();
  const { snapshot } = useApp();
  const projects = useMemo(() => [...(snapshot?.projects ?? [])], [snapshot]);
  const [projectFilter, setProjectFilter] = useState<string | undefined>(undefined);
  const [jobs, setJobs] = useState<JobOut[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);

  useEffect(() => {
    if (prefillProjectId) setProjectFilter(prefillProjectId);
  }, [prefillProjectId]);

  const hasActive = useMemo(() => jobs?.some((j) => j.status === "queued" || j.status === "running") ?? false, [jobs]);

  const load = useCallback(async () => {
    if (!domain) return;
    try {
      setJobs(await domain.listJobs({ projectId: projectFilter ?? undefined, limit: 100 }));
    } catch (err) {
      void err;
    }
  }, [domain, projectFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!hasActive) return;
    const t = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(t);
  }, [hasActive, load]);

  if (!domain) return null;

  const refresh = () => {
    clearError();
    void load();
  };

  const cancel = async (id: string) => {
    setBusy(true);
    try {
      await domain.cancelJob(id);
      message.success("已取消");
      await load();
    } catch (err) {
      message.error(`取消失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const resume = async (id: string) => {
    setBusy(true);
    try {
      await domain.resumeJob(id);
      message.success("已放入队列续跑");
      await load();
    } catch (err) {
      message.error(`续跑失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  // F8：interrupted 是「已花的比预估多，停下来问你」。续跑 = 同意继续花钱，
  // 所以这里必须先摆出预估与实际让人确认，不能一点就续。
  const confirmResume = (j: JobOut) => {
    const est = (j.cost_estimate_json?.estimated_cny as number | undefined) ?? 0;
    Modal.confirm({
      title: "继续这次加工？",
      icon: <ExclamationCircleOutlined />,
      okText: "确认继续",
      cancelText: "先别跑",
      content: (
        <div className="strayt-muted">
          <div>已完成 {j.done_units}/{j.total_units} 篇，续跑会从断点继续，不重跑已完成的篇目。</div>
          <div style={{ marginTop: "var(--tok-spaceSm-px)" }}>
            预估总额 ¥{est.toFixed(2)}
            {est > 0 ? "（估算，按牌价；实际可能更高）" : " · 走的是不花钱的规则通道"}
          </div>
        </div>
      ),
      onOk: () => resume(j.id),
    });
  };

  return (
    <div>
      <div className="strayt-row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>加工任务</h3>
        <div className="strayt-row">
          <Select
            allowClear
            placeholder="全部项目"
            value={projectFilter}
            style={{ width: "calc(var(--tok-spaceLg-px) * 8)" }}
            onChange={(v) => setProjectFilter(v)}
            options={projects.map((p) => ({ value: p.id, label: p.name }))}
          />
          <Button icon={<ReloadOutlined />} onClick={refresh} />
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
            新建任务
          </Button>
        </div>
      </div>

      {lastError ? <Alert type="error" showIcon message={lastError} style={{ marginTop: "var(--tok-spaceMd-px)" }} /> : null}

      <div style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        {jobs === null ? (
          <div style={{ textAlign: "center", padding: "var(--tok-spaceXl-px)" }}>
            <Spin />
          </div>
        ) : jobs.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有加工任务">
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>
              上传资料后创建
            </Button>
          </Empty>
        ) : (
          jobs.map((j) => {
            const st = STATUS_TAG[j.status] ?? { color: "", label: j.status };
            const est = j.cost_estimate_json?.estimated_cny as number | undefined;
            const interrupted = j.status === "interrupted";
            return (
              <Card key={j.id} size="small" style={{ marginTop: "var(--tok-spaceMd-px)" }}>
                <div className="strayt-row" style={{ justifyContent: "space-between" }}>
                  <span className="strayt-row">
                    <ThunderboltOutlined />
                    <span>{JOB_TYPE_LABEL[j.type] ?? j.type}</span>
                    <Tag color={st.color}>{st.label}</Tag>
                    <Badge status={interrupted ? "warning" : "default"} />
                  </span>
                  <span className="strayt-muted">{dayjs(j.created_at).format("YYYY-MM-DD HH:mm")}</span>
                </div>
                <Progress
                  percent={j.progress}
                  size="small"
                  status={j.status === "failed" || interrupted ? "exception" : "normal"}
                  style={{ marginTop: "var(--tok-spaceMd-px)" }}
                />
                <div className="strayt-row" style={{ justifyContent: "space-between" }}>
                  <span className="strayt-muted">
                    {j.done_units}/{j.total_units} 篇
                    {est && est > 0 ? ` · 预估 ¥${est.toFixed(2)}` : " · 规则通道（B），不花钱"}
                  </span>
                  <span className="strayt-row">
                    {(j.status === "queued" || j.status === "running") && (
                      <Button size="small" icon={<PauseCircleOutlined />} onClick={() => void cancel(j.id)} disabled={busy}>
                        取消
                      </Button>
                    )}
                    {j.status === "failed" && (
                      <Button size="small" type="primary" icon={<ReloadOutlined />} onClick={() => void resume(j.id)} disabled={busy}>
                        重试
                      </Button>
                    )}
                    {interrupted && (
                      <Button size="small" type="primary" icon={<ArrowRightOutlined />} onClick={() => confirmResume(j)} disabled={busy}>
                        续跑
                      </Button>
                    )}
                  </span>
                </div>
                {interrupted && j.error ? (
                  <Alert
                    type="warning"
                    showIcon
                    icon={<ExclamationCircleOutlined />}
                    message={j.error}
                    style={{ marginTop: "var(--tok-spaceMd-px)" }}
                  />
                ) : j.status === "failed" && j.error ? (
                  <Alert type="error" showIcon message={j.error} style={{ marginTop: "var(--tok-spaceMd-px)" }} />
                ) : null}
              </Card>
            );
          })
        )}
      </div>

      <CreateJobModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        domain={domain}
        onCreated={() => {
          setCreateOpen(false);
          void load();
        }}
      />
    </div>
  );
}

function useAppSnapshotProjects() {
  const { snapshot } = useApp();
  return useMemo(() => [...(snapshot?.projects ?? [])], [snapshot]);
}

function CreateJobModal(props: {
  open: boolean;
  onClose: () => void;
  domain: NonNullable<ReturnType<typeof useApp>["domain"]>;
  onCreated: () => void;
}) {
  const { open, onClose, domain, onCreated } = props;
  const projects = useAppSnapshotProjects();
  const [projectId, setProjectId] = useState<string | undefined>(undefined);
  const [fileId, setFileId] = useState<string | undefined>(undefined);
  const [submitting, setSubmitting] = useState(false);
  const [internalFiles, setInternalFiles] = useState<FileOut[] | null>(null);

  useEffect(() => {
    if (!open) return;
    setProjectId(undefined);
    setFileId(undefined);
    setInternalFiles(null);
  }, [open]);

  useEffect(() => {
    if (!projectId) return;
    void domain.listFiles(projectId).then((f) => setInternalFiles(f));
  }, [projectId, domain]);

  const project = projects.find((p) => p.id === projectId);
  const jobType = project?.type === "graph" ? "graph_extract" : "recite_align";
  const allowed = (f: FileOut) => (jobType === "recite_align" ? true : f.parse_channel === "text" || f.parse_channel === "vision");

  const submit = async () => {
    if (!projectId || !fileId) return;
    setSubmitting(true);
    try {
      await domain.createJob({
        project_id: projectId,
        file_id: fileId,
        type: jobType,
        overwrite: false,
      });
      message.success("任务已创建，正在排队");
      onCreated();
    } catch (err) {
      message.error(`创建失败：${(err as Error).message}`);
    } finally {
      setSubmitting(false);
    }
  };

  const typeLabel = jobType === "graph_extract" ? "图谱抽取" : "背诵对齐";

  return (
    <Modal
      open={open}
      title="新建加工任务"
      onCancel={onClose}
      onOk={() => void submit()}
      okText={`创建 ${typeLabel}`}
      confirmLoading={submitting}
    >
      <div className="strayt-col">
        <label>项目（决定任务类型：背诵型 → 对齐，图谱型 → 图谱抽取）</label>
        <Select
          value={projectId}
          placeholder="选择项目"
          style={{ width: "100%" }}
          options={projects.map((p) => ({ value: p.id, label: `${p.name}（${p.type === "graph" ? "图谱" : "背诵"}）` }))}
          onChange={(v) => setProjectId(v)}
        />
        <label style={{ marginTop: "var(--tok-spaceMd-px)" }}>资料</label>
        <Select
          value={fileId}
          placeholder={internalFiles ? undefined : "先选项目加载资料"}
          style={{ width: "100%" }}
          loading={internalFiles === null && !!projectId}
          options={(internalFiles ?? [])
            .filter(allowed)
            .map((f) => ({ value: f.id, label: `${f.orig_name}（${f.parse_channel} / ${f.size} B）` }))}
          onChange={(v) => setFileId(v)}
        />
        <p className="strayt-muted" style={{ marginTop: "var(--tok-spaceMd-px)", marginBottom: 0 }}>
          无 API Key 时自动走规则对齐（通道 B），全程不花钱。
        </p>
        {jobType === "graph_extract" && <p className="strayt-muted">图谱抽取需要配置文本模型 API Key 才能运行。</p>}
      </div>
    </Modal>
  );
}