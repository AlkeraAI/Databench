import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// The registry module imports the data source + host transitively (the panels
// do). Stub them with spies so importing it never touches a real daemon
// connection AND a hook test can assert the dispatch a command runs.
const { ds, host } = vi.hoisted(() => ({
  ds: {
    runCommand: vi.fn(async () => ({ kind: "ok" as const, command: null, payload: {}, message: null })),
    getUsage: vi.fn(async () => ({ credits: {}, usage: {} })),
    setPermissionMode: vi.fn(async () => "plan"),
    getCostState: vi.fn(async () => ({ spent: {}, caps: {}, orgManaged: false, unknownKeys: [] })),
  },
  host: { runCommand: vi.fn() },
}));
vi.mock("../data", () => ({ chatData: () => ds, chatHost: () => host }));

import { buildComposerCommands, useSlashCommands } from "./registry";
import type { ComposerCommand } from "@alkera/chat-model";

const DAEMON = [
  { name: "usage", aliases: [], summary: "Show usage.", usage: "[(window)]", hidden: false, ui: true },
  { name: "title", aliases: [], summary: "Set title.", usage: "[(text)]", hidden: false, ui: true },
  { name: "mode", aliases: [], summary: "Switch mode.", usage: "[(name)]", hidden: false, ui: false },
  { name: "preferences", aliases: ["prefs"], summary: "Preferences.", usage: "", hidden: false, ui: false },
  { name: "onboarding", aliases: [], summary: "Replay the welcome tour.", usage: "", hidden: false, ui: false },
  { name: "plan", aliases: [], summary: "Plan mode.", usage: "", hidden: true, ui: false },
  { name: "help", aliases: [], summary: "Help.", usage: "", hidden: false, ui: false },
];

describe("buildComposerCommands", () => {
  it("maps each known command to its editor presentation, daemon copy intact", () => {
    const byId = Object.fromEntries(buildComposerCommands(DAEMON).map((c) => [c.id, c]));
    expect(byId.usage.present).toBe("panel");
    expect(byId.title.present).toBe("panel");
    // cli_only daemon-side (ui:false), but the editor surfaces it as a panel.
    expect(byId.mode.present).toBe("panel");
    // A native-surface command.
    expect(byId.preferences.present).toBe("surface");
    // A hidden shortcut: still routable, but flagged out of the menu.
    expect(byId.plan.present).toBe("immediate");
    expect(byId.plan.hidden).toBe(true);
    // The daemon's own copy rides through untouched.
    expect(byId.usage).toMatchObject({
      trigger: "usage",
      label: "/usage",
      description: "Show usage.",
      usage: "[(window)]",
    });
  });

  it("drops a command the editor registry has no behavior for", () => {
    // Forward-compat: a daemon command absent from the editor REGISTRY is dropped
    // wholesale (no `present`), not surfaced with a guessed presentation that would
    // open a non-existent panel or run nothing on Enter. /help is today's case.
    const ids = buildComposerCommands([
      { name: "brandnew", aliases: [], summary: "Future cmd.", usage: "", hidden: false, ui: true },
      ...DAEMON,
    ]).map((c) => c.id);
    expect(ids).not.toContain("brandnew");
    expect(ids).not.toContain("help");
  });

  it("at home, offers only the account-level commands", () => {
    const ids = buildComposerCommands(DAEMON, { atHome: true }).map((c) => c.id);
    expect(ids).toEqual(["usage", "preferences", "onboarding"]);
  });
});

describe("useSlashCommands dispatch", () => {
  beforeEach(() => vi.clearAllMocks());

  function cmd(id: string, present: ComposerCommand["present"]): ComposerCommand {
    return { id, trigger: id, label: `/${id}`, present };
  }

  function mount(exit = vi.fn()) {
    const { result } = renderHook(() =>
      useSlashCommands({
        chatId: "c1",
        currentMode: "default",
        currentTitle: null,
        daemonCommands: [],
        exit,
        onTitleChanged: () => {},
      }),
    );
    return { api: result.current, exit };
  }

  function run(id: string, present: ComposerCommand["present"]): void {
    mount().api.onCommandRun(cmd(id, present));
  }

  it("/compact dispatches the raw line to the daemon for this chat", () => {
    run("compact", "immediate");
    expect(ds.runCommand).toHaveBeenCalledWith("c1", "/compact");
  });

  it("/exit invokes the host-supplied exit, never the daemon", () => {
    const { api, exit } = mount();
    api.onCommandRun(cmd("exit", "immediate"));
    expect(exit).toHaveBeenCalledTimes(1);
    expect(ds.runCommand).not.toHaveBeenCalledWith("c1", "/exit");
  });

  it.each([
    ["preferences", "alkera.openPreferences"],
    ["onboarding", "alkera.onboarding"],
  ])("/%s runs the host command %s, never the daemon", (id, command) => {
    run(id, "surface");
    expect(host.runCommand).toHaveBeenCalledWith({ command });
    expect(ds.runCommand).not.toHaveBeenCalled();
  });

  // The MODE_SHORTCUTS map is load-bearing: a wrong word→mode entry would silently
  // put the session in the wrong permission mode. Each typed shortcut must resolve
  // to its canonical mode (mirrors `MODE_ALIASES` in permission_mode.py).
  it.each([
    ["normal", "default"],
    ["read-only", "read_only"],
    ["auto", "auto"],
    ["plan", "plan"],
    ["bypass", "bypass"],
    ["yolo", "bypass"],
  ])("/%s shortcut switches to %s via setPermissionMode", (word, mode) => {
    run(word, "immediate");
    expect(ds.setPermissionMode).toHaveBeenCalledWith("c1", mode);
  });

  it("renderCommandPanel draws a panel command, and null for the rest", () => {
    const { result } = renderHook(() =>
      useSlashCommands({
        chatId: "c1",
        currentMode: "default",
        currentTitle: null,
        daemonCommands: [],
        exit: () => {},
        onTitleChanged: () => {},
      }),
    );
    const api = { close: () => {} };
    // A panel command resolves a real element (the registry's Panel component).
    const usage = result.current.renderCommandPanel(cmd("usage", "panel"), api);
    expect(usage).not.toBeNull();
    // An immediate/surface command has no panel — the renderer returns null so the
    // Composer falls through to onCommandRun instead of rendering an empty box.
    expect(result.current.renderCommandPanel(cmd("compact", "immediate"), api)).toBeNull();
    expect(result.current.renderCommandPanel(cmd("preferences", "surface"), api)).toBeNull();
  });
});
