// The notebook's commands and the keys that reach them.
//
// Every action a person can take (from a menu, a button, the keyboard) is a
// command with an id, a label and default keys; menus and the keyboard both
// ask this table, so a key is rebound in one place and a menu shows the key
// that actually runs. Keys follow Jupyter's conventions: in command mode bare
// letters act on cells (A, B, D D, Z, Y, M, S, J, K, I I, 0 0), Enter starts
// editing, Escape leaves it; the run keys (Shift+Enter, Mod+Enter, Alt+Enter)
// work in both modes. Pure, so the whole table and the chord resolver are a
// test.

export type EditorMode = "command" | "edit";

export type CommandId =
  // runs
  | "run.cell"
  | "run.advance"
  | "run.insert_below"
  | "run.all"
  | "run.stale"
  | "run.above"
  | "run.below"
  | "kernel.interrupt"
  | "kernel.interrupt_clear"
  | "kernel.restart"
  | "kernel.restart_run_all"
  | "kernel.shutdown"
  // cells
  | "cell.insert_above"
  | "cell.insert_below"
  | "cell.insert_markdown_below"
  | "cell.insert_sql_below"
  | "cell.insert_markdown_above"
  | "cell.insert_sql_above"
  | "cell.delete"
  | "cell.restore"
  | "cell.duplicate"
  | "cell.split"
  | "cell.merge_next"
  | "cell.move_up"
  | "cell.move_down"
  | "cell.to_python"
  | "cell.to_markdown"
  | "cell.to_sql"
  | "cell.toggle_disabled"
  | "cell.toggle_hide_code"
  | "cell.toggle_show_result"
  | "cell.toggle_expand_output"
  | "cell.copy_link"
  | "cell.rename"
  // outputs
  | "output.clear"
  | "output.clear_all"
  | "output.toggle_collapse"
  | "output.open_tab"
  | "output.download_image"
  // navigation and modes
  | "nav.previous"
  | "nav.next"
  | "mode.edit"
  | "mode.command"
  // history, find, comments
  | "doc.undo"
  | "doc.redo"
  | "find.open"
  | "find.replace"
  | "edit.toggle_comment";

export interface CommandDef {
  id: CommandId;
  label: string;
  group: "run" | "kernel" | "cell" | "output" | "navigate" | "edit" | "find";
  /** Default keys. A space separates the steps of a chord (`d d`). */
  keys: readonly string[];
  /** The modes the keys work in. */
  modes: readonly EditorMode[];
  /** Needs the right to run code. */
  run?: boolean;
  /** Needs the right to edit the document. */
  edit?: boolean;
}

const BOTH: readonly EditorMode[] = ["command", "edit"];
const COMMAND: readonly EditorMode[] = ["command"];
const EDIT: readonly EditorMode[] = ["edit"];

