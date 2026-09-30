import { createApiClient, ApiError, type StraytApiClient } from "@strayt/api-client";
import * as api from "@strayt/api-client";
import { SyncEngine, localStorageAdapter, type Writables } from "@strayt/sync-engine";
import type { ServerConfig } from "./config";

export { ApiError };

/** 网络层异常（fetch 抛 TypeError）视为离线。 */
export function isNetworkError(err: unknown): boolean {
  return err instanceof TypeError;
}

/** 绑定到具体 client 的域方法（client 始终是第一个参数）。 */
export function bindDomain(client: StraytApiClient) {
  return {
    listProjects: () => api.listProjects(client),
    createProject: (body: { name: string; type: "graph" | "recite" }) => api.createProject(client, body),
    renameProject: (projectId: string, name: string) => api.renameProject(client, projectId, name),
    deleteProject: (projectId: string) => api.deleteProject(client, projectId),
    getGoal: (projectId: string) => api.getGoal(client, projectId),

    listPieces: (projectId: string) => api.listPieces(client, projectId),
    listPairs: (projectId: string, pieceId: string, fromSeq = 0, limit = 500) =>
      api.listPairs(client, projectId, pieceId, fromSeq, limit),
    putPieceProgress: (
      projectId: string,
      pieceId: string,
      body: { last_pos: number; recited?: boolean; client_ts: string },
    ) => api.putPieceProgress(client, projectId, pieceId, body),

    getCheckin: (day: string) => api.getCheckin(client, day),
    putCheckin: (body: { date: string; items: string[] }) => api.putCheckin(client, body),
    listPlans: (projectId?: string) => api.listPlans(client, projectId),
    dailyTodos: (day?: string) => api.dailyTodos(client, day),

    listApiKeys: () => api.listApiKeys(client),
    createApiKey: (body: { provider: string; secret: string; label?: string }) =>
      api.createApiKey(client, body),
    testApiKey: (keyId: string) => api.testApiKey(client, keyId),
    deleteApiKey: (keyId: string) => api.deleteApiKey(client, keyId),
    getKv: (key: string) => api.getKv(client, key),
    putKv: (key: string, value: string) => api.putKv(client, key, value),

    listProviders: () => api.listProviders(client),
    listProfiles: () => api.listProfiles(client),
    createProfile: (body: api.ModelProfileIn) => api.createProfile(client, body),
    deleteProfile: (profileId: string) => api.deleteProfile(client, profileId),
    probeKey: (body: api.ApiKeyProbeIn) => api.probeKey(client, body),

    listFiles: (projectId: string) => api.listFiles(client, projectId),
    uploadFile: (projectId: string, file: Blob, filename: string) =>
      api.uploadFile(client, projectId, file, filename),
    deleteFile: (fileId: string) => api.deleteFile(client, fileId),
    createUploadSession: (projectId: string, body: api.UploadSessionCreate) =>
      api.createUploadSession(client, projectId, body),
    getUploadSession: (sessionId: string) => api.getUploadSession(client, sessionId),
    putUploadChunk: (sessionId: string, index: number, chunk: Blob) =>
      api.putUploadChunk(client, sessionId, index, chunk),
    completeUploadSession: (sessionId: string) => api.completeUploadSession(client, sessionId),
    abortUploadSession: (sessionId: string) => api.abortUploadSession(client, sessionId),
    downloadFileRaw: (fileId: string) => api.downloadFileRaw(client, fileId),

    listJobs: (opts: api.ListJobsOptions = {}) => api.listJobs(client, opts),
    getJob: (jobId: string) => api.getJob(client, jobId),
    createJob: (body: api.JobCreate) => api.createJob(client, body),
    cancelJob: (jobId: string) => api.cancelJob(client, jobId),
    resumeJob: (jobId: string) => api.resumeJob(client, jobId),

    getGraph: (projectId: string) => api.getGraph(client, projectId),
    getGraphDraft: (jobId: string) => api.getGraphDraft(client, jobId),
    confirmGraph: (jobId: string, body: api.GraphConfirmIn) => api.confirmGraph(client, jobId, body),
    getMasteryStats: (projectId: string) => api.getMasteryStats(client, projectId),
    setNodeMastery: (nodeId: string, mastery: "no" | "mid" | "yes") =>
      api.setNodeMastery(client, nodeId, mastery),
    generateFlashcards: (projectId: string) => api.generateFlashcards(client, projectId),
    listFlashcards: (projectId: string, mastery?: "no" | "mid" | "yes") =>
      api.listFlashcards(client, projectId, mastery),
    postFlashcardResult: (flashcardId: string, correct: boolean) =>
      api.postFlashcardResult(client, flashcardId, correct),

    exportSnapshot: () => api.exportSnapshot(client),
  };
}

export type AppDomain = ReturnType<typeof bindDomain>;

export interface AppApi {
  client: StraytApiClient;
  engine: SyncEngine;
  domain: AppDomain;
}

export function createAppApi(config: ServerConfig): AppApi {
  const client = createApiClient({ baseUrl: config.baseUrl, token: config.token });
  const engine = new SyncEngine({
    api: {
      bootstrap: () => api.fetchBootstrap(client),
      batch: (ops) => api.pushBatch(client, ops),
    },
    storage: localStorageAdapter(),
    snapshotKey: "strayt:snapshot",
    queueKey: "strayt:write-queue",
  });
  return { client, engine, domain: bindDomain(client) };
}

/** 可直传在线的实体（失败才入队）。 */
export type DirectWritables = Extract<Writables, "piece_progress" | "checkin">;

export type { ServerConfig };