// The notebook tab on the stand-in live server: two people open one notebook,
// see its cells, type into a cell and see each other's text; the engine's
// events on the notebook channel move a cell's status; a run is requested
// with the document frontier the person holds; a reader gets no controls;
// carets are drawn in the cell they stand in, and the caret a tab publishes
// names its cell; the view's kernel shows until the channel says more, and a
// resync with no view subscribes again.

import { QueryClientProvider } from "@tanstack/react-query";
import { EditorSelection } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { meKey } from "@/api/auth";
import type { Item } from "@/api/files";
import type { NotebookView } from "@/api/notebooks";
import { createQueryClient } from "@/api/queryClient";
import { LiveDocChannel, type Timers } from "@/api/realtime/crdt/channel";
import { LoroCodeMirrorBinding } from "@/api/realtime/crdt/codeMirrorBinding";
import { NotebookDocument } from "@/api/realtime/crdt/notebookDoc";
import { SendBudget } from "@/api/realtime/crdt/sendBudget";
import { liveCarets } from "@/pages/workspace/chat/workspace/liveCarets";
import NotebookTab, { NOT_OPENED, OPENING } from "@/pages/workspace/chat/workspace/notebook/NotebookTab";
import { LIVE_OPEN_DEADLINE, RETRY_JITTER_RATIO } from "@/lib/limits";
import { acquireLiveNotebook, closeAllLiveNotebooks } from "@/pages/workspace/chat/workspace/notebook/liveNotebook";
import { NotebookCarets } from "@/pages/workspace/chat/workspace/notebook/notebookCarets";
import type { WorkspaceCtx, WorkspaceTab } from "@/pages/workspace/chat/workspace/tabKinds";

import { LiveServer, loroNode, settle, type FakeSocket } from "../../../../../api/realtime/crdt/liveServer";

const NODE = "9b1c2d3e-4f50-4a6b-8c7d-0e1f2a3b4c5d";
const CHANNEL = `doc:notebook:${NODE}`;
const DRIVE = "drive-1";

function seed(server: LiveServer): { x: string; y: string } {
  const { doc } = server.docOf(CHANNEL);
  let n = 0;
  const nb = new NotebookDocument({
    loro: loroNode,
    source: { doc, canWrite: true, listen: () => () => {}, localCommitted: () => {} },
    newId: () => ["xxxxxxxxx0", "yyyyyyyyy0"][n++]!,
  });
  const [x, y] = nb.apply([
    { op: "insert", source: "x = 1", name: "load" },
    { op: "insert", source: "print(x)" },
  ]).created as [string, string];
  return { x, y };
}

async function pump(...sockets: FakeSocket[]): Promise<void> {
  for (let round = 0; round < 30; round += 1) {
    await act(async () => {
      await settle();
      for (const s of sockets) await s.deliver();
    });
    if (sockets.every((s) => s.inbox.length === 0)) {
      await act(async () => settle());
      if (sockets.every((s) => s.inbox.length === 0)) return;
    }
  }
}

interface Request {
  url: string;
  method: string;
  body: unknown;
}

let requests: Request[];
/** The view the notebook's GET answers, and what it waits for first. */
let viewBody: NotebookView;
let viewGate: Promise<void>;
/** The engine's envs listing, and the page the table route answers. */
let envsBody: unknown;
let installReply: { status: number; body: unknown } | null = null;
let kernelStatus = 200;
/** How many asks for a run (or the environments) the server answers with
 *  "waking the machine" before it answers them. */
let wakingRuns = 0;
let wakingEnvs = 0;
/** What the server answers the requests that need the notebook's machine (a
 *  run, a kernel action, an output clear) with, when it refuses them. */
let kernelRefusal: { status: number; body: unknown } | null = null;
let tableBody: unknown;
/** What the notebook's connections route answers. */
let connectionsBody: unknown;
const ENV = {
  env_id: "default:.alkera/envs/default",
  kind: "default",
  spec_root: "/w/.alkera/envs/default",
  python: "3.12.13",
  state: "ready",
  recorded_in_file: true,
};

function view(kernel: Partial<NotebookView["kernel"]> = {}): NotebookView {
  return {
    path: "analysis.alknb.py",
    token: "t",
    settings: { format: "1.0", reactivity: "autorun", dataframe: "auto", outputs_in_git: false, autoreload: "off" },
    kernel: { state: "absent", env: null, reactivity: "autorun", kernel_id: null, seq: null, env_outdated: false, ...kernel },
    cells: [],
    presence: [],
    read_only_reason: null,
  };
}

