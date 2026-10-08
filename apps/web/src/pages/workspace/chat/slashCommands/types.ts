import type { CommandPanelApi } from "@alkera/chat-model";
import type { CostStateView } from "@alkera/chat-model";
import type { SlashCommandOutcome } from "../data";

/** The chat-scoped capabilities a slash command (panel or action) drives. The
 *  ChatSurface binds these to the live chat; panels stay free of `chatData()` /
 *  `host` imports so they unit-test with a plain stub. */
export interface SlashContext {
  chatId: string;
  /** The chat's current permission mode — seeds the `/mode` picker. */
  currentMode: string;
  /** The chat's current title — seeds the `/title` field. */
  currentTitle: string | null;
  /** Dispatch a slash line to the daemon and get its structured outcome. */
  runCommand: (line: string) => Promise<SlashCommandOutcome>;
  /** The account's credit balance + usage for a window (session-less, so `/usage`
   *  works on the home screen as well as inside a chat). */
  getUsage: (window: string) => Promise<{ credits: Record<string, unknown>; usage: Record<string, unknown> }>;
  /** Switch the permission mode now (the daemon push updates the pill). */
  setMode: (mode: string) => Promise<void>;
  /** The chat's spend-vs-caps snapshot for `/cost`. */
  getCostState: () => Promise<CostStateView>;
  /** Open the native Alkera preferences panel. */
  openPreferences: () => void;
  /** Replay the first-run welcome tour (the `/onboarding` target). */
  showOnboarding: () => void;
  /** Leave the chat (the `/exit` target). */
  exit: () => void;
  /** A `/title` set succeeded — refresh the chat-list surfaces. */
  onTitleChanged: () => void;
}

/** Props every command panel receives. Commands take no inline arguments — the
 *  panel collects whatever input it needs (the window picker, the title field). */
export interface SlashPanelProps {
  ctx: SlashContext;
  api: CommandPanelApi;
}
