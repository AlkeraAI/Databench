// @vitest-environment jsdom
//
// Getting a file's bytes in front of a reader without handing anyone a key.
//
// The bytes live on a different origin than the app, behind a short-lived grant
// minted per read. What the hook must get right is the chain around that grant:
// ask for the RIGHT kind (a framed document is served a page, everything else a
// single file), fetch it as an anonymous cross-origin read so no session cookie
// ever rides along, refuse a body whose type is not what was planned, and let go
// of the object URL the moment the version moves. A superseded read must not be
// allowed to land on top of a newer one, and the grant URL itself — a bearer
// credential in a path — must never reach the page.

import { PreviewSurface } from "@alkera/ui";
import { act, cleanup, render, renderHook, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { publishFrame, resetFrameBus } from "@/api/events/frameBus";
import type { ContentGrant, Item, MintGrantVars } from "@/api/files";
import { useMintContentGrant } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { useLiveNode } from "@/pages/workspace/files/live/useLiveNode";
import { clockTime } from "@/pages/workspace/files/useLeaseFacet";
import { previewFacts, usePreviewContent } from "@/pages/workspace/files/preview/usePreviewContent";

const GRANT_URL = "http://files.localhost:8000/c/bXktbm9uY2U.Y2xhaW0.c2ln";
const PAGE_URL = "http://files.localhost:8000/c/p/bXktbm9uY2U.Y2xhaW0.c2ln/report.html";
const ITEM_ID = "11111111-1111-4111-8111-111111111111";

function item(over: Partial<Item> = {}, mime = "image/png", size = 128): Item {
  return {
    id: ITEM_ID,
    driveId: "d1",
    kind: "file",
    name: "chart.png",
    nameDisplay: "chart.png",
    etag: "etag-1",
    file: { mime_type: mime, size, content_hash: "sha256-test", scan_state: "clean" },
    ...over,
  } as Item;
}

function grant(over: Partial<ContentGrant> = {}): ContentGrant {
  return {
    url: GRANT_URL,
    expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
    kind: "file",
    etag: "etag-1",
    ...over,
  };
}

/** A mint that records what it was asked for. */
function recordingMint(answer: (vars: MintGrantVars) => ContentGrant = () => grant()) {
  const calls: MintGrantVars[] = [];
  const mint = vi.fn(async (vars: MintGrantVars) => {
    calls.push(vars);
    return answer(vars);
  });
  return { mint, calls };
}

function bytes(body: BodyInit, contentType: string, status = 200): Response {
  return new Response(body, { status, headers: { "content-type": contentType } });
}

/** jsdom has no object-URL implementation; every blob-shaped case needs one. */
function stubObjectUrls() {
  let n = 0;
  const created: string[] = [];
  const revoked: string[] = [];
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn(() => {
      const url = `blob:made-${(n += 1)}`;
      created.push(url);
      return url;
    }),
    revokeObjectURL: vi.fn((url: string) => revoked.push(url)),
  });
  return { created, revoked };
}

describe("the machine a file's missing bytes are fetched from", () => {
  const live = {
    holder: "807bf89a-464c-4867-8b08-6e020a9bd8a3",
    machine: "807bf89a-464c-4867-8b08-6e020a9bd8a3",
    machine_name: "demo-box",
    live: true,
    served: "live",
  };
  const unlanded = { state: "writing", content: "unlanded" };

  it.each([
    {
      name: "a live lease that is answering",
      lease: live,
      row: unlanded,
      machine: "demo-box",
    },
    {
      name: "a server that predates the serving word",
      lease: { ...live, served: undefined },
      row: unlanded,
      machine: "demo-box",
    },
    {
      name: "a machine the server resolved no name for",
      lease: { ...live, machine_name: "" },
      row: unlanded,
      machine: "the workspace machine",
    },
    {
      name: "a machine that stopped answering",
      lease: { ...live, served: "offline" },
      row: unlanded,
      machine: null,
    },
    {
      name: "a lease not on the live plane",
      lease: { ...live, live: false },
      row: unlanded,
      machine: null,
    },
    {
      name: "bytes left behind when the lease ended",
      lease: live,
      row: { state: "writing", content: "unsynced" },
      machine: null,
    },
    { name: "no lease at all", lease: null, row: null, machine: null },
  ])("is named for $name: $machine", ({ lease, row, machine }) => {
    const unsent = item(
      {
        lease,
        live: row,
        file: { mime_type: "text/plain", size: 0, content_hash: "" },
      } as Partial<Item>,
      "text/plain",
    );
    expect(previewFacts(unsent)).toMatchObject({ synced: false, machine });
  });
});

