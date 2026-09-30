import type { StraytApiClient } from "./client";
import type { OpResult, SyncSnapshot, WriteOp } from "@strayt/sync-engine";
import type {
  ApiKeyOut,
  ApiKeyProbeIn,
  CheckinOut,
  DailyTodos,
  FileOut,
  FlashcardGenOut,
  FlashcardOut,
  FlashcardResultOut,
  GoalOut,
  GraphConfirmIn,
  GraphConfirmOut,
  JobCreate,
  JobOut,
  MasteryStatsOut,
  ModelProfileIn,
  ModelProfileOut,
  NodeMasteryOut,
  PairOut,
  PieceOut,
  PlanListOut,
  ProjectOut,
  UploadSessionCreate,
  UploadSessionOut,
} from "./client";

export type {
  ProjectOut,
  PieceOut,
  PairOut,
  CheckinOut,
  ApiKeyOut,
  ApiKeyProbeIn,
  PlanListOut,
  GoalOut,
  DailyTodos,
  FileOut,
  JobOut,
  JobCreate,
  UploadSessionOut,
  UploadSessionCreate,
  ModelProfileOut,
  ModelProfileIn,
  GraphConfirmIn,
  GraphConfirmOut,
  MasteryStatsOut,
  NodeMasteryOut,
  FlashcardOut,
  FlashcardGenOut,
  FlashcardResultOut,
};

// ---------------------------------------------------------------------------
// 项目（F4/F5/F22 目标）
// ---------------------------------------------------------------------------
export async function listProjects(client: StraytApiClient): Promise<ProjectOut[]> {
  return client.request<ProjectOut[]>("/api/v1/projects");
}

export async function createProject(
  client: StraytApiClient,
  body: { name: string; type: "graph" | "recite" },
): Promise<ProjectOut> {
  return client.request<ProjectOut>("/api/v1/projects", { method: "POST", body });
}

export async function renameProject(
  client: StraytApiClient,
  projectId: string,
  name: string,
): Promise<ProjectOut> {
  return client.request<ProjectOut>(`/api/v1/projects/${projectId}`, {
    method: "PATCH",
    query: { name },
  });
}

export async function deleteProject(client: StraytApiClient, projectId: string): Promise<void> {
  await client.request<void>(`/api/v1/projects/${projectId}`, { method: "DELETE" });
}

export async function getGoal(
  client: StraytApiClient,
  projectId: string,
): Promise<GoalOut> {
  return client.request<GoalOut>(`/api/v1/projects/${projectId}/goal`);
}

// ---------------------------------------------------------------------------
// 背诵舱（F19/F20）
// ---------------------------------------------------------------------------
export async function listPieces(
  client: StraytApiClient,
  projectId: string,
): Promise<PieceOut[]> {
  return client.request<PieceOut[]>(`/api/v1/projects/${projectId}/pieces`);
}

export async function listPairs(
  client: StraytApiClient,
  projectId: string,
  pieceId: string,
  fromSeq = 0,
  limit = 500,
): Promise<PairOut[]> {
  return client.request<PairOut[]>(
    `/api/v1/projects/${projectId}/pieces/${pieceId}/pairs`,
    { query: { from_seq: fromSeq, limit } },
  );
}

export async function putPieceProgress(
  client: StraytApiClient,
  projectId: string,
  pieceId: string,
  body: { last_pos: number; recited?: boolean; client_ts: string },
): Promise<PieceOut> {
  return client.request<PieceOut>(
    `/api/v1/projects/${projectId}/pieces/${pieceId}/progress`,
    { method: "PUT", body },
  );
}

// ---------------------------------------------------------------------------
// 打卡 / 计划（F22/F23）
// ---------------------------------------------------------------------------
export async function getCheckin(client: StraytApiClient, day: string): Promise<CheckinOut> {
  return client.request<CheckinOut>(`/api/v1/checkins/${day}`);
}

export async function putCheckin(
  client: StraytApiClient,
  body: { date: string; items: string[] },
): Promise<CheckinOut> {
  return client.request<CheckinOut>("/api/v1/checkins", { method: "PUT", body });
}

export async function listPlans(
  client: StraytApiClient,
  projectId?: string,
): Promise<PlanListOut[]> {
  return client.request<PlanListOut[]>("/api/v1/plans", {
    query: { project_id: projectId ?? undefined },
  });
}

export async function dailyTodos(client: StraytApiClient, day?: string): Promise<DailyTodos> {
  return client.request<DailyTodos>("/api/v1/plans/daily", { query: { day } });
}

// ---------------------------------------------------------------------------
// 设置（F6/F7）
// ---------------------------------------------------------------------------
export async function listApiKeys(client: StraytApiClient): Promise<ApiKeyOut[]> {
  return client.request<ApiKeyOut[]>("/api/v1/settings/api-keys");
}

export async function createApiKey(
  client: StraytApiClient,
  body: { provider: string; secret: string; label?: string },
): Promise<ApiKeyOut> {
  return client.request<ApiKeyOut>("/api/v1/settings/api-keys", { method: "POST", body });
}

