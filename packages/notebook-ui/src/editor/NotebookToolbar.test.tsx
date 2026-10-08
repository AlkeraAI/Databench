import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { currentActorNames } from "../model/actor";
import { ABSENT_KERNEL, type CellRuntime, type EnvInfo, type KernelView, type Presence } from "../model/types";
import { kernelChipTarget, NotebookToolbar, type NotebookToolbarProps } from "./NotebookToolbar";

const ENV: EnvInfo = { env_id: "default", kind: "default", spec_root: "/w/.alkera/envs/default", python: "3.12.13", state: "ready", recorded_in_file: true };

function toolbar(overrides: Partial<NotebookToolbarProps> = {}) {
  const onPanel = vi.fn();
  const props: NotebookToolbarProps = {
    kernel: { ...ABSENT_KERNEL, env: ENV },
    envs: [ENV],
    presence: [],
    canRun: true,
    panel: null,
    narrow: false,
    findOpen: false,
    chipTarget: null,
    keyFor: () => undefined,
    onCommand: () => {},
    onEnv: () => {},
    onPanel,
    onFindToggle: () => {},
    onShowCell: () => {},
    ...overrides,
  };
  render(<NotebookToolbar {...props} />);
  return { onPanel };
}

describe("the kernel chip", () => {
  it.each([
    ["absent", "No kernel"],
    ["starting", "Starting"],
    ["idle", "Idle"],
  ] as const)("says the machine is waking over a %s kernel while it wakes", (state, label) => {
    toolbar({ kernel: { ...ABSENT_KERNEL, state }, waking: true });
    expect(screen.getByTitle("Kernel")).toHaveTextContent(/^Waking the machine…$/);
    cleanup();
    toolbar({ kernel: { ...ABSENT_KERNEL, state }, waking: false });
    expect(screen.getByTitle("Kernel")).toHaveTextContent(new RegExp(`^${label}$`));
  });
});

