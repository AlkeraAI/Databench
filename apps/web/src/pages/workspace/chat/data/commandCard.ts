import type { CommandConversationPart } from "@alkera/chat-model";
import { usageCard } from "./usageRows";

/** The renderable fields of a command card — everything on
 *  `CommandConversationPart` except the fold-assigned base (`id`/`kind`). The
 *  one remaining producer is the `conversation.cleared` boundary card. */
export type CommandCardData = Omit<CommandConversationPart, "id" | "kind" | "time" | "streaming">;

/** Map a persisted `command.result` event (and the live one — same shape) onto a
 *  command card, OR `null` when the command has no card of its own. The daemon
 *  excludes `clear`/`compact` (they render from `ConversationCleared` /
 *  `CompactionApplied`) and `exit` (navigation); those return `null` here too as
 *  a defensive belt. Single source of truth for both the fold's live and replay
 *  rendering. */
export function commandPartFromOutcome(
  command: string | null,
  outcomeKind: string,
  payload: Record<string, unknown>,
  message: string | null,
): CommandCardData | null {
  const name = command ?? "command";
  if (outcomeKind === "exit") return null;
  if (outcomeKind === "cli_only") {
    return { command: name, label: `/${name} is a CLI command`, detail: message ?? undefined };
  }
  if (outcomeKind === "unknown" || outcomeKind === "bad_usage") {
    return {
      command: name,
      label: outcomeKind === "unknown" ? "Unknown command" : `/${name}`,
      detail: message ?? undefined,
      tone: "error",
    };
  }
  switch (command) {
    case "clear":
    case "compact":
      // Rendered from their dedicated events (ConversationCleared /
      // CompactionApplied); never a duplicate card.
      return null;
    case "title": {
      const title = typeof payload.title === "string" && payload.title ? payload.title : "(untitled)";
      return payload.action === "set"
        ? { command: "title", label: "Title updated", detail: title }
        : { command: "title", label: "Chat title", detail: title };
    }
    case "usage": {
      if (typeof payload.error === "string") {
        return { command: "usage", label: "Usage", detail: payload.error, tone: "error" };
      }
      // The rows an installed extension reads off the payload (USAGE_ROW_SOURCES).
      // The account's request count is the panel's, not the card's.
      const { stats, detail } = usageCard(payload);
      return { command: "usage", label: "Usage", detail, stats };
    }
    default:
      return { command: name, label: `/${name}`, detail: message ?? undefined };
  }
}