describe("usePreviewContent — which grant a plan asks for", () => {
  afterEach(() => {
    // Unmount BEFORE the object-URL stub goes away: the hook releases its blob
    // on the way out, and a bare jsdom has no `revokeObjectURL` to release it with.
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each([
    ["text/html", "report.html", "page"],
    ["application/pdf", "q3.pdf", "page"],
    ["image/png", "chart.png", "file"],
    ["text/csv", "rows.csv", "file"],
    ["text/plain", "notes.txt", "file"],
  ])("a %s file named %s mints a %s-kind grant", async (mime, name, kind) => {
    stubObjectUrls();
    const { mint, calls } = recordingMint(() =>
      grant({ kind: kind as "file" | "page", url: kind === "page" ? PAGE_URL : GRANT_URL }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes("x", mime)),
    );

    const { result } = renderHook(() => usePreviewContent(item({ name }, mime), mint));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toMatchObject({ kind, itemId: ITEM_ID, driveId: "d1" });
    await waitFor(() => expect(result.current.status).toBe("ready"));
  });

  it("never mints for a type nothing renders — the card draws from the facts alone", async () => {
    const { mint, calls } = recordingMint();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "blob.bin" }, "application/octet-stream"), mint),
    );

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(calls).toHaveLength(0);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.content).toEqual({ kind: "none" });
  });

  it("fetches a picture of any size and hands it to the renderer", async () => {
    stubObjectUrls();
    const { mint, calls } = recordingMint();
    const fetchMock = vi.fn(async () => bytes(new Blob(["png"]), "image/png"));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "scan.png" }, "image/png", 900 * 1024 * 1024), mint),
    );

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(calls).toEqual([{ driveId: "d1", itemId: ITEM_ID, kind: "file" }]);
    expect(result.current.content).toEqual({ kind: "blob", url: "blob:made-1", mime: "image/png" });
    cleanup();
    vi.unstubAllGlobals();
  });
});

