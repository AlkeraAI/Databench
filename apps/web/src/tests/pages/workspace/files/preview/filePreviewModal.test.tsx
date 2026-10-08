// @vitest-environment jsdom
//
// The large preview a file opens into.
//
// It is the same renderer stack a tab beside a chat draws — one registry, one
// hook for the bytes — so the two surfaces can never disagree about a type. What
// is this modal's own is everything around the bytes: what it refuses to fetch,
// which keys it offers for which file, how a reader steps along the listing
// without going back to it, and the rule that the grant that bought the bytes is
// a credential which must not end up anywhere a person or a log can read it.
//
// Two refusals are load-bearing and are asserted by counting requests, not by
// reading copy: a file whose downloads are off and a file in the trash must
// issue NO request at all. A sentence with a fetch behind it is not a refusal.

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, useState, type ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { enterOrg, forgetActiveOrg } from "@/api/activeOrg";
import { createQueryClient } from "@/api/queryClient";
import { FilePreviewModal } from "@/pages/workspace/files/preview/FilePreviewModal";
import { previewActionsFor } from "@/pages/workspace/files/preview/previewActions";
import {
  isPreviewable,
  useLinkedSelection,
} from "@/pages/workspace/files/preview/useLinkedSelection";
import { filesLiveFact } from "@/tests/fixtures/statusFacts";

const DRIVE = "d1";
const GRANT_URL = "http://files.localhost:8000/c/bXktbm9uY2U.Y2xhaW0.c2ln";
const PAGE_URL = "http://files.localhost:8000/c/p/bXktbm9uY2U.Y2xhaW0.c2ln/report.html";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    driveId: DRIVE,
    kind: "file",
    name: "chart.png",
    nameDisplay: "chart.png",
    etag: "etag-1",
    trashed: false,
    stale: false,
    file: { mime_type: "image/png", size: 2048, content_hash: "sha256-test", scan_state: "clean" },
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    capabilities: { can_download: true },
    ...over,
  } as Item;
}

/** Every request the page made, and a scripted answer for each of the two doors
 *  a preview goes through: the mint route on the app origin, then the grant URL
 *  on the content origin. */
