// React-free view contracts shared by the chat surfaces — the data shapes the composer,
// chrome, and activity components consume. React-bearing contracts (option icons, panel
// renderers) stay in the UI layer.

/** Prompt chip shown when a chat transcript is empty. */
export interface SuggestedPrompt {
  id: string;
  label: string;
  prefill: string;
}

/** One chat in the top-bar history dropdown. */
export interface HistoryEntry {
  id: string;
  title: string;
  /** Last assistant snippet — one line, truncated. */
  lastSnippet?: string;
  /** Pretty timestamp ("2 m ago") or ISO. Rendered right-aligned. */
  updatedAt?: string;
}

/** The logged-in account, for the top-bar settings menu header. */
export interface AccountSummary {
  /** The logged-in email — always shown as the (non-clickable) menu header. */
  email: string;
  /** Optional display name + plan; omitted lines aren't rendered. */
  name?: string;
  plan?: string;
}

/** A reasoning-effort value. Model-driven (the gateway catalog's per-model
 *  `efforts`), so this is an open string — "low"/"medium"/"high" are just the
 *  built-in demo set. */
export type ComposerEffort = string;

/** What the composer resolved alongside the message text on send. */
export interface ComposerSendMeta {
  mode: string;
  model: string;
  /** Absent when the selected model offers no effort variants. */
  effort?: ComposerEffort;
  /** Files node ids to send with the message. Each is already linked to the
   *  chat by the host before the send — the message never attaches a file, it
   *  names one — so a chip still uploading, or one that failed, is never here. */
  attachments?: string[];
}

/** The handle a command panel uses to dismiss itself (after its action, or on
 *  cancel). The input is already empty — selecting the command consumed the
 *  typed text — so closing never brings it back. */
export interface CommandPanelApi {
  close: () => void;
}

// --- per-chat activity (Decisions | Cost | Safety), camelCased from the daemon's
// `harness.list_decisions` / `list_cost_ledger` / `get_cost_state` responses ---

/** Spent vs caps for a chat (USD). `spent` carries chat/day/week; `caps` adds
 *  `per_query`. Records (not fixed keys) so a new window doesn't break the type. */
export interface CostStateView {
  spent: Record<string, number>;
  caps: Record<string, number>;
  orgManaged: boolean;
  unknownKeys: string[];
}

/** One audited permission decision (the Decisions + Safety tabs). */
export interface DecisionView {
  at: number;
  source: string;
  capability: string;
  effect: string;
  operation: string;
  targets: string[];
  mode: string;
  decision: string;
  decidedBy: string;
  reasons: string[];
}

/** One warehouse charge in the chat's cost ledger. */
export interface CostLedgerEntryView {
  entryId: string;
  connection: string;
  operation: string;
  estimateUsd: number;
  actualUsd: number | null;
  chargedUsd: number;
  walletCurrency: string;
  at: number;
}

export type ActivityTab = "decisions" | "cost" | "safety";