describe("usePreviewContent — the fetch", () => {
  afterEach(() => {
    // Unmount BEFORE the object-URL stub goes away: the hook releases its blob
    // on the way out, and a bare jsdom has no `revokeObjectURL` to release it with.
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("reads the grant anonymously — no cookie, no cache", async () => {
    const { mint } = recordingMint();
    const fetchMock = vi.fn(async () => bytes("id,total\n1,2\n", "text/csv"));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "rows.csv" }, "text/csv"), mint),
    );

    await waitFor(() => expect(result.current.status).toBe("ready"));
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(GRANT_URL);
    expect(init.mode).toBe("cors");
    expect(init.credentials).toBe("omit");
    expect(init.cache).toBe("no-store");
    expect(result.current.content).toEqual({ kind: "text", text: "id,total\n1,2\n" });
  });

  it("refuses a body whose served type is not the one that was planned", async () => {
    const { mint } = recordingMint();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes("<html></html>", "text/html")),
    );

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "rows.csv" }, "text/csv"), mint),
    );

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.error).toBe("The file changed. Reload to see the new version.");
    expect(result.current.content).toEqual({ kind: "none" });
  });

  it.each([
    ["Chrome", "Failed to fetch"],
    ["Safari", "Load failed"],
    ["Firefox", "NetworkError when attempting to fetch resource."],
  ])("says a read that never arrived in a sentence, not in %s's words", async (_browser, words) => {
    // The content origin did not answer at all (a dead host, a dropped network):
    // fetch rejects with the browser's own TypeError.
    const { mint } = recordingMint();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError(words);
      }),
    );

    const { result } = renderHook(() => usePreviewContent(item({ name: "notes.md" }, "text/markdown"), mint));

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.error).toBe("The preview could not be loaded");
    expect(result.current.error).not.toContain(words);
  });

  it("says a refused grant in the Files copy, not the server's raw message", async () => {
    const mint = vi.fn(async () => {
      throw new ApiError(403, { code: "files.forbidden", message: "Not allowed" });
    });
    vi.stubGlobal("fetch", vi.fn(async () => bytes("x", "text/markdown")));

    const { result } = renderHook(() => usePreviewContent(item({ name: "notes.md" }, "text/markdown"), mint));

    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.error).not.toBe("Not allowed");
    expect(result.current.error).toMatch(/^[A-Z].*\.$/);
  });

  it("a file that is no longer there reads as gone, not as a failure", async () => {
    const { mint } = recordingMint();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("", { status: 404 })),
    );

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "rows.csv" }, "text/csv"), mint),
    );

    await waitFor(() => expect(result.current.status).toBe("gone"));
  });

  it("a version the holding machine has not written back yet reads as pending", async () => {
    const mint = vi.fn(async () => {
      throw Object.assign(new Error("not written back"), {
        name: "ApiError",
        status: 409,
        code: "files.live_pending",
      });
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "rows.csv" }, "text/csv"), mint),
    );

    await waitFor(() => expect(result.current.status).toBe("pending"));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("bytes the drive is still bringing from the machine", () => {
  const MACHINE = "807bf89a-464c-4867-8b08-6e020a9bd8a3";
  const lease = { machine: MACHINE, machine_name: "demo-box", live: true, served: "live" };
  const unlanded = (over: Partial<Item> = {}): Item =>
    item(
      {
        name: "rows.csv",
        lease,
        live: { state: "writing", content: "unlanded" },
        file: { mime_type: "text/csv", size: 0, content_hash: "" },
        ...over,
      } as Partial<Item>,
      "text/csv",
    );

  /** The refusal as the mint raises it: the route's flat envelope, parsed by
   *  the same error class the real door throws. */
  function pending(detail: unknown, retryAfter: string | null = null): ApiError {
    const headers = new Headers();
    if (retryAfter !== null) headers.set("retry-after", retryAfter);
    return new ApiError(
      409,
      { code: "files.live_pending", message: "not written back yet", detail },
      "could not open this file",
      headers,
    );
  }

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    resetFrameBus();
  });

  it.each([
    ["accepted", "Fetching from demo-box…"],
    ["landed", "Fetching from demo-box…"],
    ["timed_out", "Fetching from demo-box…"],
    ["changed", "Fetching from demo-box…"],
    ["not_holder", "Fetching from demo-box…"],
    ["missing", "Fetching from demo-box…"],
    ["offline", "Demo-box is offline · showing nothing yet"],
    ["busy", "Demo-box is busy · trying again"],
    ["throttled", "Demo-box is busy · trying again"],
    ["a word this build predates", "Fetching from demo-box…"],
  ])("a refusal whose machine answered %s reads %s", async (outcome, line) => {
    const mint = vi.fn(async () => {
      throw pending({ holder: "demo-box", outcome, landing: null });
    });
    vi.stubGlobal("fetch", vi.fn());

    const { result } = renderHook(() => usePreviewContent(unlanded(), mint));

    await waitFor(() => expect(result.current.pendingReason).toBe(line));
    expect(result.current.status).toBe("pending");
  });

  it("names the lease's machine when the refusal names none", async () => {
    const mint = vi.fn(async () => {
      throw pending({ holder: null, outcome: "offline" });
    });
    vi.stubGlobal("fetch", vi.fn());

    const named = renderHook(() => usePreviewContent(unlanded(), mint));
    await waitFor(() =>
      expect(named.result.current.pendingReason).toBe(
        "Demo-box is offline · showing nothing yet",
      ),
    );

    // A lease naming the machine only by its id: the drive's own word for a
    // machine it cannot name, never the id.
    const anonymous = renderHook(() =>
      usePreviewContent(unlanded({ lease: { ...lease, machine_name: "" } } as Partial<Item>), mint),
    );
    await waitFor(() =>
      expect(anonymous.result.current.pendingReason).toBe(
        "The workspace machine is offline · showing nothing yet",
      ),
    );
  });

  it("names the machine by its name when the refusal names it only by id", async () => {
    // The drive's refusal carries the lease's own word for the holder, which
    // for a registered box is its allocation id. The reader is told the name
    // the listing shows, never the id.
    const mint = vi.fn(async () => {
      throw pending({ holder: MACHINE, outcome: "accepted", landing: null });
    });
    vi.stubGlobal("fetch", vi.fn());

    const named = renderHook(() => usePreviewContent(unlanded(), mint));
    await waitFor(() =>
      expect(named.result.current.pendingReason).toBe("Fetching from demo-box…"),
    );

    const anonymous = renderHook(() =>
      usePreviewContent(unlanded({ lease: { ...lease, machine_name: "" } } as Partial<Item>), mint),
    );
    await waitFor(() =>
      expect(anonymous.result.current.pendingReason).toBe("Fetching from the workspace machine…"),
    );
  });

  it.each([
    ["a row with no lease", { lease: null }],
    ["a lease not on the live plane", { lease: { ...lease, live: false } }],
    ["bytes left behind when the lease ended", { live: { state: "writing", content: "unsynced" } }],
  ])("does not ask for %s: nobody can bring them", async (_name, over) => {
    const { mint, calls } = recordingMint();
    vi.stubGlobal("fetch", vi.fn());

    const { result } = renderHook(() => usePreviewContent(unlanded(over as Partial<Item>), mint));

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(calls).toHaveLength(0);
    expect(result.current.facts.synced).toBe(false);
  });

  it("asks for a row still on the machine, shows the wait, then draws what arrived", async () => {
    let release: ((value: ContentGrant) => void) | undefined;
    const mint = vi.fn(
      (_vars: MintGrantVars) =>
        new Promise<ContentGrant>((resolve) => {
          release = resolve;
        }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes("id\n1\n", "text/csv")),
    );

    const { result } = renderHook(() => usePreviewContent(unlanded(), mint));

    // The drive is parked on the machine: the card says so, not a spinner.
    await waitFor(() => expect(mint).toHaveBeenCalledTimes(1));
    expect(mint.mock.calls[0]?.[0]).toMatchObject({ kind: "file", itemId: ITEM_ID });
    expect(result.current.status).toBe("pending");

    release?.(grant());
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.content).toEqual({ kind: "text", text: "id\n1\n" });
    // Drawn as the file it is, although the row still calls it unsynced.
    expect(result.current.facts.synced).toBe(true);
    expect(result.current.plan.unsynced).toBe(false);
  });

  it("leaves the card its own sentence when the server sent no detail", async () => {
    const mint = vi.fn(async () => {
      throw pending(undefined);
    });
    vi.stubGlobal("fetch", vi.fn());

    const { result } = renderHook(() => usePreviewContent(unlanded(), mint));

    await waitFor(() => expect(mint).toHaveBeenCalledTimes(1));
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.status).toBe("pending");
    expect(result.current.pendingReason).toBeUndefined();
  });

  it("asks again when the drive's Retry-After runs out, and not before", async () => {
    vi.useFakeTimers();
    let refusals = 2;
    const { mint, calls } = recordingMint(() => {
      if (refusals > 0) {
        refusals -= 1;
        throw pending({ holder: "demo-box", outcome: "accepted" }, "2");
      }
      return grant();
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes("id\n1\n", "text/csv")),
    );
    const seen: string[] = [];

    const { result } = renderHook(() => {
      const view = usePreviewContent(unlanded(), mint);
      seen.push(view.status);
      return view;
    });
    await vi.waitFor(() => expect(result.current.status).toBe("pending"));
    expect(calls).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_900);
    });
    expect(calls).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(calls).toHaveLength(2);
    expect(result.current.status).toBe("pending");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    await vi.waitFor(() => expect(result.current.status).toBe("ready"));
    expect(calls).toHaveLength(3);
    // Once pending, the card stayed up through each retry until the bytes came.
    const sincePending = seen.slice(seen.indexOf("pending"));
    expect(sincePending.filter((status) => status === "loading")).toEqual([]);
  });

  it("takes a Retry-After under the portal's floor at the floor", async () => {
    vi.useFakeTimers();
    const { mint, calls } = recordingMint(() => {
      throw pending({ holder: "demo-box", outcome: "busy" }, "0");
    });
    vi.stubGlobal("fetch", vi.fn());

    renderHook(() => usePreviewContent(unlanded(), mint));
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    expect(calls).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(calls).toHaveLength(2);
  });

  it("does not poll a refusal that named no wait", async () => {
    vi.useFakeTimers();
    const { mint, calls } = recordingMint(() => {
      throw pending({ holder: "demo-box", outcome: "accepted" });
    });
    vi.stubGlobal("fetch", vi.fn());

    renderHook(() => usePreviewContent(unlanded(), mint));
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60_000);
    });
    expect(calls).toHaveLength(1);
  });

  it("stops asking once the preview is closed", async () => {
    vi.useFakeTimers();
    const { mint, calls } = recordingMint(() => {
      throw pending({ holder: "demo-box", outcome: "accepted" }, "2");
    });
    vi.stubGlobal("fetch", vi.fn());

    const { unmount } = renderHook(() => usePreviewContent(unlanded(), mint));
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    unmount();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(calls).toHaveLength(1);
  });

  it("reads the bytes again when the frame saying they landed arrives", async () => {
    // Drawn the way a tab draws it: the node is read and kept current by the
    // frames that name it, and the preview is bought for whatever it read.
    let served: Item = unlanded();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof Request
              ? input.url
              : input.toString();
        if (url.startsWith("http://files.localhost")) return bytes("id\n1\n", "text/csv");
        return new Response(JSON.stringify(served), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    const { mint, calls } = recordingMint(() => {
      if ((served.file?.content_hash ?? "") === "") {
        throw pending({ holder: "demo-box", outcome: "timed_out" });
      }
      return grant({ etag: "etag-2" });
    });
    const client = createQueryClient({ retry: false });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);

    const { result } = renderHook(
      () => {
        const node = useLiveNode("d1", ITEM_ID);
        return usePreviewContent(node.item, mint);
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toBe("pending"));
    expect(calls).toHaveLength(1);

    served = {
      ...served,
      etag: "etag-2",
      live: null,
      file: { mime_type: "text/csv", size: 6, content_hash: "sha256-landed" },
    } as Item;
    await act(async () => {
      publishFrame({
        type: "file_node.changed",
        entity: "file_node",
        entity_id: ITEM_ID,
        version: 2,
        org_id: "org_1",
        drive_id: "d1",
        reason: "live_saved",
      } as RealtimeEventFrame);
      await Promise.resolve();
    });

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(calls).toHaveLength(2);
    expect(result.current.content).toEqual({ kind: "text", text: "id\n1\n" });
  });
});

