import { ConfigProvider } from "antd";
import { createContext, useContext, useEffect, useMemo, type ReactNode } from "react";
import {
  DEFAULT_THEME,
  tokensFor,
  tokensToCssVars,
  type StraytTokens,
  type ThemeName,
} from "@strayt/tokens";
import { useStoredTheme } from "./themeStore";

export interface StraytThemeContextValue {
  themeName: ThemeName;
  tokens: StraytTokens;
  setThemeName: (t: ThemeName) => void;
}

const StraytThemeContext = createContext<StraytThemeContextValue | null>(null);

function toAntdToken(t: StraytTokens) {
  return {
    colorPrimary: t.colorPrimary,
    colorInfo: t.colorInfo,
    colorSuccess: t.colorSuccess,
    colorWarning: t.colorWarning,
    colorError: t.colorError,
    colorText: t.colorText,
    colorTextBase: t.colorText,
    colorBgLayout: t.colorBgLayout,
    colorBgContainer: t.colorBgContainer,
    colorBgElevated: t.colorBgContainer,
    borderRadius: t.borderRadius,
    lineWidth: t.lineWidth,
    fontFamily: t.fontFamily,
    fontSize: t.fontSize,
    lineHeight: t.lineHeight,
  } as const;
}

export function StraytThemeProvider({ children }: { children: ReactNode }) {
  const [stored, writeStored] = useStoredTheme();
  const themeName: ThemeName =
    stored === "pixel" || stored === "modern" ? stored : DEFAULT_THEME;

  const tokens = useMemo(() => tokensFor(themeName), [themeName]);

  // 即时生效：切主题时把 CSS 变量挂到 <html>，像素风覆盖层靠 data 属性激活
  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-strayt-theme", themeName);
    const vars = tokensToCssVars(tokens);
    for (const [k, v] of Object.entries(vars)) root.style.setProperty(k, v);
  }, [themeName, tokens]);

  const value = useMemo<StraytThemeContextValue>(
    () => ({
      themeName,
      tokens,
      setThemeName: (t) => {
        writeStored(t);
        document.documentElement.setAttribute("data-strayt-theme", t);
      },
    }),
    [themeName, tokens, writeStored],
  );

  const antdToken = useMemo(() => toAntdToken(tokens), [tokens]);

  return (
    <StraytThemeContext.Provider value={value}>
      <ConfigProvider theme={{ token: antdToken }}>{children}</ConfigProvider>
    </StraytThemeContext.Provider>
  );
}

export function useStraytTokens(): StraytTokens {
  const ctx = useContext(StraytThemeContext);
  if (!ctx) throw new Error("useStraytTokens 需在 StraytThemeProvider 内使用");
  return ctx.tokens;
}

export function useStraytTheme(): StraytThemeContextValue {
  const ctx = useContext(StraytThemeContext);
  if (!ctx) throw new Error("useStraytTheme 需在 StraytThemeProvider 内使用");
  return ctx;
}