export async function testApiKey(client: StraytApiClient, keyId: string): Promise<ApiKeyOut> {
  return client.request<ApiKeyOut>(`/api/v1/settings/api-keys/${keyId}/test`, { method: "POST" });
}

export async function deleteApiKey(client: StraytApiClient, keyId: string): Promise<void> {
  await client.request<void>(`/api/v1/settings/api-keys/${keyId}`, { method: "DELETE" });
}

export async function getKv(
  client: StraytApiClient,
  key: string,
): Promise<{ key: string; value: string | null; updated_at?: string | null }> {
  return client.request(`/api/v1/settings/kv/${key}`);
}

export async function putKv(
  client: StraytApiClient,
  key: string,
  value: string,
): Promise<{ key: string; value: string | null; updated_at?: string | null }> {
  return client.request(`/api/v1/settings/kv/${key}`, { method: "PUT", body: { value } });
}

export async function listProviders(client: StraytApiClient): Promise<unknown[]> {
  return client.request<unknown[]>("/api/v1/settings/providers");
}

// ---------------------------------------------------------------------------
// 资料上传（F9/F10）
// ---------------------------------------------------------------------------
export async function listFiles(
  client: StraytApiClient,
  projectId: string,
): Promise<FileOut[]> {
  return client.request<FileOut[]>("/api/v1/files", {
    query: { project_id: projectId },
  });
}

export async function uploadFile(
  client: StraytApiClient,
  projectId: string,
  file: Blob,
  filename: string,
): Promise<FileOut> {
  return client.upload<FileOut>(`/api/v1/files?project_id=${encodeURIComponent(projectId)}`, file, filename);
}

export async function deleteFile(client: StraytApiClient, fileId: string): Promise<void> {
  await client.raw<void>(`/api/v1/files/${fileId}`, "DELETE");
}

export async function createUploadSession(
  client: StraytApiClient,
  projectId: string,
  body: UploadSessionCreate,
): Promise<UploadSessionOut> {
  return client.request<UploadSessionOut>("/api/v1/upload-sessions", {
    method: "POST",
    query: { project_id: projectId },
    body,
  });
}

export async function getUploadSession(
  client: StraytApiClient,
  sessionId: string,
): Promise<UploadSessionOut> {
  return client.request<UploadSessionOut>(`/api/v1/upload-sessions/${sessionId}`);
}

export async function putUploadChunk(
  client: StraytApiClient,
  sessionId: string,
  index: number,
  chunk: Blob,
): Promise<void> {
  await client.raw<void>(`/api/v1/upload-sessions/${sessionId}/chunks/${index}`, "PUT", chunk);
}

export async function completeUploadSession(
  client: StraytApiClient,
  sessionId: string,
): Promise<FileOut> {
  return client.request<FileOut>(`/api/v1/upload-sessions/${sessionId}/complete`, { method: "POST" });
}

export async function abortUploadSession(
  client: StraytApiClient,
  sessionId: string,
): Promise<void> {
  await client.raw<void>(`/api/v1/upload-sessions/${sessionId}`, "DELETE");
}

export async function downloadFileRaw(client: StraytApiClient, fileId: string): Promise<Blob> {
  return client.fetchBlob(`/api/v1/files/${fileId}/raw`);
}

// ---------------------------------------------------------------------------
// 加工任务（F8/F17）
// ---------------------------------------------------------------------------
export interface ListJobsOptions {
  projectId?: string;
  status?: "queued" | "running" | "success" | "failed" | "cancelled" | "interrupted";
  limit?: number;
}

export async function listJobs(
  client: StraytApiClient,
  opts: ListJobsOptions = {},
): Promise<JobOut[]> {
  return client.request<JobOut[]>("/api/v1/jobs", {
    query: {
      project_id: opts.projectId ?? undefined,
      status: opts.status ?? undefined,
      limit: opts.limit ?? 50,
    },
  });
}

export async function getJob(client: StraytApiClient, jobId: string): Promise<JobOut> {
  return client.request<JobOut>(`/api/v1/jobs/${jobId}`);
}

export async function createJob(client: StraytApiClient, body: JobCreate): Promise<JobOut> {
  return client.request<JobOut>("/api/v1/jobs", { method: "POST", body });
}

export async function cancelJob(client: StraytApiClient, jobId: string): Promise<JobOut> {
  return client.request<JobOut>(`/api/v1/jobs/${jobId}/cancel`, { method: "POST" });
}