export const COMMANDS: readonly CommandDef[] = [
  { id: "run.cell", label: "Run cell", group: "run", keys: ["Mod-Enter"], modes: BOTH, run: true },
  { id: "run.advance", label: "Run and advance", group: "run", keys: ["Shift-Enter"], modes: BOTH, run: true },
  { id: "run.insert_below", label: "Run and insert below", group: "run", keys: ["Alt-Enter"], modes: BOTH, run: true, edit: true },
  { id: "run.all", label: "Run all", group: "run", keys: [], modes: BOTH, run: true },
  { id: "run.stale", label: "Run stale", group: "run", keys: [], modes: BOTH, run: true },
  { id: "run.above", label: "Run above", group: "run", keys: [], modes: BOTH, run: true },
  { id: "run.below", label: "Run below", group: "run", keys: [], modes: BOTH, run: true },
  { id: "kernel.interrupt", label: "Interrupt", group: "kernel", keys: ["i i"], modes: COMMAND, run: true },
  { id: "kernel.interrupt_clear", label: "Interrupt and clear the queue", group: "kernel", keys: [], modes: BOTH, run: true },
  { id: "kernel.restart", label: "Restart", group: "kernel", keys: ["0 0"], modes: COMMAND, run: true },
  { id: "kernel.restart_run_all", label: "Restart and run all", group: "kernel", keys: [], modes: BOTH, run: true },
  { id: "kernel.shutdown", label: "Shut down", group: "kernel", keys: [], modes: BOTH, run: true },
  { id: "cell.insert_above", label: "Insert cell above", group: "cell", keys: ["a"], modes: COMMAND, edit: true },
  { id: "cell.insert_below", label: "Insert cell below", group: "cell", keys: ["b"], modes: COMMAND, edit: true },
  { id: "cell.insert_markdown_below", label: "Insert Markdown below", group: "cell", keys: [], modes: BOTH, edit: true },
  { id: "cell.insert_sql_below", label: "Insert SQL below", group: "cell", keys: [], modes: BOTH, edit: true },
  { id: "cell.insert_markdown_above", label: "Insert Markdown above", group: "cell", keys: [], modes: BOTH, edit: true },
  { id: "cell.insert_sql_above", label: "Insert SQL above", group: "cell", keys: [], modes: BOTH, edit: true },
  { id: "cell.delete", label: "Delete cell", group: "cell", keys: ["d d"], modes: COMMAND, edit: true },
  { id: "cell.restore", label: "Restore deleted cell", group: "cell", keys: ["z"], modes: COMMAND, edit: true },
  { id: "cell.duplicate", label: "Duplicate cell", group: "cell", keys: [], modes: BOTH, edit: true },
  { id: "cell.split", label: "Split cell at cursor", group: "cell", keys: ["Mod-Shift-Minus"], modes: EDIT, edit: true },
  { id: "cell.merge_next", label: "Merge with next cell", group: "cell", keys: ["Shift-m"], modes: COMMAND, edit: true },
  { id: "cell.move_up", label: "Move cell up", group: "cell", keys: ["Mod-Shift-ArrowUp"], modes: COMMAND, edit: true },
  { id: "cell.move_down", label: "Move cell down", group: "cell", keys: ["Mod-Shift-ArrowDown"], modes: COMMAND, edit: true },
  { id: "cell.to_python", label: "Change to Python", group: "cell", keys: ["y"], modes: COMMAND, edit: true },
  { id: "cell.to_markdown", label: "Change to Markdown", group: "cell", keys: ["m"], modes: COMMAND, edit: true },
  { id: "cell.to_sql", label: "Change to SQL", group: "cell", keys: ["s"], modes: COMMAND, edit: true },
  { id: "cell.toggle_disabled", label: "Disable cell", group: "cell", keys: [], modes: BOTH, edit: true },
  { id: "cell.toggle_hide_code", label: "Hide code", group: "cell", keys: [], modes: BOTH, edit: true },
  { id: "cell.toggle_show_result", label: "Show result", group: "cell", keys: [], modes: BOTH, edit: true },
  { id: "cell.toggle_expand_output", label: "Expand output", group: "cell", keys: [], modes: BOTH, edit: true },
  { id: "cell.copy_link", label: "Copy cell link", group: "cell", keys: [], modes: BOTH },
  { id: "cell.rename", label: "Rename cell", group: "cell", keys: [], modes: BOTH, edit: true },
  { id: "output.clear", label: "Clear output", group: "output", keys: [], modes: BOTH, run: true },
  { id: "output.clear_all", label: "Clear all outputs", group: "output", keys: [], modes: BOTH, run: true },
  { id: "output.toggle_collapse", label: "Collapse output", group: "output", keys: ["o"], modes: COMMAND },
  { id: "output.open_tab", label: "Open output in a new tab", group: "output", keys: [], modes: BOTH },
  { id: "output.download_image", label: "Download image", group: "output", keys: [], modes: BOTH },
  { id: "nav.previous", label: "Previous cell", group: "navigate", keys: ["k", "ArrowUp"], modes: COMMAND },
  { id: "nav.next", label: "Next cell", group: "navigate", keys: ["j", "ArrowDown"], modes: COMMAND },
  { id: "mode.edit", label: "Edit cell", group: "navigate", keys: ["Enter"], modes: COMMAND },
  { id: "mode.command", label: "Leave the editor", group: "navigate", keys: ["Escape"], modes: EDIT },
  { id: "doc.undo", label: "Undo", group: "edit", keys: ["Mod-z"], modes: COMMAND, edit: true },
  { id: "doc.redo", label: "Redo", group: "edit", keys: ["Mod-Shift-z", "Mod-y"], modes: COMMAND, edit: true },
  { id: "find.open", label: "Find", group: "find", keys: ["Mod-f"], modes: BOTH },
  { id: "find.replace", label: "Find and replace", group: "find", keys: ["Mod-Shift-h"], modes: BOTH },
  { id: "edit.toggle_comment", label: "Toggle comment", group: "edit", keys: ["Mod-/"], modes: EDIT, edit: true },
];

const BY_ID = new Map(COMMANDS.map((c) => [c.id, c]));

export function commandDef(id: CommandId): CommandDef {
  const def = BY_ID.get(id);
  if (def === undefined) throw new Error(`unknown command ${id}`);
  return def;
}

/** A person's own keys for some commands, replacing the defaults for those. */
export type KeyOverrides = Partial<Record<CommandId, readonly string[]>>;