beforeEach(() => {
  requests = [];
  viewBody = view();
  viewGate = Promise.resolve();
  envsBody = { current: ENV, envs: [ENV] };
  installReply = null;
  kernelStatus = 200;
  wakingRuns = 0;
  wakingEnvs = 0;
  kernelRefusal = null;
  tableBody = { rows: { columns: [], rows: [] }, total_rows: 0, offset: 0, limit: 50 };
  connectionsBody = { connections: [] };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      // The reader is known before the notebook is held live.
      if (String(url).includes("/auth/me")) {
        return new Response(JSON.stringify(ME), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      requests.push({ url: String(url), method: init.method ?? "GET", body: init.body ? JSON.parse(String(init.body)) : null });
      const path = new URL(String(url), "http://app").pathname;
      const runs = path.endsWith("/runs");
      if (!runs) await viewGate;
      const waking = runs ? wakingRuns > 0 : path.endsWith("/envs") && wakingEnvs > 0;
      if (waking) {
        if (runs) wakingRuns -= 1;
        else wakingEnvs -= 1;
        return new Response(JSON.stringify({ code: "notebook.waking", message: "Waking the machine…" }), {
          status: 503,
          headers: { "Content-Type": "application/json", "Retry-After": "3" },
        });
      }
      if (kernelRefusal !== null && /\/(runs|kernel|outputs\/clear)$/.test(path)) {
        return new Response(JSON.stringify(kernelRefusal.body), {
          status: kernelRefusal.status,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (path.endsWith("/kernel") && kernelStatus !== 200) {
        return new Response(JSON.stringify({ code: "notebook.unavailable", message: "busy" }), {
          status: kernelStatus,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (installReply !== null && path.endsWith("/env/install")) {
        return new Response(JSON.stringify(installReply.body), {
          status: installReply.status,
          headers: { "Content-Type": "application/json" },
        });
      }
      const body = runs
        ? { run_id: "r1", status: "queued", frontier_included: true, repeat: false }
        : path.endsWith("/envs")
          ? envsBody
          : path.endsWith("/packages")
            ? { env_id: "default", packages: [{ name: "polars", version: "1.9.0" }] }
            : path.endsWith("/table")
              ? tableBody
              : path.endsWith("/connections")
                ? connectionsBody
                : viewBody;
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
});

afterEach(() => {
  closeAllLiveNotebooks();
  vi.unstubAllGlobals();
  document.documentElement.removeAttribute("data-alkera-color-scheme");
  document.body.classList.remove("vscode-light");
});

/** Who is signed in: the notebook is held live only for a known reader. */
const ME = { id: "usr_ana", org_team_id: "org_a", email: "ana@acme.test" };

function mount(socket: FakeSocket, wake?: () => void) {
  const ctx: WorkspaceCtx = { chatId: "chat-1", driveId: DRIVE, rootNodeId: "root", liveSocket: socket, ...(wake ? { wake } : {}) };
  const tab: WorkspaceTab = { id: "t1", kind: "file", node_id: NODE, name: "analysis.alknb.py", view: "notebook" };
  const item = { id: NODE, name: "analysis.alknb.py" } as unknown as Item;
  const client = createQueryClient({ retry: false });
  client.setQueryData(meKey, ME);
  return render(
    <QueryClientProvider client={client}>
      <NotebookTab tab={tab} ctx={ctx} item={item} onEdit={() => {}} loadLoro={() => Promise.resolve(loroNode)} />
    </QueryClientProvider>,
  );
}

const editorOf = (root: HTMLElement, cellId: string) => root.querySelector<HTMLElement>(`[data-cell-id="${cellId}"] .cm-content`)!;
const viewOf = (root: HTMLElement, cellId: string) => EditorView.findFromDOM(root.querySelector<HTMLElement>(`[data-cell-id="${cellId}"] .cm-editor`)!)!;

describe("the notebook tab", () => {
  /** Past the longest wait the channel arms on an unanswered ask. */
  async function outwaitDeadline(socket: FakeSocket): Promise<void> {
    await act(async () => {
      vi.advanceTimersByTime(Math.ceil(LIVE_OPEN_DEADLINE.capMs * (1 + RETRY_JITTER_RATIO)) + 1);
    });
    await pump(socket);
  }

  it.each([["hello"], ["subscribe"]])("opens live without a reload when the answer to its first %s is lost", async (lost) => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout"] });
    try {
      const server = new LiveServer();
      const { x } = seed(server);
      if (lost === "hello") server.ignoreHellos = 1;
      else server.ignoreSubscribes = 1;
      const a = server.socket("ana");
      const ana = mount(a);
      await pump(a);
      expect(screen.getByRole("status")).toHaveTextContent(OPENING);
      await outwaitDeadline(a);
      await waitFor(() => expect(editorOf(ana.container, x).textContent).toBe("x = 1"));
      expect(screen.queryByText(OPENING)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("offers Retry when the server never answers, and Retry opens it live", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout"] });
    try {
      const server = new LiveServer();
      const { x } = seed(server);
      server.ignoreHellos = Infinity;
      const a = server.socket("ana");
      const ana = mount(a);
      await pump(a);
      for (let i = 0; i < LIVE_OPEN_DEADLINE.attempts; i += 1) await outwaitDeadline(a);
      expect(screen.getByRole("alert")).toHaveTextContent(NOT_OPENED);
      server.ignoreHellos = 0;
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await pump(a);
      await waitFor(() => expect(editorOf(ana.container, x).textContent).toBe("x = 1"));
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows the live cells, and one person's typing reaches the other", async () => {
    const server = new LiveServer();
    const { x } = seed(server);
    const a = server.socket("ana");
    const b = server.socket("ben");
    const ana = mount(a);
    await pump(a);
    const ben = mount(b);
    await pump(a, b);
    expect(editorOf(ana.container, x).textContent).toBe("x = 1");
    expect(within(ana.container.querySelector<HTMLElement>(`[data-cell-id="${x}"]`)!).getByText("load")).toBeInTheDocument();
    act(() => viewOf(ana.container, x).dispatch({ changes: { from: 5, insert: "0" } }));
    await pump(a, b);
    expect(editorOf(ben.container, x).textContent).toBe("x = 10");
  });

  it("gives a view on another socket its own copy, so text reaches it only through the server", async () => {
    const server = new LiveServer();
    const { x } = seed(server);
    const a = server.socket("ana");
    const b = server.socket("ben");
    const ana = mount(a);
    await pump(a);
    const ben = mount(b);
    await pump(a, b);
    expect(b.sentKinds()).toContain("hello");
    expect(editorOf(ben.container, x).textContent).toBe("x = 1");
    b.disconnect();
    act(() => viewOf(ana.container, x).dispatch({ changes: { from: 5, insert: "0" } }));
    await pump(a, b);
    expect(editorOf(ana.container, x).textContent).toBe("x = 10");
    expect(editorOf(ben.container, x).textContent).toBe("x = 1");
  });

  it("moves a cell's status from the engine's events on the notebook channel", async () => {
    const server = new LiveServer();
    const { y } = seed(server);
    const a = server.socket("ana");
    const view = mount(a);
    await pump(a);
    expect(a.held.has(`nb:${NODE}`)).toBe(true);
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "kernel.state", kernel_id: "k1", seq: 1, state: "busy" } });
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "cell.status", kernel_id: "k1", seq: 2, cell_id: y, status: "running" } });
    // Another notebook's events are not this one's.
    a.inbox.push({ t: "nb", channel: "nb:another", event: { type: "cell.status", kernel_id: "k1", seq: 3, cell_id: y, status: "error" } });
    await pump(a);
    const cell = view.container.querySelector<HTMLElement>(`[data-cell-id="${y}"]`)!;
    expect(within(cell).getByTestId("cell-status")).toHaveAttribute("aria-label", "Running");
  });

  it("asks for a run with the frontier this tab holds", async () => {
    const server = new LiveServer();
    const { x } = seed(server);
    const a = server.socket("ana");
    const view = mount(a);
    await pump(a);
    const cell = view.container.querySelector<HTMLElement>(`[data-cell-id="${x}"]`)!;
    await act(async () => {
      fireEvent.click(within(cell).getByRole("button", { name: "Run cell" }));
      await settle();
    });
    const run = requests.find((r) => r.url.endsWith(`/api/v1/notebooks/${DRIVE}/${NODE}/runs`));
    expect(run?.method).toBe("POST");
    expect(run?.body).toMatchObject({ target: { kind: "cells", ids: [x] }, confirm_expensive: false });
    expect(typeof (run?.body as { frontier: unknown }).frontier).toBe("string");
  });

  it("wakes the chat's machine when it opens and again on a run, never on a redraw", async () => {
    const server = new LiveServer();
    const { x } = seed(server);
    const a = server.socket("ana");
    const wake = vi.fn();
    const view = mount(a, wake);
    await pump(a);
    expect(wake, "opening the notebook asks once").toHaveBeenCalledTimes(1);
    // Events redraw the tab; none of them is an open or a run.
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "kernel.state", kernel_id: "k1", seq: 1, state: "idle" } });
    await pump(a);
    expect(wake).toHaveBeenCalledTimes(1);
    const cell = view.container.querySelector<HTMLElement>(`[data-cell-id="${x}"]`)!;
    await act(async () => {
      fireEvent.click(within(cell).getByRole("button", { name: "Run cell" }));
      await settle();
    });
    expect(wake, "the run asks again").toHaveBeenCalledTimes(2);
    expect(requests.some((r) => r.url.endsWith(`/api/v1/notebooks/${DRIVE}/${NODE}/runs`))).toBe(true);
  });

  it("says the machine is waking while the server wakes it, then runs once the box answers", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout"] });
    try {
      wakingRuns = 2;
      const server = new LiveServer();
      const { x } = seed(server);
      const a = server.socket("ana");
      const view = mount(a);
      await pump(a);
      expect(screen.queryByText("Waking the machine…")).toBeNull();
      const cell = view.container.querySelector<HTMLElement>(`[data-cell-id="${x}"]`)!;
      await act(async () => {
        fireEvent.click(within(cell).getByRole("button", { name: "Run cell" }));
        await settle();
      });
      const runs = () => requests.filter((r) => r.url.endsWith(`/api/v1/notebooks/${DRIVE}/${NODE}/runs`));
      expect(runs()).toHaveLength(1);
      expect(screen.getByText("Waking the machine…")).toBeInTheDocument();

      // Asked again after the wait the server named, still waking.
      await act(async () => {
        vi.advanceTimersByTime(3_000);
        await settle();
      });
      expect(runs()).toHaveLength(2);
      expect(screen.getByText("Waking the machine…")).toBeInTheDocument();

      // The box holds the folder now: the same run is taken, and the chip
      // goes back to the kernel's own word.
      await act(async () => {
        vi.advanceTimersByTime(3_000);
        await settle();
      });
      await waitFor(() => expect(runs()).toHaveLength(3));
      await waitFor(() => expect(screen.queryByText("Waking the machine…")).toBeNull());
      expect(screen.getByText("No kernel")).toBeInTheDocument();
      const ids = new Set(runs().map((r) => (r.body as { client_run_id: string }).client_run_id));
      expect(ids.size, "every ask is the one run").toBe(1);
      expect(screen.queryByText("The run could not be started.")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reads the environments once the machine the server is waking holds the notebook", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout"] });
    try {
      wakingEnvs = 1;
      const server = new LiveServer();
      seed(server);
      const a = server.socket("ana");
      mount(a);
      await pump(a);
      await waitFor(() => expect(screen.getByText("Waking the machine…")).toBeInTheDocument());
      await act(async () => {
        vi.advanceTimersByTime(3_000);
        await settle();
      });
      await waitFor(() => expect(screen.queryByText("Waking the machine…")).toBeNull());
      expect(requests.filter((r) => new URL(r.url, "http://app").pathname.endsWith("/envs"))).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  const GONE = "This workspace's machine is gone. Open the workspace to pick another.";
  const NO_CREDIT = "Not enough credit to run this machine for a minute; add credit and try again.";
  it.each([
    ["the workspace's machine is gone", 409, { code: "notebook.machine_unavailable", message: GONE }, GONE],
    ["the org's credit refused the machine's start", 402, { code: "insufficient_credit", message: NO_CREDIT }, NO_CREDIT],
    ["the server explained nothing", 500, {}, "The run could not be started."],
  ])("says why a run did not go through when %s", async (_case, status, body, said) => {
    kernelRefusal = { status, body };
    const server = new LiveServer();
    const { x } = seed(server);
    const a = server.socket("ana");
    const view = mount(a);
    await pump(a);
    await act(async () => {
      fireEvent.click(within(view.container.querySelector<HTMLElement>(`[data-cell-id="${x}"]`)!).getByRole("button", { name: "Run cell" }));
      await settle();
    });
    expect(screen.getByText(said)).toBeInTheDocument();
  });

  it("says why the kernel did not take a request when the server explained it", async () => {
    const NO_MACHINE = "No machine holds this notebook's folder.";
    viewBody = view({ state: "busy", kernel_id: "k1", seq: 1 });
    kernelRefusal = { status: 409, body: { code: "notebook.no_machine", message: NO_MACHINE } };
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Interrupt/ }));
      await settle();
    });
    expect(requests.some((r) => r.url.endsWith("/kernel") && r.method === "POST")).toBe(true);
    expect(screen.getByText(NO_MACHINE)).toBeInTheDocument();
    expect(screen.queryByText("The kernel did not take that request.")).toBeNull();
  });

  it("says the machine did not wake when it is still waking at the end of the wait", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout", "Date"] });
    try {
      wakingRuns = Number.POSITIVE_INFINITY;
      const server = new LiveServer();
      const { x } = seed(server);
      const a = server.socket("ana");
      const view = mount(a);
      await pump(a);
      await act(async () => {
        fireEvent.click(within(view.container.querySelector<HTMLElement>(`[data-cell-id="${x}"]`)!).getByRole("button", { name: "Run cell" }));
        await settle();
      });
      expect(screen.getByText("Waking the machine…")).toBeInTheDocument();
      // Three minutes of asking again every three seconds, and one ask more.
      for (let tick = 0; tick <= 61; tick += 1) {
        await act(async () => {
          vi.advanceTimersByTime(3_000);
          await settle();
        });
      }
      await waitFor(() => expect(screen.getByText("The machine did not wake. Try again.")).toBeInTheDocument());
      expect(screen.queryByText("Waking the machine…")).toBeNull();
      expect(screen.queryByText("The run could not be started.")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("says on the cell it ran and in the status area why a run did not go through, until one does", async () => {
    const server = new LiveServer();
    const { x, y } = seed(server);
    const a = server.socket("ana");
    const view = mount(a);
    await pump(a);
    const cellOf = (id: string) => view.container.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)!;
    await act(async () => {
      fireEvent.click(within(cellOf(x)).getByRole("button", { name: "Run cell" }));
      await settle();
    });
    // The server answered the request as run r1; the machine never did.
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "run.finished", kernel_id: null, seq: 1, run_id: "r1", status: "refused", reason: "machine_silent" } });
    await pump(a);
    expect(screen.getByTestId("run-problem")).toHaveTextContent("The machine did not answer. Try again.");
    expect(within(cellOf(x)).getByTestId("cell-problem")).toHaveTextContent("The machine did not answer. Try again.");
    expect(within(cellOf(y)).queryByTestId("cell-problem")).toBeNull();

    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "run.finished", kernel_id: null, seq: 2, run_id: "r2", status: "ok" } });
    await pump(a);
    expect(screen.queryByTestId("run-problem")).toBeNull();
    expect(within(cellOf(x)).queryByTestId("cell-problem")).toBeNull();
  });

  it("stops saying an interrupt is under way when the server did not take it", async () => {
    viewBody = view({ state: "busy", kernel_id: "k1", seq: 1 });
    kernelStatus = 503;
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Interrupt/ }));
      await settle();
    });
    expect(requests.some((r) => r.url.endsWith("/kernel") && r.method === "POST")).toBe(true);
    expect(screen.getByText("The kernel did not take that request.")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: /^Interrupt/ });
    expect(button).toHaveTextContent(/^Interrupt$/);
    expect(screen.queryByText("Interrupting")).toBeNull();
  });

  it("shows the view's kernel until the channel reports one", async () => {
    const server = new LiveServer();
    seed(server);
    viewBody = view({ state: "idle", kernel_id: "k1", seq: 3 });
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    expect(screen.getByTitle("Kernel")).toHaveTextContent("Idle");
  });

  it("does not let a view with no kernel hide the kernel the channel reported", async () => {
    const server = new LiveServer();
    seed(server);
    let open!: () => void;
    viewGate = new Promise((resolve) => (open = resolve));
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "kernel.state", kernel_id: "k1", seq: 1, state: "busy" } });
    await pump(a);
    expect(screen.getByTitle("Kernel")).toHaveTextContent("Busy");
    // The view was read before the kernel started: it says `absent`.
    open();
    await pump(a);
    expect(requests.some((r) => r.method === "GET" && r.url.endsWith(`/api/v1/notebooks/${DRIVE}/${NODE}`))).toBe(true);
    expect(screen.getByTitle("Kernel")).toHaveTextContent("Busy");
  });

  it("subscribes to the notebook channel again when the server resyncs it with no view", async () => {
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    expect(a.sent.filter((f) => f.t === "subscribe")).toEqual([]);
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "resync" } });
    await pump(a);
    expect(a.sent.filter((f) => f.t === "subscribe")).toEqual([{ t: "subscribe", channel: `nb:${NODE}` }]);
  });

  it("shows a run's variables when the numbers it is sent skip, and never subscribes again for a skip", async () => {
    // The server never sends a box's answer, a widget event addressed to
    // someone else's frame, and the tab takes widget traffic itself: the
    // numbers a tab sees rise with holes in them, and that loses nothing.
    const server = new LiveServer();
    const { x } = seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    const nb = (seq: number, type: string, body: Record<string, unknown> = {}) => ({
      t: "nb" as const,
      channel: `nb:${NODE}`,
      event: { type, kernel_id: "k1", seq, ...body },
    });
    a.inbox.push(
      nb(1, "kernel.state", { state: "idle" }),
      // 2 was the box's answer to the run request.
      nb(3, "run.queued", { run_id: "r1", targets: [x] }),
      nb(4, "comm.msg", { comm_id: "w1", content: { data: { method: "update", state: {} } } }),
      nb(5, "cell.started", { cell_id: x, run_id: "r1" }),
      // 6 was a widget update for another person's frame.
      nb(7, "cell.finished", { cell_id: x, run_id: "r1", status: "fresh" }),
      nb(8, "cell.variables", { cell_id: x, run_id: "r1", variables: [{ name: "x", type: "int", repr: "1", size_bytes: 28 }] }),
    );
    await pump(a);
    expect(a.sent.filter((f) => f.t === "subscribe")).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Panels" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Variables" }));
    const panel = screen.getByRole("region", { name: "Variables" });
    expect(within(panel).queryByText("No variables")).toBeNull();
    expect(within(panel).getByRole("button", { name: "x" })).toBeInTheDocument();
  });

  it("shows the environment the engine's listing names, with no kernel running", async () => {
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    fireEvent.click(screen.getByRole("button", { name: "Panels" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Environment" }));
    const panel = screen.getByRole("region", { name: "Environment" });
    expect(within(panel).queryByText("No environment")).toBeNull();
    expect(within(panel).getByText("3.12.13")).toBeInTheDocument();
    expect(within(panel).getByText("Ready")).toBeInTheDocument();
    expect(within(panel).getByText("polars")).toBeInTheDocument();
    expect(requests.some((r) => r.url.endsWith(`/envs/${encodeURIComponent(ENV.env_id)}/packages`))).toBe(true);
  });

  it.each([
    [false, true],
    [true, false],
    [undefined, false],
  ])("tells the person when environments aren't shared on the machine (listing shared=%s)", async (shared, said) => {
    envsBody = shared === undefined ? { current: ENV, envs: [ENV] } : { current: ENV, envs: [ENV], shared };
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    fireEvent.click(screen.getByRole("button", { name: "Panels" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Environment" }));
    const panel = screen.getByRole("region", { name: "Environment" });
    expect(within(panel).queryByText("Environments aren't shared on this machine") !== null).toBe(said);
  });

  it.each([
    [{ status: 409, body: { code: "notebook.no_machine", message: "No machine holds this notebook's workspace." } }, "Could not install six: No machine holds this notebook's workspace."],
    [{ status: 500, body: { detail: "boom" } }, "Could not install six."],
  ])("says why a refused install request failed, never the client's own diagnostic", async (reply, line) => {
    installReply = reply;
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    fireEvent.click(screen.getByRole("button", { name: "Panels" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Environment" }));
    const panel = screen.getByRole("region", { name: "Environment" });
    fireEvent.change(within(panel).getByRole("textbox", { name: "Packages to install" }), { target: { value: "six" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Install" }));
    await pump(a);
    await waitFor(() => expect(within(panel).getByTestId("install-status")).toHaveTextContent(line));
  });

  it("says an install is under way until its outcome arrives on the channel, then reads the packages again", async () => {
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    fireEvent.click(screen.getByRole("button", { name: "Panels" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Environment" }));
    const panel = screen.getByRole("region", { name: "Environment" });
    fireEvent.change(within(panel).getByRole("textbox", { name: "Packages to install" }), { target: { value: "six" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Install" }));
    await pump(a);
    expect(requests.find((r) => r.url.endsWith("/env/install"))?.body).toEqual({ packages: ["six"] });
    // Accepted is not installed.
    expect(within(panel).getByTestId("install-status")).toHaveTextContent("Installing six…");
    expect(within(panel).getByRole("button", { name: "Installing…" })).toBeDisabled();
    const packageReads = () => requests.filter((r) => r.url.includes("/packages")).length;
    const envReads = () => requests.filter((r) => r.url.endsWith("/envs")).length;
    const [packagesBefore, envsBefore] = [packageReads(), envReads()];
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "env.install", status: "error", packages: ["six"], message: "add failed (exit 1)" } });
    await pump(a);
    expect(within(panel).getByTestId("install-status")).toHaveTextContent("Could not install six: add failed (exit 1).");
    expect(within(panel).getByRole("button", { name: "Install" })).toBeInTheDocument();
    expect(screen.getAllByText("Could not install six: add failed (exit 1).").length).toBeGreaterThanOrEqual(2);
    expect(packageReads()).toBeGreaterThan(packagesBefore);
    expect(envReads()).toBeGreaterThan(envsBefore);
  });

  it("reads the environment again when a kernel starts on one, and shows that one as built", async () => {
    envsBody = { current: { ...ENV, state: "missing" }, envs: [{ ...ENV, state: "missing" }] };
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    fireEvent.click(screen.getByRole("button", { name: "Panels" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Environment" }));
    const panel = screen.getByRole("region", { name: "Environment" });
    expect(within(panel).getByText("Not built yet")).toBeInTheDocument();
    const envReads = () => requests.filter((r) => r.url.endsWith("/envs")).length;
    const before = envReads();
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "kernel.state", kernel_id: "k1", seq: 1, state: "idle", env_id: ENV.env_id } });
    await pump(a);
    expect(envReads()).toBeGreaterThan(before);
    expect(within(panel).queryByText("missing")).toBeNull();
    expect(within(panel).getByText("Ready")).toBeInTheDocument();
  });

  it("builds an environment from the panel, and says how the build went", async () => {
    envsBody = { current: { ...ENV, state: "missing", allowed_actions: ["build", "install", "remove"] }, envs: [] };
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    fireEvent.click(screen.getByRole("button", { name: "Panels" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Environment" }));
    const panel = screen.getByRole("region", { name: "Environment" });
    await act(async () => {
      fireEvent.click(within(panel).getByRole("button", { name: "Build" }));
      await settle();
    });
    const build = requests.find((r) => r.url.endsWith(`/api/v1/notebooks/${DRIVE}/${NODE}/env/build`));
    expect(build?.method).toBe("POST");
    expect(build?.body).toEqual({ packages: [] });
    expect(within(panel).getByText("Building the environment…")).toBeInTheDocument();
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "env.install", action: "build", status: "ok", packages: [] } });
    await pump(a);
    expect(within(panel).getByText("Built the environment.")).toBeInTheDocument();
  });

  it("keeps showing the running kernel's environment when the notebook names another, until a restart", async () => {
    const PROJECT = { ...ENV, env_id: "uv_project:.", kind: "uv_project", spec_root: "/w", recorded_in_file: false };
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "kernel.state", kernel_id: "k1", seq: 1, state: "idle", env_id: ENV.env_id } });
    await pump(a);
    // A pyproject.toml was added: the engine's listing now names the project.
    envsBody = { current: PROJECT, envs: [ENV, PROJECT] };
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "env.state", env: PROJECT } });
    await pump(a);
    expect(screen.getByRole("toolbar", { name: "Notebook" })).toHaveTextContent("Default");
    expect(screen.getByRole("toolbar", { name: "Notebook" })).not.toHaveTextContent("uv project");
    expect(screen.getByTestId("env-pending")).toHaveTextContent("The kernel uses Default until a restart moves it to uv project.");
  });

  it("pages and sorts a table output through the cell's table route", async () => {
    const server = new LiveServer();
    const { x } = seed(server);
    const a = server.socket("ana");
    const view = mount(a);
    await pump(a);
    const rows = Array.from({ length: 10 }, (_, i) => [`r${i}`, i]);
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "kernel.state", kernel_id: "k1", seq: 1, state: "idle" } });
    a.inbox.push({
      t: "nb",
      channel: `nb:${NODE}`,
      event: {
        type: "cell.output",
        kernel_id: "k1",
        seq: 2,
        cell_id: x,
        run_id: "r1",
        mode: "append",
        // As the kernel sends a polars frame bound to a global.
        output: {
          "application/vnd.alkera.table+json": {
            rows,
            schema: [{ name: "region", type: "str" }, { name: "units", type: "i64" }],
            total_rows: 25,
            source: { name: "df" },
          },
          "text/plain": "shape: (25, 2)",
        },
      },
    });
    await pump(a);
    const cell = view.container.querySelector<HTMLElement>(`[data-cell-id="${x}"]`)!;
    expect(within(cell).queryByText("This table could not be read.")).toBeNull();
    expect(within(cell).getByText("r3")).toBeInTheDocument();
    // The same page shape the output carried, further down the frame.
    tableBody = { schema: [{ name: "region", type: "str" }, { name: "units", type: "i64" }], rows: [["page2", 10]], total_rows: 25, offset: 10, limit: 10 };
    await act(async () => {
      fireEvent.click(within(cell).getByRole("button", { name: "Next" }));
      await settle();
    });
    await pump(a);
    expect(within(cell).getByText("page2")).toBeInTheDocument();
    const paged = new URL(requests.filter((r) => r.url.includes("/table")).at(-1)!.url, "http://app");
    expect(paged.pathname).toBe(`/api/v1/notebooks/${DRIVE}/${NODE}/cells/${x}/table`);
    expect(Object.fromEntries(paged.searchParams)).toEqual({ offset: "10", limit: "10" });
    await act(async () => {
      fireEvent.click(within(cell).getByRole("button", { name: /units/ }));
      await settle();
    });
    await pump(a);
    const sorted = new URL(requests.filter((r) => r.url.includes("/table")).at(-1)!.url, "http://app");
    expect(sorted.searchParams.get("sort")).toBe("units:asc");
    expect(sorted.searchParams.get("offset")).toBe("0");
  });

  it("draws the notebook and its outputs in the app's palette, and moves with it", async () => {
    const server = new LiveServer();
    const { x } = seed(server);
    const a = server.socket("ana");
    viewBody = { ...view(), output_frame_url: "https://content.test/c/nb-output/0123456789abcdef" } as NotebookView;
    const tab = mount(a);
    await pump(a);
    a.inbox.push({ t: "nb", channel: `nb:${NODE}`, event: { type: "kernel.state", kernel_id: "k1", seq: 1, state: "idle" } });
    a.inbox.push({
      t: "nb",
      channel: `nb:${NODE}`,
      event: { type: "cell.output", kernel_id: "k1", seq: 2, cell_id: x, run_id: "r1", mode: "append", output: { "text/html": "<b>report</b>", "text/plain": "report" } },
    });
    await pump(a);
    const editor = within(tab.container).getByTestId("notebook-editor");
    const cell = tab.container.querySelector<HTMLElement>(`[data-cell-id="${x}"]`)!;
    const themes = () => [...cell.querySelectorAll<HTMLElement>("[data-nb-theme]")].map((el) => el.dataset.nbTheme);
    // The framed output sits under an element carrying the theme it was given.
    const frame = cell.querySelector("iframe")!;
    expect(frame.closest("[data-nb-theme]")).not.toBeNull();

    // The app is dark unless something declares light.
    expect(editor.dataset.nbTheme).toBe("dark");
    expect(new Set(themes())).toEqual(new Set(["dark"]));

    await act(async () => {
      document.documentElement.setAttribute("data-alkera-color-scheme", "light");
      await settle();
    });
    expect(editor.dataset.nbTheme).toBe("light");
    expect(new Set(themes())).toEqual(new Set(["light"]));
    // The same frame, told of the change, not a rebuilt one.
    expect(cell.querySelector("iframe")).toBe(frame);

    await act(async () => {
      document.documentElement.removeAttribute("data-alkera-color-scheme");
      await settle();
    });
    expect(editor.dataset.nbTheme).toBe("dark");

    // The editor host's own light class counts too.
    await act(async () => {
      document.body.classList.add("vscode-light");
      await settle();
    });
    expect(editor.dataset.nbTheme).toBe("light");
  });

  it("names an agent in a cell by the person it works for", async () => {
    const server = new LiveServer();
    const { x } = seed(server);
    viewBody = {
      ...view(),
      presence: [{ who: "agent:a1", cell_id: x, kind: "agent", display_name: "Analyst", acting_for: { id: "u1", display_name: "Ada" } } as NonNullable<NotebookView["presence"]>[number]],
    };
    const a = server.socket("ana");
    const tab = mount(a);
    await pump(a);
    const cell = tab.container.querySelector<HTMLElement>(`[data-cell-id="${x}"]`)!;
    expect(within(cell).getByTitle("Agent for Ada")).toBeInTheDocument();
    expect(tab.container.textContent).not.toContain("agent:a1");
  });

  it("shows an agent in several cells once, and lets it go from each cell when its claim lapses", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, toFake: ["setTimeout", "clearTimeout", "Date"] });
    try {
      const server = new LiveServer();
      const { x, y } = seed(server);
      const agentIn = (cell: string, expiresIn: number) =>
        ({ who: "Analyst", cell_id: cell, kind: "agent", actor_id: "agent:chat-1", caret: false, expires_in: expiresIn, acting_for: { id: "u1", display_name: "Ada" } }) as NonNullable<NotebookView["presence"]>[number];
      viewBody = { ...view(), presence: [agentIn(x, 15), agentIn(y, 10)] };
      const a = server.socket("ana");
      const tab = mount(a);
      await pump(a);
      const badge = (cell: string) => tab.container.querySelector(`[data-cell-id="${cell}"] .nb-cell__presence`);
      const faces = () => tab.container.querySelectorAll(".nb-toolbar__presence .nb-face").length;
      expect([badge(x) !== null, badge(y) !== null, faces()]).toEqual([true, true, 1]);

      await act(async () => {
        vi.advanceTimersByTime(10_500);
      });
      expect([badge(x) !== null, badge(y) !== null, faces()]).toEqual([true, false, 1]);

      await act(async () => {
        vi.advanceTimersByTime(5_000);
      });
      expect([badge(x) !== null, badge(y) !== null, faces()]).toEqual([false, false, 0]);
    } finally {
      vi.useRealTimers();
    }
  });

  describe("a SQL cell's connection", () => {
    const WAREHOUSE = {
      id: "c1",
      name: "warehouse",
      engine: "postgres",
      engine_title: "PostgreSQL",
      kind: "team",
      team_name: "Acme",
      credential_owner: "Ada Lovelace",
      can_use: true,
      reason: "",
    };
    const LAKE = { ...WAREHOUSE, id: "c2", name: "lake", engine: "snowflake", engine_title: "Snowflake", can_use: false, reason: "This connection is turned off." };

    function seedSql(server: LiveServer, meta: Record<string, unknown> = {}): string {
      const { doc } = server.docOf(CHANNEL);
      const nb = new NotebookDocument({
        loro: loroNode,
        source: { doc, canWrite: true, listen: () => () => {}, localCommitted: () => {} },
        newId: () => "sssssssss0",
      });
      return nb.apply([{ op: "insert", kind: "sql", source: "SELECT 1", meta: { output_var: "_df", ...meta } }]).created[0]!;
    }

    const metaOf = (server: LiveServer, cellId: string) => {
      const cells = server.docOf(CHANNEL).doc.getMap("cells").toJSON() as Record<string, { meta: Record<string, unknown> }>;
      return cells[cellId]!.meta;
    };

    it("offers DuckDB first, then the workspace's, with an unusable one disabled and its reason", async () => {
      connectionsBody = { connections: [WAREHOUSE, LAKE] };
      const server = new LiveServer();
      const id = seedSql(server);
      const a = server.socket("ana");
      const tab = mount(a);
      await pump(a);
      const cell = tab.container.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)!;
      const trigger = await within(cell).findByRole("button", { name: "Connection" });
      expect(trigger).toHaveTextContent("DuckDB");
      fireEvent.click(trigger);
      const options = await screen.findAllByRole("option");
      expect(options.map((o) => o.textContent)).toEqual([
        "DuckDB",
        "warehouse · PostgreSQL · Ada Lovelace",
        "lake · Snowflake · Ada Lovelace · This connection is turned off.",
      ]);
      expect(options[2]).toHaveAttribute("aria-disabled", "true");
      expect(requests.some((r) => r.url.endsWith(`/api/v1/notebooks/${DRIVE}/${NODE}/connections`))).toBe(true);
    });

    it("writes a choice to the live document, where the other tab sees it", async () => {
      connectionsBody = { connections: [WAREHOUSE] };
      const server = new LiveServer();
      const id = seedSql(server);
      const a = server.socket("ana");
      const b = server.socket("ben");
      const ana = mount(a);
      await pump(a);
      const ben = mount(b);
      await pump(a, b);
      const anaCell = ana.container.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)!;
      fireEvent.click(await within(anaCell).findByRole("button", { name: "Connection" }));
      fireEvent.mouseDown(await screen.findByRole("option", { name: "warehouse · PostgreSQL · Ada Lovelace" }));
      await pump(a, b);
      expect(metaOf(server, id).connection).toBe("warehouse");
      const benCell = ben.container.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)!;
      // Closed, the control names the connection alone; the details are in the open list.
      expect(within(benCell).getByRole("button", { name: "Connection" })).toHaveTextContent(/^warehouse$/);
      // The selected connection says whose credentials it runs on; DuckDB runs on nobody's.
      expect(within(benCell).getByTitle("Runs on Ada Lovelace's credentials")).toContainElement(
        within(benCell).getByRole("button", { name: "Connection" }),
      );
      expect(within(anaCell).queryByTitle(/Runs on/)).not.toBeNull();

      // Back to DuckDB: the cell names no connection again.
      fireEvent.click(within(benCell).getByRole("button", { name: "Connection" }));
      fireEvent.mouseDown(await screen.findByRole("option", { name: "DuckDB" }));
      await pump(a, b);
      expect(metaOf(server, id).connection).toBeUndefined();
      expect(within(benCell).queryByTitle(/Runs on/)).toBeNull();
    });

    it("shows a connection the workspace no longer has as missing", async () => {
      connectionsBody = { connections: [WAREHOUSE] };
      const server = new LiveServer();
      const id = seedSql(server, { connection: "gone" });
      const a = server.socket("ana");
      const tab = mount(a);
      await pump(a);
      const cell = tab.container.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)!;
      expect(await within(cell).findByRole("button", { name: "Connection" })).toHaveTextContent("gone · Missing");
      // A connection the workspace does not have runs on nobody's credentials.
      expect(within(cell).queryByTitle(/Runs on/)).toBeNull();
      expect(within(cell).getByRole("status")).toHaveTextContent("Not in this workspace. Running this cell fails.");
    });

    it("says why a cell's connection cannot run when the server says it cannot", async () => {
      connectionsBody = { connections: [WAREHOUSE, LAKE] };
      const server = new LiveServer();
      const id = seedSql(server, { connection: "lake" });
      const a = server.socket("ana");
      const tab = mount(a);
      await pump(a);
      const cell = tab.container.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)!;
      expect(await within(cell).findByRole("button", { name: "Connection" })).toHaveTextContent(/^lake$/);
      expect(within(cell).getByRole("status")).toHaveTextContent("This connection is turned off.");
    });

    it("gives a reader the connection's name and no control", async () => {
      connectionsBody = { connections: [WAREHOUSE] };
      const server = new LiveServer();
      const id = seedSql(server, { connection: "warehouse" });
      server.readers.add("rita");
      const r = server.socket("rita");
      const tab = mount(r);
      await pump(r);
      const cell = tab.container.querySelector<HTMLElement>(`[data-cell-id="${id}"]`)!;
      await waitFor(() => expect(within(cell).getByTitle("Runs on Ada Lovelace's credentials")).toHaveTextContent("warehouse"));
      expect(within(cell).queryByTitle("Connection")).toBeNull();
      expect(within(cell).queryByRole("button", { name: "Connection" })).toBeNull();
      expect(within(cell).queryByRole("textbox", { name: "Result name" })).toBeNull();
    });
  });

  it("gives a reader the notebook and none of its controls", async () => {
    const server = new LiveServer();
    seed(server);
    server.readers.add("rita");
    const r = server.socket("rita");
    mount(r);
    await pump(r);
    expect(screen.getAllByRole("listitem").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Run cell" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Run all" })).toBeNull();
  });

  it("says when the server would not keep a new cell, and offers its text to copy", async () => {
    const server = new LiveServer();
    seed(server);
    const a = server.socket("ana");
    mount(a);
    await pump(a);
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    const { notebook, release } = acquireLiveNotebook(NODE, { socket: a, account: null });
    const state = notebook.state;
    if (state.kind !== "live") throw new Error("not live");
    server.failNext = { code: "crdt_rejected" };
    act(() => void state.doc.apply([{ op: "insert", kind: "sql", source: "SELECT 1 AS one" }]));
    await pump(a);
    release();
    expect(await screen.findByText("Your last edit could not be shared.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy my text" }));
    expect(writeText).toHaveBeenCalledWith("SELECT 1 AS one");
  });
});

