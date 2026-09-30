import type { StraytTokens, ThemeName } from "./types";

/**
 * Token 集 → 挂到 <html> 上的 CSS 变量（值统一字符串化，编号类前缀 --tok-）。
 *
 * 数值 token 会同时产出两种变量：
 * - `--tok-spaceMd` = `16`（无单位，给 line-height / antd 这类需要数字的地方）
 * - `--tok-spaceMd-px` = `16px`（给 CSS 长度用；裸数字在 margin/padding 里非法）
 *
 * 带 `-px` 后缀的这一支是页面层唯一允许的写法：AGENTS §4 门禁会扫
 * `数字+px` 字面量，所以单位必须由 token 层生成。
 */
export function tokensToCssVars(t: StraytTokens): Record<string, string> {
  const map: Record<string, string> = {};
  const entries: Array<[string, string | number | string[]]> = Object.entries(t);
  for (const [key, value] of entries) {
    if (Array.isArray(value)) {
      // 调色板数组不合并成逗号串，逐项编址成 --tok-chartCategory-0/1/2…
      // （CSS 里没法给数组下标，只能这样摊平）
      value.forEach((v, i) => {
        map[`--tok-${key}-${i}`] = String(v);
      });
      continue;
    }
    map[`--tok-${key}`] = String(value);
    if (typeof value === "number") map[`--tok-${key}-px`] = `${value}px`;
  }
  return map;
}

/** 调色板某一项：'var(--tok-chartCategory-2)'（越界则回绕取模）。 */
export function categoryCssVar(index: number): string {
  return `var(--tok-chartCategory-${index})`;
}

/** CSS 变量读取辅助：'var(--tok-color-primary)' 这种在页面层直接用。 */
export function cssVar(name: keyof StraytTokens): string {
  return `var(--tok-${name})`;
}

export function themeCssVarName(name: keyof StraytTokens): string {
  return `--tok-${name}`;
}

export type { StraytTokens, ThemeName };

export function isThemeName(value: string): value is ThemeName {
  return value === "modern" || value === "pixel";
}