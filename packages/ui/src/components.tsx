import { Badge, Card, Tag } from "antd";
import type { CSSProperties, ReactNode } from "react";
import { cssVar, type ThemeName } from "@strayt/tokens";
import { useStraytTokens } from "./theme";

/** 项目类型标签（F5 类型图标）。颜色只读 token。 */
export function ProjectTypeTag({ type }: { type: "graph" | "recite" }) {
  const tokens = useStraytTokens();
  const color = type === "graph" ? tokens.chartNode : tokens.colorPrimary;
  return <Tag style={{ color }}>{type === "graph" ? "图谱型" : "背诵型"}</Tag>;
}

export type MasteryLevel = "no" | "mid" | "yes";

const MASTERY_KEYS: Record<MasteryLevel, "masteryNo" | "masteryMid" | "masteryYes"> = {
  no: "masteryNo",
  mid: "masteryMid",
  yes: "masteryYes",
};

/** 知识点掌握度圆点（F13/F16 三色，颜色来自 token）。 */
export function MasteryDot({ level }: { level: MasteryLevel }) {
  const dotStyle: CSSProperties = {
    width: "var(--tok-space-md)",
    height: "var(--tok-space-md)",
    borderRadius: "var(--tok-space-md)",
    backgroundColor: cssVar(MASTERY_KEYS[level]),
  };
  return <span aria-label={`掌握度：${level}`} style={dotStyle} />;
}

export type SyncState = "synced" | "syncing" | "offline";

/** 同步状态角标（F3）。 */
export function SyncBadge({ state, pending = 0 }: { state: SyncState; pending?: number }) {
  const tokens = useStraytTokens();
  const text =
    state === "synced" ? "已同步" : state === "syncing" ? "同步中" : `离线 ${pending} 条待上传`;
  const color =
    state === "synced"
      ? tokens.colorSuccess
      : state === "syncing"
        ? tokens.colorWarning
        : tokens.colorError;
  return (
    <Badge color={color} text={<span style={{ color: tokens.colorText }}>{text}</span>} />
  );
}

export interface NavItem {
  key: string;
  label: string;
  icon?: ReactNode;
}

/** 底端 Tab（移动端导航）。 */
export function BottomNav({
  items,
  active,
  onChange,
}: {
  items: NavItem[];
  active: string;
  onChange: (key: string) => void;
}) {
  const tokens = useStraytTokens();
  return (
    <nav
      aria-label="主导航"
      style={{
        position: "sticky",
        bottom: 0,
        display: "flex",
        justifyContent: "space-around",
        padding: tokens.spaceXs,
        borderTop: `solid ${tokens.colorBgLayout}`,
        background: tokens.colorBgContainer,
      }}
    >
      {items.map((item) => {
        const on = item.key === active;
        return (
          <button
            key={item.key}
            type="button"
            onClick={() => onChange(item.key)}
            style={{
              background: "none",
              border: "none",
              cursor: "pointer",
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: tokens.spaceXs,
              padding: tokens.spaceXs,
              color: on ? tokens.colorPrimary : tokens.colorText,
              fontWeight: on ? 700 : 400,
            }}
          >
            {item.icon}
            <span>{item.label}</span>
          </button>
        );
      })}
    </nav>
  );
}

/** 页头。 */
export function PageHeader({ title, extra }: { title: ReactNode; extra?: ReactNode }) {
  const tokens = useStraytTokens();
  return (
    <header
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        padding: tokens.spaceMd,
      }}
    >
      <h1 style={{ margin: 0, color: tokens.colorText }}>{title}</h1>
      {extra}
    </header>
  );
}

/** 统一卡片容器。 */
export function SectionCard({
  title,
  extra,
  children,
}: {
  title?: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card size="small" title={title} extra={extra}>
      {children}
    </Card>
  );
}

/** 打卡热力小格（F23 30 天热力，颜色来自 token 五档）。 */
export function HeatCell({ level }: { level: 0 | 1 | 2 | 3 | 4 }) {
  const keys = ["heatEmpty", "heatLow", "heatMid", "heatHigh", "heatFull"] as const;
  const style: CSSProperties = {
    width: "var(--tok-space-md)",
    height: "var(--tok-space-md)",
    backgroundColor: cssVar(keys[level]),
  };
  return <span style={style} />;
}

export type { ThemeName };