const fastTimers: Timers = {
  setTimeout: (fn, ms) => (ms >= 1000 ? null : globalThis.setTimeout(fn, 0)),
  clearTimeout: (h) => {
    if (h !== null) globalThis.clearTimeout(h as ReturnType<typeof setTimeout>);
  },
};

describe("notebook carets", () => {
  async function open(server: LiveServer, name: string) {
    const socket = server.socket(name);
    const channel = new LiveDocChannel({
      socket,
      loadLoro: () => Promise.resolve(loroNode),
      docType: "notebook",
      docId: NODE,
      timers: fastTimers,
      budget: new SendBudget(() => null),
      storage: null,
      pageEvents: null,
      account: null,
    });
    channel.start();
    await pump(socket);
    const doc = new NotebookDocument({ loro: loroNode, source: channel });
    const carets = new NotebookCarets(channel, loroNode, () => 120, fastTimers);
    return { socket, channel, doc, carets };
  }

  function editor(tab: Awaited<ReturnType<typeof open>>, cellId: string, focused: boolean) {
    const binding = new LoroCodeMirrorBinding({
      channel: tab.channel,
      loro: loroNode,
      hueOf: () => 120,
      carets: liveCarets,
      text: (d) => tab.doc.sourceText(d, cellId),
      history: tab.doc.sharedHistory,
      sharedCarets: true,
      onSelection: () => tab.carets.publish(),
      timers: fastTimers,
    });
    tab.carets.attach(cellId, binding);
    const parent = document.createElement("div");
    document.body.appendChild(parent);
    const view = new EditorView({ state: binding.createState(), parent });
    Object.defineProperty(view, "hasFocus", { get: () => focused, configurable: true });
    return { binding, view, parent };
  }

  it("publishes one caret per person and draws it only in the cell it stands in", async () => {
    const server = new LiveServer();
    const { x, y } = seed(server);
    const ana = await open(server, "ana");
    const ben = await open(server, "ben");
    editor(ana, x, false);
    const anaY = editor(ana, y, true);
    const benX = editor(ben, x, false);
    const benY = editor(ben, y, false);
    anaY.view.dispatch({ selection: EditorSelection.cursor(3) });
    await pump(ana.socket, ben.socket);
    expect(ben.carets.presence().map((p) => [p.cellId, p.stamp.displayName])).toEqual([[y, "ANA"]]);
    expect(benY.parent.querySelector(".alk-cm-caret__name")?.textContent).toBe("ANA");
    expect(benX.parent.querySelector(".alk-cm-caret")).toBeNull();
  });

  it("names the cell in the caret it publishes, so a tab with no editor on that cell still places the person", async () => {
    const server = new LiveServer();
    const { x, y } = seed(server);
    const ana = await open(server, "ana");
    const ben = await open(server, "ben");
    const anaY = editor(ana, y, true);
    // Ben has only cell x open: a cursor in y's text resolves in none of his editors.
    editor(ben, x, false);
    anaY.view.dispatch({ selection: EditorSelection.cursor(2) });
    await pump(ana.socket, ben.socket);
    expect(ben.carets.presence().map((p) => p.cellId)).toEqual([y]);
  });

  it("takes the person out of the cell the moment their editor loses focus", async () => {
    const server = new LiveServer();
    const { y } = seed(server);
    const ana = await open(server, "ana");
    const ben = await open(server, "ben");
    const anaY = editor(ana, y, true);
    editor(ben, y, false);
    anaY.view.dispatch({ selection: EditorSelection.cursor(2) });
    await pump(ana.socket, ben.socket);
    expect(ben.carets.presence().map((p) => p.cellId)).toEqual([y]);
    // Ana clicks out of the cell: the editor's next update finds it unfocused.
    Object.defineProperty(anaY.view, "hasFocus", { get: () => false });
    anaY.view.dispatch({ selection: EditorSelection.cursor(1) });
    await pump(ana.socket, ben.socket);
    expect(ben.carets.presence()).toEqual([]);
  });

  it("takes the person out of the cell when their tab closes the notebook", async () => {
    const server = new LiveServer();
    const { y } = seed(server);
    const ana = await open(server, "ana");
    const ben = await open(server, "ben");
    const anaY = editor(ana, y, true);
    editor(ben, y, false);
    anaY.view.dispatch({ selection: EditorSelection.cursor(2) });
    await pump(ana.socket, ben.socket);
    expect(ben.carets.presence().map((p) => p.cellId)).toEqual([y]);
    ana.carets.dispose();
    await pump(ana.socket, ben.socket);
    expect(ben.carets.presence()).toEqual([]);
  });
});

