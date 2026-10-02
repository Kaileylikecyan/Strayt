import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Empty, Input, InputNumber, message, Modal, Segmented, Select, Spin, Switch, Tag } from "antd";
import {
  ArrowLeftOutlined,
  EditOutlined,
  MergeCellsOutlined,
  RedoOutlined,
  ScissorOutlined,
} from "@ant-design/icons";
import { useStraytTokens } from "@strayt/ui";
import { useApp } from "../store";
import type { PairOut, PieceOut } from "@strayt/api-client";
import {
  buildUnits,
  clampLastPos,
  clampSec,
  countDoneUnits,
  currentUnitIndex as findCurrentUnitIndex,
  formatCountdown,
  lastPosAfterReveal,
  paragraphUsable as allBlocksPresent,
  readGranularity,
  TIMER_FALLBACK_SEC,
  TIMER_MAX_SEC,
  TIMER_MIN_SEC,
  TIMER_PRESETS,
  writeGranularity,
  type Granularity,
  type Unit,
} from "./reciteUnits";

type Mode = "对照阅读" | "遮罩背诵" | "逐句递进";
const MODES: Mode[] = ["对照阅读", "遮罩背诵", "逐句递进"];

/** 提示语言：显示中文遮英文 / 显示英文遮中文 / 双语都遮（文档 F19.2）。 */
type HintLang = "zh" | "en" | "both";
const HINT_LANGS: { value: HintLang; label: string }[] = [
  { value: "zh", label: "显示中文遮英文" },
  { value: "en", label: "显示英文遮中文" },
  { value: "both", label: "双语都遮" },
];

/** 计时状态机：遮蔽 → 计时中 → 已揭示（文档 F19.3）。纯会话态，不入库。 */
type TimerPhase = "idle" | "running" | "revealed";

/** 计时默认时长存服务端 settings（通用 KV），粒度与当前计时都不入库。 */
const TIMER_KV_KEY = "recite.timer_default_seconds";

/** 中文按句末标点找最接近中点的断点；找不到就按中点等比切。 */
function splitZhHalf(text: string): { a: string; b: string } {
  const t = text.trim();
  if (t.length <= 1) return { a: t, b: "" };
  const boundaries: number[] = [];
  for (let i = 0; i < t.length; i += 1) {
    if ("。！？…；;".includes(t[i])) boundaries.push(i + 1);
  }
  const mid = Math.floor(t.length / 2);
  const target = boundaries.reduce((best, b) =>
    Math.abs(b - mid) < Math.abs(best - mid) ? b : best,
  );
  if (boundaries.length === 0 || target <= 0 || target >= t.length) {
    return { a: t.slice(0, mid), b: t.slice(mid) };
  }
  return { a: t.slice(0, target), b: t.slice(target) };
}

/** 英文按比例（对齐中文一半的字数占比）找最近的句末标点断点。 */
function splitEnHalf(text: string, ratio: number): { a: string; b: string } {
  const t = text.trim();
  if (t.length <= 1) return { a: t, b: "" };
  const target = Math.min(t.length - 1, Math.max(1, Math.floor(t.length * ratio)));
  const candidates = [target];
  for (let i = target; i >= 0; i -= 1) {
    if (".!?".includes(t[i])) candidates.push(i + 1);
    break;
  }
  for (let i = target; i < t.length; i += 1) {
    if (".!?".includes(t[i])) candidates.push(i + 1);
    break;
  }
  const best = candidates.reduce((b, c) => (Math.abs(c - target) < Math.abs(b - target) ? c : b));
  const cut = best >= 1 && best < t.length ? best : target;
  return { a: t.slice(0, cut), b: t.slice(cut) };
}

