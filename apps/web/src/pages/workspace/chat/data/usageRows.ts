// The rows an extension adds to /usage. The open panel shows the window's request
// count and the open card nothing of its own; an extension that meters usage (a plan
// and its credits, for one) registers the rows it reads off the command's payload.

import { ExtensionPoint } from "@alkera/ui/extensions";

export interface UsageStatRow {
  label: string;
  value: string;
  /** Drawn larger, as the panel's lead figure. */
  hero?: boolean;
}

export interface UsageRowSource {
  key: string;
  /** Rows the /usage slash panel shows ahead of the request count. */
  panel: (payload: Record<string, unknown>) => UsageStatRow[];
  /** Rows the /usage command card shows, and its detail line when it has one. */
  card: (payload: Record<string, unknown>) => { stats: UsageStatRow[]; detail?: string };
}

export const USAGE_ROW_SOURCES = new ExtensionPoint<UsageRowSource>("chat.usage_rows");

export function usagePanelRows(payload: Record<string, unknown>): UsageStatRow[] {
  return USAGE_ROW_SOURCES.items().flatMap((source) => source.panel(payload));
}

export function usageCard(payload: Record<string, unknown>): { stats: UsageStatRow[]; detail?: string } {
  const stats: UsageStatRow[] = [];
  let detail: string | undefined;
  for (const source of USAGE_ROW_SOURCES.items()) {
    const part = source.card(payload);
    stats.push(...part.stats);
    detail ??= part.detail;
  }
  return detail === undefined ? { stats } : { stats, detail };
}
