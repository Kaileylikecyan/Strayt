import type { FileOut, UploadSessionOut } from "@strayt/api-client";
import type { AppDomain } from "./api";

/** 服务端直传阈值（config.upload_chunk_threshold=50MB 对应）。 */
const DIRECT_THRESHOLD = 50 * 1024 * 1024;

export async function sha256Hex(data: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * 上传一个文件到指定项目：≤50MB 直传；更大则声明会话→逐块上传→合并校验。
 * onProgress(loaded, total) 按字节推进，用作进度条。
 */
export async function uploadFileSmart(
  domain: AppDomain,
  projectId: string,
  file: File,
  onProgress: (loaded: number, total: number) => void,
  signal?: AbortSignal,
): Promise<FileOut> {
  const size = file.size;
  onProgress(0, size);

  if (size <= DIRECT_THRESHOLD) {
    const row = await domain.uploadFile(projectId, file, file.name);
    onProgress(size, size);
    return row;
  }

  const sha256 = await sha256Hex(await file.arrayBuffer());
  const session: UploadSessionOut = await domain.createUploadSession(projectId, {
    orig_name: file.name,
    size,
    sha256,
  });

  const chunkSize = session.chunk_size;
  const total = session.total_chunks;
  const uploaded = new Set(Object.keys(session.received_json ?? {}).map(Number));

  for (let i = 0; i < total; i++) {
    if (signal?.aborted) break;
    if (uploaded.has(i)) continue;
    const chunk = file.slice(i * chunkSize, Math.min((i + 1) * chunkSize, size));
    if (chunk.size === 0) continue;
    await domain.putUploadChunk(session.id, i, chunk);
    onProgress(Math.min((i + 1) * chunkSize, size), size);
  }

  if (signal?.aborted) {
    await domain.abortUploadSession(session.id).catch(() => void 0);
    throw new DOMException("上传已取消", "AbortError");
  }

  onProgress(size, size);
  return domain.completeUploadSession(session.id);
}