describe("the toolbar when narrow", () => {
  it("puts the panels in one menu and opens the one picked", () => {
    const { onPanel } = toolbar({ narrow: true });
    expect(screen.queryByRole("button", { name: "Graph" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Panels" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Environment" }));
    expect(onPanel).toHaveBeenCalledWith("environment");
  });

  it("is an icon-only menu that checks the open panel and closes it when picked again", () => {
    const { onPanel } = toolbar({ narrow: true, panel: "variables" });
    const menu = screen.getByRole("button", { name: "Panels" });
    expect(menu).toHaveTextContent(/^$/);
    fireEvent.click(menu);
    expect(screen.getByRole("menuitemcheckbox", { name: "Variables" })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Variables" }));
    expect(onPanel).toHaveBeenCalledWith(null);
  });

  it("shortens the environment to its Python", () => {
    toolbar({ narrow: true });
    expect(screen.getByRole("button", { name: "Environment" })).toHaveTextContent(/^Python 3\.12\.13$/);
  });

  it("keeps the panels in one menu and the whole environment when wide", () => {
    const { onPanel } = toolbar();
    expect(screen.queryByRole("button", { name: "Graph" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Panels" }));
    expect(screen.getAllByRole("menuitemcheckbox").map((i) => i.textContent)).toEqual(["Graph", "Variables", "Outline", "Environment"]);
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Graph" }));
    expect(onPanel).toHaveBeenCalledWith("graph");
    expect(screen.getByRole("button", { name: "Environment", expanded: false })).toHaveTextContent("Default · Python 3.12.13");
  });

  it("opens and closes Settings from its gear key, beside Find", () => {
    const { onPanel } = toolbar();
    const gear = screen.getByRole("button", { name: "Settings" });
    expect(gear).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(gear);
    expect(onPanel).toHaveBeenCalledWith("settings");
    cleanup();
    const second = toolbar({ panel: "settings" });
    fireEvent.click(screen.getByRole("button", { name: "Settings", pressed: true }));
    expect(second.onPanel).toHaveBeenCalledWith(null);
  });
});

describe("who the toolbar names", () => {
  const ada = { id: "user:1", display_name: "Ada" };
  // The agent's name comes from the brand the build registers, so read it from
  // the same source the toolbar does.
  const agentForAda = `${currentActorNames().agent} for Ada`;

  it("shows no run queue pill while a run is in progress", () => {
    const kernel: KernelView = {
      ...ABSENT_KERNEL,
      state: "busy",
      queue: [
        { run_id: "r1", by: { kind: "agent", id: "agent:a1", display_name: "Analyst", acting_for: ada }, trigger: "run", status: "running", targets: ["c1"] },
        { run_id: "r2", by: { kind: "person", id: "user:9", display_name: "" }, trigger: "run_all", status: "queued", targets: [] },
      ],
    };
    toolbar({ kernel });
    expect(screen.queryByRole("button", { name: /^Running for/ })).toBeNull();
    expect(document.querySelector(".nb-queue")).toBeNull();
  });

  it("labels the faces of who is here", () => {
    const presence: Presence[] = [
      { id: "p1", kind: "person", display_name: "Ben", hue: 10, cell_id: null },
      { id: "agent:a1", kind: "agent", display_name: "Analyst", acting_for: ada, hue: 20, cell_id: "c1" },
    ];
    toolbar({ presence });
    expect(screen.getByLabelText(`In this notebook: Ben, ${agentForAda}`)).toBeInTheDocument();
    expect(screen.getByTitle(agentForAda)).toBeInTheDocument();
  });

  it("shows one face for an agent that is in several cells", () => {
    const presence: Presence[] = ["c1", "c2", "c3"].map((cell_id) => ({
      id: "agent:chat-1",
      kind: "agent",
      display_name: "Analyst",
      acting_for: ada,
      hue: 20,
      cell_id,
    }));
    toolbar({ presence: [{ id: "p1", kind: "person", display_name: "Ben", hue: 10, cell_id: "c1" }, ...presence] });
    expect(screen.getByLabelText(`In this notebook: Ben, ${agentForAda}`)).toBeInTheDocument();
    expect(document.querySelectorAll(".nb-toolbar__presence .nb-face")).toHaveLength(2);
  });
});

describe("the environment picker", () => {
  const BOX = "/opt/alkera-work/orgs/0/work/.alkera/chats/fdc3271d/scratch";
  const uv: EnvInfo = { env_id: "uv_project:.", kind: "uv_project", spec_root: BOX, python: "", state: "ready", recorded_in_file: false };
  const plain: EnvInfo = { env_id: "default:.alkera/envs/default", kind: "default", spec_root: `${BOX}/.alkera/envs/default`, python: "", state: "ready", recorded_in_file: true };

  it("names each environment by its name, never by the machine's path, and leaves out an unknown version", () => {
    toolbar({ kernel: { ...ABSENT_KERNEL, env: uv }, envs: [uv, plain] });
    const trigger = screen.getByRole("button", { name: "Environment", expanded: false });
    expect(trigger).toHaveTextContent(/^uv project$/);
    fireEvent.click(trigger);
    const items = screen.getAllByRole("menuitemcheckbox").map((item) => item.textContent);
    expect(items).toEqual(["uv project", "Default"]);
    expect(document.body.textContent).not.toContain("/opt/");
    expect(document.body.textContent).not.toMatch(/Python\s*\)/);
  });

  it("shows the version from the listing when it is known", () => {
    toolbar({ kernel: { ...ABSENT_KERNEL, env: { ...uv, python: "3.12.13" } }, envs: [{ ...uv, python: "3.12.13" }] });
    fireEvent.click(screen.getByRole("button", { name: "Environment", expanded: false }));
    expect(screen.getByRole("menuitemcheckbox", { name: "uv project · Python 3.12.13" })).toHaveAttribute("aria-checked", "true");
  });

  it("falls back to the name when narrow and the version is unknown", () => {
    toolbar({ narrow: true, kernel: { ...ABSENT_KERNEL, env: uv }, envs: [uv] });
    expect(screen.getByRole("button", { name: "Environment", expanded: false })).toHaveTextContent(/^uv project$/);
  });
});

describe("the kernel chip", () => {
  it("jumps to the running cell when clicked while a cell runs", () => {
    const onShowCell = vi.fn();
    toolbar({ kernel: { ...ABSENT_KERNEL, env: ENV, state: "busy" }, chipTarget: { cellId: "c3", running: true }, onShowCell });
    fireEvent.click(screen.getByRole("button", { name: /Busy/ }));
    expect(onShowCell).toHaveBeenCalledWith("c3");
  });

  it("is plain text when no cell runs", () => {
    toolbar({ kernel: { ...ABSENT_KERNEL, env: ENV, state: "idle" } });
    expect(screen.queryByRole("button", { name: /Idle/ })).toBeNull();
    expect(screen.getByTitle("Kernel")).toHaveTextContent("Idle");
  });
});

describe("the find button", () => {
  it.each([false, true])("says whether find is open (%s) and toggles it", (findOpen) => {
    const onFindToggle = vi.fn();
    toolbar({ findOpen, onFindToggle });
    const button = screen.getByRole("button", { name: "Find" });
    expect(button).toHaveAttribute("aria-pressed", String(findOpen));
    fireEvent.click(button);
    expect(onFindToggle).toHaveBeenCalledTimes(1);
  });
});

describe("where the kernel chip jumps", () => {
  const ran = (finished_at: string | null, status: CellRuntime["status"] = "fresh") => ({
    status,
    last_run: finished_at === null ? null : { run_id: "r", by: { kind: "person" as const, id: "u", display_name: "Ann" }, trigger: "run" as const, started_at: finished_at, finished_at },
  });

  it("goes to the lowest running cell while cells run", () => {
    const cells = { a: ran(null, "running"), b: ran("2026-10-06T10:00:00Z"), c: ran(null, "running") };
    expect(kernelChipTarget(["a", "b", "c"], (id) => cells[id as keyof typeof cells])).toEqual({ cellId: "c", running: true });
  });

  it("goes to the cell whose run ended last when idle, wherever it sits", () => {
    const cells = { a: ran("2026-10-06T10:05:00Z"), b: ran("2026-10-06T10:01:00Z"), c: ran(null) };
    expect(kernelChipTarget(["a", "b", "c"], (id) => cells[id as keyof typeof cells])).toEqual({ cellId: "a", running: false });
  });

  it("has nowhere to go when nothing ran", () => {
    expect(kernelChipTarget(["a"], () => ran(null))).toBeNull();
  });
});

describe("the idle kernel chip", () => {
  it("jumps to the last cell that ran", () => {
    const onShowCell = vi.fn();
    toolbar({ kernel: { ...ABSENT_KERNEL, env: ENV, state: "idle" }, chipTarget: { cellId: "c1", running: false }, onShowCell });
    fireEvent.click(screen.getByRole("button", { name: /Idle/ }));
    expect(onShowCell).toHaveBeenCalledWith("c1");
  });
});

describe("the run keys", () => {
  it("offers Run stale right after Run all, and each sends its own command", () => {
    const sent: string[] = [];
    toolbar({ onCommand: (id) => sent.push(id) });
    const runAll = screen.getByRole("button", { name: "Run all" });
    const runStale = screen.getByRole("button", { name: "Run stale" });
    expect(runAll.nextElementSibling).toBe(runStale);
    fireEvent.click(runStale);
    fireEvent.click(runAll);
    expect(sent).toEqual(["run.stale", "run.all"]);
  });

  it("offers neither to someone who may not run", () => {
    toolbar({ canRun: false });
    expect(screen.queryByRole("button", { name: "Run stale" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Run all" })).toBeNull();
  });
});