describe("a file the server will not open as a notebook", () => {
  const CONTENT = "http://files.localhost:8000";
  const TEXT = "import os\nprint(os.getcwd())\n";

  function mountFile(socket: FakeSocket) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
        const url = input instanceof Request ? input.url : String(input);
        const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
        if (url.startsWith(CONTENT)) return new Response(TEXT, { status: 200, headers: { "Content-Type": "text/x-python" } });
        if (url.includes("/content-grants")) {
          const kind = (JSON.parse(String(init.body ?? "{}")) as { kind?: string }).kind ?? "file";
          return json({ url: `${CONTENT}/c/single`, expiresAt: new Date(Date.now() + 600_000).toISOString(), kind, etag: "e1" });
        }
        return json(viewBody);
      }),
    );
    const ctx: WorkspaceCtx = { chatId: "chat-1", driveId: DRIVE, rootNodeId: "root", liveSocket: socket };
    const tab: WorkspaceTab = { id: "t1", kind: "file", node_id: NODE, name: "analysis.alknb.py", view: "notebook" };
    const item = {
      id: NODE,
      driveId: DRIVE,
      kind: "file",
      name: "analysis.alknb.py",
      nameDisplay: "analysis.alknb.py",
      parentId: "root",
      pathBytes: "/Chats/c.alkerachat/analysis.alknb.py",
      etag: "e1",
      ctag: "c1",
      attrs: { mtime: "2026-09-01T10:00:00Z" },
      file: { mime_type: "text/x-python", size: TEXT.length, content_hash: "h", scan_state: "clean" },
      lease: null,
      live: null,
      capabilities: { can_read: true, can_write: true, can_download: true },
      trashed: false,
    } as unknown as Item;
    // The notebook is held live only once the reader is known.
    const client = createQueryClient({ retry: false });
    client.setQueryData(meKey, ME);
    return render(
      <QueryClientProvider client={client}>
        <NotebookTab tab={tab} ctx={ctx} item={item} onEdit={() => {}} loadLoro={() => Promise.resolve(loroNode)} />
      </QueryClientProvider>,
    );
  }

  it.each([
    ["not_a_notebook", "This file isn't a notebook, so it's shown read only. Fix it in Source to open it as one."],
    ["unreadable", "This notebook can't be read, so it's shown read only. Fix it in Source to open it again."],
    ["newer_format", "This notebook was saved by a newer version, so it's shown read only."],
    [null, "This notebook can't be opened live here. Its source is shown read only."],
  ])("(%s) says why, and shows the file's text read only", async (reason, sentence) => {
    const server = new LiveServer();
    server.refuseHellosWith = "not_editable";
    server.refuseHellosReason = reason;
    const socket = server.socket("ana");
    const { container } = mountFile(socket);
    await pump(socket);
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(sentence));
    await waitFor(() => expect(container.querySelector(".alk-nb-tab__source")?.textContent).toContain("print(os.getcwd())"));
    expect(container.querySelector(".nb-notebook")).toBeNull();
    expect(container.querySelector(".cm-content[contenteditable='true']")).toBeNull();
  });
});
