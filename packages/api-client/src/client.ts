import type { components, operations } from "./schemas";

/** 服务端统一错误体 { error: { code, message, detail? } }。 */
export interface ApiErrorBody {
  error: { code: string; message: string; detail?: unknown };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export interface HttpOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  /** 免鉴权（如 /health）。默认 false。 */
  public?: boolean;
}

export interface StraytApiClient {
  readonly baseUrl: string;
  request<T>(path: string, opts?: HttpOptions): Promise<T>;
  /** multipart 直传（文件字段名固定为 ``file``）。 */
  upload<T>(path: string, file: Blob, filename: string): Promise<T>;
  /** 原始字节体（分块上传的单个 chunk，无 JSON 包装）。 */
  raw<T>(path: string, method: "PUT" | "POST" | "DELETE", body?: Blob): Promise<T>;
  /** 带鉴权把文件拉成 Blob（下载原始资料，二期出处回看）。 */
  fetchBlob(path: string): Promise<Blob>;
  health(): Promise<{ ok: boolean; app?: string; server_name?: string; schema_version?: string }>;
}

function normalizeBase(baseUrl: string): string {
  return baseUrl.replace(/\/+$/, "");
}

function qs(query?: HttpOptions["query"]): string {
  if (!query) return "";
  const parts: string[] = [];
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null) continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

export type { components, operations };

export interface StraytApiClientOptions {
  baseUrl: string;
  token: string;
  /** 网络层，测试可注入。 */
  fetchImpl?: typeof fetch;
}

export function createApiClient(options: StraytApiClientOptions): StraytApiClient {
  const base = normalizeBase(options.baseUrl);
  const doFetch: typeof fetch = options.fetchImpl ?? fetch;

  async function parseJson<T>(res: Response): Promise<T> {
    if (res.status === 204) return undefined as T;
    let payload: unknown = null;
    const text = await res.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
    }
    if (!res.ok) {
      const body = (payload ?? {}) as Partial<ApiErrorBody>;
      const err = body.error;
      throw new ApiError(res.status, err?.code ?? String(res.status), err?.message ?? res.statusText);
    }
    return payload as T;
  }

  async function request<T>(path: string, opts: HttpOptions = {}): Promise<T> {
    const url = `${base}${path}${qs(opts.query)}`;
    const headers: Record<string, string> = {};
    if (!opts.public) headers["Authorization"] = `Bearer ${options.token}`;
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";

    const res = await doFetch(url, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });

    if (res.status === 204) return undefined as T;

    let payload: unknown = null;
    const text = await res.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
    }

    if (!res.ok) {
      const body = (payload ?? {}) as Partial<ApiErrorBody>;
      const err = body.error;
      throw new ApiError(res.status, err?.code ?? String(res.status), err?.message ?? res.statusText);
    }
    return payload as T;
  }

  return {
    get baseUrl() {
      return base;
    },
    request,
    async upload<T>(path: string, file: Blob, filename: string): Promise<T> {
      const form = new FormData();
      form.append("file", file, filename);
      const res = await doFetch(`${base}${path}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${options.token}` },
        body: form,
      });
      return parseJson<T>(res);
    },
    async raw<T>(path: string, method: "PUT" | "POST" | "DELETE", body?: Blob): Promise<T> {
      const res = await doFetch(`${base}${path}`, {
        method,
        headers: { Authorization: `Bearer ${options.token}` },
        body,
      });
      return parseJson<T>(res);
    },
    async fetchBlob(path: string): Promise<Blob> {
      const res = await doFetch(`${base}${path}`, {
        headers: { Authorization: `Bearer ${options.token}` },
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as Partial<ApiErrorBody> | null;
        const err = body?.error;
        throw new ApiError(res.status, err?.code ?? String(res.status), err?.message ?? res.statusText);
      }
      return res.blob();
    },
    async health() {
      return request<{ ok: boolean; app?: string; server_name?: string; schema_version?: string }>(
        "/health",
        { public: true },
      );
    },
  };
}

export type SchemaTypes = components["schemas"];

export type ProjectOut = SchemaTypes["ProjectOut"];
export type PieceOut = SchemaTypes["PieceOut"];
export type PairOut = SchemaTypes["PairOut"];
export type CheckinOut = SchemaTypes["CheckinOut"];
export type ApiKeyOut = SchemaTypes["ApiKeyOut"];
export type PlanListOut = SchemaTypes["PlanListOut"];
export type GoalOut = SchemaTypes["GoalOut"];
export type FileOut = SchemaTypes["FileOut"];
export type JobOut = SchemaTypes["JobOut"];
export type JobCreate = SchemaTypes["JobCreate"];
export type UploadSessionOut = SchemaTypes["UploadSessionOut"];
export type UploadSessionCreate = SchemaTypes["UploadSessionCreate"];
export type ModelProfileOut = SchemaTypes["ModelProfileOut"];
export type ModelProfileIn = SchemaTypes["ModelProfileIn"];
export type ApiKeyProbeIn = SchemaTypes["ApiKeyProbeIn"];
export type GraphConfirmIn = SchemaTypes["GraphConfirmIn"];
export type GraphConfirmOut = SchemaTypes["GraphConfirmOut"];
export type MasteryStatsOut = SchemaTypes["MasteryStatsOut"];
export type NodeMasteryOut = SchemaTypes["NodeMasteryOut"];
export type FlashcardOut = SchemaTypes["FlashcardOut"];
export type FlashcardGenOut = SchemaTypes["FlashcardGenOut"];
export type FlashcardResultOut = SchemaTypes["FlashcardResultOut"];

/** /plans/daily 无 schema（动态 dict），本地对齐其结构。 */
export interface DailyTodos {
  date: string;
  stats: { total: number; open: number; done: number };
  overdue: PlanListLike[];
  today: PlanListLike[];
  later: PlanListLike[];
  unscheduled: PlanListLike[];
  done: PlanListLike[];
}

export interface PlanListLike {
  id: string;
  project_id: string;
  title: string;
  due_date: string | null;
  status: "todo" | "doing" | "done";
  target_type?: string | null;
  target_id?: string | null;
  sort_order: number;
  days_until?: number | null;
  overdue?: boolean;
}