describe("a copy the drive served while the machine holds a newer one", () => {
  const MACHINE = "807bf89a-464c-4867-8b08-6e020a9bd8a3";
  const lease = { machine: MACHINE, machine_name: "demo-box", live: true, served: "live" };
  const AS_OF = "2026-09-23T10:04:00Z";
  const behindRow = (over: Partial<Item> = {}): Item =>
    item(
      {
        name: "rows.csv",
        lease,
        live: { state: "writing", content: "behind" },
        ...over,
      } as Partial<Item>,
      "text/csv",
    );
  const olderCopy = `Showing the copy from ${clockTime(AS_OF)}; demo-box has a newer one`;

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    resetFrameBus();
  });

  it("draws the copy and says when it is from", async () => {
    const mint = vi.fn(
      async () => ({ ...grant(), contentState: "behind", asOf: AS_OF }) as ContentGrant,
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes("id\n1\n", "text/csv")),
    );

    const { result } = renderHook(() => usePreviewContent(behindRow(), mint));

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.content).toEqual({ kind: "text", text: "id\n1\n" });
    expect(result.current.notice).toBe(olderCopy);
  });

  it("says an older copy when the drive did not say from when", async () => {
    const mint = vi.fn(
      async () => ({ ...grant(), contentState: "behind", asOf: null }) as ContentGrant,
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes("id\n1\n", "text/csv")),
    );

    const { result } = renderHook(() => usePreviewContent(behindRow(), mint));

    await waitFor(() =>
      expect(result.current.notice).toBe("Showing an older copy; demo-box has a newer one"),
    );
  });

  it.each([
    ["on the drive", { contentState: "on_drive", asOf: AS_OF }],
    ["with nothing to say", {}],
  ])("says nothing over a copy the drive served %s", async (_name, wire) => {
    const mint = vi.fn(async () => ({ ...grant(), ...wire }) as ContentGrant);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes("id\n1\n", "text/csv")),
    );

    const { result } = renderHook(() => usePreviewContent(behindRow(), mint));

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.notice).toBeUndefined();
  });

  it("does not re-use a page grant that served an older copy", async () => {
    let behind = true;
    const { mint, calls } = recordingMint(
      () =>
        ({
          ...grant({ kind: "page", url: PAGE_URL }),
          contentState: behind ? "behind" : "on_drive",
          asOf: AS_OF,
        }) as ContentGrant,
    );
    vi.stubGlobal("fetch", vi.fn());

    const { result, rerender } = renderHook(
      ({ ctag }: { ctag: string }) =>
        usePreviewContent(
          item({ ctag, name: "report.html", lease } as Partial<Item>, "text/html"),
          mint,
        ),
      { initialProps: { ctag: "etag-1.100.1" } },
    );
    await waitFor(() => expect(result.current.notice).toBe(olderCopy));
    expect(calls).toHaveLength(1);

    // The machine's newer bytes landed: the next version asks again rather than
    // reloading a grant that might still be serving the older copy's word.
    behind = false;
    rerender({ ctag: "etag-2" });
    await waitFor(() => expect(calls).toHaveLength(2));
    await waitFor(() => expect(result.current.notice).toBeUndefined());

    // A grant that served the newest bytes is re-used as before.
    rerender({ ctag: "etag-3" });
    await waitFor(() => expect(result.current.version).toBe("etag-3"));
    expect(calls).toHaveLength(2);
  });

  it("reads the newer bytes on the frame saying they landed, and drops the line", async () => {
    let served: Item = behindRow();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof Request
              ? input.url
              : input.toString();
        if (url.startsWith("http://files.localhost")) {
          return bytes(served.etag === "etag-2" ? "id\n2\n" : "id\n1\n", "text/csv");
        }
        return new Response(JSON.stringify(served), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    const { mint, calls } = recordingMint(
      () =>
        ({
          ...grant(),
          contentState: served.etag === "etag-2" ? "on_drive" : "behind",
          asOf: AS_OF,
        }) as ContentGrant,
    );
    const client = createQueryClient({ retry: false });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);

    const { result } = renderHook(() => usePreviewContent(useLiveNode("d1", ITEM_ID).item, mint), {
      wrapper,
    });
    await waitFor(() => expect(result.current.notice).toBe(olderCopy));
    expect(result.current.content).toEqual({ kind: "text", text: "id\n1\n" });

    served = { ...served, etag: "etag-2", live: null } as Item;
    await act(async () => {
      publishFrame({
        type: "file_node.changed",
        entity: "file_node",
        entity_id: ITEM_ID,
        version: 3,
        org_id: "org_1",
        drive_id: "d1",
        reason: "live_saved",
      } as RealtimeEventFrame);
      await Promise.resolve();
    });

    await waitFor(() => expect(result.current.content).toEqual({ kind: "text", text: "id\n2\n" }));
    expect(result.current.notice).toBeUndefined();
    expect(calls).toHaveLength(2);
  });
});

