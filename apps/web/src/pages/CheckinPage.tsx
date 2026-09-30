import { useEffect, useMemo, useState } from "react";
import { Button, Card, Checkbox, Empty } from "antd";
import dayjs from "dayjs";
import { HeatCell } from "@strayt/ui";
import { useApp } from "../store";

const DEFAULT_ITEMS = ["学习打卡"];

function parseItems(raw: string | null): string[] {
  if (!raw) return DEFAULT_ITEMS;
  try {
    const v = JSON.parse(raw);
    if (Array.isArray(v)) return v.map(String);
  } catch (err) {
    void err;
  }
  return [raw];
}

export function CheckinPage() {
  const { api, snapshot, save, refresh } = useApp();
  const [items, setItems] = useState(DEFAULT_ITEMS);
  const [selected, setSelected] = useState<string[] | null>(null);
  const [saving, setSaving] = useState(false);

  const today = dayjs().format("YYYY-MM-DD");

  useEffect(() => {
    let alive = true;
    void api?.domain
      .getKv("checkin_items")
      .then((kv) => {
        if (alive) setItems(parseItems(kv.value ?? null));
      })
      .catch(() => void 0);
    return () => {
      alive = false;
    };
  }, [api]);

  const todayEntry = snapshot?.checkins.find((c) => c.date === today);
  const effective = selected ?? todayEntry?.items_json ?? [];

  const heat = useMemo(() => {
    const map = new Map<string, number>();
    for (const c of snapshot?.checkins ?? []) map.set(c.date, c.items_json.length);
    const cells: Array<{ date: string; level: 0 | 1 | 2 | 3 | 4 }> = [];
    for (let i = 29; i >= 0; i--) {
      const d = dayjs().subtract(i, "day").format("YYYY-MM-DD");
      const raw = map.get(d) ?? 0;
      cells.push({ date: d, level: (Math.min(raw, 4) as 0 | 1 | 2 | 3 | 4) });
    }
    return cells;
  }, [snapshot]);

  const streak = useMemo(() => {
    const dates = new Set((snapshot?.checkins ?? []).map((c) => c.date));
    let n = 0;
    for (let i = 0; ; i++) {
      if (dates.has(dayjs().subtract(i, "day").format("YYYY-MM-DD"))) n += 1;
      else break;
    }
    return n;
  }, [snapshot]);

  const todayTodos = useMemo(
    () => (snapshot?.plans ?? []).filter((p) => p.due_date === today && p.status !== "done"),
    [snapshot, today],
  );

  const saveCheckin = async () => {
    setSaving(true);
    try {
      const res = await save({ entity: "checkin", entity_id: today, patch: { items_json: effective } });
      if (res === "queued" || res === "direct") {
        setSelected(null);
        void refresh();
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <div className="strayt-row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>今日打卡</h3>
        <span className="strayt-muted">连续 {streak} 天</span>
      </div>

      <Card size="small" style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        <Checkbox.Group
          value={effective}
          onChange={(vals) => setSelected(vals as string[])}
          options={items.map((it) => ({ label: it, value: it }))}
          style={{ width: "100%" }}
        />
        <Button
          type="primary"
          block
          loading={saving}
          style={{ marginTop: "var(--tok-spaceMd-px)" }}
          onClick={() => void saveCheckin()}
        >
          保存今日打卡
        </Button>
        <p className="strayt-muted" style={{ marginBottom: 0 }}>
          离线时先入队，联网自动补传。
        </p>
      </Card>

      <Card size="small" title="近 30 天" style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(10, var(--tok-spaceMd-px))",
            gap: "var(--tok-spaceXs-px)",
          }}
        >
          {heat.map((cell) => (
            <HeatCell key={cell.date} level={cell.level} />
          ))}
        </div>
      </Card>

      <Card size="small" title="今天待办" style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        {todayTodos.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="今天没有待办计划" />
        ) : (
          todayTodos.map((t) => (
            <div key={t.id} className="strayt-row" style={{ justifyContent: "space-between", width: "100%" }}>
              <span>{t.title}</span>
              <Button
                size="small"
                type="primary"
                onClick={() =>
                  void save({ entity: "plan", entity_id: t.id, patch: { status: "done" } }).then(() =>
                    void refresh(),
                  )
                }
              >
                完成
              </Button>
            </div>
          ))
        )}
      </Card>
    </div>
  );
}