import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { QueryClientProvider } from "@tanstack/react-query";
import { render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import {
  DEFAULT_WEB_LIMITS,
  HISTORY_RETRY,
  LimitsProvider,
  RETRY_BACKOFF,
  RETRY_BACKOFF_CAP_MS,
  RETRY_BACKOFF_FLOOR_MS,
  retryLadder,
  SESSION_RETRY,
  useLimits,
  type RetryLadder,
} from "@/lib/limits";
import { UndoToast } from "@/pages/workspace/files/UndoToast";
import { useSoftThreshold } from "@/pages/workspace/files/useSoftThreshold";
import { useSearchQuery } from "@/pages/workspace/files/useSearchQuery";
import type { UndoController } from "@/pages/workspace/files/undo";

// `lib/limits.ts` is the one place the web client writes a number it picked for
// itself down. Two things have to stay true for that to mean anything: the files
// it was lifted out of must not grow a replacement literal, and the call sites
// must genuinely READ it — a constant nothing consumes is a comment. The first
// half is the scan; the second half changes a limit and watches the behaviour
// move with it.

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(HERE, "../..");

/** The files this module emptied of bare numbers. A number that reappears in one
 *  of them is a limit nobody can find, compare or change again. */
const DE_MAGICKED = [
  "api/events/sseClient.ts",
  "api/realtime/wsClient.ts",
  "api/realtime/client.ts",
  "api/realtime/docSync.ts",
  "api/realtime/presence.ts",
  "api/files.ts",
  "api/chats.ts",
  "api/gate.ts",
  "api/queryClient.ts",
  "pages/workspace/files/dragDrop.ts",
  "pages/workspace/files/undo.ts",
  "pages/workspace/files/UndoToast.tsx",
  "pages/workspace/files/useSearchQuery.ts",
  "pages/workspace/files/useSoftThreshold.ts",
  "pages/workspace/files/preview/folderResolver.ts",
  "pages/workspace/files/preview/usePreviewContent.ts",
];

/** Numbers that are not limits and never were:
 *  - 0, 1, -1: an empty count, a single step, a not-found index;
 *  - 2: a halving or a pair, which no deployment tunes;
 *  - 100: a percentage, which is a unit rather than a choice;
 *  - 1000: milliseconds in a second, and the WebSocket normal-close code — a
 *    unit and a wire constant, neither of them a choice anyone makes;
 *  - an HTTP status (3 digits, 1xx–5xx): the wire's vocabulary, not ours;
 *  - a WebSocket application close code (4xxx): the same.
 *  Everything else — a cadence, a ceiling, a page size, a retry count — is a
 *  limit, and belongs in `lib/limits.ts`. */
function isAllowed(literal: string): boolean {
  const n = Number(literal.replace(/_/g, ""));
  if ([0, 1, 2, 100, 1000].includes(n)) return true;
  if (n >= 100 && n <= 599 && literal.length === 3) return true;
  if (n >= 4000 && n <= 4999 && literal.length === 4) return true;
  return false;
}

/** The operations whose numeric arguments are positions in a string or an array
 *  rather than limits: a `slice` bound, a radix, a pad width. */
const INDEXING = /\.(slice|substring|substr|charAt|codePointAt|padStart|padEnd|repeat|toFixed|toString)\([^)]*\)/g;

/** Source with comments, string/template literals and indexing arguments removed,
 *  so a number quoted in prose, spelled inside a path, or naming a character
 *  position is never mistaken for a limit at a call site. */
