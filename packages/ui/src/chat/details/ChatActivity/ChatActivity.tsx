import { useState, type ReactNode } from "react";
import { formatUsd } from "@alkera/chat-model";
import type {
  ActivityTab,
  CostLedgerEntryView,
  CostStateView,
  DecisionView,
} from "@alkera/chat-model";
import {
  Button,
  Inline,
  Meter,
  Pill,
  Stack,
  Tabs,
  Text,
  TextInput,
  type MeterTone,
  type PillTone,
} from "../../../primitives";
import "./ChatActivity.css";

/** Async data for one tab: the loaded value, plus loading / error flags so each
 *  tab renders its own state without the parent juggling three query states. */
export interface AsyncSlot<T> {
  data?: T;
  loading?: boolean;
  error?: string | null;
}

export interface ChatActivityProps {
  /** Permission-decision audit log (newest-first). */
  decisions: AsyncSlot<DecisionView[]>;
  /** The `decided_by="judge"` subset — the safety-judge verdicts. */
  safety: AsyncSlot<DecisionView[]>;
  /** Spent-vs-caps gauge state. */
  cost: AsyncSlot<CostStateView>;
  /** Per-chat warehouse cost ledger (newest-first). */
  ledger: AsyncSlot<CostLedgerEntryView[]>;
  /** Persist edited caps (USD per window). Resolves when saved; rejects on
   *  failure. Ignored when the cost state is org-managed (read-only). */
  onSaveCaps: (caps: Record<string, number>) => Promise<void>;
  /** Initial tab (defaults to Decisions). */
  initialTab?: ActivityTab;
}

const TABS: { key: ActivityTab; label: string }[] = [
  { key: "decisions", label: "Decisions" },
  { key: "cost", label: "Cost" },
  { key: "safety", label: "Safety" },
];

/** The per-chat activity surfaces: a Decisions | Cost | Safety tab view.
 *  Pure presentation — data + the caps-save effect are injected. */
export function ChatActivity({ decisions, safety, cost, ledger, onSaveCaps, initialTab = "decisions" }: ChatActivityProps) {
  const [tab, setTab] = useState<ActivityTab>(initialTab);
  return (
    <div className="alk-chatact">
      <Tabs items={TABS} value={tab} onChange={(key) => setTab(key as ActivityTab)} label="Chat activity" />
      <div className="alk-chatact__body alk-scroll" role="tabpanel">
        {tab === "decisions" ? <DecisionsTable slot={decisions} kind="decisions" /> : null}
        {tab === "safety" ? <DecisionsTable slot={safety} kind="safety" /> : null}
        {tab === "cost" ? <CostTab cost={cost} ledger={ledger} onSaveCaps={onSaveCaps} /> : null}
      </div>
    </div>
  );
}

function SlotState({ slot, empty }: { slot: AsyncSlot<unknown>; empty: string }) {
  if (slot.loading) return <div className="alk-chatact__status" role="status">Loading…</div>;
  if (slot.error) return <div className="alk-chatact__status" role="alert">{slot.error}</div>;
  return <div className="alk-chatact__status">{empty}</div>;
}

const WINDOWS = ["per_query", "chat", "day", "week"] as const;
const WINDOW_LABEL: Record<string, string> = {
  per_query: "Per query",
  chat: "This chat",
  day: "Today",
  week: "This week",
};

function CostTab({
  cost,
  ledger,
  onSaveCaps,
}: {
  cost: AsyncSlot<CostStateView>;
  ledger: AsyncSlot<CostLedgerEntryView[]>;
  onSaveCaps: (caps: Record<string, number>) => Promise<void>;
}) {
  if (!cost.data) return <SlotState slot={cost} empty="No cost data yet." />;
  return (
    <Stack gap={4} align="stretch">
      <CostGauge state={cost.data} />
      <CapsEditor state={cost.data} onSaveCaps={onSaveCaps} />
      <CostLedgerTable slot={ledger} />
    </Stack>
  );
}

const GAUGE_TONE: Record<string, MeterTone> = { ok: "brand", warn: "warning", over: "danger", none: "neutral" };

/** Spent-vs-cap meters for the chat/day/week windows. A window with no cap shows
 *  the spend over an empty track; one near/over its cap goes amber/red. */
function CostGauge({ state }: { state: CostStateView }) {
  return (
    <Stack gap={2} align="stretch" aria-label="Spend vs caps">
      {(["chat", "day", "week"] as const).map((window) => {
        const spent = state.spent[window] ?? 0;
        const cap = state.caps[window];
        const ratio = cap && cap > 0 ? spent / cap : null;
        const tone = ratio === null ? "none" : ratio >= 1 ? "over" : ratio >= 0.8 ? "warn" : "ok";
        return (
          <div key={window} className="alk-chatact__gauge-row" data-tone={tone}>
            <span>{WINDOW_LABEL[window]}</span>
            <Meter value={ratio ?? 0} tone={GAUGE_TONE[tone]} size="lg" label={`${WINDOW_LABEL[window]} spend vs cap`} />
            <span>
              {formatUsd(spent)}
              {cap ? ` / ${formatUsd(cap)}` : ""}
            </span>
          </div>
        );
      })}
    </Stack>
  );
}

