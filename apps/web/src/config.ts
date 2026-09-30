export interface ServerConfig {
  baseUrl: string;
  token: string;
}

const CONFIG_KEY = "strayt:config";

export function loadConfig(): ServerConfig | null {
  try {
    const raw = localStorage.getItem(CONFIG_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ServerConfig;
    if (typeof parsed.baseUrl !== "string" || typeof parsed.token !== "string") return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveConfig(config: ServerConfig): void {
  try {
    localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
  } catch {
    // 存储不可用：仅本次会话生效
  }
}

export function clearConfig(): void {
  try {
    localStorage.removeItem(CONFIG_KEY);
  } catch {
    // ignore
  }
}