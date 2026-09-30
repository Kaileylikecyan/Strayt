import { modernTokens } from "./modern";
import { pixelTokens } from "./pixel";
import type { StraytTokens, ThemeName } from "./types";

export { modernTokens } from "./modern";
export { pixelTokens } from "./pixel";
export { cssVar, categoryCssVar, themeCssVarName, tokensToCssVars, isThemeName } from "./cssVars";
export type { StraytTokens, ThemeName } from "./types";
export { THEME_NAMES, DEFAULT_THEME } from "./types";

export function tokensFor(themeName: ThemeName): StraytTokens {
  return themeName === "pixel" ? pixelTokens : modernTokens;
}