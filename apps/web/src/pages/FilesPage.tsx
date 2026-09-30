import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Card, Empty, Popconfirm, Progress, Select, Tag } from "antd";
import {
  CloudUploadOutlined,
  DeleteOutlined,
  FileTextOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import type { FileOut } from "@strayt/api-client";
import { useApp } from "../store";
import { uploadFileSmart } from "../upload";

interface UploadJob {
  name: string;
  loaded: number;
  total: number;
  state: "uploading" | "done" | "error" | "aborted";
  msg?: string;
}

function fmtBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

export function FilesPage({ onOpenJobs }: { onOpenJobs: (projectId?: string) => void }) {
  const { snapshot, domain, lastError, clearError } = useApp();
  const [projectId, setProjectId] = useState<string | null>(null);
  const [files, setFiles] = useState<FileOut[] | null>(null);
  const [jobs, setJobs] = useState<Record<string, UploadJob>>({});
  const [inputRef, setInputRef] = useState<HTMLInputElement | null>(null);

  const projects = useMemo(() => [...(snapshot?.projects ?? [])], [snapshot]);

  const loadFiles = useCallback(
    async (pid: string) => {
      if (!domain) return;
      try {
        setFiles(await domain.listFiles(pid));
      } catch (err) {
        void err;
      }
    },
    [domain],
  );

  useEffect(() => {
    if (projectId) void loadFiles(projectId);
    else setFiles(null);
  }, [projectId, loadFiles]);

  if (!domain) return null;

  const pickFiles = async (list: FileList | null) => {
    if (!list || !projectId || list.length === 0) return;
    for (const f of Array.from(list)) {
      const key = `${Date.now()}-${f.name}`;
      setJobs((prev) => ({ ...prev, [key]: { name: f.name, loaded: 0, total: f.size, state: "uploading" } }));
      try {
        await uploadFileSmart(domain, projectId, f, (loaded, total) =>
          setJobs((prev) => ({ ...prev, [key]: { name: f.name, loaded, total, state: "uploading" } })),
        );
        setJobs((prev) => ({ ...prev, [key]: { name: f.name, loaded: f.size, total: f.size, state: "done" } }));
        await loadFiles(projectId);
      } catch (err) {
        setJobs((prev) => ({
          ...prev,
          [key]: { name: f.name, loaded: 0, total: f.size, state: "error", msg: String((err as Error).message ?? err) },
        }));
      }
    }
  };

  const remove = async (fid: string) => {
    clearError();
    try {
      await domain.deleteFile(fid);
      if (projectId) await loadFiles(projectId);
    } catch (err) {
      void err;
    }
  };

  const running = Object.values(jobs).some((j) => j.state === "uploading");

  return (
    <div>
      <div className="strayt-row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>资料上传</h3>
        <span className="strayt-muted">支持 pdf / docx / txt / md，≤50MB 直传，更大自动分块续传</span>
      </div>

      <Card size="small" style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        <div className="strayt-row">
          <Select
            style={{ minWidth: "calc(var(--tok-spaceLg-px) * 8)" }}
            placeholder="选择项目"
            value={projectId ?? undefined}
            options={projects.map((p) => ({ value: p.id, label: p.name }))}
            onChange={(v) => setProjectId(v)}
          />
          <Button
            type="primary"
            icon={<CloudUploadOutlined />}
            disabled={!projectId}
            onClick={() => inputRef?.click()}
          >
            选择文件上传
          </Button>
          <input
            ref={setInputRef}
            type="file"
            multiple
            hidden
            accept=".pdf,.docx,.txt,.md"
            onChange={(e) => {
              const list = e.target.files;
              void pickFiles(list);
              e.target.value = "";
            }}
          />
        </div>
        {projectId ? (
          <p className="strayt-muted" style={{ marginTop: "var(--tok-spaceSm-px)", marginBottom: 0 }}>
            上传到「{projects.find((p) => p.id === projectId)?.name}」——涉及页面逻辑 id 展示
          </p>
        ) : null}
      </Card>

      {Object.entries(jobs).length > 0 ? (
        <Card size="small" title="上传进度" style={{ marginTop: "var(--tok-spaceMd-px)" }}>
          {Object.entries(jobs).map(([k, j]) => (
            <div key={k} style={{ marginBottom: "var(--tok-spaceSm-px)" }}>
              <div className="strayt-row">
                <FileTextOutlined />
                <span>{j.name}</span>
                {j.state === "done" ? <Tag color="var(--tok-colorSuccess)">完成</Tag> : null}
                {j.state === "error" ? <Tag color="var(--tok-colorError)">失败</Tag> : null}
                {j.state === "aborted" ? <Tag>已取消</Tag> : null}
              </div>
              {j.state === "uploading" ? (
                <Progress percent={j.total ? Math.round((j.loaded / j.total) * 100) : 0} size="small" />
              ) : null}
              {j.msg ? <p className="strayt-muted" style={{ margin: 0 }}>{j.msg}</p> : null}
            </div>
          ))}
        </Card>
      ) : null}

      <Card
        size="small"
        title="项目资料"
        style={{ marginTop: "var(--tok-spaceMd-px)" }}
        extra={
          <Button size="small" type="primary" icon={<ThunderboltOutlined />} onClick={() => onOpenJobs(projectId ?? undefined)}>
            去加工
          </Button>
        }
      >
        {lastError ? <p className="strayt-muted">{lastError}</p> : null}
        {!projectId ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="先选一个项目查看资料" />
        ) : files === null ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该项目还没有资料" />
        ) : files.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该项目还没有资料" />
        ) : (
          files.map((f) => (
            <div key={f.id} className="strayt-row" style={{ justifyContent: "space-between", width: "100%" }}>
              <span className="strayt-row">
                <FileTextOutlined />
                <span>{f.orig_name}</span>
                <span className="strayt-muted">
                  {fmtBytes(f.size)} · {f.parse_channel}
                  {f.page_count != null ? ` · ${f.page_count} 页` : ""}
                </span>
                <span className="strayt-muted">{dayjs(f.uploaded_at).format("YYYY-MM-DD HH:mm")}</span>
              </span>
              <Popconfirm title="删除资料？" description="文件移入服务端回收站，可后续恢复。" onConfirm={() => void remove(f.id)}>
                <Button size="small" danger icon={<DeleteOutlined />} disabled={running} />
              </Popconfirm>
            </div>
          ))
        )}
      </Card>
    </div>
  );
}