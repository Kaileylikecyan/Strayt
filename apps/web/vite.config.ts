import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const src = {
  "tokens/pixel.css": fileURLToPath(new URL("../../packages/tokens/src/pixel.css", import.meta.url)),
  tokens: fileURLToPath(new URL("../../packages/tokens/src/index.ts", import.meta.url)),
  ui: fileURLToPath(new URL("../../packages/ui/src/index.ts", import.meta.url)),
  "api-client": fileURLToPath(new URL("../../packages/api-client/src/index.ts", import.meta.url)),
  "sync-engine": fileURLToPath(new URL("../../packages/sync-engine/src/index.ts", import.meta.url)),
};

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      { find: "@strayt/tokens/pixel.css", replacement: src["tokens/pixel.css"] },
      { find: "@strayt/tokens", replacement: src.tokens },
      { find: "@strayt/ui", replacement: src.ui },
      { find: "@strayt/api-client", replacement: src["api-client"] },
      { find: "@strayt/sync-engine", replacement: src["sync-engine"] },
    ],
  },
  server: {
    host: true,
    port: 5173,
  },
  optimizeDeps: {
    exclude: ["@strayt/tokens", "@strayt/ui", "@strayt/api-client", "@strayt/sync-engine"],
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});