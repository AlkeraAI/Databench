// The tray's state: what a drop turned into, and what it is doing now.
//
// The order a directory drop is executed in is not an implementation detail, it
// is the contract:
//
//   1. expand the entries (directories recorded even when empty);
//   2. ONE `POST …/tree` for the whole skeleton, so a 200-folder drop is one
//      request and the empty folders exist before anything lands in them;
//   3. one upload session per file, into the folder id the tree call handed
//      back;
//   4. one `PATCH …/items/{id}` per finished file carrying `attrs.mtime`
//      from `File.lastModified`, because the upload routes have nowhere to put
//      the laptop's modification time and a tree that arrives all stamped
//      "now" is not the tree the user dropped.
//
// Sidecars never enter that pipeline at all — they are counted and dropped
// during (1), so no request ever mentions one.
//
// The node id and the dedup verdict both come from the `complete` response,
// which the upload client returns to its injected `fetch`. Observing it there
// keeps the client's contract untouched and still gives the tray the two facts
// only the server knows: which node this became, and whether the bytes were
// already in Files. A server that queues the commit answers 202 and an
// operation instead — which names neither — so step (4) waits on a shape the
// operation does not carry, and the client's poll is what decides whether the
// file landed at all.

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { useMutation, useQueryClient, type InfiniteData, type QueryClient } from "@tanstack/react-query";

import { blake3HexOfBlob } from "@/api/blake3";
import { api, request } from "@/api/client";
import { ApiError } from "@/api/errors";
import {
  type ChildrenPage,
  type Item,
  type Operation,
  useCreateTree,
  useMoveItem,
  withIdempotency,
} from "@/api/files";
import {
  TERMINAL_UPLOAD_STATES,
  UploadClient,
  uploadConcurrency,
  type ConflictAnswer,
  type UploadClientOptions,
  type UploadHandle,
  type UploadProgress,
  type ResumableUpload,
} from "@/api/filesUpload";
import { keys } from "@/api/keys";
import type { AccountScope } from "@/lib/accountScope";
import { FILES_UPLOAD_REFRESH_EVERY, FILES_UPLOAD_REFRESH_MS } from "@/lib/limits";

import { planNames, type ContentKey } from "./batchNames";
import { runPool, UPLOAD_CONCURRENCY } from "./dragDrop";
import {
  dropVerdict,
  expandDrop,
  indexTree,
  readMovePayload,
  type DropItem,
  type DropTarget,
} from "./dropHandlers";
import { filesErrorCopy, QUOTA_STATUS } from "@/lib/files/errors";

/** One dropped batch, as the tray reports it while it runs. */
export interface BatchProgress {
  /** Files the drop expanded to, sidecars excluded. */
  total: number;
  /** Files that have settled — uploaded, already in Files, failed or cancelled. */
  done: number;
  failed: number;
  /** The file most recently started, relative to the drop. */
  current: string | null;
  /** Set when the server said no more will fit or may be added: the files not
   *  yet started stay on the laptop, and this is why. */
  stopped: string | null;
}

/** The refusals that end a batch rather than one file: nothing later in the same
 *  drop can succeed where this one did not. 403 is a permission the whole drop
 *  lacks; 507 is the storage quota (`files.quota_bytes` and the per-person
 *  `files.user_quota_bytes` alike, and the storage safety mode). */
export function stopsBatch(error: ApiError | undefined): boolean {
  return error?.status === 403 || error?.status === QUOTA_STATUS;
}

/** One row of the tray. */
export interface UploadRow {
  uploadId: string;
  name: string;
  sent: number;
  total: number;
  partsDone: number;
  partsTotal: number;
  state: UploadProgress["state"];
  error?: ApiError;
  /** What a name collision was settled as, once one was answered. The session
   *  API's own answer to a taken name is a refusal, so every other outcome is
   *  something the person chose — and a file that quietly became "report (2)"
   *  or a new version of somebody else's is the outcome they most need told. */
  outcome?: string;
}

/** The refusal the server gives a name that starts or ends with whitespace. */
export const SURROUNDING_SPACE = "files.invalid_name.surrounding_space";

/** Every refusal of a name itself — too long, reserved, a control character,
 *  spaces at the ends. The server decides these from the name alone, so the
 *  same name sent again is refused again. */
export const NAME_REFUSED = "files.invalid_name.";

/**
 * The name a failed upload can be sent under again.
 *
 * `undefined` when the row failed for a reason a retry can outlast (a dropped
 * connection, a busy server): its retry sends the name it has. A string when
 * only the name was wrong and the product can mend it: a file named
 * " report.csv " is refused before a byte moves, and the one retry that can
 * land is the name with the whitespace taken off the ends. `null` when no retry
 * can land it: a name the server refuses for what it IS — 300 characters, `..`,
 * a tab inside it — or one that is nothing but whitespace. Offering Retry there
 * sent the same name into the same refusal.
 */
export function retryName(row: Pick<UploadRow, "name" | "state" | "error">): string | null | undefined {
  if (row.state !== "failed") return undefined;
  const code = row.error?.code;
  if (code === SURROUNDING_SPACE) {
    const trimmed = row.name.trim();
    return trimmed === "" ? null : trimmed;
  }
  if (typeof code === "string" && code.startsWith(NAME_REFUSED)) return null;
  return undefined;
}

/** The same bytes under another name: a `File`'s name is fixed, and every step
 *  of the session (the open, the resume record, the row) reads it from there. */
