// The chat's tool-card extension point. A card the open chat does not ship (a data
// product's lineage trace, say) registers here with the tool names it draws, and the
// step registry routes those tools to it ahead of the open table.

import { ExtensionPoint } from "../../extensions";
import type { Card } from "./step";

export interface ToolCardEntry {
  /** Unique within the point. */
  key: string;
  /** The alkera tool names this card draws, as the manifest spells them. */
  tools: readonly string[];
  card: Card;
}

export const TOOL_CARDS = new ExtensionPoint<ToolCardEntry>("chat.tool_cards");

/** The registered card for an alkera tool, if an extension draws it. */
export function registeredCard(tool: string): Card | undefined {
  return TOOL_CARDS.items().find((entry) => entry.tools.includes(tool))?.card;
}