describe("usePreviewContent — versions, object URLs and grants", () => {
  afterEach(() => {
    // Unmount BEFORE the object-URL stub goes away: the hook releases its blob
    // on the way out, and a bare jsdom has no `revokeObjectURL` to release it with.
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lets go of the object URL when the version moves and again on unmount", async () => {
    const { created, revoked } = stubObjectUrls();
    const { mint } = recordingMint();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes(new Blob(["png"]), "image/png")),
    );

    const { result, rerender, unmount } = renderHook(
      ({ etag }: { etag: string }) => usePreviewContent(item({ etag }), mint),
      { initialProps: { etag: "etag-1" } },
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(created).toHaveLength(1);

    rerender({ etag: "etag-2" });
    await waitFor(() => expect(revoked).toEqual([created[0]]));
    await waitFor(() => expect(created).toHaveLength(2));

    unmount();
    await waitFor(() => expect(revoked).toEqual(created));
  });

  it("aborts the read for a version that has been superseded", async () => {
    const { created } = stubObjectUrls();
    const { mint } = recordingMint();
    const signals: AbortSignal[] = [];
    let release: ((value: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        signals.push(init.signal as AbortSignal);
        if (signals.length === 1) {
          return new Promise<Response>((resolve) => {
            release = resolve;
          });
        }
        return bytes(new Blob(["new"]), "image/png");
      }),
    );

    const { result, rerender } = renderHook(
      ({ etag }: { etag: string }) => usePreviewContent(item({ etag }), mint),
      { initialProps: { etag: "etag-1" } },
    );
    await waitFor(() => expect(signals).toHaveLength(1));

    rerender({ etag: "etag-2" });
    await waitFor(() => expect(signals).toHaveLength(2));
    expect(signals[0]!.aborted).toBe(true);

    await waitFor(() => expect(result.current.status).toBe("ready"));
    const current = result.current.content;

    // The superseded answer arrives late; it must not become bytes, and it must
    // not paint over the version that replaced it.
    release?.(bytes(new Blob(["stale"]), "image/png"));
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(created).toHaveLength(1);
    expect(result.current.content).toEqual(current);
  });

  it("mints a fresh file grant for every version, and re-uses a page grant until it is nearly spent", async () => {
    vi.useFakeTimers();
    try {
      const fileMint = recordingMint();
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => bytes("rows", "text/csv")),
      );
      const csv = renderHook(
        ({ etag }: { etag: string }) =>
          usePreviewContent(item({ etag, name: "rows.csv" }, "text/csv"), fileMint.mint),
        { initialProps: { etag: "etag-1" } },
      );
      await vi.waitFor(() => expect(fileMint.calls).toHaveLength(1));
      csv.rerender({ etag: "etag-2" });
      await vi.waitFor(() => expect(fileMint.calls).toHaveLength(2));

      const pageMint = recordingMint(() =>
        grant({
          kind: "page",
          url: PAGE_URL,
          expiresAt: new Date(Date.now() + 60_000).toISOString(),
        }),
      );
      const html = renderHook(
        ({ etag }: { etag: string }) =>
          usePreviewContent(item({ etag, name: "report.html" }, "text/html"), pageMint.mint),
        { initialProps: { etag: "etag-1" } },
      );
      await vi.waitFor(() => expect(pageMint.calls).toHaveLength(1));

      // Still well inside the grant's life: the same URL serves the new version.
      html.rerender({ etag: "etag-2" });
      await vi.waitFor(() => expect(html.result.current.version).toBe("etag-2"));
      expect(pageMint.calls).toHaveLength(1);

      // Within half a minute of expiry the grant is spent; the next version mints again.
      vi.setSystemTime(new Date(Date.now() + 45_000));
      html.rerender({ etag: "etag-3" });
      await vi.waitFor(() => expect(pageMint.calls).toHaveLength(2));
    } finally {
      vi.useRealTimers();
    }
  });

  it("frames a document under sandbox, and a PDF without one", async () => {
    const { mint } = recordingMint(() => grant({ kind: "page", url: PAGE_URL }));
    vi.stubGlobal("fetch", vi.fn());

    const html = renderHook(() =>
      usePreviewContent(item({ name: "report.html" }, "text/html"), mint),
    );
    await waitFor(() => expect(html.result.current.status).toBe("ready"));
    expect(html.result.current.content).toEqual({
      kind: "frame",
      url: PAGE_URL,
      sandboxed: true,
      scripts: true,
      title: "Preview of report.html (sandboxed)",
    });

    const pdf = renderHook(() =>
      usePreviewContent(item({ name: "q3.pdf" }, "application/pdf"), mint),
    );
    await waitFor(() => expect(pdf.result.current.status).toBe("ready"));
    expect(pdf.result.current.content).toMatchObject({ kind: "frame", sandboxed: false, scripts: false });
  });

  it("leaves the viewer alone for a PDF whose writer set no content type", async () => {
    // The frame is chosen by the planned kind, so the sandbox has to be too.
    // Sandboxing a PDF disables the browser's viewer: the pane stays blank and
    // the reader waits out the frame's timeout for a card blaming the server.
    const { mint } = recordingMint(() => grant({ kind: "page", url: PAGE_URL }));
    vi.stubGlobal("fetch", vi.fn());

    const pdf = renderHook(() =>
      usePreviewContent(item({ name: "q3.pdf" }, "application/octet-stream"), mint),
    );

    await waitFor(() => expect(pdf.result.current.status).toBe("ready"));
    expect(pdf.result.current.content).toMatchObject({ kind: "frame", sandboxed: false });
  });
});

