import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Input,
  Modal,
  Progress,
  Segmented,
  Select,
  Spin,
  Tag,
  message,
} from "antd";
import {
  ApartmentOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { categoryCssVar } from "@strayt/tokens";
import type {
  CategoryRuleIn,
  CategoryRuleOut,
  FlashcardOut,
  GraphBody,
  GraphDraftBody,
  GraphNodeOut,
  MasteryStatsOut,
} from "@strayt/api-client";
import { useApp } from "../store";

const MASTERY_ORDER = ["no", "mid", "yes"] as const;
const MASTERY_LABEL: Record<string, string> = { no: "未掌握", mid: "掌握中", yes: "已掌握" };
const MASTERY_COLOR: Record<string, string> = {
  no: "var(--tok-masteryNo)",
  mid: "var(--tok-masteryMid)",
  yes: "var(--tok-masteryYes)",
};

/** 与服务端 `app/graph/progress.py::next_mastery` 同规则：对升一档，错降一档，两端钳位。 */
function nextMastery(current: string, correct: boolean): "no" | "mid" | "yes" {
  const idx = MASTERY_ORDER.indexOf(current as (typeof MASTERY_ORDER)[number]);
  const base = idx < 0 ? 1 : idx;
  const step = correct ? 1 : -1;
  return MASTERY_ORDER[Math.max(0, Math.min(MASTERY_ORDER.length - 1, base + step))];
}

const NODE_W = 210;
const NODE_H = 42;
const ROW_GAP = 34;
const COL_GAP = 44;
const TOP = 58;
const BOTTOM = 48;

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

interface DraftNode {
  key: string;
  name: string;
  summary: string;
  quote: string;
  loc_page: number;
  category_key: string;
  edited?: boolean;
}

export function GraphPage({ initialProjectId }: { initialProjectId?: string }) {
  const { snapshot } = useApp();
  const graphProjects = useMemo(
    () => (snapshot?.projects ?? []).filter((p) => p.type === "graph"),
    [snapshot],
  );
  const [projectId, setProjectId] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (initialProjectId) setProjectId(initialProjectId);
  }, [initialProjectId]);

  const [view, setView] = useState<string>("graph");

  const body =
    projectId && graphProjects.some((p) => p.id === projectId) ? (
      view === "flash" ? (
        <FlashcardsView projectId={projectId} />
      ) : (
        <GraphView projectId={projectId} />
      )
    ) : (
      <Empty description="当前没有图谱型项目。先在项目页创建一个「图谱」项目，上传资料并跑图谱抽取任务。" />
    );

  return (
    <div>
      <div className="strayt-row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>知识图谱</h3>
        <div className="strayt-row">
          <Select
            placeholder="选择图谱项目"
            value={projectId}
            style={{ width: "calc(var(--tok-spaceLg-px) * 8)" }}
            onChange={(v) => setProjectId(v)}
            options={graphProjects.map((p) => ({ value: p.id, label: p.name }))}
          />
          <Segmented options={[{ label: "图谱", value: "graph" }, { label: "闪卡", value: "flash" }]} value={view} onChange={(v) => setView(String(v))} />
        </div>
      </div>
      <div style={{ marginTop: "var(--tok-spaceMd-px)" }}>{body}</div>
    </div>
  );
}

// ============================================================================
// 图谱视图（F13 图谱 + F16 掌握度）
// ============================================================================

