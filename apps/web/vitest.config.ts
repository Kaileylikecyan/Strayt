import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  resolve: {
    alias: {
      // 只测纯函数模块，但 import type 会牵进工作区包解析。
      "@strayt/api-client": path.resolve(__dirname, "../../packages/api-client/src/index.ts"),
    },
  },
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
  },
});