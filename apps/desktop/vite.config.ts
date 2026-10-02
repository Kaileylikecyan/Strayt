import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * 桌面端与网页端共用同一份页面（apps/web/src），这里只做两件事：
 * 1. 把 @strayt/* 指向源码（与 apps/web/vite.config.ts 一致，走 workspace 别名而非构建产物）
 * 2. 告诉 Tauri dev 用哪个端口（tauri.conf.json 的 devUrl 必须与这里一致）
 */
const src = {
  "tokens/pixel.css": fileURLToPath(new URL("../../packages/tokens/src/pixel.css", import.meta.url)),
  tokens: fileURLToPath(new URL("../../packages/tokens/src/index.ts", import.meta.url)),
  ui: fileURLToPath(new URL("../../packages/ui/src/index.ts", import.meta.url)),
  "api-client": fileURLToPath(new URL("../../packages/api-client/src/index.ts", import.meta.url)),
  "sync-engine": fileURLToPath(new URL("../../packages/sync-engine/src/index.ts", import.meta.url)),
  web: fileURLToPath(new URL("../web/src", import.meta.url)),
};

const PORT = 5174;

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      { find: "@strayt/tokens/pixel.css", replacement: src["tokens/pixel.css"] },
      { find: "@strayt/tokens", replacement: src.tokens },
      { find: "@strayt/ui", replacement: src.ui },
      { find: "@strayt/api-client", replacement: src["api-client"] },
      { find: "@strayt/sync-engine", replacement: src["sync-engine"] },
      // 注意顺序：必须在 @strayt/* 之后无所谓，但要在 "@strayt/web" 之前用精确前缀
      { find: /^@strayt\/web\/(.*)$/, replacement: `${src.web}/$1` },
    ],
  },
  server: {
    host: "127.0.0.1",
    port: PORT,
    strictPort: true,
    // 页面在 apps/web/src（apps/desktop 的兄弟目录），Vite 默认只允许 root 内文件
    fs: { allow: [fileURLToPath(new URL("../..", import.meta.url))] },
  },
  envPrefix: ["VITE_", "TAURI_"],
  optimizeDeps: {
    exclude: ["@strayt/tokens", "@strayt/ui", "@strayt/api-client", "@strayt/sync-engine"],
  },
  build: {
    outDir: "dist",
    // Tauri 走 file:// 或自定义协议加载，relative 路径更稳
    assetsDir: "assets",
    sourcemap: false,
    emptyOutDir: true,
  },
});