function renamedFile(file: File, name: string): File {
  return new File([file], name, { type: file.type, lastModified: file.lastModified });
}

type ConflictChoiceName = "keep-both" | "replace" | "skip";

/** What each answer to a name collision did, said as the outcome rather than as
 *  the choice: "rename" is the server's word for it, and a file that quietly
 *  became `report (2).pdf` is the outcome a person most needs told. */
const OUTCOMES: Record<ConflictChoiceName, string> = {
  "keep-both": "Kept both. This one was saved under a new name.",
  replace: "Replaced: a new version of the file.",
  skip: "Skipped. Nothing was uploaded.",
};

/** How long a tray whose every upload finished cleanly stays on screen before it
 *  clears itself: long enough to read "Uploaded", short enough that it is not
 *  furniture. A failure never clears itself — it waits for Dismiss. */
export const DONE_LINGER_MS = 2500;

/** The states an upload does not leave — the client's own set, so the tray and
 *  the controls that refuse to act on a settled row cannot drift apart. */
export const TERMINAL_STATES: ReadonlySet<UploadRow["state"]> = TERMINAL_UPLOAD_STATES;

/** What a person may answer a name collision with. All three are real: `replace`
 *  writes a new version onto the file already holding the name, which the
 *  session API has honoured since the `replace` commit behaviour landed. */
export type { ConflictAnswer };

export interface ConflictPrompt {
  /** WHICH upload is asking. A name stopped being an identity the moment a drop
   *  could carry two files with one: an answer routed by name lands on
   *  whichever of them settled first, and the person is told their other file
   *  was renamed. */
  uploadId: string;
  /** The colliding file's name — what the person answering recognises. */
  name: string;
  /** The folder the upload was going into. Replace has to fence against the
   *  version of the file already there, and this is the only place it can be
   *  looked up from. */
  parentId: string;
}

export interface ConflictsState {
  /** Oldest first: one prompt is answered at a time. */
  queue: ConflictPrompt[];
}

export type ConflictsAction =
  | { type: "ask"; uploadId: string; name: string; parentId: string }
  | { type: "answered"; uploadId: string };

/** Pure so the queue's behaviour is testable without a network: a second
 *  question about the same upload never queues twice, and answering one only
 *  removes that one — including when a second upload of the same name is
 *  asking its own question beside it. */
export function conflictsReducer(state: ConflictsState, action: ConflictsAction): ConflictsState {
  switch (action.type) {
    case "ask":
      if (state.queue.some((prompt) => prompt.uploadId === action.uploadId)) return state;
      return {
        queue: [
          ...state.queue,
          { uploadId: action.uploadId, name: action.name, parentId: action.parentId },
        ],
      };
    case "answered":
      return { queue: state.queue.filter((prompt) => prompt.uploadId !== action.uploadId) };
  }
}

/**
 * Whether the tray has nothing left to show and may clear itself.
 *
 * Four things hold it open, and every one of them is something the person has
 * not been told yet:
 *
 *  - a row that is still moving;
 *  - a row that failed — the sentence on it is the only place the person learns
 *    the file is not in Files, so it waits for Dismiss;
 *  - a question nobody has answered. A queued commit decides a taken name
 *    seconds or tens of seconds after the bytes are up, and the linger the
 *    previous file armed would otherwise take the question's own row away
 *    while it is still being asked;
 *  - a drop the tray has accepted and not finished. The linger belongs to the
 *    batch that armed it; firing it over a later one erases a running drop's
 *    progress and leaves an empty tray over an upload that is still going.
 */
export function traySettled(
  rows: readonly UploadRow[],
  batch: BatchProgress | null,
  conflicts: readonly ConflictPrompt[],
): boolean {
  if (rows.length === 0 || conflicts.length > 0) return false;
  if (batch !== null && batch.done < batch.total) return false;
  return rows.every((row) => TERMINAL_STATES.has(row.state) && row.state !== "failed");
}

/**
 * Put a file that has just landed into the cached listings of its folder, ahead
 * of the refetch that brings its full row.
 *
 * The row in the tray said "Uploaded" while the folder above it went on
 * without the file for as long as the listing's refetch took, which is as long
 * as the server's pause on reads when it is limiting them: half a minute in
 * which the file was in Files and nowhere on screen. The row put here carries
 * what the upload knows (the node, its name, size and type, and the folder's
 * own say on what the reader may do), and the refetch that follows replaces
 * it with the server's.
 *
 * Only a listing that could hold it gets it: one already holding the node (a
 * new version of a file that was there) is left alone, and so is a filtered
 * one, which might not list it at all.
 */
export function seedLandedFile(
  qc: QueryClient,
  driveId: string,
  parentId: string,
  landed: { nodeId: string; name: string; size: number; mimeType: string },
): void {
  const parent = qc.getQueryData<Item>(keys.files.item(parentId));
  const row = {
    id: landed.nodeId,
    driveId,
    kind: "file",
    name: landed.name,
    nameDisplay: landed.name,
    parentId,
    file: { mime_type: landed.mimeType || "application/octet-stream", size: landed.size },
    ...(parent?.capabilities ? { capabilities: parent.capabilities } : {}),
  } as unknown as Item;
  const listings = qc.getQueriesData<InfiniteData<ChildrenPage>>({
    queryKey: keys.files.childrenOf(driveId, parentId),
  });
  for (const [queryKey, data] of listings) {
    const params = queryKey[4] as Readonly<Record<string, string | undefined>> | undefined;
    const filtered = Object.entries(params ?? {}).some(
      ([name, value]) => name !== "orderBy" && value !== undefined,
    );
    if (filtered || !data || data.pages.length === 0) continue;
    if (data.pages.some((page) => page.value.some((item) => item.id === landed.nodeId))) continue;
    const [first, ...rest] = data.pages;
    qc.setQueryData<InfiniteData<ChildrenPage>>(queryKey, {
      ...data,
      pages: [{ ...first!, value: [...first!.value, row] }, ...rest],
    });
  }
}