describe("what a framed preview reloads on", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** The hook and the surface together, as a host draws them. */
  function Framed({ row, mint }: { row: Item; mint: (v: MintGrantVars) => Promise<ContentGrant> }) {
    const bytes = usePreviewContent(row, mint);
    return createElement(PreviewSurface, { ...bytes, onDownload: () => {} });
  }

  const report = (over: Partial<Item>): Item =>
    item({ name: "report.html", ctag: "etag-1.100.1", ...over } as Partial<Item>, "text/html");

  it("replaces the frame when the machine's report moves the ctag, and not for an unrelated field", async () => {
    const { mint } = recordingMint(() => grant({ kind: "page", url: PAGE_URL }));
    vi.stubGlobal("fetch", vi.fn());

    const view = render(createElement(Framed, { row: report({}), mint }));
    await waitFor(() => expect(view.container.querySelector("iframe")).not.toBeNull());
    const first = view.container.querySelector("iframe");

    // A rename, a lease facet moving: the bytes did not, so the frame stays.
    view.rerender(
      createElement(Framed, {
        row: report({ nameDisplay: "report (1).html", lease: null } as Partial<Item>),
        mint,
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(view.container.querySelector("iframe")).toBe(first);

    // The machine rewrote the file: same etag (no bytes landed yet), new ctag.
    view.rerender(createElement(Framed, { row: report({ ctag: "etag-1.200.2" }), mint }));
    await waitFor(() => expect(view.container.querySelector("iframe")).not.toBe(first));
    expect(view.container.querySelector("iframe")).not.toBeNull();
  });

  it("reports the ctag as the version, and the etag where the server sends no ctag", () => {
    const mint = vi.fn(async () => grant());
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes("x", "image/png")),
    );
    stubObjectUrls();

    const tagged = renderHook(() =>
      usePreviewContent(item({ ctag: "etag-1.100.1" } as Partial<Item>), mint),
    );
    expect(tagged.result.current.version).toBe("etag-1.100.1");
    const bare = renderHook(() => usePreviewContent(item({ ctag: "" } as Partial<Item>), mint));
    expect(bare.result.current.version).toBe("etag-1");
  });

  it("buys the rewritten bytes when only the ctag moved", async () => {
    stubObjectUrls();
    const { mint, calls } = recordingMint();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes("id\n1\n", "text/csv")),
    );

    const { rerender } = renderHook(
      ({ ctag }: { ctag: string }) =>
        usePreviewContent(item({ ctag, name: "rows.csv" } as Partial<Item>, "text/csv"), mint),
      { initialProps: { ctag: "etag-1.100.1" } },
    );
    await waitFor(() => expect(calls).toHaveLength(1));

    rerender({ ctag: "etag-1.200.2" });
    await waitFor(() => expect(calls).toHaveLength(2));
  });
});

describe("the grant URL never reaches the page", () => {
  afterEach(() => {
    // Unmount BEFORE the object-URL stub goes away: the hook releases its blob
    // on the way out, and a bare jsdom has no `revokeObjectURL` to release it with.
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("keeps a read's grant out of the document and out of history", async () => {
    const { mint } = recordingMint();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => bytes("id,total\n1,2\n", "text/csv")),
    );
    const before = window.history.length;

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "rows.csv" }, "text/csv"), mint),
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));

    expect(window.history.length).toBe(before);
    expect(window.location.href).not.toContain("/c/");
    expect(document.body.innerHTML).not.toContain(GRANT_URL);
    expect(JSON.stringify(result.current.content)).not.toContain(GRANT_URL);
  });
});