function code(text: string): string {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1 ")
    .replace(/`(?:[^`\\]|\\.)*`/g, '""')
    .replace(/'(?:[^'\\\n]|\\.)*'/g, '""')
    .replace(/"(?:[^"\\\n]|\\.)*"/g, '""')
    .replace(INDEXING, ".at()");
}

function bareNumbers(text: string): string[] {
  const found = code(text).match(/(?<![\w.$])\d[\d_]*(?![\w.])/g) ?? [];
  return found.filter((literal) => !isAllowed(literal));
}

describe("the de-magicked files keep no numbers of their own", () => {
  it.each(DE_MAGICKED)("%s", (relPath) => {
    const source = readFileSync(join(SRC, relPath), "utf8");
    expect(bareNumbers(source)).toEqual([]);
  });

  it("every retry ladder in the module is the one shape", () => {
    // Several prefixes were invented for the same three numbers before this was one shape, by two
    // people who never met in the file. The guard is the shape itself: a ladder's attempts default
    // to the rungs that reach its cap, so a cap can never describe a wait nothing arms.
    const rungs = (ladder: RetryLadder): number[] =>
      Array.from({ length: ladder.attempts - 1 }, (_, n) =>
        Math.min(ladder.floorMs * 2 ** n, ladder.capMs),
      );

    expect(rungs(HISTORY_RETRY)).toContain(HISTORY_RETRY.capMs);
    expect(Math.max(...rungs(HISTORY_RETRY))).toBe(HISTORY_RETRY.capMs);

    // The portal's backoff and the transcript's were the same three numbers written twice. They
    // are one ladder now, so moving the floor moves both rather than one of them.
    expect(HISTORY_RETRY).toBe(RETRY_BACKOFF);
    expect(RETRY_BACKOFF_FLOOR_MS).toBe(RETRY_BACKOFF.floorMs);
    expect(RETRY_BACKOFF_CAP_MS).toBe(RETRY_BACKOFF.capMs);

    // A ladder may name a count instead when the number of asks is the point rather than the
    // climb — the session's is three, bought against a rolling restart, and stops below its
    // ceiling on purpose.
    expect(SESSION_RETRY.attempts).toBe(3);
    expect(Math.max(...rungs(SESSION_RETRY))).toBeLessThan(SESSION_RETRY.capMs);

    // The derivation is what makes the default honest: halve the floor and the ladder grows a rung.
    expect(retryLadder(500, 4_000).attempts).toBe(retryLadder(1_000, 4_000).attempts + 1);
  });

  it("the session ladder is reachable whole through the provider", () => {
    // The cap was the one of the three a host could not move, with no stated reason — and it is
    // the one that decides whether a Retry-After is waited out or handed to the reader.
    expect(DEFAULT_WEB_LIMITS.sessionRetryAttempts).toBe(SESSION_RETRY.attempts);
    expect(DEFAULT_WEB_LIMITS.sessionRetryFloorMs).toBe(SESSION_RETRY.floorMs);
    expect(DEFAULT_WEB_LIMITS.sessionRetryCapMs).toBe(SESSION_RETRY.capMs);
  });

  it("the scan would catch a number put back", () => {
    // The guard above is only worth having if it fails on the thing it forbids.
    expect(bareNumbers("const POLL_MS = 15_000;")).toEqual(["15_000"]);
    expect(bareNumbers("retry(count < 3)")).toEqual(["3"]);
    // ...and stays quiet on what it allows.
    expect(bareNumbers("const first = rows[0]; if (status === 404) return;")).toEqual([]);
    expect(bareNumbers("// a 250 ms window\nconst x = LIMIT;")).toEqual([]);
  });
});

describe("the limits reach the call sites through the provider", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function wrap(overrides?: Partial<typeof DEFAULT_WEB_LIMITS>) {
    const client = createQueryClient({ retry: false });
    return function Wrapper({ children }: { children: React.ReactNode }) {
      return (
        <QueryClientProvider client={client}>
          <LimitsProvider overrides={overrides}>{children}</LimitsProvider>
        </QueryClientProvider>
      );
    };
  }

  it("a host's override is what a component below it reads, and nesting composes", () => {
    const { result } = renderHook(() => useLimits(), {
      wrapper: ({ children }) => (
        <LimitsProvider overrides={{ filesUndoToastMs: 1_234 }}>
          <LimitsProvider overrides={{ filesSearchMinLength: 1 }}>{children}</LimitsProvider>
        </LimitsProvider>
      ),
    });
    // The inner provider starts from what it is inside, not from the defaults.
    expect(result.current.filesUndoToastMs).toBe(1_234);
    expect(result.current.filesSearchMinLength).toBe(1);
    expect(result.current.filesSoftThresholdRows).toBe(DEFAULT_WEB_LIMITS.filesSoftThresholdRows);
  });

  it("the folder view stops prefetching at the host's row threshold, not a built-in one", async () => {
    // Six children behind pages of two. The limit is the only thing that decides
    // where the prefetch stops, so the same folder read under two hosts loads two
    // different amounts.
    const CHILDREN = 6;
    const PAGE = 2;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const raw =
          typeof input === "string"
            ? input
            : input instanceof Request
              ? input.url
              : input.toString();
        const url = new URL(raw, "http://localhost");
        let body: unknown = { id: "folder-1", kind: "folder" };
        if (url.pathname.endsWith("/children")) {
          const from = Number(url.searchParams.get("marker") ?? "0");
          const end = Math.min(from + PAGE, CHILDREN);
          body = {
            value: Array.from({ length: end - from }, (_, i) => ({
              id: `nd_${from + i}`,
              name: `f${from + i}`,
              nameDisplay: `f${from + i}`,
            })),
            nextMarker: end < CHILDREN ? String(end) : null,
          };
        }
        return Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        );
      }),
    );

    const narrow = renderHook(() => useSoftThreshold("drive-1", "folder-1", { limit: PAGE }), {
      wrapper: wrap({ filesSoftThresholdRows: PAGE * 2 }),
    });
    await waitFor(() => expect(narrow.result.current.loaded).toBe(PAGE * 2));
    // It stopped because of the ceiling, and said so rather than claiming the end.
    expect(narrow.result.current.hasMore).toBe(true);

    // Negative control: the same folder with the shipped default reaches the end.
    const wide = renderHook(() => useSoftThreshold("drive-1", "folder-1", { limit: PAGE }), {
      wrapper: wrap(),
    });
    await waitFor(() => expect(wide.result.current.loaded).toBe(CHILDREN));
    expect(wide.result.current.hasMore).toBe(false);
  });

  it("search waits for the host's minimum length before it asks the server", async () => {
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ items: [], next_marker: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const one = renderHook(
      () => useSearchQuery({ driveId: "drive-1", folderId: undefined, scope: "drive", text: "q", debounceMs: 0 }),
      { wrapper: wrap({ filesSearchMinLength: 5 }) },
    );
    await waitFor(() => expect(one.result.current.isActive).toBe(false));
    expect(fetchMock).not.toHaveBeenCalled();

    // Same single character, a host that accepts it: now the request goes out.
    const two = renderHook(
      () => useSearchQuery({ driveId: "drive-1", folderId: undefined, scope: "drive", text: "q", debounceMs: 0 }),
      { wrapper: wrap({ filesSearchMinLength: 1 }) },
    );
    await waitFor(() => expect(two.result.current.isActive).toBe(true));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  });

  it("the undo prompt closes on the host's timeout, and a passed zero still means never", () => {
    vi.useFakeTimers();
    try {
      const controller: UndoController = {
        pending: { operationId: "op-1", label: "Moved 2 items" },
        undo: vi.fn(),
        redo: vi.fn(),
        running: false,
        canUndo: true,
        canRedo: false,
        push: vi.fn(),
        clear: vi.fn(),
      } as unknown as UndoController;

      const { rerender, unmount } = render(
        <LimitsProvider overrides={{ filesUndoToastMs: 1_000 }}>
          <UndoToast controller={controller} />
        </LimitsProvider>,
      );
      expect(screen.queryByRole("status")).not.toBeNull();
      vi.advanceTimersByTime(1_001);
      rerender(
        <LimitsProvider overrides={{ filesUndoToastMs: 1_000 }}>
          <UndoToast controller={controller} />
        </LimitsProvider>,
      );
      expect(screen.queryByRole("status")).toBeNull();
      unmount();

      // A `0` from the caller is "stay up", and must not fall through to the limit.
      render(
        <LimitsProvider overrides={{ filesUndoToastMs: 1_000 }}>
          <UndoToast controller={controller} timeoutMs={0} />
        </LimitsProvider>,
      );
      vi.advanceTimersByTime(60_000);
      expect(screen.queryByRole("status")).not.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
