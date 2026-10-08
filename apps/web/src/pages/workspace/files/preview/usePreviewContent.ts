// The bytes behind a preview.
//
// A file's bytes are served from a different origin than the app, and reaching
// them needs a grant: a short-lived, single-use URL the server mints for one
// node after deciding the reader may export it. This hook is the whole chain
// from a row to something a renderer can draw — plan the file, ask for the right
// kind of grant, spend it, and hold the result only as long as the version it
// belongs to.
//
// Three properties are load-bearing:
//
//  * The read is anonymous (`credentials: "omit"`). The grant IS the
//    authorization; sending the session cookie to the content origin as well
//    would make a leaked page able to read as the reader.
//  * The served type must be the planned type. A renderer chosen for a CSV and
//    handed HTML would be drawing bytes nobody decided it should draw.
//  * A grant never reaches the page. What the renderer gets is an object URL or
//    the text — never the URL that buys the bytes, which is a bearer credential
//    for as long as it lives.
//
// A version change supersedes the read in flight: the old fetch is aborted and
// its object URL released before the new one starts, so a slow answer for an old
// version can never paint over a newer one.
//
// No file is refused for its size. Pictures, media and framed documents are the
// browser's to stream. Text is read in windows: a file larger than one window
// lands its first with a `Range` request, and each `more()` buys a grant for the
// next — a file grant is spent by the one read it was minted for.

import {
  planPreviewWith,
  previewKindFor,
  type PreviewContent,
  type PreviewFacts,
  type PreviewFileRef,
  type PreviewPlan,
  type PreviewStatus,
  type PreviewText,
} from "@alkera/ui";
import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "@/api/errors";
import type { ContentGrant, Item, MintContentGrant } from "@/api/files";
import { retryAfterHeaderMs } from "@/api/retry";
import {
  PREVIEW_GRANT_SPENT_MARGIN_MS,
  PREVIEW_TEXT_WINDOW_BYTES,
  RETRY_BACKOFF_FLOOR_MS,
} from "@/lib/limits";

import { filesErrorCopy } from "@/lib/files/errors";
import { fetchingLine, staleCopyLine } from "../liveRoot/liveCopy";
import { SOMEWHERE, leaseName } from "../useLeaseFacet";
import { grantFreshness, isLivePending, livePendingDetail } from "./promoteWire";
import { cutWindow, joinBytes, servedRange, type CutAt } from "./textWindow";
// The renderers this app adds to the shared registry, registered before any
// surface plans a preview through it.
import "./notebook/register";

/** A grant is re-minted once it is this close to expiry, so a frame that loads
 *  seconds after the decision still loads. */
const GRANT_SPENT_MARGIN_MS = PREVIEW_GRANT_SPENT_MARGIN_MS;

/** What the reader is told when the bytes are not the type the plan chose. It is
 *  the honest reading of the mismatch: the row's sniffed type is from the version
 *  the browser last read, so the file moved under it. */
export const TYPE_CHANGED = "The file changed. Reload to see the new version.";

/** What the reader is told when a later window of a text file could not be read.
 *  The text already on screen stays. */
export const MORE_FAILED = "The next part of the file could not be loaded";
/** What a read that did not arrive says, whatever the browser called it. */
export const NOT_LOADED = "The preview could not be loaded";

/** The window size a text file is read in. */
const TEXT_WINDOW_BYTES = PREVIEW_TEXT_WINDOW_BYTES;

export interface PreviewBytes {
  facts: PreviewFacts;
  /** Where the file lives, for a renderer this app registered. */
  file?: PreviewFileRef;
  /** What a renderer reloads on: the item's content tag. */
  version: string;
  plan: PreviewPlan;
  content: PreviewContent;
  status: PreviewStatus;
  error?: string;
  /** Why the bytes are still pending, when the drive said. */
  pendingReason?: string;
  /** Said over bytes the drive served from an older copy than the machine's. */
  notice?: string;
}

interface Loaded {
  content: PreviewContent;
  status: PreviewStatus;
  error?: string;
  pendingReason?: string;
  notice?: string;
}

const NOTHING: Loaded = { content: { kind: "none" }, status: "loading" };
/** Where a read of bytes still on the machine starts: the card that says they
 *  are being fetched, not a spinner, for as long as the drive takes to bring
 *  them. */