export interface KeyFacts {
  key: string;
  code?: string;
  metaKey: boolean;
  ctrlKey: boolean;
  shiftKey: boolean;
  altKey: boolean;
}

export type Platform = "mac" | "other";

/** One keystroke in the spelling the table uses: `Mod-Shift-Enter`, `a`. */
export function strokeOf(event: KeyFacts, platform: Platform): string | null {
  if (["Shift", "Control", "Alt", "Meta"].includes(event.key)) return null;
  const mod = platform === "mac" ? event.metaKey : event.ctrlKey;
  const other = platform === "mac" ? event.ctrlKey : event.metaKey;
  if (other) return null;
  let key = event.key;
  if (key === " ") key = "Space";
  if (key === "-" || event.code === "Minus") key = "Minus";
  if (key.length === 1) key = key.toLowerCase();
  // A shifted letter arrives upper case; Shift is spelled out instead.
  const parts: string[] = [];
  if (mod) parts.push("Mod");
  if (event.altKey) parts.push("Alt");
  if (event.shiftKey) parts.push("Shift");
  parts.push(key);
  return parts.join("-");
}

/** How long the second key of a chord (`d d`) may wait. */
export const CHORD_MS = 800;

export type Resolution = { kind: "command"; id: CommandId } | { kind: "pending" } | { kind: "none" };

/** Turns keystrokes into commands for one mode at a time, chords included. */
export class KeyResolver {
  private readonly table: Map<EditorMode, Map<string, CommandId>>;
  private readonly prefixes: Map<EditorMode, Set<string>>;
  private pending: { stroke: string; at: number } | null = null;

  constructor(overrides: KeyOverrides = {}, commands: readonly CommandDef[] = COMMANDS) {
    this.table = new Map([
      ["command", new Map()],
      ["edit", new Map()],
    ]);
    this.prefixes = new Map([
      ["command", new Set()],
      ["edit", new Set()],
    ]);
    for (const def of commands) {
      const keys = overrides[def.id] ?? def.keys;
      for (const mode of def.modes) {
        for (const spec of keys) {
          this.table.get(mode)!.set(spec, def.id);
          const steps = spec.split(" ");
          if (steps.length > 1) this.prefixes.get(mode)!.add(steps[0]!);
        }
      }
    }
  }

  /** The keys a command answers to now, for a menu to show. */
  keysFor(id: CommandId): string[] {
    const out = new Set<string>();
    for (const map of this.table.values()) for (const [spec, cmd] of map) if (cmd === id) out.add(spec);
    return [...out];
  }

  resolve(event: KeyFacts, mode: EditorMode, platform: Platform, now: number): Resolution {
    const stroke = strokeOf(event, platform);
    if (stroke === null) return { kind: "none" };
    const table = this.table.get(mode)!;
    const pending = this.pending;
    this.pending = null;
    if (pending !== null && now - pending.at <= CHORD_MS) {
      const chord = table.get(`${pending.stroke} ${stroke}`);
      if (chord !== undefined) return { kind: "command", id: chord };
    }
    if (this.prefixes.get(mode)!.has(stroke)) {
      this.pending = { stroke, at: now };
      // A key that is both a chord's first step and a command of its own
      // waits for the chord; nothing in the default table is both.
      return { kind: "pending" };
    }
    const single = table.get(stroke);
    return single === undefined ? { kind: "none" } : { kind: "command", id: single };
  }

  reset(): void {
    this.pending = null;
  }
}

/** A key spec as a person reads it on this platform: `⌘⇧Enter`, and a
 *  sequence of presses with a comma between them (`D, D`), so a key pressed
 *  twice never reads as a label drawn twice. */
export function describeKeys(spec: string, platform: Platform): string {
  return spec
    .split(" ")
    .map((step) =>
      step
        .split("-")
        .map((part) => {
          if (part === "Mod") return platform === "mac" ? "⌘" : "Ctrl+";
          if (part === "Shift") return platform === "mac" ? "⇧" : "Shift+";
          if (part === "Alt") return platform === "mac" ? "⌥" : "Alt+";
          if (part === "Minus") return "-";
          if (part === "ArrowUp") return "↑";
          if (part === "ArrowDown") return "↓";
          return part.length === 1 ? part.toUpperCase() : part;
        })
        .join(""),
    )
    .join(", ");
}

export function platformOf(nav: { platform?: string; userAgent?: string } | undefined): Platform {
  const hint = `${nav?.platform ?? ""} ${nav?.userAgent ?? ""}`;
  return /Mac|iPhone|iPad/.test(hint) ? "mac" : "other";
}