function GraphView({ projectId }: { projectId: string }) {
  const { domain, save } = useApp();
  const [graph, setGraph] = useState<GraphBody | null>(null);
  const [stats, setStats] = useState<MasteryStatsOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState<GraphDraftBody | null>(null);
  const [draftJobId, setDraftJobId] = useState<string | undefined>(undefined);
  const [draftOpen, setDraftOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<GraphNodeOut | null>(null);
  const [rulesOpen, setRulesOpen] = useState(false);

  const load = useCallback(
    async (which?: {
      draft?: GraphDraftBody | null;
      draftJob?: string | null;
      graph?: GraphBody | null;
      stats?: MasteryStatsOut | null;
    }) => {
      if (!domain) return;
      try {
        const [g, s, jobs] = await Promise.all([
          domain.getGraph(projectId),
          domain.getMasteryStats(projectId),
          domain.listJobs({ projectId, limit: 100 }),
        ]);
        setGraph(g);
        setStats(s);
        const latest = jobs
          .filter((j) => j.type === "graph_extract" && j.status === "success")
          .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
        if (which?.draft !== undefined) {
          setDraft(which.draft);
          setDraftJobId(which.draftJob ?? undefined);
        } else if (latest) {
          try {
            const dr = await domain.getGraphDraft(latest.id);
            if (dr.status !== "confirmed") {
              setDraft(dr.draft);
              setDraftJobId(latest.id);
            } else {
              setDraft(null);
              setDraftJobId(undefined);
            }
          } catch {
            setDraft(null);
            setDraftJobId(undefined);
          }
        } else {
          setDraft(null);
          setDraftJobId(undefined);
        }
      } catch (err) {
        message.error(`加载图谱失败：${(err as Error).message}`);
      } finally {
        setLoading(false);
      }
    },
    [domain, projectId],
  );

  useEffect(() => {
    setLoading(true);
    setGraph(null);
    setDraft(null);
    void load();
  }, [load]);

  const moveNode = async (nodeId: string, categoryId: string) => {
    try {
      // 走弱同步写通道（node_category 实体）：在线直传优先，离线入队重放
      const res = await save({
        entity: "node_category",
        entity_id: nodeId,
        patch: { category_id: categoryId },
      });
      if (res === "error") throw new Error("服务端拒绝写入");
      // 乐观更新：立刻反映到本地，避免等 bootstrap 回来才变
      setGraph((prev) =>
        prev
          ? { ...prev, nodes: prev.nodes.map((n) => (n.id === nodeId ? { ...n, category_id: categoryId } : n)) }
          : prev,
      );
      message.success("已调整分类归属");
      void load();
    } catch (err) {
      message.error(`调整分类失败：${(err as Error).message}`);
    }
  };

  if (loading) return <Spin style={{ display: "block", margin: "var(--tok-spaceXl-px) auto" }} />;

  const hasGraph = (graph?.nodes.length ?? 0) > 0;
  const match = search.trim().toLowerCase();

  return (
    <div>
      {draft && !draftOpen ? (
        <Alert
          type="info"
          showIcon
          message={`有未确认的抽取草稿：${draft.nodes.length} 个节点 / ${draft.categories.length} 个分类`}
          description={
            draft.warnings.length
              ? `抽取提示：${draft.warnings.join("；")}`
              : "确认（可先删错抽、调分类、改内容）后才会正式入库，并覆盖旧图谱。"
          }
          action={
            <Button type="primary" icon={<ApartmentOutlined />} onClick={() => setDraftOpen(true)}>
              预览并确认
            </Button>
          }
          style={{ marginBottom: "var(--tok-spaceMd-px)" }}
        />
      ) : null}

      <div className="strayt-row" style={{ justifyContent: "space-between" }}>
        <div className="strayt-row" style={{ gap: "var(--tok-spaceLg-px)" }}>
          <Stat name="节点" value={stats?.total ?? 0} color="var(--tok-colorText)" />
          <Stat name="未掌握" value={stats?.by_mastery.no ?? 0} color="var(--tok-masteryNo)" />
          <Stat name="掌握中" value={stats?.by_mastery.mid ?? 0} color="var(--tok-masteryMid)" />
          <Stat name="已掌握" value={stats?.by_mastery.yes ?? 0} color="var(--tok-masteryYes)" />
          {stats?.total ? <Stat name="掌握率" value={`${(stats.mastery_ratio * 100).toFixed(0)}%`} color="var(--tok-colorPrimary)" /> : null}
        </div>
        <div className="strayt-row">
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="搜名称 / 概要 / 摘录"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ width: "calc(var(--tok-spaceLg-px) * 10)" }}
          />
          <Button icon={<SettingOutlined />} onClick={() => setRulesOpen(true)}>
            分类规则
          </Button>
        </div>
      </div>

      <div style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        {hasGraph ? (
          <GraphSvg graph={graph!} search={match} onSelect={setSelected} onMoveNode={(a, b) => void moveNode(a, b)} />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这张图还是空的——跑完「图谱抽取」任务后，到这里预览确认入库" />
        )}
      </div>

      {(graph?.categories.length ?? 0) > 0 ? (
        <div className="strayt-row" style={{ gap: "var(--tok-spaceLg-px)", marginTop: "var(--tok-spaceMd-px)" }}>
          {graph!.categories.map((c, i) => (
            <span key={c.id} className="strayt-row" style={{ gap: "var(--tok-spaceXs-px)" }}>
              <span style={{ width: "var(--tok-spaceSm-px)", height: "var(--tok-spaceSm-px)", background: categoryCssVar(i % 8) }} />
              <span className="strayt-muted">{truncate(c.name, 12)}</span>
            </span>
          ))}
        </div>
      ) : null}

      <NodeDrawer node={selected} stats={stats} onClose={() => setSelected(null)} onMasteryChanged={() => void load()} />

      <CategoryRulesDrawer open={rulesOpen} projectId={projectId} onClose={() => setRulesOpen(false)} />

      {draft ? (
        <DraftModal
          open={draftOpen}
          draft={draft}
          jobId={draftJobId}
          onClose={() => setDraftOpen(false)}
          onConfirmed={() => {
            setDraftOpen(false);
            setDraft(null);
            setDraftJobId(undefined);
            void load({ draft: null, draftJob: null });
          }}
          onDismiss={() => {
            setDraftOpen(false);
            setDraft(null);
            setDraftJobId(undefined);
          }}
        />
      ) : null}
    </div>
  );
}