export function RecitePage({ initialProjectId }: { initialProjectId?: string | null }) {
  const { snapshot, domain, save } = useApp();
  const reciteProjects = useMemo(
    () => (snapshot?.projects ?? []).filter((p) => p.type === "recite"),
    [snapshot],
  );

  if (!domain) return null;

  const [projectId, setProjectId] = useState<string | null>(
    initialProjectId && reciteProjects.some((p) => p.id === initialProjectId) ? initialProjectId : null,
  );
  const [pieces, setPieces] = useState<PieceOut[] | null>(null);
  const [pieceId, setPieceId] = useState<string | null>(null);
  const [pairs, setPairs] = useState<PairOut[] | null>(null);
  const [loadingPieces, setLoadingPieces] = useState(false);

  useEffect(() => {
    if (!projectId) return;
    setLoadingPieces(true);
    setPieces(null);
    setPieceId(null);
    void domain
      .listPieces(projectId)
      .then((list) => setPieces(list))
      .catch(() => {
        // 离线：用本地 snapshot 的篇目兜底
        setPieces((snapshot?.pieces ?? []).filter((p) => p.project_id === projectId) as PieceOut[]);
      })
      .finally(() => setLoadingPieces(false));
  }, [projectId, snapshot, domain]);

  if (reciteProjects.length === 0) {
    return <Empty description="还没有背诵型项目，先去「项目」页建一个。" />;
  }

  if (!projectId) {
    return (
      <div>
        <h3>选择项目</h3>
        <Select
          style={{ width: "100%" }}
          placeholder="选择一个背诵型项目"
          options={reciteProjects.map((p) => ({ value: p.id, label: p.name }))}
          onChange={(v) => setProjectId(v)}
        />
      </div>
    );
  }

  if (pieceId && pairs) {
    return (
      <Cabin
        piece={pieces?.find((p) => p.id === pieceId)}
        pairs={pairs}
        onBackList={() => {
          setPieceId(null);
          setPairs(null);
        }}
        onSave={(lastPos, recited) =>
          void save({ entity: "piece_progress", entity_id: pieceId, patch: { last_pos: lastPos, recited } })
        }
        onSavePair={(pairKey, zh, en) =>
          void save({ entity: "pair_edit", entity_id: pairKey, patch: { zh, en, manually_edited: true } })
        }
        onReorder={(pairKey, seq) =>
          void save({ entity: "pair_edit", entity_id: pairKey, patch: { seq, manually_edited: true } })
        }
        onSplit={async (pairKey, halves) => {
          const list = await domain.splitPair(projectId, pieceId, {
            pair_key: pairKey,
            zh_a: halves.zh_a,
            en_a: halves.en_a,
            zh_b: halves.zh_b,
            en_b: halves.en_b,
          });
          setPairs(list);
        }}
        onMerge={async (pairKey, withKey) => {
          const list = await domain.mergePair(projectId, pieceId, {
            pair_key: pairKey,
            with_key: withKey,
          });
          setPairs(list);
        }}
        getKv={(key) => domain.getKv(key)}
      />
    );
  }

  const list = pieces ?? [];

  return (
    <div>
      <div className="strayt-row" style={{ justifyContent: "space-between" }}>
        <h3 style={{ margin: 0 }}>背诵舱</h3>
        <Button size="small" onClick={() => setProjectId(null)}>
          换项目
        </Button>
      </div>
      {loadingPieces ? (
        <Spin style={{ marginTop: "var(--tok-spaceLg-px)" }} />
      ) : list.length === 0 ? (
        <Empty description="该项目还没有加工出篇目。先去桌面端上传资料并运行加工任务。" />
      ) : (
        list.map((p) => (
          <div
            key={p.id}
            onClick={() => {
              setPieceId(p.id);
              setPairs(null);
              void domain
                .listPairs(projectId, p.id)
                .then((list) => setPairs(list))
                .catch(() => {
                  setPairs(
                    (snapshot?.pairs ?? [])
                      .filter((x) => (x as { piece_id?: string }).piece_id === p.id)
                      .sort((a, b) => (a as { seq: number }).seq - (b as { seq: number }).seq) as PairOut[],
                  );
                });
            }}
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              padding: "var(--tok-spaceMd-px)",
              background: "var(--tok-colorBgContainer)",
              borderBottom: "solid var(--tok-lineWidth-px)",
              borderBottomColor: "var(--tok-colorBgLayout)",
              cursor: "pointer",
            }}
          >
            <span>
              {p.title}
              <span className="strayt-muted" style={{ marginLeft: "var(--tok-spaceSm-px)" }}>
                {p.last_pos}/{p.pair_count || "-"}
              </span>
            </span>
            {p.recited ? <Tag color="var(--tok-colorPrimary)">已背</Tag> : null}
          </div>
        ))
      )}
    </div>
  );
}

