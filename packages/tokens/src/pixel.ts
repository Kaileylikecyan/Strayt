import type { StraytTokens } from "./types";

/**
 * 像素风（文档 §8.2 + AGENTS.md §4）。
 *
 * 独立 token 覆盖（方角、粗描边、等宽字体、屏幕感色板）+ 独立 CSS 覆盖层
 * （pixel.css，见本目录下的同层文件）。两者与 modern.ts 物理隔离。
 */
export const pixelTokens: StraytTokens = {
  themeName: "pixel",

  colorPrimary: "#2a5bd7",
  colorInfo: "#2a5bd7",
  colorSuccess: "#2f9e44",
  colorWarning: "#c47a12",
  colorError: "#c62828",
  colorText: "#1a1308",
  colorBgLayout: "#e9d9a7",
  colorBgContainer: "#fdf4d3",

  borderRadius: 0,
  lineWidth: 4,
  fontSize: 14,
  lineHeight: 1.55,
  fontFamily:
    "'Cascadia Mono', 'Consolas', 'Courier New', 'PingFang SC', 'Microsoft YaHei', monospace",

  masteryNo: "#c62828",
  masteryMid: "#c47a12",
  masteryYes: "#2f9e44",
  chartNode: "#2a5bd7",
  chartEdge: "#5c4a1e",
  chartLink: "#6a4fa3",
  // 分区着色：像素风的「屏幕」色板，明度差拉大以便在方角网格里分得清
  chartCategory: [
    "#2a5bd7",
    "#2f9e44",
    "#6a4fa3",
    "#c47a12",
    "#0f7d8c",
    "#b03060",
    "#5c4a1e",
    "#6b8e23",
  ],

  heatEmpty: "#ded5ae",
  heatLow: "#a8d0a4",
  heatMid: "#67b26f",
  heatHigh: "#3c8c46",
  heatFull: "#1f6b2c",

  spaceXs: 4,
  spaceSm: 8,
  spaceMd: 16,
  spaceLg: 24,
  spaceXl: 40,

  reciteHint: "#2a5bd7",
  reciteMaskBg: "#d8c892",

  focusOutline: "#000000",
};