function Stat({ name, value, color }: { name: string; value: number | string; color: string }) {
  return (
    <div className="strayt-row" style={{ gap: "var(--tok-spaceXs-px)" }}>
      <span style={{ width: "var(--tok-spaceSm-px)", height: "var(--tok-spaceSm-px)", borderRadius: "var(--tok-borderRadius-px)", background: color }} />
      <span className="strayt-muted">{name}</span>
      <span style={{ fontWeight: 700 }}>{value}</span>
    </div>
  );
}

// ============================================================================
// SVG 图：分类成列，掌握度三色描边 + 左色条（F13 分区着色 + F16）
// ============================================================================

function GraphSvg({
  graph,
  search,
  onSelect,
  onMoveNode,
}: {
  graph: GraphBody;
  search: string;
  onSelect: (n: GraphNodeOut) => void;
  onMoveNode?: (nodeId: string, categoryId: string) => void;
}) {
  const [dragId, setDragId] = useState<string | null>(null);
  const [overCol, setOverCol] = useState<string | null>(null);
  const cats = graph.categories;
  const byCat = new Map<string | null, GraphNodeOut[]>();
  for (const n of graph.nodes) {
    const k = n.category_id ?? null;
    byCat.set(k, [...(byCat.get(k) ?? []), n]);
  }
  const colNames = new Map<string, string>();
  graph.categories.forEach((c) => colNames.set(c.id, c.name));
  const groups: Array<{ key: string | null; name: string; nodes: GraphNodeOut[] }> = cats.map((c) => ({
    key: c.id,
    name: c.name,
    nodes: byCat.get(c.id) ?? [],
  }));
  if (byCat.has(null)) groups.push({ key: null, name: "未分类", nodes: byCat.get(null) ?? [] });

  const colCount = Math.max(groups.length, 1);
  const maxRows = Math.max(...groups.map((g) => g.nodes.length), 1);
  const position = new Map<string, { x: number; y: number }>();
  groups.forEach((g, gi) => {
    g.nodes.forEach((n, i) => {
      position.set(n.id, { x: gi * (NODE_W + COL_GAP) + 10, y: TOP + i * (NODE_H + ROW_GAP) });
    });
  });

  const width = colCount * NODE_W + (colCount - 1) * COL_GAP + 30;
  const height = TOP + maxRows * NODE_H + (maxRows - 1) * ROW_GAP + BOTTOM;

  const isHit = (n: GraphNodeOut) =>
    search === "" ||
    n.name.toLowerCase().includes(search) ||
    (n.card_summary ?? "").toLowerCase().includes(search) ||
    (n.quote?.text ?? "").toLowerCase().includes(search);

  return (
    <div style={{ overflow: "auto" }}>
      <svg viewBox={`0 0 ${width} ${height}`} style={{ minWidth: width, display: "block" }}>
        {groups.map((g, gi) => (
          <g key={g.key ?? "__none__"}>
            <text
              x={gi * (NODE_W + COL_GAP) + 12}
              y={24}
              fill={categoryCssVar(gi % 8)}
              style={{ fontSize: "var(--tok-fontSize-px)", fontWeight: 700 }}
            >
              {truncate(g.name, 12)}
            </text>
            <rect x={gi * (NODE_W + COL_GAP) + 8} y={34} width={NODE_W} height={2} fill={categoryCssVar(gi % 8)} opacity={0.35} />
            {/* 整列作为拖放目标：把节点拖到列头即改归属（F12 手动调整） */}
            <rect
              x={gi * (NODE_W + COL_GAP) + 6}
              y={4}
              width={NODE_W + 4}
              height={height - 4}
              fill="var(--tok-colorPrimaryBg)"
              opacity={overCol === g.key ? 0.12 : 0}
              rx={4}
              onDragOver={(e) => {
                if (!dragId || g.key === null) return;
                e.preventDefault();
                setOverCol(g.key);
              }}
              onDragLeave={() => setOverCol((c) => (c === g.key ? null : c))}
              onDrop={(e) => {
                e.preventDefault();
                if (dragId && g.key !== null && onMoveNode) onMoveNode(dragId, g.key);
                setDragId(null);
                setOverCol(null);
              }}
              style={{ pointerEvents: dragId ? "auto" : "none" }}
            />
          </g>
        ))}
        <g>
          {graph.edges.map((e, i) => {
            const a = position.get(e.from_node);
            const b = position.get(e.to_node);
            if (!a || !b) return <g key={`e${i}`} />;
            const x1 = a.x + NODE_W;
            const y1 = a.y + NODE_H / 2;
            const x2 = b.x;
            const y2 = b.y + NODE_H / 2;
            const d = `M ${x1} ${y1} C ${(x1 + x2) / 2} ${y1}, ${(x1 + x2) / 2} ${y2}, ${x2} ${y2}`;
            return <path key={`e${i}`} d={d} fill="none" stroke="var(--tok-chartEdge)" strokeWidth={1} opacity={0.55} />;
          })}
        </g>
        {graph.nodes.map((n) => {
          const p = position.get(n.id);
          if (!p) return <g key={n.id} />;
          const hit = isHit(n);
          const mColor = MASTERY_COLOR[n.mastery] ?? "var(--tok-colorText)";
          return (
            <g
              key={n.id}
              onClick={() => onSelect(n)}
              {...({ draggable: Boolean(onMoveNode) } as Record<string, unknown>)}
              onDragStart={(e) => {
                e.dataTransfer.effectAllowed = "move";
                e.dataTransfer.setData("text/plain", n.id);
                setDragId(n.id);
              }}
              onDragEnd={() => {
                setDragId(null);
                setOverCol(null);
              }}
              style={{ cursor: onMoveNode ? "grab" : "pointer" }}
              transform={`translate(${p.x}, ${p.y})`}
              opacity={dragId === n.id ? 0.5 : 1}
            >
              <rect
                width={NODE_W}
                height={NODE_H}
                rx={2}
                fill="var(--tok-colorBgContainer)"
                stroke={hit ? "var(--tok-focusOutline)" : mColor}
                strokeWidth={hit ? 2 : 1.5}
              />
              <rect x={0} y={0} width={4} height={NODE_H} fill={mColor} />
              <text x={12} y={18} fill="var(--tok-colorText)" style={{ fontSize: "var(--tok-fontSize-px)" }}>
                {truncate(n.name, 18)}
              </text>
              <text x={12} y={34} fill="var(--tok-colorText)" opacity={0.7} style={{ fontSize: "var(--tok-fontSize-px)" }}>
                {truncate(n.card_summary ?? n.quote?.text ?? "", 22)}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

const MATCH_ON_LABEL: Record<CategoryRuleIn["match_on"], string> = {
  file_name: "文件名",
  unit_title: "篇章标题",
};
const KIND_LABEL: Record<CategoryRuleIn["kind"], string> = {
  prefix: "前缀",
  contains: "包含",
  regex: "正则",
};

/** F12 分类规则管理：整表覆盖保存。规则在**下一次**图谱抽取入库时生效。 */
function CategoryRulesDrawer({
  open,
  projectId,
  onClose,
}: {
  open: boolean;
  projectId: string;
  onClose: () => void;
}) {
  const { domain } = useApp();
  const [rules, setRules] = useState<CategoryRuleIn[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(async () => {
    if (!domain) return;
    setLoading(true);
    try {
      const list = await domain.listCategoryRules(projectId);
      setRules(list.map((r: CategoryRuleOut) => ({ ...r, enabled: r.enabled })));
    } catch (err) {
      message.error(`加载分类规则失败：${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [domain, projectId]);

  useEffect(() => {
    if (open) void reload();
  }, [open, reload]);

  const patch = (i: number, next: Partial<CategoryRuleIn>) =>
    setRules((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...next } : r)));

  const save = async () => {
    setSaving(true);
    try {
      await domain!.putCategoryRules(
        projectId,
        rules.map((r) => ({ ...r, pattern: r.pattern.trim(), category: r.category.trim() })),
      );
      message.success("分类规则已保存，下次抽取入库时生效");
      onClose();
    } catch (err) {
      message.error(`保存分类规则失败：${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={520}
      title="一级分类规则（F12）"
      extra={
        <Button type="primary" loading={saving} onClick={() => void save()}>
          保存
        </Button>
      }
    >
      <p className="strayt-muted">
        规则在图谱抽取**入库前**按「文件名 / 篇章标题」匹配，命中即把整篇归到指定一级分类（取第一条命中）；没命中的保持模型判定。保存后需重新跑一次抽取任务才生效。
      </p>
      <Button
        icon={<PlusOutlined />}
        onClick={() =>
          setRules((prev) => [...prev, { match_on: "file_name", kind: "prefix", pattern: "", category: "", enabled: true }])
        }
        style={{ marginBottom: "var(--tok-spaceSm-px)" }}
      >
        新增规则
      </Button>
      {loading ? (
        <Spin />
      ) : rules.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有规则——不配规则就完全按模型判定分类" />
      ) : (
        rules.map((r, i) => (
          <Card key={i} size="small" style={{ marginBottom: "var(--tok-spaceSm-px)" }}>
            <div className="strayt-row" style={{ gap: "var(--tok-spaceSm-px)", marginBottom: "var(--tok-spaceSm-px)" }}>
              <Select<CategoryRuleIn["match_on"]>
                size="small"
                value={r.match_on}
                style={{ width: "calc(var(--tok-spaceLg-px) * 5)" }}
                options={(Object.keys(MATCH_ON_LABEL) as Array<CategoryRuleIn["match_on"]>).map((k) => ({ value: k, label: MATCH_ON_LABEL[k] }))}
                onChange={(v) => patch(i, { match_on: v })}
              />
              <Select<CategoryRuleIn["kind"]>
                size="small"
                value={r.kind}
                style={{ width: "calc(var(--tok-spaceLg-px) * 4)" }}
                options={(Object.keys(KIND_LABEL) as Array<CategoryRuleIn["kind"]>).map((k) => ({ value: k, label: KIND_LABEL[k] }))}
                onChange={(v) => patch(i, { kind: v })}
              />
              <Button
                size="small"
                type="text"
                icon={<DeleteOutlined />}
                onClick={() => setRules((prev) => prev.filter((_, idx) => idx !== i))}
              />
            </div>
            <Input
              size="small"
              placeholder={r.kind === "regex" ? "匹配内容（如 ^数学）" : "匹配内容（如 数学）"}
              value={r.pattern}
              onChange={(e) => patch(i, { pattern: e.target.value })}
              style={{ marginBottom: "var(--tok-spaceSm-px)" }}
            />
            <Input
              size="small"
              placeholder="归入的一级分类名（如 高中数学）"
              value={r.category}
              onChange={(e) => patch(i, { category: e.target.value })}
            />
          </Card>
        ))
      )}
    </Drawer>
  );
}

function NodeDrawer({
  node,
  stats,
  onClose,
  onMasteryChanged,
}: {
  node: GraphNodeOut | null;
  stats: MasteryStatsOut | null;
  onClose: () => void;
  onMasteryChanged: () => void;
}) {
  const { save } = useApp();
  const [mastery, setMastery] = useState<string | undefined>(undefined);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setMastery(node?.mastery);
  }, [node?.id, node?.mastery]);

  if (!node) return null;

  const setM = async (m: string) => {
    if (m === mastery) return;
    setSaving(true);
    try {
      // 走弱同步写通道（node_mastery 实体）：在线直传优先，离线入队重放
      const res = await save({ entity: "node_mastery", entity_id: node.id, patch: { mastery: m } });
      if (res === "error") throw new Error("服务端拒绝写入");
      onMasteryChanged();
      setMastery(m);
      onClose();
    } catch (err) {
      message.error(`保存掌握度失败：${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer
      open={!!node}
      onClose={onClose}
      width={360}
      title={<span className="strayt-row"><ThunderboltOutlined /> {node.name}</span>}
    >
      {node.card_summary ? <p>{node.card_summary}</p> : null}
      {node.quote?.text ? (
        <blockquote className="strayt-muted" style={{ borderLeft: "var(--tok-lineWidth-px) solid var(--tok-chartEdge)", marginInline: 0, paddingLeft: "var(--tok-spaceSm-px)" }}>
          {node.quote.text}
          <div style={{ marginTop: "var(--tok-spaceXs-px)" }}>出处：第 {node.quote.page ?? "?"} 页</div>
        </blockquote>
      ) : null}
      <div style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        <div className="strayt-muted" style={{ marginBottom: "var(--tok-spaceSm-px)" }}>掌握度（F16，离线也走弱同步）</div>
        <Segmented
          value={mastery}
          onChange={(v) => void setM(String(v))}
          disabled={saving}
          options={MASTERY_ORDER.map((m) => ({ value: m, label: MASTERY_LABEL[m] }))}
        />
      </div>
      {stats ? (
        <div className="strayt-muted" style={{ marginTop: "var(--tok-spaceLg-px)" }}>
          {stats.by_category
            .filter((c) => c.total > 0)
            .map((c) => (
              <div key={c.category_id ?? "none"} style={{ marginBottom: "var(--tok-spaceSm-px)" }}>
                <div className="strayt-row" style={{ justifyContent: "space-between" }}>
                  <span>{c.name}</span>
                  <span>{c.yes}/{c.mid}/{c.no} 已/中/未</span>
                </div>
                <Progress
                  percent={c.total ? Math.round((c.yes / c.total) * 100) : 0}
                  showInfo={false}
                  size="small"
                />
              </div>
            ))}
        </div>
      ) : null}
    </Drawer>
  );
}

// ============================================================================
// 草稿预览与确认（F11）
// ============================================================================

function DraftModal({
  open,
  draft,
  jobId,
  onClose,
  onConfirmed,
  onDismiss,
}: {
  open: boolean;
  draft: GraphDraftBody | null;
  jobId?: string;
  onClose: () => void;
  onConfirmed: () => void;
  onDismiss: () => void;
}) {
  const { domain } = useApp();
  const [deletes, setDeletes] = useState<Set<string>>(new Set());
  const [reassign, setReassign] = useState<Record<string, string>>({});
  const [edits, setEdits] = useState<Record<string, { name?: string; summary?: string }>>({});
  const [editing, setEditing] = useState<{ key: string; name: string; summary: string } | null>(null);
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    if (open) {
      setDeletes(new Set());
      setReassign({});
      setEdits({});
      setEditing(null);
    }
  }, [open, draft?.nodes.length]);

  if (!open || !draft || !jobId) return null;

  const nodeHas = (n: DraftNode) => (deletes.has(n.key) ? "n" : reassign[n.key] ?? n.category_key);
  const groups = new Map<string, { node: DraftNode; state: string }[]>();
  for (const n of draft.nodes) {
    const state = nodeHas(n);
    groups.set(state, [...(groups.get(state) ?? []), { node: n, state }]);
  }

  const confirm = async () => {
    setConfirming(true);
    try {
      const body: {
        delete_keys: string[];
        reassign: Record<string, string>;
        edits: Record<string, { name?: string; summary?: string }>;
      } = {
        delete_keys: [...deletes],
        reassign,
        edits: {},
      };
      for (const [k, e] of Object.entries(edits)) {
        if (e.name || e.summary) body.edits[k] = e;
      }
      const res = await domain!.confirmGraph(jobId, body);
      message.success(`已入库：${res.node_count} 节点 / ${res.category_count} 分类 / ${res.edge_count} 边`);
      onConfirmed();
    } catch (err) {
      message.error(`确认失败：${(err as Error).message}`);
    } finally {
      setConfirming(false);
    }
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      width={720}
      title="抽取结果预览（确认才入库，可删/调分类/改内容）"
      footer={
        <div className="strayt-row" style={{ justifyContent: "flex-end", gap: "var(--tok-spaceSm-px)" }}>
          <Button onClick={onDismiss}>先不确认</Button>
          <Button type="primary" icon={<CheckCircleOutlined />} loading={confirming} onClick={() => void confirm()}>
            确认入库
          </Button>
        </div>
      }
    >
      <div style={{ maxHeight: "60vh", overflow: "auto" }}>
        {[...groups.entries()].map(([state, items]) => {
          const cat = draft.categories.find((c) => c.key === state);
          const colorI = draft.categories.findIndex((c) => c.key === state);
          return (
            <div key={state} style={{ marginBottom: "var(--tok-spaceMd-px)" }}>
              <div className="strayt-row" style={{ gap: "var(--tok-spaceXs-px)", marginBottom: "var(--tok-spaceSm-px)" }}>
                <span style={{ width: "var(--tok-spaceSm-px)", height: "var(--tok-spaceSm-px)", background: categoryCssVar(Math.max(colorI, 0) % 8) }} />
                <b>{cat?.name ?? state}</b>
                <span className="strayt-muted">{items.length}</span>
              </div>
              {items.map(({ node }) => (
                <Card
                  key={node.key}
                  size="small"
                  style={{
                    marginBottom: "var(--tok-spaceSm-px)",
                    opacity: deletes.has(node.key) ? 0.5 : 1,
                    borderColor: deletes.has(node.key) ? "var(--tok-colorError)" : undefined,
                  }}
                >
                  <div className="strayt-row" style={{ justifyContent: "space-between" }}>
                    <div>
                      <div style={{ fontWeight: 600 }}>{node.name}</div>
                      <div className="strayt-muted">{truncate(node.summary, 48)}</div>
                      {node.loc_page ? <div className="strayt-muted">第 {node.loc_page} 页</div> : null}
                    </div>
                    <div className="strayt-row" style={{ gap: "var(--tok-spaceXs-px)" }}>
                      <Select
                        size="small"
                        value={deletes.has(node.key) ? "删除" : (reassign[node.key] ?? node.category_key)}
                        style={{ width: "calc(var(--tok-spaceSm-px) * 14)" }}
                        onClick={(e) => e.stopPropagation()}
                        onChange={(v) => {
                          if (v === "删除") setDeletes((d) => new Set(d).add(node.key));
                          else {
                            setDeletes((d) => {
                              const nd = new Set(d);
                              nd.delete(node.key);
                              return nd;
                            });
                            setReassign((r) => ({ ...r, [node.key]: v }));
                          }
                        }}
                        options={[
                          { value: "删除", label: "删" },
                          ...draft.categories.map((c) => ({ value: c.key, label: truncate(c.name, 6) })),
                        ]}
                      />
                      <Button
                        size="small"
                        icon={<EditOutlined />}
                        onClick={() => setEditing({ key: node.key, name: node.name, summary: node.summary })}
                        disabled={deletes.has(node.key)}
                      />
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          );
        })}
        <EditNodeModal
          editing={editing}
          onClose={() => setEditing(null)}
          onSave={(k, name, summary) => {
            setEdits((e) => ({ ...e, [k]: { name: name || undefined, summary: summary || undefined } }));
            setEditing(null);
          }}
          draft={draft}
        />
      </div>
    </Modal>
  );
}

function EditNodeModal({
  editing,
  draft,
  onClose,
  onSave,
}: {
  editing: { key: string; name: string; summary: string } | null;
  draft: GraphDraftBody;
  onClose: () => void;
  onSave: (key: string, name: string, summary: string) => void;
}) {
  const [name, setName] = useState("");
  const [summary, setSummary] = useState("");
  useEffect(() => {
    setName(editing?.name ?? "");
    setSummary(editing?.summary ?? "");
  }, [editing?.key]);
  const node = editing ? draft.nodes.find((n) => n.key === editing.key) : null;
  return (
    <Modal
      open={!!editing}
      title="修改节点（仅改草稿，确认入库才生效）"
      onCancel={onClose}
      onOk={() => editing && onSave(editing.key, name.trim(), summary.trim())}
      okButtonProps={{ disabled: name.trim() === "" }}
    >
      <Input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="名称"
        style={{ marginBottom: "var(--tok-spaceSm-px)" }}
      />
      <Input.TextArea
        value={summary}
        onChange={(e) => setSummary(e.target.value)}
        placeholder="概要（200 字内，超出将被截断）"
        rows={4}
        maxLength={240}
        showCount
      />
      {node?.quote ? (
        <div className="strayt-muted" style={{ marginTop: "var(--tok-spaceSm-px)" }}>
          原文摘录（不随编辑改动）：{truncate(node.quote, 60)}
        </div>
      ) : null}
    </Modal>
  );
}

// ============================================================================
// 闪卡自测（F15）
// ============================================================================

function FlashcardsView({ projectId }: { projectId: string }) {
  const { domain, save } = useApp();
  const [cards, setCards] = useState<FlashcardOut[] | null>(null);
  const [index, setIndex] = useState(0);
  const [revealed, setRevealed] = useState(false);
  const [filter, setFilter] = useState<string>("all");
  const [tally, setTally] = useState<{ right: number; wrong: number }>({ right: 0, wrong: 0 });
  const [generating, setGenerating] = useState(false);
  const [stat, setStat] = useState<{ generated: number; skipped: number } | null>(null);

  const load = useCallback(async () => {
    if (!domain) return;
    try {
      const list = await domain.listFlashcards(projectId, filter === "all" ? undefined : (filter as "no" | "mid" | "yes"));
      setCards(list);
      setIndex(0);
      setRevealed(false);
      setTally({ right: 0, wrong: 0 });
    } catch (err) {
      message.error(`加载卡组失败：${(err as Error).message}`);
    }
  }, [domain, projectId, filter]);

  useEffect(() => {
    void load();
  }, [load]);

  const generate = async () => {
    setGenerating(true);
    try {
      const res = await domain!.generateFlashcards(projectId);
      setStat({ generated: res.created, skipped: res.skipped });
      message.success(`重建卡组：${res.created} 张（跳过 ${res.skipped} 张无概要/摘录的）`);
      await load();
    } catch (err) {
      message.error(`生成失败：${(err as Error).message}`);
    } finally {
      setGenerating(false);
    }
  };

  const answer = async (correct: boolean) => {
    if (!cards) return;
    const card = cards[index];
    if (card) {
      const next = nextMastery(card.mastery, correct);
      try {
        // 在线直传：服务端 next_mastery 权威推进（RTM F15）
        await domain!.postFlashcardResult(card.id, correct);
      } catch {
        // 离线/失败 → 走弱同步 node_mastery 写队列，联网后 LWW 重放
        await save({ entity: "node_mastery", entity_id: card.node_id, patch: { mastery: next } });
      }
    }
    setTally((t) => ({ right: t.right + (correct ? 1 : 0), wrong: t.wrong + (correct ? 0 : 1) }));
    if (index + 1 >= cards.length) {
      setRevealed(false);
      setIndex(cards.length);
    } else {
      setIndex((i) => i + 1);
      setRevealed(false);
    }
  };

  if (cards === null) return <Spin style={{ display: "block", margin: "var(--tok-spaceXl-px) auto" }} />;
  const done = tally.right + tally.wrong;

  return (
    <div style={{ maxWidth: 560, margin: "0 auto" }}>
      <div className="strayt-row" style={{ justifyContent: "space-between" }}>
        <div className="strayt-row" style={{ gap: "var(--tok-spaceSm-px)" }}>
          <Select
            size="small"
            value={filter}
            style={{ width: "calc(var(--tok-spaceSm-px) * 12)" }}
            onChange={(v) => setFilter(v)}
            options={[
              { value: "all", label: "全部" },
              { value: "no", label: "只看未掌握" },
              { value: "mid", label: "掌握中" },
              { value: "yes", label: "已掌握" },
            ]}
          />
          <Button size="small" icon={<ReloadOutlined />} loading={generating} onClick={() => void generate()}>
            重建卡组
          </Button>
        </div>
        <span className="strayt-muted">
          {done}/{cards.length} {cards.length === 0 ? "" : `· 对 ${tally.right} 错 ${tally.wrong}`}
        </span>
      </div>
      {stat ? (
        <Alert
          type="info"
          showIcon
          message={`卡组重建：${stat.generated} 张，跳过 ${stat.skipped} 张无内容的节点`}
          closable
          onClose={() => setStat(null)}
          style={{ marginTop: "var(--tok-spaceMd-px)" }}
        />
      ) : null}

      {cards.length === 0 ? (
        <Empty
          style={{ marginTop: "var(--tok-spaceXl-px)" }}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="还没有闪卡。先确认图谱入库，再点「重建卡组」（确定性生成，不花钱）。"
        />
      ) : index >= cards.length ? (
        <Card style={{ marginTop: "var(--tok-spaceLg-px)", textAlign: "center" }}>
          <CheckCircleOutlined style={{ color: "var(--tok-colorSuccess)", fontSize: 32 }} />
          <h3>这一轮过完了</h3>
          <p className="strayt-muted">
            共 {cards.length} 张 · 对 {tally.right} · 错 {tally.wrong}
            {tally.right >= cards.length && cards.length > 0 ? "，全对，掌握度正在爬升。" : ""}
          </p>
          <div className="strayt-row" style={{ justifyContent: "center", gap: "var(--tok-spaceSm-px)" }}>
            <Button icon={<ReloadOutlined />} onClick={() => { setIndex(0); setRevealed(false); setTally({ right: 0, wrong: 0 }); }}>
              再来一轮
            </Button>
          </div>
        </Card>
      ) : (
        <Card style={{ marginTop: "var(--tok-spaceLg-px)", minHeight: 240 }}>
          <Progress percent={Math.round((done / cards.length) * 100)} showInfo={false} size="small" />
          {(() => {
            const card = cards[index];
            return (
              <div style={{ textAlign: "center", padding: "var(--tok-spaceLg-px) 0" }}>
                <Tag color="var(--tok-colorPrimary)">{MASTERY_LABEL[card.mastery] ?? card.mastery}</Tag>
                {card.page ? <Tag className="strayt-muted">第 {card.page} 页</Tag> : null}
                <h2 style={{ marginTop: "var(--tok-spaceLg-px)" }}>{card.question}</h2>
                {revealed ? (
                  <>
                    <p style={{ marginTop: "var(--tok-spaceMd-px)" }}>{card.answer}</p>
                    <div className="strayt-row" style={{ justifyContent: "center", gap: "var(--tok-spaceMd-px)", marginTop: "var(--tok-spaceLg-px)" }}>
                      <Button icon={<CloseCircleOutlined />} onClick={() => void answer(false)}>
                        没记住
                      </Button>
                      <Button type="primary" icon={<CheckCircleOutlined />} onClick={() => void answer(true)}>
                        记住了
                      </Button>
                    </div>
                  </>
                ) : (
                  <Button type="primary" size="large" icon={<EyeOutlined />} onClick={() => setRevealed(true)}>
                    显示答案
                  </Button>
                )}
              </div>
            );
          })()}
        </Card>
      )}
      <div className="strayt-muted" style={{ textAlign: "center", marginTop: "var(--tok-spaceMd-px)" }}>
        自测结果经「对/错」推进节点掌握度（未掌握 ↔ 掌握中 ↔ 已掌握）
      </div>
    </div>
  );
}