const Cabin = ({
  piece,
  pairs,
  onBackList,
  onSave,
  onSavePair,
  onReorder,
  onSplit,
  onMerge,
  getKv,
}: {
  piece?: PieceOut;
  pairs: PairOut[];
  onBackList: () => void;
  onSave: (lastPos: number, recited: boolean) => void;
  onSavePair: (pairKey: string, zh: string, en: string) => void;
  onReorder: (pairKey: string, seq: number) => void;
  onSplit: (pairKey: string, halves: { zh_a: string; en_a: string; zh_b: string; en_b: string }) => Promise<void>;
  onMerge: (pairKey: string, withKey: string) => Promise<void>;
  getKv: (key: string) => Promise<{ key: string; value: string | null; updated_at?: string | null }>;
}) => {
  const tokens = useStraytTokens();

  // ---- 粒度：本地存（按篇目），缺块号自动降级为按句（文档 F19.1）----
  const hasBlocks = useMemo(() => allBlocksPresent(pairs), [pairs]);
  const [granularity, setGranularity] = useState<Granularity>(() => readGranularity(piece?.id));
  const effectiveGranularity: Granularity = granularity === "paragraph" && !hasBlocks ? "sentence" : granularity;

  const units = useMemo(() => buildUnits(pairs, effectiveGranularity), [pairs, effectiveGranularity]);

  // ---- 进度：只存句级 last_pos，按单元推导（文档 F19.1）----
  const [doneCount, setDoneCount] = useState(() => clampLastPos(piece?.last_pos, pairs.length));
  const [recited, setRecited] = useState(piece?.recited ?? false);

  const doneUnits = useMemo(() => countDoneUnits(units, doneCount), [units, doneCount]);
  /** 第一个没整段背完的单元；全背完时停在最后一个（用于揭示态显示）。 */
  const currentIdx = useMemo(() => findCurrentUnitIndex(units, doneCount), [units, doneCount]);
  const currentUnit = units[currentIdx] ?? null;

  // ---- 模式与提示语言 ----
  const [mode, setMode] = useState<Mode>("对照阅读");
  const [hintLang, setHintLang] = useState<HintLang>("zh");
  const [revealedUnits, setRevealedUnits] = useState<Set<number>>(new Set());

  // ---- 计时器：会话态，不入库（文档 F19.3）----
  const [timerPhase, setTimerPhase] = useState<TimerPhase>("idle");
  const [limitSec, setLimitSec] = useState(TIMER_FALLBACK_SEC);
  const [secondsLeft, setSecondsLeft] = useState(TIMER_FALLBACK_SEC);
  const [lastSpentSec, setLastSpentSec] = useState<number | null>(null);

  const [fontDelta, setFontDelta] = useState(0);
  const [edits, setEdits] = useState<Record<string, { zh: string; en: string }>>({});
  const [editing, setEditing] = useState<PairOut | null>(null);
  const [draft, setDraft] = useState({ zh: "", en: "" });
  const [reordering, setReordering] = useState(stringToKeys(pairs));
  const [dragKey, setDragKey] = useState<string | null>(null);
  const [overKey, setOverKey] = useState<string | null>(null);
  const [splitOpen, setSplitOpen] = useState(false);
  const [splitDraft, setSplitDraft] = useState({ zh_a: "", en_a: "", zh_b: "", en_b: "" });
  const seqRef = useRef<Record<string, number>>(buildSeqMap(pairs));

  useEffect(() => {
    seqRef.current = buildSeqMap(pairs);
  }, [pairs]);

  useEffect(() => setReordering(stringToKeys(pairs)), [pairs]);

  const shown = (p: PairOut) => edits[p.pair_key] ?? { zh: p.zh, en: p.en };

  const fontSize = tokens.fontSize + 2 * fontDelta;
  const lineHeight = tokens.lineHeight + 0.06 * fontDelta;

  // 粒度一换，单元序号全变，已揭示状态直接清空，避免张冠李戴。
  useEffect(() => {
    setRevealedUnits(new Set());
    setTimerPhase("idle");
    setLastSpentSec(null);
  }, [effectiveGranularity]);

  const saveRef = useRef(onSave);
  saveRef.current = onSave;
  useEffect(() => {
    saveRef.current(doneCount, recited);
  }, [doneCount, recited]);

  const reciteRef = useRef<Unit | null>(null);
  reciteRef.current = currentUnit;
  const timerRef = useRef({ limit: limitSec, left: secondsLeft });
  timerRef.current = { limit: limitSec, left: secondsLeft };

  /** 揭示完成 = 该单元整段算背过，推进 last_pos 到单元末句之后（文档 F19.1/F19.3）。 */
  const revealUnit = useCallback((auto: boolean) => {
    const unit = reciteRef.current;
    if (!unit) return;
    setLastSpentSec(Math.max(timerRef.current.limit - timerRef.current.left, 0));
    setTimerPhase("revealed");
    setRevealedUnits((prev) => new Set(prev).add(unit.index));
    setDoneCount((n) => lastPosAfterReveal(n, unit));
    if (auto) message.info("时间到，已揭示");
  }, []);

  // 默认时长读服务端 settings（KV）；离线读不到就用内置默认值，不阻断背诵。
  useEffect(() => {
    let alive = true;
    void getKv(TIMER_KV_KEY)
      .then((row) => {
        if (!alive) return;
        const n = Number(row.value);
        if (Number.isFinite(n) && n >= TIMER_MIN_SEC && n <= TIMER_MAX_SEC) {
          setLimitSec(n);
          setSecondsLeft(n);
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [getKv]);

  useEffect(() => {
    if (timerPhase !== "running") return;
    const id = window.setInterval(() => setSecondsLeft((s) => Math.max(s - 1, 0)), 1000);
    return () => window.clearInterval(id);
  }, [timerPhase]);

  useEffect(() => {
    if (timerPhase === "running" && secondsLeft <= 0) revealUnit(true);
  }, [timerPhase, secondsLeft, revealUnit]);

  const startTimer = () => {
    setSecondsLeft(limitSec);
    setTimerPhase("running");
  };
  /** 再来一遍：回到本单元初始遮蔽态，时长可再调（文档 F19.3）。 */
  const again = () => {
    setTimerPhase("idle");
    setRevealedUnits(new Set());
    if (lastSpentSec !== null) {
      setLimitSec(clampSec(lastSpentSec));
      setSecondsLeft(clampSec(lastSpentSec));
    }
  };
  /** 换到下一单元：回到遮蔽态，下次开始时才计时。 */
  const toNextUnit = () => {
    setTimerPhase("idle");
    setRevealedUnits(new Set());
  };

  const isMasked = (unit: Unit, side: "zh" | "en") => {
    if (mode === "对照阅读") return false;
    const revealed =
      timerPhase === "revealed" || revealedUnits.has(unit.index) || unit.index !== currentIdx;
    if (revealed) return false;
    if (hintLang === "both") return true;
    return hintLang !== side;
  };

  const ordered = reordering
    .map((k) => pairs.find((p) => p.pair_key === k))
    .filter((p): p is PairOut => Boolean(p));

  /** 按 seq 重排后的 pair 再按当前粒度聚单元（拖拽后块号连续性可能变，松手即时生效）。 */
  const displayUnits = useMemo(() => {
    const bySeq = [...pairs].sort((a, b) => a.seq - b.seq);
    const index = new Map(bySeq.map((p, i) => [p.pair_key, i]));
    return buildUnits(
      [...ordered].sort((a, b) => (index.get(a.pair_key) ?? 0) - (index.get(b.pair_key) ?? 0)),
      effectiveGranularity,
    );
  }, [ordered, pairs, effectiveGranularity]);

  const startSplit = (basis: PairOut) => {
    const text = shown(basis);
    const zhHalf = splitZhHalf(text.zh);
    const ratio = zhHalf.a.length / Math.max(text.zh.length, 1);
    const enHalf = splitEnHalf(text.en, ratio);
    setSplitDraft({
      zh_a: zhHalf.a.trim(),
      en_a: enHalf.a.trim(),
      zh_b: zhHalf.b.trim(),
      en_b: enHalf.b.trim(),
    });
    setSplitOpen(true);
  };

  const commitSplit = async () => {
    if (!editing) return;
    if (
      !splitDraft.zh_a.trim() ||
      !splitDraft.en_a.trim() ||
      !splitDraft.zh_b.trim() ||
      !splitDraft.en_b.trim()
    ) {
      message.warning("拆开的四段内容都不能为空");
      return;
    }
    try {
      await onSplit(editing.pair_key, splitDraft);
      message.success("已拆分");
      setSplitOpen(false);
      setEditing(null);
    } catch {
      message.error("拆分失败：需要联网（拆分是结构性操作，仅在线上支持）");
    }
  };

  const commitMerge = async (pair: PairOut, withKey: string) => {
    try {
      await onMerge(pair.pair_key, withKey);
      message.success("已合并");
    } catch {
      message.error("合并失败：需要联网（合并是结构性操作，仅在线上支持）");
    }
  };

  const dropTo = (targetKey: string) => {
    if (!dragKey || dragKey === targetKey) {
      setDragKey(null);
      setOverKey(null);
      return;
    }
    const from = reordering.indexOf(dragKey);
    const to = reordering.indexOf(targetKey);
    if (from === -1 || to === -1) {
      setDragKey(null);
      setOverKey(null);
      return;
    }
    const next = [...reordering];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    setReordering(next);
    // 只把 seq 变化的行发弱同步（含 manually_edited，重跑不冲）
    next.forEach((k, i) => {
      if (seqRef.current[k] !== i) {
        seqRef.current[k] = i;
        onReorder(k, i);
      }
    });
    message.info("顺序已更新，联网时自动同步");
    setDragKey(null);
    setOverKey(null);
  };

  /** 遮罩背诵档点遮罩 = 揭示整段并推进 last_pos；粒度跟随按句/按段（文档 F19.1）。 */
  const peekUnit = (unit: Unit) => {
    setRevealedUnits((prev) => new Set(prev).add(unit.index));
    setDoneCount((n) => lastPosAfterReveal(n, unit));
  };

  const renderPair = (pair: PairOut, unit: Unit, nextKey?: string) => {
    const text = shown(pair);
    const onMask = mode === "遮罩背诵" ? () => peekUnit(unit) : undefined;
    return (
      <div
        key={pair.pair_key}
        draggable
        onDragStart={() => setDragKey(pair.pair_key)}
        onDragOver={(e) => {
          e.preventDefault();
          setOverKey(pair.pair_key);
        }}
        onDragLeave={() => setOverKey((k) => (k === pair.pair_key ? null : k))}
        onDrop={(e) => {
          e.preventDefault();
          dropTo(pair.pair_key);
        }}
        style={{
          padding: "var(--tok-spaceMd-px)",
          marginBottom: "var(--tok-spaceMd-px)",
          background: "var(--tok-colorBgContainer)",
          border: "solid var(--tok-lineWidth-px)",
          borderColor: overKey === pair.pair_key ? "var(--tok-colorPrimary)" : "var(--tok-colorBgContainer)",
          cursor: "grab",
        }}
      >
        <div className="strayt-row" style={{ justifyContent: "space-between" }}>
          {isMasked(unit, "zh") ? (
            <MaskButton fontSize={fontSize} label="揭示中文" onClick={onMask} />
          ) : (
            <p style={{ margin: 0, fontWeight: 600, fontSize, lineHeight, flex: 1 }}>{text.zh}</p>
          )}
          <span className="strayt-row">
            {nextKey ? (
              <Button
                size="small"
                type="text"
                title="与下一条合并"
                icon={<MergeCellsOutlined />}
                onClick={() => void commitMerge(pair, nextKey)}
              />
            ) : null}
            <Button
              size="small"
              type="text"
              title="拆成两条"
              icon={<ScissorOutlined />}
              onClick={() => {
                setEditing(pair);
                startSplit(pair);
              }}
            />
            <Button
              size="small"
              type="text"
              icon={<EditOutlined />}
              onClick={() => {
                setEditing(pair);
                setDraft(text);
              }}
            />
          </span>
        </div>
        {isMasked(unit, "en") ? (
          <MaskButton fontSize={fontSize} label="揭示英文" inline onClick={onMask} />
        ) : (
          <p
            className="strayt-muted"
            style={{ marginTop: "var(--tok-spaceSm-px)", marginBottom: 0, fontSize, lineHeight }}
          >
            {text.en}
          </p>
        )}
        {pair.loc_page !== null && pair.loc_page !== undefined ? (
          <span className="strayt-muted" style={{ fontSize }}>
            页 {pair.loc_page}
          </span>
        ) : null}
      </div>
    );
  };

  const renderUnit = (unit: Unit) => (
    <div key={unit.index}>
      {displayUnits.length > 1 && effectiveGranularity === "paragraph" ? (
        <div className="strayt-muted" style={{ marginBottom: "var(--tok-spaceSm-px)" }}>
          第 {unit.index + 1} / {displayUnits.length} 段
        </div>
      ) : null}
      {unit.pairs.map((p, i) => renderPair(p, unit, unit.pairs[i + 1]?.pair_key))}
    </div>
  );

  /** 逐句递进只显示当前单元；另两档整篇平铺（文档 F19.2）。 */
  const visibleUnits =
    mode === "逐句递进" ? displayUnits.filter((u) => u.index === currentIdx) : displayUnits;

  return (
    <div>
      <div className="strayt-row" style={{ justifyContent: "space-between", marginBottom: "var(--tok-spaceMd-px)" }}>
        <Button size="small" onClick={onBackList} icon={<ArrowLeftOutlined />}>
          返回篇目
        </Button>
        <h3 style={{ margin: 0 }}>{piece?.title ?? ""}</h3>
        <span className="strayt-row">
          <span className="strayt-muted">字号</span>
          <Button size="small" onClick={() => setFontDelta((d) => Math.max(d - 1, -3))}>
            -
          </Button>
          <Button size="small" onClick={() => setFontDelta((d) => Math.min(d + 1, 6))}>
            +
          </Button>
        </span>
      </div>

      <div style={{ marginBottom: "var(--tok-spaceMd-px)" }}>
        <Segmented
          block
          value={mode}
          options={MODES.map((m) => ({ value: m, label: m }))}
          onChange={(v) => setMode(v as Mode)}
        />
      </div>

      <div className="strayt-row" style={{ justifyContent: "space-between", marginBottom: "var(--tok-spaceSm-px)" }}>
        <span className="strayt-muted">背诵单元</span>
        <Segmented
          size="small"
          value={effectiveGranularity}
          options={[
            { value: "sentence", label: "按句" },
            { value: "paragraph", label: "按段" },
          ]}
          onChange={(v) => {
            const next = v as Granularity;
            writeGranularity(piece?.id, next);
            setGranularity(next);
          }}
        />
      </div>
      {granularity === "paragraph" && !hasBlocks ? (
        <Alert
          type="info"
          showIcon
          message="这篇目还没有原文段落信息，已自动按句背诵。重跑一次加工任务就能按段。"
        />
      ) : null}

      {mode !== "对照阅读" ? (
        <div className="strayt-row" style={{ justifyContent: "space-between", marginBottom: "var(--tok-spaceSm-px)" }}>
          <span className="strayt-muted">提示语言</span>
          <Segmented
            size="small"
            value={hintLang}
            options={HINT_LANGS}
            onChange={(v) => setHintLang(v as HintLang)}
          />
        </div>
      ) : null}

      <div className="strayt-row" style={{ justifyContent: "space-between", marginBottom: "var(--tok-spaceMd-px)" }}>
        <span className="strayt-muted">
          已背 {doneCount}/{pairs.length} 句
          {effectiveGranularity === "paragraph" ? ` · ${doneUnits}/${units.length} 段` : ""}
        </span>
        <span className="strayt-row">
          <span className="strayt-muted">已背诵</span>
          <Switch
            size="small"
            checked={recited}
            onChange={(v) => {
              setRecited(v);
              saveRef.current(doneCount, v);
            }}
          />
        </span>
      </div>

      {mode === "逐句递进" && currentUnit ? (
        <div
          style={{
            marginBottom: "var(--tok-spaceMd-px)",
            padding: "var(--tok-spaceMd-px)",
            background: "var(--tok-colorBgContainer)",
            border: "solid var(--tok-lineWidth-px)",
            borderColor: "var(--tok-colorBorderSecondary)",
          }}
        >
          <div className="strayt-row" style={{ justifyContent: "space-between" }}>
            <span className="strayt-muted">
              第 {currentUnit.index + 1} / {displayUnits.length} 段
            </span>
            <span
              style={{
                fontSize: tokens.fontSize * 1.6,
                fontVariantNumeric: "tabular-nums",
                color: timerPhase === "running" && secondsLeft <= 10 ? "var(--tok-colorError)" : "var(--tok-colorText)",
              }}
            >
              {timerPhase === "running" ? formatCountdown(secondsLeft) : formatCountdown(limitSec)}
            </span>
          </div>
          <div className="strayt-row" style={{ marginTop: "var(--tok-spaceSm-px)" }}>
            <Select
              size="small"
              value={limitSec}
              options={TIMER_PRESETS.map((s) => ({ value: s, label: `${s / 60} 分钟` }))}
              onChange={(v) => {
                setLimitSec(v);
                setSecondsLeft(v);
              }}
              style={{ minWidth: 0 }}
            />
            <InputNumber
              size="small"
              min={TIMER_MIN_SEC}
              max={TIMER_MAX_SEC}
              step={30}
              value={limitSec}
              onChange={(v) => {
                const n = clampSec(Number(v));
                setLimitSec(n);
                setSecondsLeft(n);
              }}
            />
            <span className="strayt-muted">秒</span>
          </div>
          <div className="strayt-row" style={{ marginTop: "var(--tok-spaceSm-px)" }}>
            {timerPhase === "running" ? (
              <Button type="primary" onClick={() => revealUnit(false)}>
                完成背诵（提前揭示）
              </Button>
            ) : timerPhase === "revealed" ? (
              <>
                <Button onClick={again} icon={<RedoOutlined />}>
                  再来一遍
                </Button>
                <Button
                  type="primary"
                  disabled={currentIdx >= displayUnits.length - 1}
                  onClick={toNextUnit}
                >
                  背诵下一部分
                </Button>
              </>
            ) : (
              <Button type="primary" onClick={startTimer}>
                开始背诵
              </Button>
            )}
          </div>
          {lastSpentSec !== null && timerPhase !== "running" ? (
            <span className="strayt-muted" style={{ marginTop: "var(--tok-spaceSm-px)", display: "block" }}>
              上一次用时 {formatCountdown(lastSpentSec)}，时长可再调。
            </span>
          ) : null}
        </div>
      ) : null}

      <div>
        {mode === "对照阅读"
          ? visibleUnits.map((u) => renderUnit(u))
          : visibleUnits.map((u) => (
              <div key={u.index}>
                <div className="strayt-row" style={{ justifyContent: "space-between" }}>
                  <span className="strayt-muted">
                    {effectiveGranularity === "paragraph" ? `第 ${u.index + 1} / ${displayUnits.length} 段` : ""}
                  </span>
                  {revealedUnits.has(u.index) ? <Tag>已揭示</Tag> : null}
                </div>
                {u.pairs.map((p, i) => renderPair(p, u, u.pairs[i + 1]?.pair_key))}
              </div>
            ))}
        {visibleUnits.length === 0 ? <Empty description="这篇目还没有可背诵的句子。" /> : null}
      </div>

      <Modal
        open={editing !== null}
        title="编辑这一句"
        onCancel={() => setEditing(null)}
        okText="保存"
        onOk={() => {
          if (!editing) return;
          if (draft.zh.trim() === "" || draft.en.trim() === "") {
            message.warning("中英文都不能为空");
            return;
          }
          setEdits((prev) => ({ ...prev, [editing.pair_key]: { zh: draft.zh, en: draft.en } }));
          onSavePair(editing.pair_key, draft.zh, draft.en);
          setEditing(null);
        }}
      >
        <p className="strayt-muted">改动按弱同步排队：联网时直传，断了下次同步自动补上。</p>
        <label>中文</label>
        <Input.TextArea value={draft.zh} onChange={(e) => setDraft((d) => ({ ...d, zh: e.target.value }))} rows={3} />
        <label style={{ marginTop: "var(--tok-spaceSm-px)" }}>英文</label>
        <Input.TextArea value={draft.en} onChange={(e) => setDraft((d) => ({ ...d, en: e.target.value }))} rows={3} />
      </Modal>

      <Modal
        open={splitOpen}
        title="拆成两条"
        okText="拆分"
        onCancel={() => {
          setSplitOpen(false);
          setEditing(null);
        }}
        onOk={() => void commitSplit()}
      >
        <p className="strayt-muted">已按句读自动断句，可直接修改。拆分是结构性操作，仅在联网时可用。</p>
        <label>中文（前）</label>
        <Input.TextArea value={splitDraft.zh_a} onChange={(e) => setSplitDraft((d) => ({ ...d, zh_a: e.target.value }))} rows={2} />
        <label style={{ marginTop: "var(--tok-spaceSm-px)" }}>英文（前）</label>
        <Input.TextArea value={splitDraft.en_a} onChange={(e) => setSplitDraft((d) => ({ ...d, en_a: e.target.value }))} rows={2} />
        <label style={{ marginTop: "var(--tok-spaceSm-px)" }}>中文（后）</label>
        <Input.TextArea value={splitDraft.zh_b} onChange={(e) => setSplitDraft((d) => ({ ...d, zh_b: e.target.value }))} rows={2} />
        <label style={{ marginTop: "var(--tok-spaceSm-px)" }}>英文（后）</label>
        <Input.TextArea value={splitDraft.en_b} onChange={(e) => setSplitDraft((d) => ({ ...d, en_b: e.target.value }))} rows={2} />
      </Modal>
    </div>
  );
};

/**
 * 遮罩块。`遮罩背诵` 档可点揭示；`逐句递进` 档刻意**不可点**——
 * 那档的揭示只走「到时自动揭示」或「完成背诵」，否则计时器形同虚设（文档 F19.3）。
 */
function MaskButton({
  label,
  inline,
  fontSize,
  onClick,
}: {
  label: string;
  inline?: boolean;
  fontSize: number;
  onClick?: () => void;
}) {
  const style = {
    width: "100%",
    minHeight: inline ? undefined : `var(--tok-spaceLg-px)`,
    border: "none",
    background: "var(--tok-reciteMaskBg)",
    color: "var(--tok-colorText)",
    cursor: onClick ? "pointer" : "default",
    fontSize,
    marginTop: inline ? "var(--tok-spaceSm-px)" : undefined,
    textAlign: "left" as const,
  };
  if (!onClick) {
    return (
      <div className="strayt-mask" style={style} aria-label={label}>
        {label}
      </div>
    );
  }
  return (
    <button type="button" className="strayt-mask" style={style} onClick={onClick}>
      {label}
    </button>
  );
}

function stringToKeys(pairs: PairOut[]): string[] {
  return pairs.map((p) => p.pair_key);
}

function buildSeqMap(pairs: PairOut[]): Record<string, number> {
  const m: Record<string, number> = {};
  for (const p of pairs) m[p.pair_key] = p.seq;
  return m;
}