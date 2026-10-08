import type { CommandPanelApi, ComposerCommand } from "@alkera/chat-model";
import { useCallback, useMemo, type ReactNode } from "react";
import { resolveMode } from "./modes";
import { ClearConfirmPanel, CostPanel, ModePanel, TitlePanel, UsagePanel } from "./panels";
import type { SlashContext, SlashPanelProps } from "./types";
import { chatData, chatHost, type SlashCommandInfo } from "../data";

type PanelComponent = (props: SlashPanelProps) => React.JSX.Element;

interface BehaviorBase {
  /** Usable on the home screen, where there is no chat session yet. Only
   *  account-level commands (usage, preferences) qualify. */
  home?: boolean;
}
type Behavior = BehaviorBase &
  (
    | { present: "panel"; Panel: PanelComponent }
    | { present: "immediate"; run: (ctx: SlashContext) => void }
    | { present: "surface"; run: (ctx: SlashContext) => void }
  );

/** The hidden mode-shortcut commands surfaced for a typed invocation. Each sets
 *  the canonical mode its name resolves to (see `modes.ts`). */
const MODE_SHORTCUTS = ["normal", "read-only", "auto", "plan", "bypass", "yolo"];

/** The editor's presentation for each harness command — the single place that
 *  decides whether a command opens a panel, runs an action, or jumps to a
 *  native surface. The Python registry (`chat_slash.COMMANDS`) stays the source
 *  of truth for which commands EXIST and their summary/usage. */
const REGISTRY: Record<string, Behavior> = {
  // `/usage` and `/preferences` are account-level — they work on the home screen
  // (`home: true`); everything else needs a live chat session.
  usage: { present: "panel", Panel: UsagePanel, home: true },
  cost: { present: "panel", Panel: CostPanel },
  mode: { present: "panel", Panel: ModePanel },
  title: { present: "panel", Panel: TitlePanel },
  clear: { present: "panel", Panel: ClearConfirmPanel },
  compact: { present: "immediate", run: (ctx) => void ctx.runCommand("/compact") },
  exit: { present: "immediate", run: (ctx) => ctx.exit() },
  preferences: { present: "surface", run: (ctx) => ctx.openPreferences(), home: true },
  // Account-level, so it's offered on the home composer too (where a user is
  // most likely to reach for it after finishing the tour).
  onboarding: { present: "surface", run: (ctx) => ctx.showOnboarding(), home: true },
  ...Object.fromEntries(
    MODE_SHORTCUTS.map((word) => [
      word,
      {
        present: "immediate",
        run: (ctx: SlashContext) => {
          const mode = resolveMode(word);
          if (mode) void ctx.setMode(mode);
        },
      } satisfies Behavior,
    ]),
  ),
};

/** Merge the daemon's command vocabulary with the editor registry, dropping any
 *  command the editor has no presentation for (e.g. `/help`). On the home screen
 *  (`atHome`), only the account-level commands survive. */
export function buildComposerCommands(
  daemon: SlashCommandInfo[],
  opts: { atHome?: boolean } = {},
): ComposerCommand[] {
  const out: ComposerCommand[] = [];
  for (const info of daemon) {
    const behavior = REGISTRY[info.name];
    if (!behavior) continue;
    if (opts.atHome && !behavior.home) continue;
    out.push({
      id: info.name,
      trigger: info.name,
      label: `/${info.name}`,
      description: info.summary,
      usage: info.usage,
      present: behavior.present,
      hidden: info.hidden,
    });
  }
  return out;
}

export interface UseSlashCommandsInput {
  chatId: string;
  currentMode: string;
  currentTitle: string | null;
  daemonCommands: SlashCommandInfo[];
  /** The home composer has no chat session — offer only account-level commands. */
  atHome?: boolean;
  /** Leave the chat (`/exit`). */
  exit: () => void;
  /** A `/title` change landed — refresh the chat-list surfaces. */
  onTitleChanged: () => void;
}

/** Route a typed slash line. A command with an action or surface presentation
 *  runs through the registry; a panel-presented one goes straight to the
 *  daemon, which owns its result card. An unknown command returns false so the
 *  line falls through to the agent as prose. */
export function routeSlashLine(
  text: string,
  commands: ComposerCommand[],
  route: { run: (command: ComposerCommand) => void; daemon: (line: string) => void },
): boolean {
  const name = text.slice(1).split(/\s/, 1)[0];
  const command = commands.find((candidate) => candidate.trigger === name);
  if (!command) return false;
  if (command.present !== "panel") route.run(command);
  else route.daemon(text);
  return true;
}

/** Wire the registry to a live chat: the composer's `commands`, its panel
 *  renderer, and its immediate/surface runner. */
export function useSlashCommands(input: UseSlashCommandsInput): {
  commands: ComposerCommand[];
  renderCommandPanel: (command: ComposerCommand, api: CommandPanelApi) => ReactNode;
  onCommandRun: (command: ComposerCommand) => void;
} {
  const { chatId, currentMode, currentTitle, daemonCommands, atHome, exit, onTitleChanged } = input;

  const ctx = useMemo<SlashContext>(
    () => ({
      chatId,
      currentMode,
      currentTitle,
      runCommand: (line) => chatData().runCommand(chatId, line),
      getUsage: (window) => chatData().getUsage(window),
      setMode: async (mode) => {
        await chatData().setPermissionMode(chatId, mode);
      },
      getCostState: () => chatData().getCostState(chatId),
      openPreferences: () => void chatHost().runCommand({ command: "alkera.openPreferences" }),
      showOnboarding: () => void chatHost().runCommand({ command: "alkera.onboarding" }),
      exit,
      onTitleChanged,
    }),
    [chatId, currentMode, currentTitle, exit, onTitleChanged],
  );

  const commands = useMemo(
    () => buildComposerCommands(daemonCommands, { atHome }),
    [daemonCommands, atHome],
  );

  const renderCommandPanel = useCallback(
    (command: ComposerCommand, api: CommandPanelApi): ReactNode => {
      const behavior = REGISTRY[command.id];
      if (behavior?.present !== "panel") return null;
      const Panel = behavior.Panel;
      return <Panel ctx={ctx} api={api} />;
    },
    [ctx],
  );

  const onCommandRun = useCallback(
    (command: ComposerCommand): void => {
      const behavior = REGISTRY[command.id];
      if (behavior && behavior.present !== "panel") behavior.run(ctx);
    },
    [ctx],
  );

  return { commands, renderCommandPanel, onCommandRun };
}