const WAITING: Loaded = { content: { kind: "none" }, status: "pending" };

/** The mime without its parameters — `text/html; charset=utf-8` is `text/html`. */
export function mimeEssence(mime: string): string {
  return mime.split(";")[0]?.trim().toLowerCase() ?? "";
}

/** What the SERVER knows about the row: the sniffed type, the stored name, the
 *  size, when the bytes last landed and the machine they come from.
 *
 *  The mime leads, but a writer that set no content type leaves an
 *  `application/octet-stream` the name can answer for. The stamp and the machine
 *  are what let the card tell a file still on its way from the box apart from one
 *  whose type simply has no renderer. */
export function previewFacts(item: Item | undefined): PreviewFacts {
  return {
    mime: item?.file?.mime_type ?? "application/octet-stream",
    name: item?.name ?? "",
    size: item?.file?.size ?? 0,
    synced: isSynced(item),
    machine: fetchingFrom(item),
  };
}

/** The machine a file's missing bytes are on their way from, by name, or null
 *  when nothing is bringing them: no lease, a lease not on the live plane, a
 *  machine the server says stopped answering, or bytes left behind when the
 *  lease ended. The card names the machine only when it is honest to say the
 *  bytes are being fetched. */
function fetchingFrom(item: Item | undefined): string | null {
  const lease = item?.lease ?? null;
  if (lease === null || lease.live !== true || lease.served === "offline") return null;
  if (item?.live?.content === "unsynced") return null;
  return leaseName(lease.machine_name, lease.machine, SOMEWHERE);
}

/** Whether asking for a row's missing bytes can bring them: the machine
 *  holding its folder writes on the live plane and still has them. The drive
 *  asks the machine for the file on the reader's behalf, and answers with the
 *  bytes once they land or with why they have not. Bytes left behind when a
 *  lease ended are not coming, and a row with no lease has nobody to ask. */
function bringable(item: Item | undefined): boolean {
  const lease = item?.lease ?? null;
  if (lease === null || lease.live !== true) return false;
  return item?.live?.content !== "unsynced";
}

/** Whether this row's bytes have been committed.
 *
 *  A content hash is written when a head version lands, so an empty one is the
 *  server saying there is no version yet — the shape a node adopted before its
 *  bytes arrive is served in. Size and timestamps cannot answer this: a node is
 *  created with `size 0` and an unset `mtime_ns`, and so is a real empty file. */
function isSynced(item: Item | undefined): boolean {
  if (!item || item.kind !== "file") return true;
  return (item.file?.content_hash ?? "") !== "";
}

/** The tag a preview reloads on: the row's `ctag`, which is its etag and —
 *  while the machine holding the folder has reported a rewrite — that report's
 *  time and sequence too. Keyed on the etag alone, a preview sat on the old
 *  bytes while the machine's disk moved on; keyed on the ctag, a reported
 *  rewrite asks for the new bytes and the drive brings them from the machine.
 *  A server that predates the ctag still reloads on the etag. */
function contentVersion(item: Item | undefined): string {
  const ctag = item?.ctag ?? "";
  return ctag !== "" ? ctag : (item?.etag ?? "");
}

/** A framed PDF keeps the browser's own viewer, which `sandbox` would disable;
 *  every other framed document is drawn under an opaque origin with nothing
 *  allowed.
 *
 *  It asks the same question the renderer was chosen by, not the raw mime: a PDF
 *  whose writer set no content type is framed as a PDF, and sandboxing it would
 *  blank the viewer and leave the reader waiting out the frame's timeout. */
function sandboxedFrame(facts: PreviewFacts): boolean {
  return previewKindFor(facts) !== "pdf";
}

/** The machine a lease names, whatever it is doing — the name the refusal
 *  falls back on when it names none itself. */
function holdingMachine(item: Item | undefined): string {
  const lease = item?.lease ?? null;
  return lease === null ? SOMEWHERE : leaseName(lease.machine_name, lease.machine, SOMEWHERE);
}

/** When to ask again for bytes that are still on their way, or null when the
 *  drive named no wait. Only the drive's own `Retry-After` arms a retry — the
 *  other way pending bytes are asked for again is the frame that says they
 *  landed — and a wait shorter than the portal's floor is taken at the floor. */
