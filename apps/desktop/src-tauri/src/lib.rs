//! 桌面端原生层。
//!
//! 架构红线（AGENTS.md §3）：**解析与 LLM 调用只在服务端**，客户端只做拉取、交互展示、进度标记。
//! 因此这里**不**放任何业务逻辑、HTTP 调用或文件读写 —— 网页端那套
//! （`packages/api-client` + `packages/sync-engine`）已经能直连服务端，
//! 原生层只负责「开一个窗口把前端页面装进去」。
//!
//! 将来要加的原生能力（打开资料所在目录、系统托盘常驻、全局快捷键）都应作为
//! 显式 command 暴露，并在 `capabilities/` 里按最小权限授权。

/// 启动 Tauri 应用。
///
/// 刻意不带任何 plugin：当前形态不需要托盘/通知/自动更新，装了就是白扩权限面。
/// 真要加时记得同步 `tauri.conf.json` 的 `plugins` 与 `capabilities/default.json`。
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("启动 Strayt 桌面端失败");
}
