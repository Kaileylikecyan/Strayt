import type { ReactNode } from "react";
import {
  ApartmentOutlined,
  CalendarOutlined,
  CloudUploadOutlined,
  HomeOutlined,
  ReadOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { Button } from "antd";
import { SyncBadge, BottomNav, type SyncState } from "@strayt/ui";
import { useApp } from "./store";

export type TabKey = "projects" | "files" | "jobs" | "graph" | "recite" | "checkin" | "settings";

const NAV_ITEMS = [
  { key: "projects", label: "项目", icon: <HomeOutlined /> },
  { key: "files", label: "资料", icon: <CloudUploadOutlined /> },
  { key: "jobs", label: "任务", icon: <ThunderboltOutlined /> },
  { key: "graph", label: "图谱", icon: <ApartmentOutlined /> },
  { key: "recite", label: "背诵舱", icon: <ReadOutlined /> },
  { key: "checkin", label: "打卡", icon: <CalendarOutlined /> },
  { key: "settings", label: "设置", icon: <SettingOutlined /> },
];

export function Shell({
  tab,
  onTab,
  children,
}: {
  tab: TabKey;
  onTab: (t: TabKey) => void;
  children: ReactNode;
}) {
  const { syncState, pending, flushNow } = useApp();

  return (
    <div className="strayt-app">
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "var(--tok-spaceMd-px)",
        }}
      >
        <span style={{ fontWeight: 700 }}>Strayt 学习工作台</span>
        <span className="strayt-row">
          <SyncBadge state={syncState as SyncState} pending={pending} />
          <Button size="small" onClick={() => void flushNow()}>
            同步
          </Button>
        </span>
      </header>
      <main className="strayt-main">{children}</main>
      <BottomNav items={NAV_ITEMS} active={tab} onChange={(k) => onTab(k as TabKey)} />
    </div>
  );
}