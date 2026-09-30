import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { StraytThemeProvider } from "@strayt/ui";
import "@strayt/tokens/pixel.css";
import { AppProvider } from "./store";
import { App } from "./App";
import "./app.css";

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