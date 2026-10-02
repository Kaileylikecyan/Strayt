/**
 * 接入配置：服务器地址 + 登录换来的会话令牌。
 *
 * **只存地址与令牌，绝不存口令。** 口令只在 Setup/Login 页的表单里短暂存在，
 * 提交给 `/auth/login` 换出 `session_token` 之后就该从内存里消失（ADR-0009）。
 * 这与旧版把 token 当长期凭据不同 —— 令牌是派生出来的、可失效、30 天过期。
 */
export interface ServerConfig {
  baseUrl: string;
  sessionToken: string;
}

const CONFIG_KEY = "strayt:config";

export function loadConfig(): ServerConfig | null {
  try {
    const raw = localStorage.getItem(CONFIG_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ServerConfig;
    if (typeof parsed.baseUrl !== "string" || typeof parsed.sessionToken !== "string") return null;
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