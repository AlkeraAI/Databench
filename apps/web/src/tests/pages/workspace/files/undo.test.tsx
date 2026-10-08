import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { UndoToast } from "@/pages/workspace/files/UndoToast";
import {
  EMPTY_UNDO_STATE,
  UNDO_DEPTH,
  undoReducer,
  undoShortcutFor,
  useUndoStack,
  type UndoController,
  type UndoEntry,
} from "@/pages/workspace/files/undo";

// The stack is asserted through the request the undo actually sends — which operation
// id the server was asked to invert — because that, not any client-side bookkeeping, is
// what makes an undo correct.

const entry = (operationId: string, extra: Partial<UndoEntry> = {}): UndoEntry => ({
  operationId,
  driveId: "dr_1",
  kind: "trash",
  label: "Moved 3 items to trash",
  ...extra,
});

/** Every `POST …/undo` the page made, newest last. */
/** The `If-Match` each stubbed undo carried, in order — reset by every `stubUndo`. */
let undoIfMatch: (string | null)[] = [];

function stubUndo(nextIds: string[]) {
  const undone: string[] = [];
  undoIfMatch = [];
  let index = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const req = input instanceof Request ? input : new Request(String(input), init);
      const url = req.url;
      const match = /\/operations\/([^/]+)\/undo$/.exec(url);
      if (!match) throw new Error(`unexpected request: ${url}`);
      undone.push(match[1]);
      undoIfMatch.push(req.headers.get("if-match"));
      const id = nextIds[index] ?? `op_undo_${index}`;
      index += 1;
      return new Response(JSON.stringify({ id, driveId: "dr_1", kind: "undo", state: "done" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return undone;
}

let controller: UndoController | null = null;

function Harness({ timeoutMs = 0 }: { timeoutMs?: number }) {
  const stack = useUndoStack();
  controller = stack;
  return <UndoToast controller={stack} timeoutMs={timeoutMs} />;
}

function renderStack(timeoutMs = 0) {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Harness timeoutMs={timeoutMs} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  controller = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("undoReducer", () => {
  it("pushes onto the past and abandons the redo branch", () => {
    const undone = undoReducer({ past: [entry("a")], future: [entry("f")] }, {
      type: "push",
      entry: entry("b"),
    });
    expect(undone.past.map((e) => e.operationId)).toEqual(["a", "b"]);
    expect(undone.future).toEqual([]);
  });

  it("moves the top of the past onto the future when it is undone", () => {
    const state = undoReducer({ past: [entry("a"), entry("b")], future: [] }, {
      type: "undone",
      entry: entry("inv_b"),
    });
    expect(state.past.map((e) => e.operationId)).toEqual(["a"]);
    expect(state.future.map((e) => e.operationId)).toEqual(["inv_b"]);
  });

  it("does nothing on an undo with an empty past, and on a redo with an empty future", () => {
    expect(undoReducer(EMPTY_UNDO_STATE, { type: "undone", entry: entry("x") })).toBe(
      EMPTY_UNDO_STATE,
    );
    expect(undoReducer(EMPTY_UNDO_STATE, { type: "redone", entry: entry("x") })).toBe(
      EMPTY_UNDO_STATE,
    );
  });

  it("keeps only the newest UNDO_DEPTH steps", () => {
    let state = EMPTY_UNDO_STATE;
    for (let i = 0; i < UNDO_DEPTH + 5; i += 1) {
      state = undoReducer(state, { type: "push", entry: entry(`op_${i}`) });
    }
    expect(state.past).toHaveLength(UNDO_DEPTH);
    expect(state.past[0].operationId).toBe("op_5");
  });

  it("clears both branches", () => {
    expect(
      undoReducer({ past: [entry("a")], future: [entry("b")] }, { type: "clear" }),
    ).toEqual(EMPTY_UNDO_STATE);
  });
});

describe("undoShortcutFor", () => {
  const event = (over: Partial<KeyboardEvent>) => ({
    key: "z",
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    ...over,
  });

  it.each([
    ["Cmd+Z", { metaKey: true }, "undo"],
    ["Ctrl+Z", { ctrlKey: true }, "undo"],
    ["Cmd+Shift+Z", { metaKey: true, shiftKey: true }, "redo"],
    ["Ctrl+Shift+Z", { ctrlKey: true, shiftKey: true }, "redo"],
    ["Ctrl+Y", { ctrlKey: true, key: "y" }, "redo"],
  ] as const)("%s is %s", (_name, over, expected) => {
    expect(undoShortcutFor(event(over))).toBe(expected);
  });

  it.each([
    ["bare Z", { key: "z" }],
    ["Cmd+Y (not a redo on macOS)", { metaKey: true, key: "y" }],
    ["Cmd+Alt+Z", { metaKey: true, altKey: true }],
    ["Cmd+X", { metaKey: true, key: "x" }],
  ] as const)("%s is neither", (_name, over) => {
    expect(undoShortcutFor(event(over))).toBeNull();
  });
});

describe("the undo toast and Cmd+Z", () => {
  it("undoes the operation the mutation returned, from the toast's button", async () => {
    const undone = stubUndo(["op_inverse"]);
    renderStack();
    act(() => controller?.push(entry("op_trash_1")));

    expect(await screen.findByText("Moved 3 items to trash")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));

    await waitFor(() => expect(undone).toEqual(["op_trash_1"]));
  });

  it("names the version the write acted on, so the server does not refuse the undo", async () => {
    // Every Files mutation must carry If-Match; an undo that carried none was refused
    // with a 428 the toast showed as "someone changed this".
    const undone = stubUndo(["op_inverse"]);
    renderStack();
    act(() => controller?.push(entry("op_trash_1", { etag: "et_acted" })));

    await userEvent.click(await screen.findByRole("button", { name: "Undo" }));

    await waitFor(() => expect(undone).toEqual(["op_trash_1"]));
    expect(undoIfMatch).toEqual(["et_acted"]);
  });

  it("still undoes with Cmd+Z after the toast has been dismissed", async () => {
    const undone = stubUndo(["op_inverse"]);
    renderStack();
    act(() => controller?.push(entry("op_trash_2")));

    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Moved 3 items to trash")).not.toBeInTheDocument();

    await userEvent.keyboard("{Meta>}z{/Meta}");
    await waitFor(() => expect(undone).toEqual(["op_trash_2"]));
    expect(controller?.canUndo).toBe(false);
  });

  it("redoes by inverting the operation the undo produced, not the original", async () => {
    const undone = stubUndo(["op_inverse", "op_redo"]);
    renderStack();
    act(() => controller?.push(entry("op_move_1", { kind: "move", label: "Moved 3 items" })));

    await userEvent.keyboard("{Meta>}z{/Meta}");
    await waitFor(() => expect(controller?.canRedo).toBe(true));

    await userEvent.keyboard("{Meta>}{Shift>}z{/Shift}{/Meta}");
    await waitFor(() => expect(undone).toEqual(["op_move_1", "op_inverse"]));
    expect(controller?.canUndo).toBe(true);
    expect(controller?.canRedo).toBe(false);
  });

  it("does nothing on Cmd+Z with an empty stack", async () => {
    const undone = stubUndo([]);
    renderStack();
    await userEvent.keyboard("{Meta>}z{/Meta}");
    expect(undone).toEqual([]);
  });

  it("leaves the step on the stack and shows the refusal when the undo is refused", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify({ error: { code: "files.held", message: "held" } }), {
            status: 409,
            headers: { "content-type": "application/json" },
          }),
      ),
    );
    renderStack();
    act(() => controller?.push(entry("op_trash_3")));

    await userEvent.click(screen.getByRole("button", { name: "Undo" }));

    expect(await screen.findByText("This is on legal hold.")).toBeInTheDocument();
    expect(controller?.canUndo).toBe(true);
  });

  it("re-opens for a new step even though the previous toast was dismissed", async () => {
    stubUndo([]);
    renderStack();
    act(() => controller?.push(entry("op_a", { label: "Moved 1 item to trash" })));
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    act(() => controller?.push(entry("op_b", { label: "Renamed report.csv" })));
    expect(await screen.findByText("Renamed report.csv")).toBeInTheDocument();
  });
});