function pendingRetryMs(error: unknown): number | null {
  const header = (error as { retryAfter?: unknown } | null)?.retryAfter;
  const asked = retryAfterHeaderMs(typeof header === "string" ? header : null);
  return asked === null ? null : Math.max(asked, RETRY_BACKOFF_FLOOR_MS);
}

/** The line over bytes the drive served from its older copy, or undefined for
 *  a grant that serves the newest there is. */
function olderCopyNotice(grant: ContentGrant, item: Item | undefined): string | undefined {
  const fresh = grantFreshness(grant);
  return fresh.behind ? staleCopyLine(fresh.asOf, holdingMachine(item)) : undefined;
}

interface WindowSource {
  /** Where a window is cut so nothing drawn is half of something. */
  at: CutAt;
  /** The item's size — the total when an answer does not name one. */
  size: number;
  /** One ranged read of `start..end` (inclusive), or with no span the whole
   *  file; null once superseded. */
  fetchWindow(start?: number, end?: number): Promise<Response | null>;
  /** Put the text, as it now stands, in front of the renderer. */
  land(content: PreviewText, error?: string): void;
  /** Whether this read still belongs to the version on screen. */
  live(): boolean;
}

/**
 * A text file read one window at a time.
 *
 * It holds the text drawn so far, the bytes carried past the last cut, and how
 * many of the file's bytes have landed. `more()` is shared by every caller while
 * a window is in flight, so a scroll and a click asking at once buy one window.
 */
function textWindows(source: WindowSource): { first(bytes: Uint8Array, status: number, range: string | null): void } {
  let text = "";
  let carry: Uint8Array = new Uint8Array(0);
  let loaded = 0;
  let total = source.size;
  let inflight: Promise<void> | null = null;

  const snapshot = (): PreviewText => ({ kind: "text", text, loaded, total, more, whole });

  /** The entire file, in one unranged read — or the text itself once every
   *  window has landed. */
  const whole = async (): Promise<string> => {
    if (loaded >= total && carry.length === 0) return text;
    const response = await source.fetchWindow();
    if (response === null || !response.ok) throw new Error(MORE_FAILED);
    return new TextDecoder().decode(await response.arrayBuffer());
  };

  /** Take `bytes` as the window starting at `start`, answered with `status`. */
  const append = (bytes: Uint8Array, start: number, status: number, range: string | null): void => {
    const span = servedRange(range);
    if (status !== 206) {
      // A server that ignores `Range` answers the whole file: that is every byte
      // there is, so the window is the file and nothing is left to ask for.
      text = new TextDecoder().decode(bytes);
      carry = new Uint8Array(0);
      loaded = bytes.length;
      total = bytes.length;
      return;
    }
    if (span !== null && span.start !== start) throw new Error(MORE_FAILED);
    if (span?.total != null) total = span.total;
    const end = span === null ? start + bytes.length : span.end + 1;
    const cut = cutWindow(joinBytes(carry, bytes), { final: end >= total, at: source.at });
    // The first window drops a byte-order mark the way a whole read would; a
    // later one keeps U+FEFF, which is then text rather than a mark.
    text += new TextDecoder("utf-8", { ignoreBOM: start > 0 }).decode(cut.take);
    carry = cut.carry;
    loaded = end;
  };

  const more = (): Promise<void> => {
    if (!source.live() || loaded >= total) return Promise.resolve();
    if (inflight !== null) return inflight;
    const start = loaded;
    const end = Math.min(start + TEXT_WINDOW_BYTES, total) - 1;
    inflight = (async () => {
      try {
        const response = await source.fetchWindow(start, end);
        if (response === null || !source.live()) return;
        if (!response.ok) throw new Error(MORE_FAILED);
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (!source.live()) return;
        append(bytes, start, response.status, response.headers.get("content-range"));
        source.land(snapshot());
      } catch {
        if (!source.live()) return;
        // The text already drawn stays; the failure is said once, here, and the
        // next ask starts from the same byte.
        source.land(snapshot(), MORE_FAILED);
        throw new Error(MORE_FAILED);
      } finally {
        inflight = null;
      }
    })();
    return inflight;
  };

  return {
    first(bytes, status, range) {
      append(bytes, 0, status, range);
      source.land(snapshot());
    },
  };
}