/** What the server said when it committed one upload. */
interface CommitFacts {
  itemId: string;
  /** Absent when the commit was queued: the operation names the node it
   *  created but not the version, so the stamp goes on unconditionally rather
   *  than fencing against a version the page never read. */
  etag?: string;
  /** True when the head already held these exact bytes: nothing was written,
   *  and the tray counts it as "already in Files". */
  unchanged: boolean;
}

function readCommitFacts(body: unknown): CommitFacts | null {
  if (!body || typeof body !== "object") return null;
  const shape = body as { item?: { id?: unknown; etag?: unknown }; unchanged?: unknown };
  const item = shape.item;
  if (!item || typeof item.id !== "string") return null;
  return {
    itemId: item.id,
    etag: typeof item.etag === "string" ? item.etag : "",
    unchanged: shape.unchanged === true,
  };
}

const COMPLETE = /\/api\/v1\/files\/uploads\/([^/]+)\/complete$/;

/** A row for a file whose upload threw somewhere the pipeline does not report
 *  through, so that failure still gets a row and does not strand the files
 *  behind it in the same drop. Its own name and what went wrong are the
 *  two things the person needs; the id is the file's, so a retry replaces the
 *  row rather than adding a second one. */
function unexpectedFailure(file: File, parentId: string, error: unknown): UploadProgress {
  return {
    uploadId: `failed:${parentId}:${file.name}:${file.size}:${file.lastModified}`,
    name: file.name,
    sent: 0,
    total: file.size,
    partsDone: 0,
    partsTotal: 0,
    state: "failed",
    error:
      error instanceof ApiError
        ? error
        : new ApiError(0, {
            code: "files.error",
            message: "This upload stopped partway. Check whether the file is in Files before sending it again.",
          }),
  };
}

/** How far a Replace will page a folder looking for the name it collided with.
 *  Bounded on purpose: the answer has to arrive while a person is waiting, and
 *  not finding it is a refusal with a sentence, never a silent overwrite. */
export const REPLACE_LOOKUP_PAGES = 10;

/**
 * The version of the file already holding this name, or `null`.
 *
 * The 409 the session API answers a collision with carries only a code, so the
 * node it collided with has to be found the way the listing finds anything —
 * by paging the folder. `null` means the caller could not be sure which node it
 * would be replacing, which is refused rather than guessed: a `replace` that
 * fences against nothing overwrites whoever wrote in between.
 */
export async function existingEtag(
  driveId: string,
  parentId: string,
  name: string,
): Promise<string | null> {
  let marker: string | undefined;
  for (let page = 0; page < REPLACE_LOOKUP_PAGES; page += 1) {
    const answer = await request(
      api.GET("/api/v1/files/drives/{drive_id}/items/{item_id}/children", {
        params: {
          path: { drive_id: driveId, item_id: parentId },
          query: { limit: 500, ...(marker ? { marker } : {}) },
        },
      }),
    );
    for (const row of answer.value) {
      if (row.name === name || row.nameDisplay === name) {
        return typeof row.etag === "string" && row.etag !== "" ? row.etag : null;
      }
    }
    marker = answer.nextMarker ?? undefined;
    if (marker === undefined) return null;
  }
  return null;
}

/** How many files a drop keeps in flight: the queue's own small default, or
 *  fewer when the server's last part answer said this person's share of the
 *  upload budget is smaller than that. Read live, so a share that shrinks while
 *  a drop runs holds the extra lanes back rather than sending into a shed. */
export function lanesAllowed(): number {
  const share = uploadConcurrency.hint;
  return share === null ? UPLOAD_CONCURRENCY : Math.max(1, Math.min(UPLOAD_CONCURRENCY, share));
}

/** How long the folder listing waits for the files landing beside one another
 *  before it refetches once for all of them. */
export const FOLDER_REFRESH_MS = FILES_UPLOAD_REFRESH_MS;

/** Files that may land inside one of those windows before the listing refetches
 *  anyway, whatever the timer is doing. */
export const FOLDER_REFRESH_EVERY = FILES_UPLOAD_REFRESH_EVERY;

/** How long a finished file waits for the others finishing beside it before
 *  their versions are read back together. Shorter than the poll that settles
 *  a commit, so the wait is never the slow step of a drop. */
export const ETAG_COALESCE_MS = 50;

/** The most ids one read-back names: the lookup route's own cap. */
export const ETAG_LOOKUP_IDS = 100;

interface EtagWaiter {
  itemId: string;
  resolve: (etag: string | null) => void;
}

/**
 * The versions the finished files are at now, read back a batch at a time.
 *
 * A queued commit answers an operation, and the operation names the node it
 * created but not the version it left it at. The items route refuses any
 * mutation that does not name the version it is changing, so the stamp that
 * follows an upload has nothing to fence against until the node is read back.
 * Reading every file back one GET at a time made a five-hundred-file drop five
 * hundred reads, and the per-person read budget refused the tail of it; the
 * lanes finish files within milliseconds of each other, so the ids are pooled
 * for a moment and asked for in one `POST …/items/lookup` per hundred.
 *
 * `null` means the stamp is skipped rather than sent blind -- an unconditional
 * PATCH is a 428 the person would never see -- and a read the server refuses
 * answers `null` for the whole batch rather than throwing into a lane.
 */