describe("one undo per press", () => {
  // The history only moves once the server has accepted the inverse, so every
  // keystroke that arrives before then reads the same top of the stack. Left
  // alone, a held Cmd+Z sends the same undo dozens of times: one succeeds and
  // the rest come back 412, as error toasts nobody asked for.
  const press = () =>
    act(() => {
      fireEvent.keyDown(window, { key: "z", metaKey: true });
    });

  it("undoes once for a held Cmd+Z, not once per auto-repeat", async () => {
    const undone = stubUndo(["op_inverse"]);
    renderStack();
    act(() => controller?.push(entry("op_trash_1")));

    // The whole burst before anything can settle, which is what holding the key
    // produces: a repeat every ~30ms against a round trip an order slower.
    act(() => {
      fireEvent.keyDown(window, { key: "z", metaKey: true });
      for (let i = 0; i < 4; i += 1) {
        fireEvent.keyDown(window, { key: "z", metaKey: true, repeat: true });
      }
    });

    await waitFor(() => expect(controller?.canUndo).toBe(false));
    expect(undone).toEqual(["op_trash_1"]);
  });

  it("ignores a fresh Cmd+Z while the inverse is still in flight", async () => {
    // Not every repeat arrives flagged — a key mashed by hand, or a host that
    // re-sends the keystroke, lands as a run of first presses.
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const undone: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const req = input instanceof Request ? input : new Request(String(input), init);
        const match = /\/operations\/([^/]+)\/undo$/.exec(req.url);
        if (!match) throw new Error(`unexpected request: ${req.url}`);
        undone.push(match[1]);
        await gate;
        return new Response(
          JSON.stringify({ id: "op_inverse", driveId: "dr_1", kind: "undo", state: "done" }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }),
    );
    renderStack();
    act(() => controller?.push(entry("op_trash_1")));

    for (let i = 0; i < 5; i += 1) await press();
    await waitFor(() => expect(undone).toHaveLength(1));

    release();
    await waitFor(() => expect(controller?.canRedo).toBe(true));
    expect(undone).toEqual(["op_trash_1"]);
  });

  it("undoes the next step on a second press once the first has landed", async () => {
    const undone = stubUndo(["op_inv_b", "op_inv_a"]);
    renderStack();
    act(() => controller?.push(entry("op_a")));
    act(() => controller?.push(entry("op_b")));

    await press();
    await waitFor(() => expect(controller?.canRedo).toBe(true));

    await press();
    await waitFor(() => expect(undone).toEqual(["op_b", "op_a"]));
  });
});
