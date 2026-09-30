/** 本地 KV 存储适配（浏览器 localStorage / 测试内存实现）。 */

export interface KVStorage {
  get(key: string): string | null;
  set(key: string, value: string): void;
  remove(key: string): void;
}

/** 测试/无 DOM 环境用的内存实现。 */
export function memoryStorage(): KVStorage {
  const m = new Map<string, string>();
  return {
    get: (k) => m.get(k) ?? null,
    set: (k, v) => {
      m.set(k, v);
    },
    remove: (k) => {
      m.delete(k);
    },
  };
}

/** 浏览器 localStorage 兜底（隐私模式可能抛异常，包一层）。 */
export function localStorageAdapter(): KVStorage {
  const ls = typeof localStorage !== "undefined" ? localStorage : undefined;
  return {
    get: (k) => {
      try {
        return ls?.getItem(k) ?? null;
      } catch {
        return null;
      }
    },
    set: (k, v) => {
      try {
        ls?.setItem(k, v);
      } catch {
        // 存储满/禁用时静默丢，写队列照常在内存里跑
      }
    },
    remove: (k) => {
      try {
        ls?.removeItem(k);
      } catch {
        // ignore
      }
    },
  };
}