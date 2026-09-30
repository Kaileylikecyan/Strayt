import type { StraytTokens } from "./types";

/**
 * 现代简约风（文档 §8.2「现代简约 = antd 默认」）。
 *
 * 语义色与 antd 默认调色板对齐，组件观感即 antd 原生风格。
 * 本文件与 pixel.ts / pixel.css 物理隔离，互不 import。
 */
export const modernTokens: StraytTokens = {
  themeName: "modern",

  colorPrimary: "#1677ff",
  colorInfo: "#1677ff",
  colorSuccess: "#52c41a",
  colorWarning: "#faad14",
  colorError: "#ff4d4f",
  colorText: "rgba(0, 0, 0, 0.88)",
  colorBgLayout: "#f5f5f5",
  colorBgContainer: "#ffffff",

  borderRadius: 6,
  lineWidth: 1,
  fontSize: 14,
  lineHeight: 1.6,
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif",

  masteryNo: "#fa541c",
  masteryMid: "#faad14",
  masteryYes: "#52c41a",
  chartNode: "#1677ff",
  chartEdge: "rgba(0, 0, 0, 0.45)",
  chartLink: "#722ed1",
  // 分区着色：与 antd 默认色板同源，相邻分类色相足够远（灰度打印也能靠明度区分）
  chartCategory: [
    "#1677ff",
    "#52c41a",
    "#722ed1",
    "#fa8c16",
    "#13c2c2",
    "#eb2f96",
    "#8c8c8c",
    "#a0d911",
  ],

  heatEmpty: "rgba(0, 0, 0, 0.04)",
  heatLow: "rgba(22, 119, 255, 0.25)",
  heatMid: "rgba(22, 119, 255, 0.5)",
  heatHigh: "rgba(22, 119, 255, 0.8)",
  heatFull: "#1677ff",

  spaceXs: 4,
  spaceSm: 8,
  spaceMd: 16,
  spaceLg: 24,
  spaceXl: 40,

  reciteHint: "#1677ff",
  reciteMaskBg: "rgba(0, 0, 0, 0.12)",

  focusOutline: "#1677ff",
};