export async function resumeJob(client: StraytApiClient, jobId: string): Promise<JobOut> {
  return client.request<JobOut>(`/api/v1/jobs/${jobId}/resume`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// 设置 / 模型档案（F7）
// ---------------------------------------------------------------------------
export async function listProfiles(client: StraytApiClient): Promise<ModelProfileOut[]> {
  return client.request<ModelProfileOut[]>("/api/v1/settings/model-profiles");
}

export async function createProfile(
  client: StraytApiClient,
  body: ModelProfileIn,
): Promise<ModelProfileOut> {
  return client.request<ModelProfileOut>("/api/v1/settings/model-profiles", { method: "POST", body });
}

export async function deleteProfile(client: StraytApiClient, profileId: string): Promise<void> {
  await client.raw<void>(`/api/v1/settings/model-profiles/${profileId}`, "DELETE");
}

export async function probeKey(
  client: StraytApiClient,
  body: ApiKeyProbeIn,
): Promise<{ ok: boolean; latency_ms?: number; error?: string }> {
  return client.request<{ ok: boolean; latency_ms?: number; error?: string }>(
    "/api/v1/settings/api-keys/probe",
    { method: "POST", body },
  );
}

// ---------------------------------------------------------------------------
// 图谱（F11/F13/F15/F16）
// ---------------------------------------------------------------------------
/** GET /projects/{id}/graph 无 response_model，这里补上响应形状。 */
export interface GraphCategoryOut {
  id: string;
  name: string;
  sort_order: number;
}

export interface GraphQuoteOut {
  text: string | null;
  page: number | null;
}

export interface GraphNodeOut {
  id: string;
  category_id: string | null;
  name: string;
  weight: number;
  card_summary: string | null;
  user_notes: string | null;
  edited: boolean;
  mastery: "no" | "mid" | "yes";
  created_at: string;
  updated_at: string;
  quote: GraphQuoteOut;
}

export interface GraphEdgeOut {
  from_node: string;
  to_node: string;
  reason: string | null;
}

export interface GraphBody {
  project_id: string;
  categories: GraphCategoryOut[];
  nodes: GraphNodeOut[];
  edges: GraphEdgeOut[];
}

/** graph 预览草稿内部结构（service 层 GraphDraft.to_dict）。 */
export interface GraphDraftNode {
  key: string;
  name: string;
  weight: number;
  summary: string;
  quote: string;
  loc_page: number;
  category_key: string;
  links: Array<{ to: string; reason?: string }>;
  edited?: boolean;
}

export interface GraphDraftBody {
  unit_count: number;
  warnings: string[];
  categories: Array<{ key: string; name: string; sort_order: number }>;
  nodes: GraphDraftNode[];
  edges: Array<{ from: string; to: string; reason?: string }>;
}

export async function getGraph(client: StraytApiClient, projectId: string): Promise<GraphBody> {
  return client.request<GraphBody>(`/api/v1/projects/${projectId}/graph`);
}

export async function getGraphDraft(
  client: StraytApiClient,
  jobId: string,
): Promise<{ job_id: string; status: string; draft: GraphDraftBody }> {
  return client.request<{ job_id: string; status: string; draft: GraphDraftBody }>(
    `/api/v1/jobs/${jobId}/graph-draft`,
  );
}

export async function confirmGraph(
  client: StraytApiClient,
  jobId: string,
  body: GraphConfirmIn,
): Promise<GraphConfirmOut> {
  return client.request<GraphConfirmOut>(`/api/v1/jobs/${jobId}/graph-confirm`, {
    method: "POST",
    body,
  });
}

export async function getMasteryStats(
  client: StraytApiClient,
  projectId: string,
): Promise<MasteryStatsOut> {
  return client.request<MasteryStatsOut>(`/api/v1/projects/${projectId}/mastery-stats`);
}

export async function setNodeMastery(
  client: StraytApiClient,
  nodeId: string,
  mastery: "no" | "mid" | "yes",
): Promise<NodeMasteryOut> {
  return client.request<NodeMasteryOut>(`/api/v1/nodes/${nodeId}/mastery`, {
    method: "PUT",
    body: { mastery },
  });
}

export async function generateFlashcards(
  client: StraytApiClient,
  projectId: string,
): Promise<FlashcardGenOut> {
  return client.request<FlashcardGenOut>(`/api/v1/projects/${projectId}/flashcards/generate`, {
    method: "POST",
  });
}

export async function listFlashcards(
  client: StraytApiClient,
  projectId: string,
  mastery?: "no" | "mid" | "yes",
): Promise<FlashcardOut[]> {
  return client.request<FlashcardOut[]>(`/api/v1/projects/${projectId}/flashcards`, {
    query: { mastery: mastery ?? undefined },
  });
}

export async function postFlashcardResult(
  client: StraytApiClient,
  flashcardId: string,
  correct: boolean,
): Promise<FlashcardResultOut> {
  return client.request<FlashcardResultOut>(`/api/v1/flashcards/${flashcardId}/result`, {
    method: "POST",
    body: { correct },
  });
}

// ---------------------------------------------------------------------------
// 弱同步（F2）
// ---------------------------------------------------------------------------
export async function fetchBootstrap(client: StraytApiClient): Promise<SyncSnapshot> {
  return client.request<SyncSnapshot>("/api/v1/sync/bootstrap");
}

export async function pushBatch(
  client: StraytApiClient,
  ops: WriteOp[],
): Promise<{ results: OpResult[]; server_time: string }> {
  return client.request<{ results: OpResult[]; server_time: string }>("/api/v1/sync/batch", {
    method: "POST",
    body: { ops },
  });
}

// ---------------------------------------------------------------------------
// 导出（F24）
// ---------------------------------------------------------------------------
export async function exportSnapshot(client: StraytApiClient): Promise<unknown> {
  return client.request<unknown>("/api/v1/export/snapshot");
}