function stubNetwork(options: { grantUrl?: string; kind?: "file" | "page"; body?: BodyInit; type?: string } = {}) {
  const urls: string[] = [];
  const mintBodies: string[] = [];
  const grantUrl = options.grantUrl ?? GRANT_URL;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    urls.push(url);
    if (url.includes("/content-grants")) {
      mintBodies.push(String(init?.body ?? ""));
      return new Response(
        JSON.stringify({
          url: grantUrl,
          expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(),
          kind: options.kind ?? "file",
          etag: "etag-1",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    // The content origin honours a single `Range`, as the real one does.
    const range = /^bytes=(\d+)-(\d+)$/.exec(new Headers(init?.headers).get("Range") ?? "");
    if (range && typeof options.body === "string") {
      const all = new TextEncoder().encode(options.body);
      const start = Number(range[1]);
      const end = Math.min(Number(range[2]), all.length - 1);
      return new Response(all.slice(start, end + 1), {
        status: 206,
        headers: {
          "content-type": options.type ?? "image/png",
          "content-range": `bytes ${start}-${end}/${all.length}`,
        },
      });
    }
    return new Response(options.body ?? "bytes", {
      status: 200,
      headers: { "content-type": options.type ?? "image/png" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { urls, mintBodies, mints: () => urls.filter((u) => u.includes("/content-grants")) };
}

/** jsdom implements no object URLs. The statics are added to the real `URL`
 *  rather than the global replaced, because the mint route builds its request
 *  with `new URL(...)` and a stand-in object is not a constructor. */
function stubObjectUrls(): void {
  let n = 0;
  const url = URL as unknown as Record<string, unknown>;
  url.createObjectURL = vi.fn(() => `blob:made-${(n += 1)}`);
  url.revokeObjectURL = vi.fn();
}

function unstubObjectUrls(): void {
  const url = URL as unknown as Record<string, unknown>;
  delete url.createObjectURL;
  delete url.revokeObjectURL;
}

function show(node: ReactNode) {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={createQueryClient()}>{node}</QueryClientProvider>
    </MemoryRouter>,
  );
}

/** A key in the dialog's action row. Scoped to the footer because the card a
 *  preview falls back to offers a Download of its own. */
function key(name: string | RegExp): HTMLElement {
  const foot = document.querySelector(".alk-modal__foot");
  if (!foot) throw new Error("the dialog has no action row");
  return within(foot as HTMLElement).getByRole("button", { name });
}

function noKey(name: string | RegExp): boolean {
  const foot = document.querySelector(".alk-modal__foot");
  if (!foot) throw new Error("the dialog has no action row");
  return within(foot as HTMLElement).queryByRole("button", { name }) === null;
}

afterEach(() => {
  cleanup();
  unstubObjectUrls();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

beforeEach(() => {
  stubObjectUrls();
});

describe("the file preview modal", () => {
  it("draws the bytes through the shared renderer stack", async () => {
    const net = stubNetwork();
    show(<FilePreviewModal driveId={DRIVE} item={item()} open onClose={() => {}} />);

    const image = await screen.findByAltText("chart.png");
    expect(image.getAttribute("src")).toMatch(/^blob:/);
    // One mint for the bytes, and it asked for the single-use file grant.
    expect(net.mints()).toHaveLength(1);
    expect((JSON.parse(net.mintBodies[0]!) as { kind: string }).kind).toBe("file");
  });

  it("shows the first window of a 3 MiB text file with the line that offers the rest", async () => {
    const MIB = 1024 * 1024;
    const body = Array.from({ length: MIB / 16 }, (_, row) => `row ${String(row).padStart(11, "0")}\n`)
      .join("")
      .repeat(3);
    stubNetwork({ body, type: "text/plain" });
    const log = item({
      name: "server.log",
      nameDisplay: "server.log",
      file: { mime_type: "text/plain", size: body.length, content_hash: "sha256-test", scan_state: "clean" },
    } as Partial<Item>);
    show(<FilePreviewModal driveId={DRIVE} item={log} open onClose={() => {}} />);

    const bar = await screen.findByTestId("preview-more");
    expect(body.length).toBe(3 * MIB);
    expect(bar).toHaveTextContent("Showing 1 MB of 3.1 MB");
    expect(within(bar).getByRole("button", { name: "Show more" })).toBeInTheDocument();
    expect(screen.getByTestId("preview-text-body").textContent).toBe(body.slice(0, MIB));
  });

  it("shows a text file under one window whole, with no line under it", async () => {
    const net = stubNetwork({ body: "short\nfile\n", type: "text/plain" });
    const note = item({
      name: "note.txt",
      nameDisplay: "note.txt",
      file: { mime_type: "text/plain", size: 11, content_hash: "sha256-test", scan_state: "clean" },
    } as Partial<Item>);
    show(<FilePreviewModal driveId={DRIVE} item={note} open onClose={() => {}} />);

    await waitFor(() =>
      expect(screen.getByTestId("preview-text-body").textContent).toBe("short\nfile\n"),
    );
    expect(screen.queryByTestId("preview-more")).toBeNull();
    expect(net.urls.filter((url) => url === GRANT_URL)).toHaveLength(1);
  });

  it("draws a picture past the size that once kept it out of the preview", async () => {
    stubNetwork();
    const huge = item({
      file: {
        mime_type: "image/png",
        size: 900 * 1024 * 1024,
        content_hash: "sha256-test",
        scan_state: "clean",
      },
    } as Partial<Item>);
    show(<FilePreviewModal driveId={DRIVE} item={huge} open onClose={() => {}} />);

    const image = await screen.findByAltText("chart.png");
    expect(image.getAttribute("src")).toMatch(/^blob:/);
    expect(screen.queryByTestId("preview-fallback")).toBeNull();
  });

  it("never lets the grant that bought the bytes reach the page", async () => {
    const net = stubNetwork();
    const before = window.location.href;
    show(<FilePreviewModal driveId={DRIVE} item={item()} open onClose={() => {}} />);
    await screen.findByAltText("chart.png");

    // The grant is a bearer credential for as long as it lives: in the DOM it is
    // a link anyone can copy, and in the URL bar it is in history and in every
    // referrer.
    expect(net.urls).toContain(GRANT_URL);
    expect(document.body.innerHTML).not.toContain(GRANT_URL);
    expect(window.location.href).toBe(before);
    for (const anchor of Array.from(document.querySelectorAll("a"))) {
      expect(anchor.getAttribute("href") ?? "").not.toContain(GRANT_URL);
    }
  });

  it("refuses to fetch a file whose downloads are off", async () => {
    const net = stubNetwork();
    show(
      <FilePreviewModal
        driveId={DRIVE}
        item={item({ capabilities: { can_download: false } as Item["capabilities"] })}
        open
        onClose={() => {}}
      />,
    );

    expect(await screen.findByText(/downloads are off for this item/i)).toBeTruthy();
    expect(net.urls).toEqual([]);
    expect(noKey("Download")).toBe(true);
  });

  it("refuses to fetch a file in the trash", async () => {
    const net = stubNetwork();
    show(<FilePreviewModal driveId={DRIVE} item={item({ trashed: true })} open onClose={() => {}} />);

    expect(await screen.findByText("This is in the trash.")).toBeTruthy();
    expect(net.urls).toEqual([]);
  });

  it("draws the shared action table, in its order, and adds nothing of its own", async () => {
    stubNetwork();
    show(
      <FilePreviewModal
        driveId={DRIVE}
        item={item()}
        open
        onClose={() => {}}
        onOpenInFiles={() => {}}
        onShare={() => {}}
      />,
    );

    // Copy is offered once the bytes are on screen, so the row is read then.
    await waitFor(() => expect(key("Copy")).toBeTruthy());
    const foot = document.querySelector(".alk-modal__foot") as HTMLElement;
    const drawn = within(foot)
      .getAllByRole("button")
      .map((button) => button.textContent?.trim());
    // The table is the contract every surface that shows a file renders; a
    // button this modal invented would show up here as an extra.
    expect(drawn).toEqual(
      previewActionsFor({
        sealed: false,
        reachable: true,
        canOpenInFiles: true,
        canShare: true,
        canCopy: true,
      }).map((action) => action.label),
    );
  });

  it("offers the same action row for a type nothing draws", async () => {
    stubNetwork();
    show(
      <FilePreviewModal
        driveId={DRIVE}
        item={item({
          name: "archive.bin",
          nameDisplay: "archive.bin",
          file: { mime_type: "application/octet-stream", size: 90, content_hash: "sha256-test", scan_state: "clean" } as Item["file"],
        })}
        open
        onClose={() => {}}
      />,
    );

    expect(key("Download")).toBeTruthy();
    // Nothing draws it in the pane, but the bytes are still reachable: opening
    // it whole is the browser's job from there, and an action row that thinned
    // out with the kind made the same file look differently capable.
    expect(key("Open in new tab")).toBeTruthy();
    expect(key("Copy link")).toBeTruthy();
    expect(key("Close")).toBeTruthy();
  });

  it("sandboxes a framed page and leaves a PDF's own viewer alone", async () => {
    stubNetwork({ grantUrl: PAGE_URL, kind: "page" });
    const page = item({
      name: "report.html",
      nameDisplay: "report.html",
      file: { mime_type: "text/html", size: 900, content_hash: "sha256-test", scan_state: "clean" } as Item["file"],
    });
    const { unmount } = show(<FilePreviewModal driveId={DRIVE} item={page} open onClose={() => {}} />);

    const htmlFrame = await waitFor(() => {
      const frame = document.querySelector("iframe");
      expect(frame).not.toBeNull();
      return frame!;
    });
    // An empty sandbox withholds same-origin, scripts, forms and navigation at
    // once; an ABSENT attribute grants every one of them.
    // Its own scripts run; same-origin, forms, popups and navigation stay withheld.
    expect(htmlFrame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(htmlFrame.getAttribute("referrerpolicy")).toBe("no-referrer");
    unmount();

    stubNetwork({ grantUrl: PAGE_URL, kind: "page" });
    show(
      <FilePreviewModal
        driveId={DRIVE}
        item={item({
          name: "q3.pdf",
          nameDisplay: "q3.pdf",
          file: { mime_type: "application/pdf", size: 900, content_hash: "sha256-test", scan_state: "clean" } as Item["file"],
        })}
        open
        onClose={() => {}}
      />,
    );
    const pdfFrame = await waitFor(() => {
      const frame = document.querySelector("iframe");
      expect(frame).not.toBeNull();
      return frame!;
    });
    expect(pdfFrame.hasAttribute("sandbox")).toBe(false);
  });

  it("buys new bytes once when the version moves, and not before", async () => {
    const net = stubNetwork();
    const { rerender } = show(<FilePreviewModal driveId={DRIVE} item={item()} open onClose={() => {}} />);
    await screen.findByAltText("chart.png");
    expect(net.mints()).toHaveLength(1);

    // The same etag re-rendered is the same bytes.
    rerender(
      <MemoryRouter>
        <QueryClientProvider client={createQueryClient()}>
          <FilePreviewModal driveId={DRIVE} item={item()} open onClose={() => {}} />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    expect(net.mints()).toHaveLength(1);

    rerender(
      <MemoryRouter>
        <QueryClientProvider client={createQueryClient()}>
          <FilePreviewModal driveId={DRIVE} item={item({ etag: "etag-2" })} open onClose={() => {}} />
        </QueryClientProvider>
      </MemoryRouter>,
    );
    await waitFor(() => expect(net.mints()).toHaveLength(2));
  });

  it("names what the reader is looking at", async () => {
    stubNetwork();
    show(
      <FilePreviewModal
        driveId={DRIVE}
        item={item({
          lease: {
            holder: "Dana",
            machine: "box-7",
            live: true,
            status: filesLiveFact("live"),
            pending: 0,
            expires_at: new Date(Date.now() + 60_000).toISOString(),
          } as Item["lease"],
        })}
        open
        onClose={() => {}}
      />,
    );
    expect(screen.getByRole("heading", { name: "chart.png" })).toBeTruthy();
    expect(screen.getByText("PNG file")).toBeTruthy();
    expect(screen.getByText("Live. Dana is working on box-7")).toBeTruthy();
  });

  it("steps and closes from the keyboard", async () => {
    stubNetwork();
    const onPrev = vi.fn();
    const onNext = vi.fn();
    const onClose = vi.fn();
    const user = userEvent.setup();
    show(
      <FilePreviewModal
        driveId={DRIVE}
        item={item()}
        open
        onClose={onClose}
        onPrev={onPrev}
        onNext={onNext}
      />,
    );
    await screen.findByAltText("chart.png");

    await user.keyboard("{ArrowRight}");
    expect(onNext).toHaveBeenCalledTimes(1);
    await user.keyboard("{ArrowLeft}");
    expect(onPrev).toHaveBeenCalledTimes(1);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("offers only the keys the host wired", async () => {
    stubNetwork();
    const onShare = vi.fn();
    const onOpenInFiles = vi.fn();
    show(
      <FilePreviewModal
        driveId={DRIVE}
        item={item()}
        open
        onClose={() => {}}
        onShare={onShare}
        onOpenInFiles={onOpenInFiles}
      />,
    );
    const user = userEvent.setup();
    await user.click(key("Share…"));
    expect(onShare).toHaveBeenCalledTimes(1);
    await user.click(key("Open in Files"));
    expect(onOpenInFiles).toHaveBeenCalledTimes(1);

    cleanup();
    show(<FilePreviewModal driveId={DRIVE} item={item()} open onClose={() => {}} />);
    expect(noKey("Share…")).toBe(true);
    expect(noKey("Open in Files")).toBe(true);
  });

  it("says the link is copied only once the clipboard took it", async () => {
    stubNetwork();
    const writeText = vi.fn(async () => {
      throw new Error("denied");
    });
    const user = userEvent.setup({ writeToClipboard: false });
    // Defined on the real navigator, after the user session is set up: replacing
    // the whole object leaves userEvent without the one it reads.
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    show(<FilePreviewModal driveId={DRIVE} item={item()} open onClose={() => {}} />);

    await user.click(key("Copy link"));
    // A refused write must not report success — the reader would paste whatever
    // was on the clipboard before.
    expect(await screen.findByText(/couldn't copy/i)).toBeTruthy();
    expect(screen.queryByText("Link copied")).toBeNull();
  });

  it("copies the file's own link when the clipboard takes it", async () => {
    stubNetwork();
    const writeText = vi.fn(async () => {});
    const user = userEvent.setup({ writeToClipboard: false });
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    show(<FilePreviewModal driveId={DRIVE} item={item()} open onClose={() => {}} />);

    await user.click(key("Copy link"));
    expect(await screen.findByText("Link copied")).toBeTruthy();
    expect(writeText).toHaveBeenCalledWith(
      `${window.location.origin}/files/11111111-1111-4111-8111-111111111111`,
    );
  });

  it("names the org the link was copied in", async () => {
    stubNetwork();
    const writeText = vi.fn(async () => {});
    const user = userEvent.setup({ writeToClipboard: false });
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    enterOrg("org_a");
    try {
      show(<FilePreviewModal driveId={DRIVE} item={item()} open onClose={() => {}} />);
      await user.click(key("Copy link"));
      expect(await screen.findByText("Link copied")).toBeTruthy();
      expect(writeText).toHaveBeenCalledWith(
        `${window.location.origin}/files/11111111-1111-4111-8111-111111111111?org=org_a`,
      );
    } finally {
      forgetActiveOrg();
    }
  });
});

describe("stepping along the listing", () => {
  const png = item({ id: "a", name: "a.png", nameDisplay: "a.png" });
  const folder = item({ id: "f", kind: "folder", name: "sub", nameDisplay: "sub", file: null as Item["file"] });
  const bin = item({
    id: "b",
    name: "b.bin",
    nameDisplay: "b.bin",
    file: { mime_type: "application/octet-stream", size: 4, content_hash: "sha256-test", scan_state: "clean" } as Item["file"],
  });
  const md = item({
    id: "m",
    name: "m.md",
    nameDisplay: "m.md",
    file: { mime_type: "text/plain", size: 4, content_hash: "sha256-test", scan_state: "clean" } as Item["file"],
  });

  it("counts only the rows something can draw", () => {
    expect(isPreviewable(png)).toBe(true);
    expect(isPreviewable(md)).toBe(true);
    expect(isPreviewable(folder)).toBe(false);
    expect(isPreviewable(bin)).toBe(false);
  });

  function Harness({ rows, onSelect }: { rows: readonly Item[]; onSelect: (id: string) => void }) {
    const [returned, setReturned] = useState<string | null>(null);
    const linked = useLinkedSelection({ rows, onSelect, onReturnFocus: setReturned });
    return (
      <div>
        <span data-testid="open">{linked.item?.id ?? "none"}</span>
        <span data-testid="returned">{returned ?? "none"}</span>
        <button onClick={() => linked.openOn(rows[0]!)}>open first</button>
        <button disabled={!linked.onNext} onClick={() => linked.onNext?.()}>
          next
        </button>
        <button disabled={!linked.onPrev} onClick={() => linked.onPrev?.()}>
          prev
        </button>
        <button onClick={linked.close}>close</button>
      </div>
    );
  }

  it("skips the rows nothing draws and stops at the ends", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<Harness rows={[png, folder, bin, md]} onSelect={onSelect} />);

    expect(screen.getByTestId("open").textContent).toBe("none");
    await user.click(screen.getByRole("button", { name: "open first" }));
    expect(screen.getByTestId("open").textContent).toBe("a");
    // At the first previewable row there is nothing to step back to.
    expect(screen.getByRole("button", { name: "prev" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "next" }));
    // The folder and the undrawable file are stepped over, not stopped on.
    expect(screen.getByTestId("open").textContent).toBe("m");
    expect(onSelect).toHaveBeenLastCalledWith("m");
    expect(screen.getByRole("button", { name: "next" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "prev" }));
    expect(screen.getByTestId("open").textContent).toBe("a");
  });

  it("hands focus back to the row it was opened from", async () => {
    const user = userEvent.setup();
    render(<Harness rows={[png, md]} onSelect={() => {}} />);
    await user.click(screen.getByRole("button", { name: "open first" }));
    await user.click(screen.getByRole("button", { name: "close" }));

    expect(screen.getByTestId("open").textContent).toBe("none");
    expect(screen.getByTestId("returned").textContent).toBe("a");
  });

  it("follows the row it is open on when the listing refreshes", () => {
    const rows = [png, md];
    const { rerender } = render(<Harness rows={rows} onSelect={() => {}} />);
    act(() => {
      screen.getByRole("button", { name: "open first" }).click();
    });
    // A refetch answers new objects for the same ids; the modal must read the new
    // one (a new etag is new bytes), not the copy it captured on open.
    const refreshed = [{ ...png, etag: "etag-9" } as Item, md];
    rerender(<Harness rows={refreshed} onSelect={() => {}} />);
    expect(screen.getByTestId("open").textContent).toBe("a");
  });
});

describe("copying what the preview shows", () => {
  it("offers Copy for text and copies the text itself", async () => {
    // user-event stands in a clipboard of its own for the session.
    const user = userEvent.setup();
    stubNetwork({ body: "alpha beta", type: "text/plain" });
    show(
      <FilePreviewModal
        driveId={DRIVE}
        item={item({
          name: "notes.txt",
          nameDisplay: "notes.txt",
          file: { mime_type: "text/plain", size: 10, content_hash: "sha256-test", scan_state: "clean" } as Item["file"],
        })}
        open
        onClose={() => {}}
      />,
    );
    await screen.findByText("alpha beta");

    await user.click(key("Copy"));

    await screen.findByText("Copied");
    expect(await navigator.clipboard.readText()).toBe("alpha beta");
  });

  it("offers no Copy for a document only a frame can draw", async () => {
    stubNetwork({ grantUrl: PAGE_URL, kind: "page" });
    show(
      <FilePreviewModal
        driveId={DRIVE}
        item={item({
          name: "report.pdf",
          nameDisplay: "report.pdf",
          file: { mime_type: "application/pdf", size: 900, content_hash: "sha256-test", scan_state: "clean" } as Item["file"],
        })}
        open
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(key("Download")).toBeTruthy());

    expect(noKey("Copy")).toBe(true);
  });
});