function refusal(error: unknown, item: Item | undefined): Loaded {
  const status = (error as { status?: number } | null)?.status;
  if (status === 404) return { content: { kind: "none" }, status: "gone" };
  if (isLivePending(error)) {
    const detail = livePendingDetail(error);
    if (detail === null) return { content: { kind: "none" }, status: "pending" };
    return {
      content: { kind: "none" },
      status: "pending",
      // The refusal names the holder by the lease's own word for it — for a
      // registered box, its allocation id. An id is never shown: the name the
      // listing resolved for the same lease stands in for it.
      pendingReason: fetchingLine(
        detail.outcome,
        leaseName(null, detail.holder, holdingMachine(item)),
      ),
    };
  }
  // The browser's own words for a read that never arrived ("Failed to fetch",
  // "Load failed", "NetworkError when attempting to fetch resource") are a
  // developer's, and a reader shown one cannot tell a dead network from a
  // broken file. A refusal the server gave is said in the Files copy table's
  // sentence; anything else is the one sentence a failed read already has.
  if (error instanceof ApiError) {
    return { content: { kind: "none" }, status: "error", error: filesErrorCopy(error).title };
  }
  return { content: { kind: "none" }, status: "error", error: NOT_LOADED };
}

/**
 * The bytes for one row, in the shape its renderer asked for.
 *
 * `mint` is the host's door to the grant route — passed in rather than called
 * here so the same hook serves a tab beside a chat, the modal on the Files page
 * and the page a share link lands on.
 *
 * `rendererId` asks for the bytes one renderer in particular needs, rather than
 * the one the type would get: a tab offering an SVG as its source and as its
 * drawing names the framed renderer for the second, so the drawing is fetched as
 * a page on the content origin and never enters this one.
 */
