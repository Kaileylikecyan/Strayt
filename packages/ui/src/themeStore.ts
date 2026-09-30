import { useSyncExternalStore } from "react";

/** 主题偏好本地记忆（文档 §8.2：一键切换、即时生效、偏好存在本地）。 */

const THEME_KEY = "strayt:theme";
const listeners = new Set<() => void>();
let cached: string | null = null;

function read(): string {
  if (cached !== null) return cached;
  try {
    cached = localStorage.getItem(THEME_KEY);
  } catch {
    cached = null;
  }
  return cached ?? "";
}

function write(value: string): void {
  cached = value;
  try {
    if (value) localStorage.setItem(THEME_KEY, value);
    else localStorage.removeItem(THEME_KEY);
  } catch {
    // 存储不可用时仅内存生效
  }
  for (const l of listeners) l();
}

/** 供 useSyncExternalStore 使用（保证每次 subscribe 稳定）。 */
export function useStoredTheme(): [string, (v: string) => void] {
  const value = useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    read,
    () => "",
  );
  return [value, write];
}