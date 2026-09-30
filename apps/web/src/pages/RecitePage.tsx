import { useEffect, useMemo, useState } from "react";
import { Button, Empty, Input, message, Modal, Segmented, Select, Spin, Switch, Tag } from "antd";
import { ArrowLeftOutlined, ArrowRightOutlined, EditOutlined } from "@ant-design/icons";
import { useStraytTokens } from "@strayt/ui";
import { useApp } from "../store";
import type { PairOut, PieceOut } from "@strayt/api-client";

type Mode = "对照阅读" | "中文提示" | "遮罩背诵" | "逐句递进";
const MODES: Mode[] = ["对照阅读", "中文提示", "遮罩背诵", "逐句递进"];

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
}: {
  piece?: PieceOut;
  pairs: PairOut[];
  onBackList: () => void;
  onSave: (lastPos: number, recited: boolean) => void;
  onSavePair: (pairKey: string, zh: string, en: string) => void;
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

  const shown = (p: PairOut) => edits[p.pair_key] ?? { zh: p.zh, en: p.en };

  const fontSize = tokens.fontSize + 2 * fontDelta;
  const lineHeight = tokens.lineHeight + 0.06 * fontDelta;
  const advance = () => setRevealIndex((n) => Math.min(n + 1, pairs.length));
  const isMaskedEn = (seq: number) =>
    mode === "中文提示" || mode === "遮罩背诵"
      ? !revealed.has(String(seq))
      : mode === "逐句递进"
        ? seq > revealIndex
        : false;

  useEffect(() => {
    if (mode === "逐句递进") onSave(Math.min(revealIndex + 1, pairs.length), recited);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealIndex, mode, pairs.length]);

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
        {pairs.map((pair) => {
          const masked = isMaskedEn(pair.seq);
          const text = shown(pair);
          return (
            <div
              key={pair.seq}
              style={{
                padding: "var(--tok-spaceMd-px)",
                marginBottom: "var(--tok-spaceMd-px)",
                background: "var(--tok-colorBgContainer)",
              }}
            >
              <div className="strayt-row" style={{ justifyContent: "space-between" }}>
                <p style={{ margin: 0, fontWeight: 600, fontSize, lineHeight, flex: 1 }}>{text.zh}</p>
                <Button
                  size="small"
                  type="text"
                  icon={<EditOutlined />}
                  onClick={() => {
                    setEditing(pair);
                    setDraft(text);
                  }}
                />
              </div>
              {masked ? (
                <button
                  type="button"
                  onClick={() => setRevealed((prev) => new Set(prev).add(String(pair.seq)))}
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
    </div>
  );
};