describe("useMintContentGrant", () => {
  function wrapper({ children }: { children: ReactNode }) {
    return createElement(QueryClientProvider, { client: createQueryClient() }, children);
  }

  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks the node's own route for a grant and answers what it minted", async () => {
    const fetchMock = vi.fn(async () =>
      bytes(
        JSON.stringify({
          url: GRANT_URL,
          expiresAt: "2026-09-16T00:05:00Z",
          kind: "file",
          etag: "e",
        }),
        "application/json",
        201,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useMintContentGrant(), { wrapper });
    const minted = await result.current.mutateAsync({
      driveId: "d1",
      itemId: "n1",
      kind: "page",
    });

    expect(minted.url).toBe(GRANT_URL);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/v1/files/drives/d1/items/n1/content-grants");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(String(init.body))).toEqual({ kind: "page", disposition: "inline" });
  });

  it("raises the server's refusal with its code so a caller can tell pending from gone", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        bytes(
          JSON.stringify({ code: "files.live_pending", message: "not written back yet" }),
          "application/json",
          409,
        ),
      ),
    );

    const { result } = renderHook(() => useMintContentGrant(), { wrapper });
    await expect(
      result.current.mutateAsync({ driveId: "d1", itemId: "n1", kind: "file" }),
    ).rejects.toMatchObject({ status: 409, code: "files.live_pending" });
  });
});