export class EtagLookup {
  private readonly waiting = new Map<string, EtagWaiter[]>();
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly coalesceMs: number = ETAG_COALESCE_MS) {}

  etag(driveId: string, itemId: string): Promise<string | null> {
    return new Promise((resolve) => {
      const queue = this.waiting.get(driveId) ?? [];
      queue.push({ itemId, resolve });
      this.waiting.set(driveId, queue);
      if (queue.length >= ETAG_LOOKUP_IDS) {
        void this.flush(driveId);
        return;
      }
      this.timer ??= setTimeout(() => {
        this.timer = null;
        for (const drive of [...this.waiting.keys()]) void this.flush(drive);
      }, this.coalesceMs);
    });
  }

  private async flush(driveId: string): Promise<void> {
    const batch = (this.waiting.get(driveId) ?? []).splice(0, ETAG_LOOKUP_IDS);
    if (batch.length === 0) return;
    const ids = [...new Set(batch.map((waiter) => waiter.itemId))];
    let found = new Map<string, string | null>();
    try {
      const page = await request(
        api.POST("/api/v1/files/drives/{drive_id}/items/lookup", {
          params: { path: { drive_id: driveId } },
          body: { ids },
        }),
      );
      found = new Map(
        page.value.map((item) => [
          item.id,
          typeof item.etag === "string" && item.etag !== "" ? item.etag : null,
        ]),
      );
    } catch {
      // Answered below as "unknown" for every id in the batch.
    }
    for (const waiter of batch) waiter.resolve(found.get(waiter.itemId) ?? null);
    if ((this.waiting.get(driveId)?.length ?? 0) > 0) void this.flush(driveId);
  }
}

/** One pool for the page: every tray and every drop shares the batches. */
const etags = new EtagLookup();

/** The version the node is at now, or `null` when it cannot be read. */
export function itemEtag(driveId: string, itemId: string): Promise<string | null> {
  return etags.etag(driveId, itemId);
}

/** Said on the row of a file that IS in Files but kept the server's clock: the
 *  bytes landed, so calling it a failure would be a lie, and saying nothing is
 *  how a wrong modification date goes unexplained. */
export const MTIME_NOT_KEPT = "Uploaded, but the file's modified time could not be kept.";

interface SetMtimeVars {
  driveId: string;
  itemId: string;
  etag?: string;
  mtimeNs: number;
}

/** Stamp the laptop's modification time onto the node the upload created.
 *  Declared through the mutation cache policy like every other Files write —
 *  nothing here invalidates by hand. */
function useSetMtime() {
  // The PATCH route answers an item for a small change and an operation for one
  // it had to queue, so the mutation is typed for both rather than narrowed by a
  // cast the server can contradict.
  return useMutation<Item | Operation, ApiError, SetMtimeVars>({
    // Invalidating here fanned every finished file out into a refetch of every
    // Files query -- five hundred files, five hundred rounds of listing reads,
    // which is what the read budget refused. The drop refreshes the folders it
    // landed in itself, coalesced across the files finishing together, and the
    // whole family once when the batch is over.
    meta: { invalidates: "none" },
    mutationFn: (vars) =>
      request(
        api.PATCH("/api/v1/files/drives/{drive_id}/items/{item_id}", {
          params: {
            path: { drive_id: vars.driveId, item_id: vars.itemId },
            query: { conflict_behavior: "fail" },
          },
          headers: withIdempotency(vars),
          body: { attrs: { mtime: vars.mtimeNs } },
        }),
        "could not set the modified time",
      ),
  });
}

/** The `DataTransfer` fields a drop reads. */
export interface DropTransfer {
  items?: readonly DropItem[] | null;
  getData(type: string): string;
}

export interface UseUploadsOptions {
  driveId: string;
  /** Injected so a test drives the real client over a stubbed `fetch` with a
   *  pinned digest and its own storage. The network path is unchanged. */
  clientOptions?: Pick<UploadClientOptions, "digest" | "storage" | "fetchImpl">;
  /** Who is uploading and in which org; unfinished uploads are remembered under
   *  it. Without one nothing is remembered and nothing is offered for resume. */
  account?: AccountScope | null;
  /** Test seam: how long a cleanly finished tray lingers before clearing itself. */
  doneLingerMs?: number;
}

export interface UseUploads {
  rows: UploadRow[];
  /** Sidecars folded away across every drop so far. */
  skippedSidecars: number;
  /** Copies not uploaded because a file of the same name in the same drop held
   *  the same bytes. */
  identicalCopies: number;
  /** How many finished uploads the server already had, and how many finished. */
  alreadyInFiles: number;
  finished: number;
  /** Set when the last drop was refused; cleared by the next accepted one. */
  refusal: string | null;
  /** The batch the last drop turned into, while it runs and until dismissed. */
  batch: BatchProgress | null;
  conflicts: ConflictPrompt[];
  /** Sessions a previous page load left open, offered for re-drop. */
  resumable: ResumableUpload[];
  onDrop(target: DropTarget | null, transfer: DropTransfer): Promise<void>;
  /** Send one failed file again, into the folder it was going to. */
  retry(rowId: string): Promise<void>;
  /** Send every failed file again, and only those. */
  retryFailed(): Promise<void>;
  pause(uploadId: string): void;
  resume(uploadId: string): void;
  cancel(uploadId: string): void;
  /** Settle one upload's name collision. Keyed by the upload, never by the
   *  name: a drop can carry two files that spell it the same way. */
  answerConflict(uploadId: string, answer: ConflictAnswer): void;
  /** Clear every finished row and the counts that went with them. */
  dismiss(): void;
}

