import { execSync } from "node:child_process";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, ".."); // packages/api-client
const spec = resolve(import.meta.dirname, "../../../docs/api/openapi.yaml");
const out = resolve(root, "src/schemas.d.ts");

// npm workspaces 会把 bin 提升到根 node_modules/.bin
const bin = resolve(import.meta.dirname, "../../../node_modules/.bin/openapi-typescript");
const cmd = `"${bin}" "${spec}" -o "${out}"`;
execSync(cmd, { stdio: "inherit", shell: process.platform === "win32" ? "cmd.exe" : "/bin/sh" });
console.log(`openapi-typescript 已生成 ${out}`);