export function usePreviewContent(
  item: Item | undefined,
  mint: MintContentGrant,
  rendererId?: string,
): PreviewBytes {
  const mime = item?.file?.mime_type ?? "application/octet-stream";
  const name = item?.name ?? "";
  const size = item?.file?.size ?? 0;
  const synced = isSynced(item);
  const machine = fetchingFrom(item);
  const askable = !synced && bringable(item);
  const version = contentVersion(item);
  const itemId = item?.id;
  const driveId = item?.driveId;

  const facts = useMemo<PreviewFacts>(
    () => ({ mime, name, size, synced, machine }),
    [mime, name, size, synced, machine],
  );
  // A row whose bytes are still on the machine is planned twice: as the card it
  // is until they arrive, and as the file it will be, which is what says which
  // grant to ask for — the ask is what brings the bytes.
  const plan = useMemo(() => planPreviewWith(facts, rendererId), [facts, rendererId]);
  const arriving = useMemo(
    () => (askable ? planPreviewWith({ ...facts, synced: true }, rendererId) : plan),
    [askable, facts, plan, rendererId],
  );
  const [loaded, setLoaded] = useState<Loaded>(NOTHING);

  // One page grant per node, re-used across versions: the page route serves the
  // node's current bytes, so a new version needs a reload, not a new grant.
  // A grant that served an older copy is not re-used: the next version mints
  // again, which is what asks the machine for the newer bytes and says whether
  // they came.
  const pageGrant = useRef<{
    itemId: string;
    url: string;
    expiresAt: number;
    behind: boolean;
  } | null>(null);
  // The row as last rendered, for naming the machine in a refusal without
  // making every change to the row's lease facet a new read.
  const current = useRef(item);
  current.current = item;

  useEffect(() => {
    if (!itemId || !driveId) {
      setLoaded(NOTHING);
      return;
    }
    if (arriving.need === "none") {
      setLoaded({ content: { kind: "none" }, status: "ready" });
      return;
    }

    const controller = new AbortController();
    let live = true;
    let created: string | null = null;
    let again: ReturnType<typeof setTimeout> | null = null;
    setLoaded(askable ? WAITING : NOTHING);

    const land = (next: Loaded): void => {
      if (live) setLoaded(next);
    };

    // One read of the bytes. A pending answer that names a wait runs it again
    // after that wait, inside this same version: what is on screen stays the
    // pending card rather than flashing back to "loading" every retry.
    const read = async (): Promise<void> => {
      try {
        if (arriving.need === "frame") {
          const held = pageGrant.current;
          const usable =
            held &&
            held.itemId === itemId &&
            !held.behind &&
            held.expiresAt - Date.now() > GRANT_SPENT_MARGIN_MS
              ? held.url
              : null;
          let url = usable;
          let notice: string | undefined;
          if (!url) {
            const grant = await mint({ driveId, itemId, kind: "page" });
            if (!live) return;
            url = grant.url;
            notice = olderCopyNotice(grant, current.current);
            pageGrant.current = {
              itemId,
              url: grant.url,
              expiresAt: Date.parse(grant.expiresAt),
              behind: notice !== undefined,
            };
          }
          land({
            content: {
              kind: "frame",
              url,
              sandboxed: sandboxedFrame(facts),
              scripts: previewKindFor(facts) === "html",
              title: `Preview of ${name} (sandboxed)`,
            },
            status: "ready",
            notice,
          });
          return;
        }

        // A single-use grant is spent by the read it was minted for, so every
        // version buys its own.
        const grant = await mint({ driveId, itemId, kind: "file" });
        if (!live) return;
        const notice = olderCopyNotice(grant, current.current);
        // A text file past one window asks for its first window only; one under
        // it is read whole, exactly as before windows existed.
        const windowed = arriving.need === "text" && size > TEXT_WINDOW_BYTES;
        const response = await fetch(grant.url, {
          mode: "cors",
          credentials: "omit",
          cache: "no-store",
          signal: controller.signal,
          ...(windowed ? { headers: { Range: `bytes=0-${TEXT_WINDOW_BYTES - 1}` } } : {}),
        });
        if (!live) return;
        if (response.status === 404) {
          land({ content: { kind: "none" }, status: "gone" });
          return;
        }
        if (!response.ok) {
          land({ content: { kind: "none" }, status: "error", error: NOT_LOADED });
          return;
        }
        if (mimeEssence(response.headers.get("content-type") ?? "") !== mimeEssence(mime)) {
          land({ content: { kind: "none" }, status: "error", error: TYPE_CHANGED });
          return;
        }
        if (arriving.need === "text" && windowed) {
          const first = new Uint8Array(await response.arrayBuffer());
          if (!live) return;
          const windows = textWindows({
            at: previewKindFor(facts) === "markdown" ? "block" : "line",
            size,
            fetchWindow: async (start, end) => {
              // Each window is its own single-use read.
              const windowGrant = await mint({ driveId, itemId, kind: "file" });
              if (!live) return null;
              return fetch(windowGrant.url, {
                mode: "cors",
                credentials: "omit",
                cache: "no-store",
                signal: controller.signal,
                ...(start === undefined || end === undefined
                  ? {}
                  : { headers: { Range: `bytes=${start}-${end}` } }),
              });
            },
            land: (content, error) =>
              land({ content, status: "ready", notice, ...(error ? { error } : {}) }),
            live: () => live && !controller.signal.aborted,
          });
          windows.first(first, response.status, response.headers.get("content-range"));
          return;
        }
        if (arriving.need === "text") {
          const text = await response.text();
          if (!live) return;
          land({ content: { kind: "text", text }, status: "ready", notice });
          return;
        }
        const blob = await response.blob();
        if (!live) return;
        created = URL.createObjectURL(blob);
        land({ content: { kind: "blob", url: created, mime }, status: "ready", notice });
      } catch (error) {
        if (!live || controller.signal.aborted) return;
        land(refusal(error, current.current));
        const wait = isLivePending(error) ? pendingRetryMs(error) : null;
        if (wait !== null) again = setTimeout(() => void read(), wait);
      }
    };
    void read();

    return () => {
      live = false;
      controller.abort();
      if (again !== null) clearTimeout(again);
      if (created) URL.revokeObjectURL(created);
    };
  }, [arriving, askable, driveId, facts, itemId, mime, mint, name, size, version]);

  // Bytes that arrived for a row the listing still calls unsynced are drawn as
  // the file they are; the row catches up on the frame that says they landed.
  const drawn = askable && loaded.status === "ready" && loaded.content.kind !== "none";
  const shown = useMemo<PreviewFacts>(
    () => (drawn ? { ...facts, synced: true } : facts),
    [drawn, facts],
  );
  const file = useMemo<PreviewFileRef | undefined>(
    () => (driveId && itemId ? { driveId, itemId } : undefined),
    [driveId, itemId],
  );
  return { facts: shown, ...(file ? { file } : {}), version, plan: drawn ? arriving : plan, ...loaded };
}