function CapsEditor({ state, onSaveCaps }: { state: CostStateView; onSaveCaps: (caps: Record<string, number>) => Promise<void> }) {
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(WINDOWS.map((window) => [window, capToInput(state.caps[window])])),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (state.orgManaged) {
    return <Text as="div" variant="meta" role="note">Cost limits are managed by your organization.</Text>;
  }

  const save = () => {
    const caps: Record<string, number> = {};
    for (const window of WINDOWS) {
      const raw = draft[window]?.trim();
      if (!raw) continue;
      const num = Number(raw);
      if (!Number.isFinite(num) || num < 0) {
        setError(`Enter a valid amount for "${WINDOW_LABEL[window]}".`);
        return;
      }
      caps[window] = num;
    }
    setError(null);
    setSaving(true);
    onSaveCaps(caps).then(
      () => setSaving(false),
      (err: unknown) => {
        setSaving(false);
        setError(err instanceof Error ? err.message : "Couldn't save the caps.");
      },
    );
  };

  return (
    <Stack gap={2}>
      <Inline gap={2} align="flex-end">
        {WINDOWS.map((window) => (
          <TextInput
            key={window}
            size="sm"
            type="number"
            min={0}
            step="0.01"
            inputMode="decimal"
            label={`${WINDOW_LABEL[window]} ($)`}
            aria-label={`${WINDOW_LABEL[window]} cap (USD)`}
            value={draft[window] ?? ""}
            onChange={(event) => setDraft((prev) => ({ ...prev, [window]: event.target.value }))}
            rootStyle={{ width: "6.5rem" }}
          />
        ))}
      </Inline>
      {error ? <Text as="div" variant="meta" tone="danger" role="alert">{error}</Text> : null}
      <Button size="sm" fill="outline" onClick={save} loading={saving}>
        Save limits
      </Button>
    </Stack>
  );
}

function capToInput(value: number | undefined): string {
  return value && Number.isFinite(value) && value > 0 ? String(value) : "";
}

function CostLedgerTable({ slot }: { slot: AsyncSlot<CostLedgerEntryView[]> }) {
  const entries = slot.data ?? [];
  if (entries.length === 0) return <SlotState slot={slot} empty="No charges recorded for this chat." />;
  return (
    <table className="alk-chatact__table">
      <thead>
        <tr>
          <th scope="col">When</th>
          <th scope="col">Connection</th>
          <th scope="col">Operation</th>
          <th scope="col">Charged</th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          <tr key={entry.entryId || `${entry.at}-${entry.operation}`}>
            <td>{formatAt(entry.at)}</td>
            <td>{entry.connection || "—"}</td>
            <td title={entry.operation}>{entry.operation || "—"}</td>
            <td>
              {formatUsd(entry.chargedUsd)}
              {entry.actualUsd === null ? <Text tone="muted">{" (est.)"}</Text> : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const EFFECT_TONE: Record<string, PillTone> = {
  destroy: "danger",
  egress: "danger",
  exec: "danger",
  write: "warning",
};
const DECISION_TONE: Record<string, PillTone> = { reject: "danger", prompt: "warning", allow: "success" };

/** An effect/decision stamp: a toned chip when the value carries risk semantics,
 *  a chip-less neutral mark otherwise (a `read` effect stays quiet). */
function TonePill({ tone, children }: { tone?: PillTone; children: ReactNode }) {
  return (
    <Pill shape="rect" tone={tone ?? "neutral"} variant={tone ? "soft" : "plain"}>
      {children}
    </Pill>
  );
}

function DecisionsTable({ slot, kind }: { slot: AsyncSlot<DecisionView[]>; kind: "decisions" | "safety" }) {
  const rows = slot.data ?? [];
  if (rows.length === 0) {
    return (
      <SlotState
        slot={slot}
        empty={kind === "safety" ? "No safety-judge verdicts for this chat yet." : "No permission decisions recorded yet."}
      />
    );
  }
  return (
    <table className="alk-chatact__table">
      <thead>
        <tr>
          <th scope="col">When</th>
          <th scope="col">Action</th>
          <th scope="col">Decision</th>
          {kind === "safety" ? <th scope="col">Reason</th> : <th scope="col">By</th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
          <tr key={`${row.at}-${index}`} data-decision={row.decision}>
            <td>{formatAt(row.at)}</td>
            <td title={row.operation}>
              <TonePill tone={EFFECT_TONE[row.effect]}>{row.effect || "?"}</TonePill>
              {" "}
              {row.capability || row.operation || "action"}
            </td>
            <td><TonePill tone={DECISION_TONE[row.decision]}>{row.decision || "?"}</TonePill></td>
            <td title={row.reasons.join("; ")}>
              {kind === "safety" ? (row.reasons[0] ?? "—") : (row.decidedBy || "—")}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function formatAt(epochSeconds: number): string {
  if (!epochSeconds) return "—";
  const date = new Date(epochSeconds * 1000);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}
