import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Empty, Input, message, Modal, Segmented, Select, Spin, Switch, Tag } from "antd";
import {
  ArrowLeftOutlined,
  ArrowRightOutlined,
  EditOutlined,
  MergeCellsOutlined,
  ScissorOutlined,
} from "@ant-design/icons";
import { useStraytTokens } from "@strayt/ui";
import { useApp } from "../store";
import type { PairOut, PieceOut } from "@strayt/api-client";

type Mode = "对照阅读" | "中文提示" | "遮罩背诵" | "逐句递进";
const MODES: Mode[] = ["对照阅读", "中文提示", "遮罩背诵", "逐句递进"];

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
}: {
  piece?: PieceOut;
  pairs: PairOut[];
  onBackList: () => void;
  onSave: (lastPos: number, recited: boolean) => void;
  onSavePair: (pairKey: string, zh: string, en: string) => void;
  onReorder: (pairKey: string, seq: number) => void;
  onSplit: (pairKey: string, halves: { zh_a: string; en_a: string; zh_b: string; en_b: string }) => Promise<void>;
  onMerge: (pairKey: string, withKey: string) => Promise<void>;
}) => {
  const tokens = useStraytTokens();
  const [mode, setMode] = useState<Mode>("对照阅读");
  const [revealed, setRevealed] = useState<Set<string>>(new Set());
  const [revealIndex, setRevealIndex] = useState(0);
  const [recited, setRecited] = useState(piece?.recited ?? false);
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

  const shown = (p: PairOut) => edits[p.pair_key] ?? { zh: p.zh, en: p.en };

  const fontSize = tokens.fontSize + 2 * fontDelta;
  const lineHeight = tokens.lineHeight + 0.06 * fontDelta;
  const advance = () => setRevealIndex((n) => Math.min(n + 1, pairs.length));
  const isMaskedEn = (index: number, pair: PairOut) =>
    mode === "中文提示" || mode === "遮罩背诵"
      ? !revealed.has(pair.pair_key)
      : mode === "逐句递进"
        ? index > revealIndex
        : false;

  useEffect(() => {
    if (mode === "逐句递进") onSave(Math.min(revealIndex + 1, pairs.length), recited);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealIndex, mode, pairs.length]);

  useEffect(() => setReordering(stringToKeys(pairs)), [pairs]);

  const ordered = reordering
    .map((k) => pairs.find((p) => p.pair_key === k))
    .filter((p): p is PairOut => Boolean(p));

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
      <div className="strayt-row" style={{ justifyContent: "space-between" }}>
        <span className="strayt-muted">
          已背 {revealIndex}/{pairs.length}
        </span>
        <span className="strayt-row">
          <span className="strayt-muted">已背诵</span>
          <Switch
            size="small"
            checked={recited}
            onChange={(v) => {
              setRecited(v);
              onSave(Math.min(revealIndex + 1, pairs.length), v);
            }}
          />
        </span>
      </div>

      <div style={{ marginTop: "var(--tok-spaceMd-px)" }}>
        {ordered.map((pair, index) => {
          const masked = isMaskedEn(index, pair);
          const text = shown(pair);
          const nextKey = ordered[index + 1]?.pair_key;
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
                <p style={{ margin: 0, fontWeight: 600, fontSize, lineHeight, flex: 1 }}>{text.zh}</p>
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
              {masked ? (
                <button
                  type="button"
                  onClick={() => setRevealed((prev) => new Set(prev).add(pair.pair_key))}
                  className="strayt-mask"
                  style={{
                    width: "100%",
                    minHeight: `var(--tok-spaceLg-px)`,
                    border: "none",
                    background: "var(--tok-reciteMaskBg)",
                    color: "var(--tok-colorText)",
                    cursor: "pointer",
                    marginTop: "var(--tok-spaceSm-px)",
                  }}
                >
                  {mode === "中文提示" ? "揭示英文" : "点击揭示"}
                </button>
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
        })}
      </div>

      {mode === "逐句递进" ? (
        <div className="strayt-row" style={{ justifyContent: "center" }}>
          <Button type="primary" onClick={advance} icon={<ArrowRightOutlined />}>
            下一句{revealIndex >= pairs.length ? "（已到底）" : ""}
          </Button>
        </div>
      ) : null}

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

function stringToKeys(pairs: PairOut[]): string[] {
  return pairs.map((p) => p.pair_key);
}

function buildSeqMap(pairs: PairOut[]): Record<string, number> {
  const m: Record<string, number> = {};
  for (const p of pairs) m[p.pair_key] = p.seq;
  return m;
}