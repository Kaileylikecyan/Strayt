import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { StraytThemeProvider } from "@strayt/ui";
import "@strayt/tokens/pixel.css";
import "@strayt/web/app.css";
import { AppProvider } from "@strayt/web/store";
import { App } from "@strayt/web/App";

/**
 * 桌面端与网页端共用同一套页面（apps/web/src），这里只负责挂载。
 * 差异都留在 apps/desktop/src-tauri（原生窗口）与 tauri.conf.json（窗口尺寸/标题）里。
 */
const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("缺少 #root 挂载点");

createRoot(rootEl).render(
  <StrictMode>
    <StraytThemeProvider>
      <AppProvider>
        <App />
      </AppProvider>
    </StraytThemeProvider>
  </StrictMode>,
);