export function useUploads(options: UseUploadsOptions): UseUploads {
  const { driveId, clientOptions, doneLingerMs = DONE_LINGER_MS } = options;
  const [rows, setRows] = useState<Record<string, UploadRow>>({});
  const [skippedSidecars, setSkippedSidecars] = useState(0);
  const [identicalCopies, setIdenticalCopies] = useState(0);
  const [alreadyInFiles, setAlreadyInFiles] = useState(0);
  const [finished, setFinished] = useState(0);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [batch, setBatch] = useState<BatchProgress | null>(null);
  const [conflicts, dispatchConflict] = useReducer(conflictsReducer, { queue: [] });
  const qc = useQueryClient();

  const createTree = useCreateTree();
  const moveItem = useMoveItem();
  const setMtime = useSetMtime();

  const handles = useRef(new Map<string, UploadHandle>());
  // Both halves of the question, because a question nobody will ever answer is
  // an upload that waits forever: the client is inside `await onConflict(...)`,
  // and if the tray goes away with a prompt open, only a rejection ends it.
  const answers = useRef(
    new Map<string, { resolve: (answer: ConflictAnswer) => void; reject: (reason: Error) => void }>(),
  );
  // Where each in-flight upload is going, by the id of the row it produced.
  // Replace needs the folder to fence against the version already there, and
  // the name to find it — and two files of one name in two folders would
  // answer each other's question if the id were not the key.
  const destinations = useRef(new Map<string, { parentId: string; name: string }>());
  const commits = useRef(new Map<string, CommitFacts>());
  // Every file this tray has attempted, by the id of the row it produced: a
  // failed row is only retryable if the bytes and the destination are still
  // here, and the row itself carries neither.
  const attempts = useRef(new Map<string, { file: File; parentId: string }>());
  // What each upload's name collision was settled as, by the id of the row it
  // belongs to — read once that upload settles, so the sentence lands on the
  // file it describes rather than on whichever one finished first.
  const outcomes = useRef(new Map<string, string>());
  // The rows as they stand now, for the callbacks that act on "every failure"
  // without being rebuilt on each byte of progress.
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

  // Unfinished uploads are remembered under the signed-in person and the org
  // they are in, so a resume never offers a file sent from another org. Held
  // by value so a fresh object for the same account does not rebuild the client.
  const accountUser = options.account?.userId ?? null;
  const accountOrg = options.account?.orgId ?? "";
  const account = useMemo<AccountScope | null>(
    () => (accountUser ? { userId: accountUser, orgId: accountOrg } : null),
    [accountOrg, accountUser],
  );

  const client = useMemo(() => {
    // Plain `fetch`, for the reason the client's own default states: an
    // upload's reads are part of the person's file moving, not a poll.
    const inner = clientOptions?.fetchImpl ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
    // The commit answer is read from a clone, so the client still consumes the
    // body it was given.
    const observing: typeof fetch = async (input, init) => {
      const response = await inner(input, init);
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const match = COMPLETE.exec(url);
      if (match && response.ok) {
        const facts = readCommitFacts(await response.clone().json().catch(() => null));
        if (facts) commits.current.set(match[1] ?? "", facts);
      }
      return response;
    };
    return new UploadClient({
      ...clientOptions,
      account,
      // The queued commit is read back per drive, so the client cannot follow
      // one it was never told the drive for.
      driveId,
      fetchImpl: observing,
      onConflict: (name, uploadId) =>
        new Promise<ConflictAnswer>((resolve, reject) => {
          answers.current.set(uploadId, { resolve, reject });
          dispatchConflict({
            type: "ask",
            uploadId,
            name,
            parentId: destinations.current.get(uploadId)?.parentId ?? "",
          });
        }),
      onProgress: (progress) => {
        setRows((current) => ({
          ...current,
          [progress.uploadId]: {
            uploadId: progress.uploadId,
            name: progress.name,
            sent: progress.sent,
            total: progress.total,
            partsDone: progress.partsDone,
            partsTotal: progress.partsTotal,
            state: progress.state,
            ...(progress.error ? { error: progress.error } : {}),
          },
        }));
      },
    });
  }, [account, clientOptions, driveId]);

  // What a previous visit left half-sent, less what this tray is sending now.
  // Read again whenever an upload starts or settles: a file dropped again
  // resumes (or starts over) and its line must go, and an upload abandoned
  // with a question open stays offered only while it can still complete.
  const [resumable, setResumable] = useState<ResumableUpload[]>(() => client.resumable());
  const refreshResumable = useCallback((): void => {
    const next = client.resumable().filter((entry) => !handles.current.has(entry.uploadId));
    // The same sessions keep the same array, so reading again re-renders
    // nothing (a caller handing in fresh options each render rebuilds the
    // client, and with it this callback).
    setResumable((held) =>
      held.length === next.length && held.every((entry, i) => entry.uploadId === next[i]?.uploadId) ? held : next,
    );
  }, [client]);
  useEffect(() => refreshResumable(), [refreshResumable]);

  // What decides whether two files of one drop hold the same bytes. The
  // injected digest when a caller has one, so a test pins it and the page
  // shares the hashing pool it already runs the parts through.
  const contentKey = useMemo<ContentKey>(() => {
    const digest = clientOptions?.digest;
    return digest ? (file) => digest(file) : (file) => blake3HexOfBlob(file);
  }, [clientOptions]);

  // A sentence the row carries alongside whatever a name collision was answered
  // as: both are outcomes of the same upload, and dropping one to show the other
  // is how a person ends up with a file whose date is wrong and no reason given.
  const noteOnRow = useCallback((uploadId: string, note: string): void => {
    setRows((current) => {
      const row = current[uploadId];
      if (!row) return current;
      const outcome = row.outcome === undefined || row.outcome === "" ? note : `${row.outcome} ${note}`;
      return { ...current, [uploadId]: { ...row, outcome } };
    });
  }, []);

  // The folders a drop has landed files in since the last refresh: one listing
  // refetch per folder per moment, however many files finished in it.
  const landedIn = useRef(new Set<string>());
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sinceRefresh = useRef(0);
  const flushRefresh = useCallback((): void => {
    if (refreshTimer.current !== null) {
      clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    }
    sinceRefresh.current = 0;
    const folders = [...landedIn.current];
    landedIn.current.clear();
    for (const folder of folders) {
      void qc.invalidateQueries({ queryKey: keys.files.childrenOf(driveId, folder) });
    }
  }, [driveId, qc]);
  const refreshFolder = useCallback(
    (parentId: string): void => {
      landedIn.current.add(parentId);
      sinceRefresh.current += 1;
      // A drop whose lanes finish faster than the window is the case the timer
      // alone cannot serve: twelve files landing inside a quarter of a second
      // left the listing unchanged until the whole tray settled. The count is
      // the floor under that — the rows catch up while the drop is still going.
      if (sinceRefresh.current >= FOLDER_REFRESH_EVERY) {
        flushRefresh();
        return;
      }
      refreshTimer.current ??= setTimeout(flushRefresh, FOLDER_REFRESH_MS);
    },
    [flushRefresh],
  );

  const runOne = useCallback(
    async (
      file: File,
      parentId: string,
      conflictBehavior: "fail" | "rename" = "fail",
    ): Promise<UploadProgress> => {
      let handle: UploadHandle;
      try {
        handle = await client.start(file, parentId, { conflictBehavior });
      } catch (error) {
        // Refused before a session existed (no room, no permission, a name
        // taken at the door): there is no handle to report through, so the
        // failure is written as a row of its own — the file's name and the
        // reason are the two things the person needs and nothing else says.
        const failure: UploadProgress = {
          uploadId: `refused:${parentId}:${file.name}:${file.size}:${file.lastModified}`,
          name: file.name,
          sent: 0,
          total: file.size,
          partsDone: 0,
          partsTotal: 0,
          state: "failed",
          ...(error instanceof ApiError ? { error } : {}),
        };
        attempts.current.set(failure.uploadId, { file, parentId });
        setRows((current) => ({ ...current, [failure.uploadId]: failure }));
        return failure;
      }
      handles.current.set(handle.uploadId, handle);
      refreshResumable();
      attempts.current.set(handle.uploadId, { file, parentId });
      destinations.current.set(handle.uploadId, { parentId, name: file.name });
      // A file the drop already decided goes in beside a name of its own says
      // so on its row: "Uploaded" beside a name nobody typed is the outcome a
      // person most needs told. Recorded under the row's own id, because the
      // other file of this name is going to settle too.
      if (conflictBehavior === "rename") {
        outcomes.current.set(handle.uploadId, OUTCOMES["keep-both"]);
      }
      const settled = await handle.done();
      refreshResumable();
      const outcome = outcomes.current.get(handle.uploadId);
      if (outcome !== undefined) {
        outcomes.current.delete(handle.uploadId);
        setRows((current) => {
          const row = current[handle.uploadId];
          return row ? { ...current, [handle.uploadId]: { ...row, outcome } } : current;
        });
      }
      if (settled.state !== "done") return settled;
      setFinished((count) => count + 1);
      // The listing behind the tray grows as files land, not once the whole drop
      // is over: a batch of sixty leaves a person watching an unchanged folder
      // for minutes otherwise, with no way to tell progress from loss. Files
      // finishing within the same moment share one refresh.
      refreshFolder(parentId);
      // A queued commit is answered 202 and says what it landed on the
      // operation the client followed; a synchronous one said it in the body.
      // Either way the facts are the same three, and the queued path is the
      // one a real server takes.
      const facts: CommitFacts | undefined = settled.result
        ? { itemId: settled.result.nodeId, unchanged: settled.result.unchanged }
        : commits.current.get(handle.uploadId);
      if (!facts) return settled;
      if (facts.unchanged) {
        setAlreadyInFiles((count) => count + 1);
        return settled;
      }
      // On screen now, not when the listing's refetch gets through. A file
      // that went in under a name the server chose is left to the refetch:
      // this side does not know the name.
      if (conflictBehavior !== "rename") {
        seedLandedFile(qc, driveId, parentId, {
          nodeId: facts.itemId,
          name: file.name,
          size: file.size,
          mimeType: file.type,
        });
      }
      // The stamp is the last step of a file that has ALREADY landed. It names
      // the version it is changing — the queued commit does not carry one, so
      // the node is read back for it — and it can never fail the file or the
      // drop: a throw here killed the lane that was going to start the next
      // file, and the rest of the batch was never attempted.
      const known = facts.etag !== undefined && facts.etag !== "" ? facts.etag : null;
      const etag = known ?? (await itemEtag(driveId, facts.itemId).catch(() => null));
      const stamped =
        etag === null
          ? false
          : await setMtime
              .mutateAsync({
                driveId,
                itemId: facts.itemId,
                etag,
                // `lastModified` is milliseconds since the epoch; the attrs facet is
                // nanoseconds, and the laptop's time is the one that must survive.
                mtimeNs: file.lastModified * 1_000_000,
              })
              .then(() => true)
              .catch(() => false);
      if (!stamped) noteOnRow(handle.uploadId, MTIME_NOT_KEPT);
      return settled;
    },
    [client, driveId, noteOnRow, qc, refreshFolder, refreshResumable, setMtime],
  );

  const onDrop = useCallback(
    async (target: DropTarget | null, transfer: DropTransfer): Promise<void> => {
      const verdict = dropVerdict(target);
      if (!verdict.accepted || !target) {
        setRefusal(verdict.accepted ? null : verdict.reason);
        return;
      }
      setRefusal(null);

      const moving = readMovePayload(transfer);
      if (moving) {
        for (const subject of moving) {
          if (subject.id === target.id) continue;
          moveItem.mutate({
            driveId,
            itemId: subject.id,
            etag: subject.etag,
            parentId: target.id,
          });
        }
        return;
      }

      const expansion = await expandDrop(transfer.items ?? []);
      if (expansion.skippedSidecars > 0) {
        setSkippedSidecars((count) => count + expansion.skippedSidecars);
      }

      // Two files of this drop wanting one name in one folder is settled here,
      // before a session exists: the store holds one live node per name, so a
      // queue that simply sends both is a queue that loses one of them.
      const plan = await planNames(expansion.files, contentKey);
      if (plan.identicalCopies > 0) {
        setIdenticalCopies((count) => count + plan.identicalCopies);
      }

      setBatch({
        total: plan.files.length,
        done: 0,
        failed: 0,
        current: null,
        stopped: null,
      });

      let folderIds: Record<string, string> = {};
      if (expansion.directories.length > 0) {
        try {
          const created = await createTree.mutateAsync({
            driveId,
            parentId: target.id,
            paths: expansion.directories,
          });
          folderIds = indexTree(expansion.directories, created);
        } catch (error) {
          // The skeleton is the first write of the drop: refused, nothing under it
          // can land, so the batch ends here with the server's reason.
          const why = filesErrorCopy(error).title;
          setRefusal(why);
          setBatch((held) => (held ? { ...held, stopped: why } : held));
          return;
        }
      }

      // A few files at a time, never the whole folder at once. A refusal that
      // means "no more will fit" (quota, permission) stops the files not yet
      // started; whatever is in flight settles on its own and shows its state.
      let stopped: string | null = null;
      // Every file's own failure is already a row by the time the pool drains,
      // so the aggregate it throws has nothing left to tell the person — and a
      // drop that ends by rejecting would skip the listing refresh below.
      const drained = runPool(
        plan.files,
        lanesAllowed,
        async (planned) => {
          const dropped = planned.dropped;
          const parentId = dropped.directory ? folderIds[dropped.directory] : target.id;
          if (!parentId) {
            // The folder this file was going into is not in the skeleton the
            // tree call answered. Counting it as failed and saying nothing is
            // how a file disappears out of a drop, so it gets a named row.
            const id = `unplaced:${dropped.relativePath}`;
            setRows((current) => ({
              ...current,
              [id]: {
                uploadId: id,
                name: dropped.file.name,
                sent: 0,
                total: dropped.file.size,
                partsDone: 0,
                partsTotal: 0,
                state: "failed",
                error: new ApiError(0, {
                  code: "files.error",
                  message: "The folder for this file was not created.",
                }),
              },
            }));
            setBatch((held) => (held ? { ...held, done: held.done + 1, failed: held.failed + 1 } : held));
            return;
          }
          setBatch((held) => (held ? { ...held, current: dropped.relativePath } : held));
          let settled: UploadProgress;
          try {
            settled = await runOne(dropped.file, parentId, planned.conflictBehavior);
          } catch (error) {
            // Nothing the pipeline throws may end the drop or leave the file
            // unaccounted for. The row says what the person can check, because
            // an error this far in means the bytes may well be in Files.
            settled = unexpectedFailure(dropped.file, parentId, error);
            attempts.current.set(settled.uploadId, { file: dropped.file, parentId });
            setRows((current) => ({ ...current, [settled.uploadId]: settled }));
          }
          const failed = settled.state !== "done";
          if (failed && stopsBatch(settled.error) && stopped === null) {
            stopped = filesErrorCopy(settled.error).title;
            setRefusal(stopped);
          }
          const why = stopped;
          setBatch((held) =>
            held
              ? {
                  ...held,
                  done: held.done + 1,
                  failed: held.failed + (failed ? 1 : 0),
                  stopped: why,
                }
              : held,
          );
        },
        () => stopped !== null,
      );
      await drained.catch(() => undefined);
      // The listing the drop landed in shows the new rows once the batch is over;
      // the upload sessions themselves are raw fetches the mutation cache never sees.
      await qc.invalidateQueries({ queryKey: keys.files.all });
    },
    [contentKey, createTree, driveId, moveItem, qc, runOne],
  );

  const retry = useCallback(
    async (rowId: string): Promise<void> => {
      const attempt = attempts.current.get(rowId);
      if (!attempt) return;
      const failedRow = rowsRef.current[rowId];
      const rename = failedRow ? retryName(failedRow) : undefined;
      // A name the server refuses for what it is is refused however often it
      // is sent: the row keeps its reason rather than trading it for the same
      // refusal again.
      if (rename === null) return;
      const file = rename === undefined ? attempt.file : renamedFile(attempt.file, rename);
      // The row is removed rather than reset, because a retry that resumes an
      // open session comes back under the same id and a restart comes back
      // under a new one — and a failure must never outlive the attempt it
      // describes on a file that has since landed.
      setRows((current) => {
        if (!(rowId in current)) return current;
        const kept = { ...current };
        delete kept[rowId];
        return kept;
      });
      attempts.current.delete(rowId);
      handles.current.delete(rowId);
      setRefusal(null);
      setBatch((held) =>
        held
          ? {
              ...held,
              done: Math.max(0, held.done - 1),
              failed: Math.max(0, held.failed - 1),
              stopped: null,
            }
          : held,
      );
      let settled: UploadProgress;
      try {
        settled = await runOne(file, attempt.parentId);
      } catch (error) {
        settled = unexpectedFailure(file, attempt.parentId, error);
        attempts.current.set(settled.uploadId, { file, parentId: attempt.parentId });
        setRows((current) => ({ ...current, [settled.uploadId]: settled }));
      }
      const failed = settled.state !== "done";
      setBatch((held) =>
        held ? { ...held, done: held.done + 1, failed: held.failed + (failed ? 1 : 0) } : held,
      );
    },
    [runOne],
  );

  const retryFailed = useCallback(async (): Promise<void> => {
    // Only the failures: the files that landed are in Files already, and
    // sending them again would ask a person to answer a name collision for
    // every one of them. A name the server refuses for what it is is left out
    // too: no retry can land it.
    const failed = Object.values(rowsRef.current)
      .filter((row) => row.state === "failed" && retryName(row) !== null)
      .map((row) => row.uploadId);
    await runPool(failed, lanesAllowed, (id) => retry(id));
  }, [retry]);

  // The tray is going away with questions still open. Every upload behind one is
  // parked inside `await onConflict(...)`, holding its File, its client and its
  // queue slot for the life of the tab; refusing the question is what lets the
  // client's own failure path end them.
  useEffect(() => {
    const pending = answers.current;
    return () => {
      for (const waiting of pending.values()) {
        waiting.reject(new Error("the upload was abandoned before the name was settled"));
      }
      pending.clear();
    };
  }, []);

  const answerConflict = useCallback(
    (uploadId: string, answer: ConflictAnswer): void => {
      const pending = answers.current.get(uploadId);
      if (!pending) return;
      const resolve = pending.resolve;
      // Recorded before the upload settles, so the row it belongs to can say
      // what the answer did rather than only that it finished.
      outcomes.current.set(uploadId, OUTCOMES[typeof answer === "string" ? answer : answer.choice]);
      const settle = (final: ConflictAnswer): void => {
        answers.current.delete(uploadId);
        dispatchConflict({ type: "answered", uploadId });
        resolve(final);
      };
      if (answer !== "replace") {
        settle(answer);
        return;
      }
      // Replace is the one answer that writes onto a node that already exists,
      // so it has to name the version it believes it is replacing. Handing the
      // bare choice on when the version cannot be read is what makes the
      // client refuse with a sentence instead of overwriting blindly.
      const going = destinations.current.get(uploadId);
      if (going === undefined || going.parentId === "") {
        settle("replace");
        return;
      }
      void existingEtag(driveId, going.parentId, going.name)
        .then((etag) => settle(etag === null ? "replace" : { choice: "replace", etag }))
        .catch(() => settle("replace"));
    },
    [driveId],
  );

  const pause = useCallback((uploadId: string): void => {
    handles.current.get(uploadId)?.pause();
  }, []);

  const resume = useCallback((uploadId: string): void => {
    void handles.current.get(uploadId)?.resume();
  }, []);

  const cancel = useCallback((uploadId: string): void => {
    void handles.current.get(uploadId)?.cancel();
  }, []);

  const dismiss = useCallback((): void => {
    setRows((current) => {
      const kept: Record<string, UploadRow> = {};
      for (const row of Object.values(current)) {
        if (!TERMINAL_STATES.has(row.state)) kept[row.uploadId] = row;
      }
      return kept;
    });
    setSkippedSidecars(0);
    setIdenticalCopies(0);
    setAlreadyInFiles(0);
    setFinished(0);
    setBatch(null);
  }, []);

  const list = Object.values(rows);
  const settledClean = traySettled(list, batch, conflicts.queue);
  useEffect(() => {
    if (!settledClean) return undefined;
    const timer = setTimeout(dismiss, doneLingerMs);
    return () => clearTimeout(timer);
  }, [settledClean, dismiss, doneLingerMs]);

  return {
    rows: list,
    skippedSidecars,
    identicalCopies,
    alreadyInFiles,
    finished,
    refusal,
    batch,
    conflicts: conflicts.queue,
    resumable,
    onDrop,
    retry,
    retryFailed,
    pause,
    resume,
    cancel,
    answerConflict,
    dismiss,
  };
}
