/**
 * Design Token 定义（AGENTS.md §4：样式字面量物理隔离在两套主题文件里，
 * 组件层 / 页面层只允许从这里读取）。
 */

export type ThemeName = "modern" | "pixel";

export interface StraytTokens {
  themeName: ThemeName;

  /** antd ConfigProvider 种子 token 映射 */
  colorPrimary: string;
  colorInfo: string;
  colorSuccess: string;
  colorWarning: string;
  colorError: string;
  colorText: string;
  colorBgLayout: string;
  colorBgContainer: string;
  borderRadius: number;
  lineWidth: number;
  fontSize: number;
  lineHeight: number;
  fontFamily: string;

  /** 图谱 / 闪卡 / 掌握度三色（图表调色板必须来自 token，不得硬编码） */
  masteryNo: string;
  masteryMid: string;
  masteryYes: string;
  chartNode: string;
  chartEdge: string;
  chartLink: string;
  /** 一级分类着色板（F13 分区着色）：按分类序号取色，循环复用 */
  chartCategory: string[];

  /** 打卡 30 天热力五档 */
  heatEmpty: string;
  heatLow: string;
  heatMid: string;
  heatHigh: string;
  heatFull: string;

  /** 间距刻度 */
  spaceXs: number;
  spaceSm: number;
  spaceMd: number;
  spaceLg: number;
  spaceXl: number;

  /** 背诵舱：提示 / 遮罩图层 */
  reciteHint: string;
  reciteMaskBg: string;

  /** 聚焦描边（对比度可达标用） */
  focusOutline: string;
}

export const THEME_NAMES: readonly ThemeName[] = ["modern", "pixel"] as const;

export const DEFAULT_THEME: ThemeName = "modern";