#!/usr/bin/env node
/**
 * AGENTS.md §4 门禁：扫描 apps/ 与 packages/ui，命中 CSS 颜色 / 间距 / 圆角字面量即失败。
 *
 * 两套主题的样式字面量只能存在于 packages/tokens（modern.ts / pixel.ts / pixel.css），
 * 组件层与页面层只准读 Design Token（CSS 变量 var(--tok-*) 或 useStraytTokens()）。
 *
 * 用法：node scripts/check-tokens.mjs [--fix 预留]
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));

const SCAN_ROOTS = ["apps", "packages/ui"];
const EXTENSIONS = new Set([".ts", ".tsx", ".js", ".jsx", ".css"]);
const IGNORE_DIRS = new Set(["node_modules", "dist", ".vite"]);

const RULES = [
  {
    name: "hex 颜色",
    pattern: /#[0-9a-fA-F]{3,4}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{8}\b/g,
  },
  {
    name: "rgb/rgba/hsl/hsla 颜色",
    pattern: /\b(?:rgb|rgba|hsl|hsla)\s*\(\s*\d+(?:\.\d+)?/g,
  },
  {
    name: "命名颜色",
    // 见 readme：挑常见 CSS 命名色，用 \b 隔离避免命中 whiteSpace 之类的标识符
    pattern:
      /\b(?:red|green|blue|black|white|gray|grey|orange|yellow|purple|pink|teal|cyan|magenta|brown|navy|maroon|olive|lime|indigo|violet|coral|gold|silver|tomato|khaki|beige)\b/g,
  },
  {
    name: "间距/圆角/字号 px·pt·rem·em 字面量",
    pattern: /\b\d+(?:\.\d+)?(?:px|pt|rem|em)\b/g,
  },
];

function walk(dir, out) {
  for (const name of readdirSync(dir)) {
    if (IGNORE_DIRS.has(name)) continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (EXTENSIONS.has(name.slice(name.lastIndexOf(".")))) out.push(p);
  }
}

const violations = [];
for (const scanRoot of SCAN_ROOTS) {
  const abs = resolve(root, scanRoot);
  if (!statSync(abs).isDirectory()) continue;
  const files = [];
  walk(abs, files);
  for (const file of files) {
    const src = readFileSync(file, "utf8");
    const lines = src.split("\n");
    for (const rule of RULES) {
      for (const line of lines) {
        rule.pattern.lastIndex = 0;
        const hits = [...line.matchAll(rule.pattern)];
        if (hits.length > 0) {
          violations.push({ file, line: lines.indexOf(line) + 1, rule, hits });
        }
      }
    }
  }
}

if (violations.length > 0) {
  console.error(`check:tokens 失败：应该只读 Design Token，不许写样式字面量（${violations.length} 处）`);
  for (const v of violations) {
    const relPath = v.file.slice(root.length + 1);
    const frags = v.hits.map((h) => h[0]).join(", ");
    console.error(`  ${relPath}:${v.line} [${v.rule.name}] ${frags}`);
  }
  process.exitCode = 1;
} else {
  console.log("check:tokens 通过：apps/ 与 packages/ui 无样式字面量");
}