describe("a text file larger than one window", () => {
  const WINDOW = 1024 * 1024;
  const encoder = new TextEncoder();

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** Exactly `bytes` of lines of uneven length with a two-byte character in each,
   *  so no window edge lands on a line end or between characters by luck. */
  function linesOf(bytes: number): string {
    const lines: string[] = [];
    let length = 0;
    for (let row = 0; ; row += 1) {
      const line = `row ${row} café ${"x".repeat(row % 97)}\n`;
      const width = encoder.encode(line).length;
      if (length + width > bytes - 2) break;
      lines.push(line);
      length += width;
    }
    lines.push(`${"z".repeat(bytes - length - 1)}\n`);
    return lines.join("");
  }

  /** Ask for windows until the file is whole. */
  async function readToEnd(result: { current: { content: unknown } }): Promise<void> {
    for (let guard = 0; guard < 16; guard += 1) {
      const content = textOf(result);
      if ((content.loaded ?? 0) >= (content.total ?? 0)) return;
      await act(async () => {
        await content.more?.();
      });
    }
    throw new Error("the file never finished landing");
  }

  /** Paragraphs of several lines each, separated by blank lines. */
  function paragraphsOf(bytes: number): string {
    const blocks: string[] = [];
    let length = 0;
    for (let block = 0; length < bytes; block += 1) {
      const lines = Array.from({ length: 3 + (block % 5) }, (_, line) => `para ${block} line ${line} ${"y".repeat(block % 61)}`);
      const text = `${lines.join("\n")}\n\n`;
      blocks.push(text);
      length += encoder.encode(text).length;
    }
    return blocks.join("");
  }

  interface Served {
    ranges: (string | null)[];
  }

  /** The content origin: answers a `Range` with a 206 and its `Content-Range`,
   *  unless it is told to ignore ranges or to fail a given window. */
  function contentOrigin(
    body: Uint8Array<ArrayBuffer>,
    type: string,
    options: { ignoreRange?: boolean; failStart?: number } = {},
  ): Served {
    const served: Served = { ranges: [] };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        const range = new Headers(init?.headers).get("Range");
        served.ranges.push(range);
        const match = range === null ? null : /^bytes=(\d+)-(\d+)$/.exec(range);
        if (match && Number(match[1]) === options.failStart) {
          return new Response("nope", { status: 503, headers: { "content-type": type } });
        }
        if (!match || options.ignoreRange) {
          return new Response(body.slice(), { status: 200, headers: { "content-type": type } });
        }
        const start = Number(match[1]);
        const end = Math.min(Number(match[2]), body.length - 1);
        return new Response(body.slice(start, end + 1), {
          status: 206,
          headers: { "content-type": type, "content-range": `bytes ${start}-${end}/${body.length}` },
        });
      }),
    );
    return served;
  }

  type Text = {
    kind: "text";
    text: string;
    loaded?: number;
    total?: number;
    more?: () => Promise<void>;
    whole?: () => Promise<string>;
  };

  function textOf(result: { current: { content: unknown } }): Text {
    const content = result.current.content as Text;
    expect(content.kind).toBe("text");
    return content;
  }

  it("lands the first window from a ranged read, cut at its last line", async () => {
    const full = linesOf(3 * WINDOW);
    const body = encoder.encode(full);
    const served = contentOrigin(body, "text/plain");
    const { mint } = recordingMint();

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "server.log" }, "text/plain", body.length), mint),
    );

    await waitFor(() => expect(result.current.status).toBe("ready"));
    const content = textOf(result);
    expect(served.ranges).toEqual([`bytes=0-${WINDOW - 1}`]);
    expect(content.loaded).toBe(WINDOW);
    expect(content.total).toBe(body.length);
    // Every whole line of the window, and not a byte of the line it cut through.
    const window = new TextDecoder().decode(body.slice(0, WINDOW));
    expect(content.text).toBe(window.slice(0, window.lastIndexOf("\n") + 1));
    expect(full.startsWith(content.text)).toBe(true);
  });

  it("brings the next windows on more() and appends them until the file is whole", async () => {
    const full = linesOf(3 * WINDOW);
    const body = encoder.encode(full);
    const served = contentOrigin(body, "text/plain");
    const { mint, calls } = recordingMint();
    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "server.log" }, "text/plain", body.length), mint),
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    const firstText = textOf(result).text;

    await act(async () => {
      await textOf(result).more?.();
    });
    expect(served.ranges[1]).toBe(`bytes=${WINDOW}-${2 * WINDOW - 1}`);
    expect(textOf(result).loaded).toBe(2 * WINDOW);
    expect(textOf(result).text.startsWith(firstText)).toBe(true);
    expect(textOf(result).text.length).toBeGreaterThan(firstText.length);
    expect(textOf(result).text.endsWith("\n")).toBe(true);

    await readToEnd(result);
    expect(body.length).toBe(3 * WINDOW);
    expect(served.ranges.slice(2)).toEqual([`bytes=${2 * WINDOW}-${body.length - 1}`]);
    expect(textOf(result).loaded).toBe(body.length);
    expect(textOf(result).total).toBe(body.length);
    // Carried bytes are completed, never dropped or doubled.
    expect(textOf(result).text).toBe(full);
    // Every window is its own single-use grant.
    expect(calls).toHaveLength(3);
  });

  it("buys one window for asks that overlap", async () => {
    const body = encoder.encode(linesOf(3 * WINDOW));
    const served = contentOrigin(body, "text/plain");
    const { mint } = recordingMint();
    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "server.log" }, "text/plain", body.length), mint),
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      const more = textOf(result).more!;
      await Promise.all([more(), more()]);
    });

    expect(served.ranges).toEqual([`bytes=0-${WINDOW - 1}`, `bytes=${WINDOW}-${2 * WINDOW - 1}`]);
    expect(textOf(result).loaded).toBe(2 * WINDOW);
  });

  it("takes a 200 that ignored the range as the whole file, in one request", async () => {
    const full = linesOf(3 * WINDOW);
    const body = encoder.encode(full);
    const served = contentOrigin(body, "text/plain", { ignoreRange: true });
    const { mint } = recordingMint();

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "server.log" }, "text/plain", body.length), mint),
    );

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(served.ranges).toHaveLength(1);
    expect(textOf(result).loaded).toBe(body.length);
    expect(textOf(result).total).toBe(body.length);
    expect(textOf(result).text).toBe(full);
  });

  it("cuts a markdown window at its last blank line, so no block is split", async () => {
    const full = paragraphsOf(3 * WINDOW);
    const body = encoder.encode(full);
    contentOrigin(body, "text/markdown");
    const { mint } = recordingMint();

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "book.md" }, "text/markdown", body.length), mint),
    );

    await waitFor(() => expect(result.current.status).toBe("ready"));
    const window = new TextDecoder().decode(body.slice(0, WINDOW));
    const text = textOf(result).text;
    expect(text).toBe(window.slice(0, window.lastIndexOf("\n\n") + 2));
    // A plain line cut would have ended mid-paragraph, on a single newline.
    expect(window.lastIndexOf("\n\n") + 2).toBeLessThan(window.lastIndexOf("\n") + 1);

    await readToEnd(result);
    expect(textOf(result).text).toBe(full);
  });

  it("keeps the text it has when a window fails, says so once, and resumes from the same byte", async () => {
    const full = linesOf(3 * WINDOW);
    const body = encoder.encode(full);
    const served = contentOrigin(body, "text/plain", { failStart: WINDOW });
    const { mint } = recordingMint();
    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "server.log" }, "text/plain", body.length), mint),
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    const before = textOf(result).text;
    expect(result.current.error).toBeUndefined();

    let failure: unknown;
    await act(async () => {
      failure = await textOf(result)
        .more?.()
        .catch((error: unknown) => error);
    });

    expect((failure as Error).message).toBe("The next part of the file could not be loaded");
    expect(result.current.status).toBe("ready");
    expect(result.current.error).toBe("The next part of the file could not be loaded");
    expect(textOf(result).text).toBe(before);
    expect(textOf(result).loaded).toBe(WINDOW);

    // The same window is asked for again, and landing it clears the failure.
    contentOrigin(body, "text/plain");
    await act(async () => {
      await textOf(result).more?.();
    });
    expect(textOf(result).loaded).toBe(2 * WINDOW);
    expect(result.current.error).toBeUndefined();
    expect(served.ranges).toEqual([`bytes=0-${WINDOW - 1}`, `bytes=${WINDOW}-${2 * WINDOW - 1}`]);
  });

  it("reads the whole file in one unranged request for an action that means the file", async () => {
    const full = linesOf(3 * WINDOW);
    const body = encoder.encode(full);
    const served = contentOrigin(body, "text/plain");
    const { mint, calls } = recordingMint();
    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "server.log" }, "text/plain", body.length), mint),
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));

    let whole = "";
    await act(async () => {
      whole = (await textOf(result).whole?.()) ?? "";
    });

    expect(whole).toBe(full);
    expect(served.ranges).toEqual([`bytes=0-${WINDOW - 1}`, null]);
    expect(calls).toHaveLength(2);
    // The screen still shows only the window it had.
    expect(textOf(result).loaded).toBe(WINDOW);
  });

  it("reads a file under one window whole, in one request with no range", async () => {
    const full = linesOf(WINDOW - 64);
    const body = encoder.encode(full);
    const served = contentOrigin(body, "text/plain");
    const { mint } = recordingMint();

    const { result } = renderHook(() =>
      usePreviewContent(item({ name: "server.log" }, "text/plain", body.length), mint),
    );

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(served.ranges).toEqual([null]);
    expect(result.current.content).toEqual({ kind: "text", text: full });
  });
});
