// The browser's upload client: one session, N parts, resumable across a reload.
//
// The shape is dictated by the upload session API rather than invented here:
//
//   POST   /api/v1/files/uploads            → {uploadId, partSize, partsTotal, limits}
//   PUT    /api/v1/files/uploads/{id}/parts/{n}
//   GET    /api/v1/files/uploads/{id}       → {offset, complete, partsDone, acceptedParts}
//   POST   /api/v1/files/uploads/{id}/complete
//   DELETE /api/v1/files/uploads/{id}
//
// Three decisions worth knowing before reading the code:
//
//  * **Checksums are BLAKE3, because the store recomputes them.** The part
//    route REQUIRES `X-Part-Checksum` — it is never optional and never
//    size-conditional (a missing header is `files.part_checksum_required`) —
//    and the value is not merely recorded: the object-store driver hashes the
//    streamed bytes with BLAKE3 and refuses the part when the two disagree
//    (`files.part_checksum_mismatch`, 422). So "any stable hex digest" is NOT
//    enough and SubtleCrypto is no help: the browser has no BLAKE3, so this
//    module carries a small portable one (`blake3.ts`). At ~1 s per 32 MiB part
//    it does NOT run on the main thread — a 1 GB drop would freeze the tab for
//    half a minute — so parts are hashed by a small pool of module workers
//    (`blake3.worker.ts`), falling back in-thread only where there is no
//    `Worker` (jsdom, an old embedder) or the worker fails to load. It stays
//    injectable so a wasm or native build can replace it without touching the
//    session logic.
//  * **Resume is server-truth, never client bookkeeping.** `localStorage` holds
//    only enough to RECOGNISE the file after a reload (`uploadId` plus the
//    file's name/size/lastModified); which parts actually landed comes from
//    `GET /uploads/{id}`, because only the server knows what it accepted.
//  * **Conflicts are answered by the page, not guessed here.** The complete
//    route offers `fail | rename` — "replace" is a *content* mode (a new
//    version on an existing node) that the session API does not expose on day
//    one — so the tray's prompt maps "keep both" to `rename` and "skip" to
//    aborting the session, and asks again for nothing else.
//  * **A completion is accepted, not done.** `POST …/complete` always answers
//    `202` and an operation: the commit is queued, so the taken name, the
//    quota refusal and a store failure are every one of them decided AFTER the
//    response. "The parts were accepted" is not "the file is in Files", and
//    treating the 202 as success is how a refused upload reads as "Uploaded"
//    and never appears in the folder. So the client follows the operation to a
//    terminal state before it calls an upload done.

import { accountKey, safeLocalStorage as guardedLocalStorage } from "@alkera/ui/storage";

import type { AccountScope } from "@/lib/accountScope";
import { parseRetryAfter } from "@/lib/retryAfter";

import { blake3Hex, blake3HexOfBlob } from "./blake3";
import type { HashReply, HashRequest } from "./blake3.worker";
import { REQUEST_FAILED, apiBaseUrl, forTheReader, withSession } from "./client";
import { ApiError } from "./errors";

export { blake3Hex };

/** Where a resumable session's identity is remembered between page loads: per
 *  person and org, so a resume is never offered an upload made in another org.
 *  Records written before keys named the org are ignored, not migrated: the
 *  server sweeps the sessions they point at, and the file simply starts over. */
export function uploadStorageKey(scope: AccountScope): string {
  return accountKey(scope.userId, scope.orgId, "files.uploads");
}

/** What the tray needs to recognise a file after a reload. Deliberately not the
 *  progress: the server is the only honest source for what it accepted. */
export interface ResumableUpload {
  uploadId: string;
  name: string;
  size: number;
  lastModified: number;
  parentId: string;
  partSize: number;
  /** What each part this browser sent hashed to, by part number.
   *
   *  The four facts above describe where a file was going and how the picker
   *  labelled it; none of them is about its contents, and two different files
   *  agree on all four routinely — two exports written in the same second, a
   *  note corrected without changing its length. Resuming on those alone sent
   *  one file's parts into another file's session; the pump skips every part
   *  the server already holds, so the commit declared one file's digests over
   *  the other's bytes and the server refused the whole upload with a
   *  `files.parts_mismatch` the person could do nothing about.
   *
   *  A digest of the first part alone was not enough: two files can open the
   *  same way and differ later, which is exactly what a resume would then skip
   *  past. So every part is recorded, and a rejoin has to agree with ALL of
   *  the ones the server is holding.
   *
   *  A part the server holds but this record does not name — an older build's
   *  record, another tab's part, a session whose first part never landed — is
   *  refused rather than guessed at: the cost is one upload starting over, and
   *  the alternative is an upload that cannot be completed at all. */
  partDigests?: Record<string, string>;
}

/** One upload's live state, as the tray renders it. */
export interface UploadProgress {
  uploadId: string;
  name: string;
  /** Bytes the server has accepted. */
  sent: number;
  total: number;
  partsDone: number;
  partsTotal: number;
  state: "uploading" | "waiting" | "paused" | "completing" | "done" | "cancelled" | "failed";
  /** Set only in `failed`; carries the server's `{code}`. */
  error?: ApiError;
  /** Set only in `done`, and only when the commit named what it landed. */
  result?: UploadResult;
}

/** The states an upload does not leave. A row in one of them has already said
 *  its last word — a failure's sentence and its Retry, a completion's "Uploaded"
 *  — and a late control that overwrites it takes that word away. */
export const TERMINAL_UPLOAD_STATES: ReadonlySet<UploadProgress["state"]> = new Set([
  "done",
  "cancelled",
  "failed",
]);

/** What a finished commit created, as the operation reported it. */
export interface UploadResult {
  nodeId: string;
  versionId?: string;
  /** The drive already held these bytes, so no version was written. */
  unchanged: boolean;
}

/** Replace with nothing to fence against: the page could not read the version
 *  of the file already holding the name, so the write is refused rather than
 *  landed on whatever the etag happens to be by the time the bytes arrive. */
export const REPLACE_NEEDS_ETAG =
  "Could not read the current version of the file being replaced. Try again.";

/** What the page answers a name collision with.
 *
 *  `replace` writes a new version onto the file already holding the name, so it
 *  is the one answer that touches a node that already exists — which puts it
 *  under the same rule as every other Files mutation: it must name the version
 *  it believes it is replacing. That is why it arrives as an object carrying
 *  the etag and the other two do not. Without the header the server refuses
 *  the completion, and with a stale one it refuses with a 412 rather than
 *  silently overwriting whoever wrote in between. */
export type ConflictChoice = "keep-both" | "skip" | "replace";

/** `replace`, with the version of the existing node it is agreed against. */
export interface ConflictReplace {
  readonly choice: "replace";
  readonly etag: string;
}

/** A bare choice, or `replace` with its precondition. */
export type ConflictAnswer = ConflictChoice | ConflictReplace;

/** The commit behaviour a choice asks the session API for. */
export function behaviourFor(answer: ConflictAnswer): "rename" | "replace" | null {
  const choice = typeof answer === "string" ? answer : answer.choice;
  if (choice === "skip") return null;
  return choice === "replace" ? "replace" : "rename";
}

/** The `If-Match` a choice carries, when it carries one. */
export function preconditionFor(answer: ConflictAnswer): string | null {
  if (typeof answer === "string") return null;
  return answer.etag !== "" ? answer.etag : null;
}

/** What the caller knows about this file's place in the drop before a byte is
 *  sent. A file landing beside a name an EARLIER file of the same drop is
 *  already taking asks for `rename` outright, which is the commit behaviour
 *  "Keep both" sends — so a batch settles its own collisions the way a person
 *  settles a collision with a file already in the folder. */
export interface StartOptions {
  conflictBehavior?: "fail" | "rename";
}

export interface UploadHandle {
  readonly uploadId: string;
  /** Stop after the part in flight. */
  pause(): void;
  /** Continue from whatever the server says it already holds. */
  resume(): Promise<void>;
  /** Abort the session and release its quota hold. */
  cancel(): Promise<void>;
  /** Resolves when the upload finishes, is cancelled, or fails. */
  done(): Promise<UploadProgress>;
}

export interface UploadClientOptions {
  /** The drive the queued commit's operation is read back from. Without it the
   *  client has no address to poll — the operation route is addressed by drive
   *  — so a caller that cannot name one (the chat attachment path) gets the
   *  202 as its last word and no completion verdict. */
  driveId?: string;
  /** Injected so a test drives real request/response shapes without a network. */
  fetchImpl?: typeof fetch;
  /** Injected so a reload can be simulated, and so a private window that throws
   *  on `localStorage` degrades to "no resume" rather than to no upload. */
  storage?: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  /** Who is uploading and in which org. Resumable sessions are remembered under
   *  it; a client that names nobody remembers nothing, so it never offers, or
   *  leaves behind, a session another account could be handed. */
  account?: AccountScope | null;
  /** The part digest, hex. Defaults to the BLAKE3 the store verifies against,
   *  computed off the main thread. */
  digest?: Digest;
  /** Injected so a test drives the OFF-THREAD path without a real `Worker`
   *  (jsdom has none). Production passes nothing and gets the module worker. */
  workerFactory?: WorkerFactory;
  /** Called on every accepted part and on every state change. */
  onProgress?: (progress: UploadProgress) => void;
  /** Asked once when the server refuses a completion on a name collision. The
   *  name is what the person recognises; the upload id is which of their files
   *  is asking, and two files of one name make that a different question. */
  onConflict?: (name: string, uploadId: string) => Promise<ConflictAnswer> | ConflictAnswer;
}


/* ---------------------------------------------------------------------------
 * Hashing off the main thread.
 *
 * A part costs ~1 s of arithmetic per 32 MiB (`blake3.ts`). On the main thread
 * that is a frozen tab for the length of the upload — which a customer reads as
 * "the page hung". So parts are handed to a small pool of module workers and
 * awaited; the client's own loop is unchanged, because it already awaited an
 * injectable digest.
 *
 * Two properties matter more than the pool's cleverness:
 *
 *  * **The pool is never a way for an upload to fail.** A missing `Worker`, a
 *    constructor that throws, a chunk that will not load, a worker that dies
 *    mid-part — every one of them falls back to hashing in-thread, and the part
 *    still goes out with the right digest. Slow beats broken. The one thing
 *    that DOES fail a part is the file itself no longer being readable, and
 *    that is the file's news, not the pool's.
 *  * **A part it could not read is not a worker it should distrust.** A worker
 *    that answers `{error}` is alive; the reply is about one part, and a file
 *    that was moved or unplugged makes it the ordinary reply. So that part is
 *    re-read in-thread and the worker keeps working. Only a worker that fails
 *    as a worker — `error`, `messageerror` — takes the pool down with it.
 *  * **A part is never resident to be hashed.** The pool is handed the Blob
 *    slice, not its bytes: structured clone carries a reference rather than a
 *    copy, and the hasher reads it one window at a time — so a 128 MiB part
 *    costs a window on either side of the seam instead of a part on both.
 * ------------------------------------------------------------------------- */

/** A part in, its hex digest out.
 *
 *  The signal is how a cancel reaches a hash already running: a 128 MiB part is
 *  seconds of arithmetic and a worker that has stopped answering is forever, and
 *  neither may hold the row's queue slot after the person has let it go. */
export type Digest = (part: Blob, signal?: AbortSignal) => Promise<string>;

/** Just enough of `Worker` for the pool — so a test can stand in for one. */
export interface HashWorker {
  postMessage(message: HashRequest): void;
  terminate(): void;
  onmessage: ((event: { data: HashReply }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  /** A reply that arrived but could not be deserialised. Distinct from
   *  `onerror`, and without it that part is simply never answered. */
  onmessageerror?: ((event: unknown) => void) | null;
}

export type WorkerFactory = () => HashWorker;

/** Enough parallelism to keep hashing ahead of a fast link without turning a
 *  5,000-file drop into 5,000 threads. */
const HASH_WORKERS = 2;

const moduleWorkerFactory: WorkerFactory = () => {
  if (typeof Worker === "undefined") throw new Error("this runtime has no Worker");
  // A same-origin module worker: the SPA's CSP grants `worker-src 'self'` and
  // deliberately not `blob:` (the hosted web CDN's policy), so
  // the URL has to be one Vite emits as a real asset.
  return new Worker(new URL("./blake3.worker.ts", import.meta.url), {
    type: "module",
  }) as unknown as HashWorker;
};

interface HashJob {
  part: Blob;
  signal: AbortSignal | undefined;
  settle: (outcome: { hex: string } | { error: unknown }) => void;
}

/** A cancel, said the way the platform says it, so a caller that already knows
 *  `AbortError` needs to learn nothing new. */
export function abortError(): Error {
  if (typeof DOMException === "function") return new DOMException("aborted", "AbortError");
  const error = new Error("aborted");
  error.name = "AbortError";
  return error;
}

/** Whether a rejection is the cancel that asked for it, rather than news. */
export function isAbort(error: unknown): boolean {
  return (
    typeof error === "object" && error !== null && (error as { name?: unknown }).name === "AbortError"
  );
}

/**
 * A digest backed by up to `poolSize` workers, degrading to the in-thread
 * hasher the moment the workers are not there.
 *
 * Falling back is not the same as never failing. The in-thread hasher reads the
 * part off disk (`blake3.ts`), and a file that has been moved, renamed or
 * unmounted since it was picked answers `NotReadableError` — which during a
 * multi-hour upload from an external drive is an ordinary event. So every job
 * has a reject path and every path that finishes one uses it: a promise that
 * can only ever resolve is a row stuck at its last percentage for the life of
 * the tab, with the queue slot it holds never handed on.
 */
export function createWorkerDigest(
  factory: WorkerFactory = moduleWorkerFactory,
  poolSize: number = HASH_WORKERS,
): Digest {
  const idle: HashWorker[] = [];
  const waiting: HashJob[] = [];
  const inFlight = new Map<HashWorker, HashJob>();
  let spawned = 0;
  let broken = false;

  /** Finish a job on this thread, whichever way the read goes. */
  const inThread = (job: HashJob): void => {
    blake3HexOfBlob(job.part, undefined, job.signal).then(
      (hex) => job.settle({ hex }),
      (error: unknown) => job.settle({ error }),
    );
  };

  /** The pool has lost a worker AS A WORKER — it failed to load, or answered
   *  something that is not a message. Finish everything it was holding in-thread
   *  and stop trying: one dead worker is enough to distrust the rest.
   *
   *  `spawned` is deliberately NOT given back here, unlike in {@link discard}:
   *  `broken` never goes false again, and every path that could reach `pump()`
   *  after this checks it, so a count that can no longer admit a spawn is the
   *  cheaper guard. */
  const collapse = (worker: HashWorker): void => {
    if (broken) return;
    broken = true;
    // EVERY job the pool is holding, not just this worker's: a part left
    // in-flight on a sibling would otherwise never settle and the upload would
    // hang instead of falling back.
    const stranded = [...inFlight.values(), ...waiting];
    waiting.length = 0;
    const workers = [...inFlight.keys(), ...idle, worker];
    inFlight.clear();
    for (const spare of workers) {
      try {
        spare.terminate();
      } catch {
        // Already gone; the in-thread fallback below is what matters.
      }
    }
    idle.length = 0;
    for (const job of stranded) inThread(job);
  };

  /** The job this worker was holding is over — cancelled, or answered with
   *  something that is not a digest. The worker goes with it rather than being
   *  handed the next part: it may still be mid-part on the one just dropped,
   *  and a pool that cannot tell which answer belongs to which job is worse than
   *  a pool one short. */
  const discard = (worker: HashWorker): void => {
    inFlight.delete(worker);
    try {
      worker.terminate();
    } catch {
      // Already gone.
    }
    spawned -= 1;
    pump();
  };

  const hand = (worker: HashWorker, job: HashJob): void => {
    inFlight.set(worker, job);
    worker.postMessage({ part: job.part });
  };

  const spawn = (): HashWorker | null => {
    try {
      const worker = factory();
      worker.onmessage = (event) => {
        const job = inFlight.get(worker);
        if (!job) return;
        const reply = event.data;
        inFlight.delete(worker);
        const next = waiting.shift();
        if (next) hand(worker, next);
        else idle.push(worker);
        if ("hex" in reply) {
          job.settle({ hex: reply.hex });
          return;
        }
        // The worker ANSWERED — it is alive and has just been handed the next
        // part. What it could not do is read THIS one, which for a file that
        // has been moved or unplugged is the ordinary case, not a broken pool:
        // collapsing here cost the whole tab its off-thread hashing for the
        // session on the first such file. The part is re-read in-thread, so a
        // worker-side hiccup still falls back and still sends the right digest,
        // and a file that is genuinely gone rejects there instead.
        inThread(job);
      };
      worker.onerror = () => collapse(worker);
      // A reply that could not be deserialised is not a reply: without this the
      // part it belongs to is never answered at all.
      worker.onmessageerror = () => collapse(worker);
      spawned += 1;
      return worker;
    } catch {
      broken = true;
      return null;
    }
  };

  /** Start whatever the pool can start now. Called when a slot opens. */
  function pump(): void {
    while (waiting.length > 0) {
      const worker = idle.pop() ?? (spawned < poolSize ? spawn() : null);
      if (!worker) {
        if (broken) for (const job of waiting.splice(0)) inThread(job);
        return;
      }
      hand(worker, waiting.shift() as HashJob);
    }
  }

  return (part, signal) =>
    new Promise<string>((resolve, reject) => {
      if (signal?.aborted === true) {
        reject(abortError());
        return;
      }
      let over = false;
      const job: HashJob = {
        part,
        signal,
        settle: (outcome) => {
          if (over) return;
          over = true;
          signal?.removeEventListener("abort", dropped);
          if ("hex" in outcome) resolve(outcome.hex);
          else reject(outcome.error);
        },
      };
      // A cancel frees the slot immediately, whatever the job is doing: a worker
      // that has stopped answering would otherwise hold it until the tab closes.
      function dropped(): void {
        const holder = [...inFlight.entries()].find(([, held]) => held === job)?.[0];
        const queued = waiting.indexOf(job);
        if (queued >= 0) waiting.splice(queued, 1);
        job.settle({ error: abortError() });
        if (holder) discard(holder);
      }
      signal?.addEventListener("abort", dropped, { once: true });

      if (broken) {
        inThread(job);
        return;
      }
      waiting.push(job);
      pump();
    });
}

/** One pool for the page: the tray remounts, the workers do not respawn. */
let sharedDigest: Digest | null = null;

function pageDigest(): Digest {
  sharedDigest ??= createWorkerDigest();
  return sharedDigest;
}

/** A key per (file, destination), so re-picking the same file into the same
 *  folder after a reload finds its session and a different folder does not. */
function fingerprint(file: File, parentId: string): string {
  return `${parentId}:${file.name}:${file.size}:${file.lastModified}`;
}

interface OpenResponse {
  uploadId: string;
  partSize: number;
  partsTotal: number;
  expiresAt: string;
}

/** A session the server still has, and what it is holding for it. */
interface Rejoined {
  session: OpenResponse;
  accepted: readonly number[];
}

interface StatusResponse {
  uploadId: string;
  state: string;
  offset: number;
  length: number;
  complete: boolean;
  partsDone: number;
  partsTotal: number;
  acceptedParts: number[];
}

/** One entry of an operation's `errors[]`. */
export interface OperationError {
  itemId?: string | null;
  code?: string;
  message?: string;
}

/** The operation a queued commit is followed through. */
export interface OperationSnapshot {
  id: string;
  kind?: string;
  state: string;
  done?: number;
  total?: number | null;
  errors?: OperationError[];
  resultNodeId?: string | null;
  resultVersionId?: string | null;
  resultUnchanged?: boolean;
}

/** The states an operation does not leave. */
const SETTLED_STATES = new Set(["done", "failed", "cancelled"]);

/** How the poll waits between looks: short at first, because most commits are
 *  already done by the time the 202 is read, then widening to a cap so a large
 *  one costs requests in the tens rather than one every 50 ms for its length. */
export const OPERATION_POLL_MS: readonly number[] = [50, 100, 200, 400, 800, 1600, 3000];

/** When the client stops asking. Only a terminal state or a refusal ends the
 *  poll; this is the backstop for an operation whose runner died without
 *  recording anything, so a tab does not follow a row nobody will touch. */
export const OPERATION_POLL_LIMIT_MS = 10 * 60_000;

/** The queued commit an answer describes, or `null` when it does not describe
 *  one — a server that committed synchronously and answered with the item it
 *  created is still understood. */
export function asOperation(body: unknown): OperationSnapshot | null {
  if (!body || typeof body !== "object") return null;
  const shape = body as { id?: unknown; state?: unknown };
  if (typeof shape.id !== "string" || typeof shape.state !== "string") return null;
  return body as OperationSnapshot;
}

/**
 * The refusal a settled operation carries, or `null` when there is none.
 *
 * Only a terminal failure is a refusal: an operation still queued or running
 * has not refused anything, and answering one as an error is how a slow commit
 * would read as a lost file.
 */
export function operationRefusal(operation: OperationSnapshot): ApiError | null {
  if (operation.state !== "failed" && operation.state !== "cancelled") return null;
  const first = operation.errors?.find((entry) => entry.message ?? entry.code);
  return new ApiError(0, {
    code: first?.code ?? "files.error",
    message: first?.message ?? "The upload could not be committed.",
  });
}

/** What a settled operation created, or `null` when it named nothing — a
 *  refused commit, or a server too old to say. */
export function operationResult(operation: OperationSnapshot): UploadResult | null {
  if (operation.state !== "done") return null;
  const nodeId = operation.resultNodeId;
  if (typeof nodeId !== "string" || nodeId === "") return null;
  return {
    nodeId,
    ...(typeof operation.resultVersionId === "string"
      ? { versionId: operation.resultVersionId }
      : {}),
    unchanged: operation.resultUnchanged === true,
  };
}

/**
 * Whether a refusal is "that name is already used here", and so a question for
 * the page rather than a failure.
 *
 * The refusal arrives two ways and both say the same word. A completion
 * refused ON THE CALL is a 409 — the session API's synchronous answer to an
 * agreed set of parts it will not commit. A completion the server accepted and
 * then refused inside the queued commit carries no status at all: it is read
 * off the operation's `errors[]`, which records the code the library raised.
 * The prose is never matched: it is the library's and may be reworded.
 */
export function isNameTaken(error: ApiError): boolean {
  if (error.code === SESSION_STATE) return false;
  return error.status === 409 || error.code === "files.exists";
}

/** The refusal of a request against a session that can no longer take it:
 *  aborted by a refused commit, committed, or being committed. Also a 409, and
 *  not a name collision: answering it with "keep both" completes nothing. */
export const SESSION_STATE = "files.session_state";

/** The session states that still take parts and a completion. */
const LIVE_SESSION_STATES: ReadonlySet<string> = new Set(["open", "uploading"]);

/**
 * Whether a refusal means this SESSION is over, rather than this attempt.
 *
 * The session API agrees a completion against the parts it is holding: the
 * caller declares a size and a digest for every one of them, and a list that is
 * not exactly what the server has is refused (`_agree`). Nothing the client can
 * do afterwards changes either side of that comparison, so the session is spent
 * — and a spent session that is still remembered is a row whose Retry rejoins
 * it and is refused again, for as long as the server keeps it.
 */
export function sessionIsSpent(error: unknown): boolean {
  return error instanceof ApiError && (error.code === "files.parts_mismatch" || error.code === SESSION_STATE);
}

/**
 * A file this browser can no longer read.
 *
 * The bytes are read twice per part — once to hash, once to send — and the
 * `File` is a handle, not a copy: moved, renamed, deleted, or on a drive that
 * was unplugged, and every read of it answers `NotReadableError`. Over a
 * multi-hour upload that is an ordinary event, so it is an ordinary failed row
 * carrying the one sentence that tells the person what to do about it.
 *
 * Status 0 on purpose: nothing about it is worth another try THIS run
 * (`canWaitOut`), and the same file may read perfectly well once it is back —
 * which is what the row's own Retry is for, rather than a loop here.
 *
 * The code is `upload.`, not `files.`: this is the one upload refusal the
 * BROWSER raises rather than the server, and `files.` is the server's namespace
 * to spend. Its copy is a row in the page's one table like every other refusal.
 */
export class UploadReadError extends ApiError {
  constructor(name: string, cause?: unknown) {
    super(0, {
      code: "upload.unreadable",
      // A drive that was unplugged moved and deleted nothing, and it is the
      // case a long upload meets most, so the sentence names all three.
      message: `${name} could not be read. It may have been moved, deleted, or on a drive that is no longer connected.`,
    });
    this.name = "UploadReadError";
    this.cause = cause;
  }
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted === true) {
      resolve();
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", stop);
      resolve();
    }, ms);
    const stop = (): void => {
      clearTimeout(timer);
      resolve();
    };
    signal?.addEventListener("abort", stop, { once: true });
  });
}

/* ---------------------------------------------------------------------------
 * Waiting out a request the server has no room for.
 *
 * The process bounds the bytes it holds in flight and sheds the rest with a
 * `503` + `Retry-After` (`backend/api/body_limit.py`). The budget admits only a
 * couple of max-size parts at a time, so a third person uploading while two
 * others are mid-part meets the shed routinely — it is queueing, not an
 * outage, and the part that is refused is the part that would have landed a
 * second later. A client that gives up on it reports a lost file for a wait.
 *
 * The same holds after the last part: a restart or a throttle that refuses the
 * completion or an operation read loses a file whose bytes are already on the
 * server, which is the most expensive moment there is to give up at. So one
 * policy covers every request the upload makes.
 * ------------------------------------------------------------------------- */

/** A refusal that carries the server's own wait before the next try. */
export class BusyError extends ApiError {
  /** `Retry-After`, in milliseconds, when the server sent one. */
  readonly retryAfterMs: number | null;

  constructor(status: number, body: unknown, retryAfterMs: number | null) {
    super(status, body, REQUEST_FAILED);
    this.name = "BusyError";
    this.retryAfterMs = retryAfterMs;
    forTheReader(this);
  }
}

/**
 * The server's guidance on how many parts this browser may keep in flight.
 *
 * Every part answer -- admitted or shed -- carries `X-Upload-Concurrency`, the
 * share of the process's upload budget this principal holds while others are
 * uploading too (`backend/api/body_limit.py`). A queue that keeps more lanes
 * open than that is asking for the shed it then has to wait out; one that reads
 * the hint sends what will be admitted. Absent on an older server, and then
 * the queue keeps its own small default.
 */
export const uploadConcurrency = {
  hint: null as number | null,
  /** Read the guidance off a response; a response without it changes nothing. */
  observe(response: Response): void {
    const raw = response.headers.get("x-upload-concurrency");
    if (raw === null) return;
    const share = Number(raw);
    if (Number.isInteger(share) && share > 0) this.hint = share;
  },
};

/** How many times a request is made before the upload is called failed.
 *  Generous, because the shed is what a busy deployment does rather than what
 *  a broken one does. */
export const PART_RETRY_ATTEMPTS = 8;

/** The first wait, doubled on every further refusal. */
export const PART_RETRY_BASE_MS = 500;

/** The longest wait between two tries, whatever the server asked for. */
export const PART_RETRY_MAX_MS = 30_000;

/** Whether a part is worth sending again, or the server has decided something
 *  a second send would not change. A 429 and a 5xx are about the moment; every
 *  other 4xx is about the request, and re-sending a 128 MiB body to be told the
 *  same thing is the expensive way to learn it. A rejected `fetch` answered
 *  nothing at all — a dropped connection — and is always worth another try. */
function canWaitOut(error: unknown): boolean {
  // A cancel and a file that will not read are both about the request the
  // client has already decided not to make again; retrying either is eight
  // waits before the row says what it has known since the first attempt.
  if (isAbort(error)) return false;
  if (!(error instanceof ApiError)) return true;
  return error.status === 429 || error.status >= 500;
}

/** How long before the next try: never sooner than the server asked, and never
 *  without the client's own backoff, so a deployment that keeps answering
 *  `Retry-After: 1` is not asked once a second for eight seconds. */
function waitBefore(attempt: number, error: unknown): number {
  const backoff = PART_RETRY_BASE_MS * 2 ** (attempt - 1);
  const asked = error instanceof BusyError ? error.retryAfterMs : null;
  return Math.min(PART_RETRY_MAX_MS, Math.max(backoff, asked ?? 0));
}

/**
 * The one upload client the tray and the drop target both drive.
 *
 * Every request it makes carries an `Idempotency-Key` (the session API answers
 * 428 without one) minted per attempt, so a retried part or a retried
 * completion replays the server's stored answer instead of doing the work
 * twice.
 */
export class UploadClient {
  /** The sessions this client is sending into RIGHT NOW.
   *
   *  A remembered session is recognised by the folder, the name, the size and
   *  the modification time — four facts two different files can agree on
   *  exactly (two copies of a template, two exports written in the same
   *  second). Rejoining one that is already being driven would put two files
   *  into one upload: the parts overwrite each other part for part, the folder
   *  grows a single node, and the second set of bytes is gone with nothing
   *  said. So a session in flight is never adopted — the second file opens its
   *  own. */
  private readonly driving = new Set<string>();

  private readonly driveId: string | null;
  private readonly fetchImpl: typeof fetch;
  private readonly storage: Pick<Storage, "getItem" | "setItem" | "removeItem"> | null;
  private readonly storageKey: string;
  private readonly digest: Digest;
  private readonly onProgress: ((progress: UploadProgress) => void) | undefined;
  private readonly onConflict:
    | ((name: string, uploadId: string) => Promise<ConflictAnswer> | ConflictAnswer)
    | undefined;

  constructor(options: UploadClientOptions = {}) {
    this.driveId = options.driveId ?? null;
    // Deliberately NOT the read gate (`api/readGate.ts`). Every read this
    // client makes serves the file the person is watching — the run opens by
    // asking what the server already holds, the completion asks again to name
    // the parts, and a queued commit is followed until it settles — so holding
    // one behind a refusal that some background poll collected stalls their
    // upload, which is the same failure the gate refuses to inflict on a send.
    // A refusal this client meets itself is already waited out, on the server's
    // own terms, by `waitingOut`. The session around it is the portal's own:
    // the org assertion, the renewal of a token about to lapse, and one retry
    // after a `token_expired`, so an upload that outlives the access token is
    // not lost to it.
    this.fetchImpl = withSession(options.fetchImpl ?? ((...args) => fetch(...args)));
    this.storage = options.account ? (options.storage ?? safeLocalStorage()) : null;
    this.storageKey = options.account ? uploadStorageKey(options.account) : "";
    this.digest =
      options.digest ??
      (options.workerFactory ? createWorkerDigest(options.workerFactory) : pageDigest());
    this.onProgress = options.onProgress;
    this.onConflict = options.onConflict;
  }

  /** Every session this browser has open, newest last. The tray asks for these
   *  on mount and offers to resume the ones whose file the user re-picks. */
  resumable(): ResumableUpload[] {
    if (!this.storage) return [];
    try {
      const raw = this.storage.getItem(this.storageKey);
      if (!raw) return [];
      const parsed: unknown = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object") return [];
      return Object.values(parsed as Record<string, ResumableUpload>).filter(
        (entry): entry is ResumableUpload =>
          Boolean(entry) && typeof entry.uploadId === "string" && typeof entry.name === "string",
      );
    } catch {
      return [];
    }
  }

  /**
   * Open a session for `file` and start sending, or pick up the session a
   * previous page load left behind for the same file and destination.
   *
   * A 507 at open (over quota, by bytes or by nodes) is thrown as an
   * {@link ApiError} before a single byte is sent — the point of asking the
   * server for room first.
   */
  async start(file: File, parentId: string, options: StartOptions = {}): Promise<UploadHandle> {
    const known = this.remembered(file, parentId);
    let session: OpenResponse | null = null;
    if (known !== null) {
      let held: Rejoined | null = null;
      try {
        held = await this.rejoin(known);
      } catch (error) {
        // A session the server has swept or expired is not a dead end: `rejoin`
        // has already forgotten it, so the file starts over rather than failing
        // for the one reason a retry exists to get past.
        if (!(error instanceof ApiError && (error.status === 404 || error.status === 410))) {
          throw error;
        }
      }
      // Which parts the SERVER is holding is the only list worth checking
      // against: those are the ones the pump will skip, and a part it holds
      // that this browser cannot vouch for is one nobody can prove belongs to
      // this file.
      if (held !== null && (await this.holdsTheSameBytes(file, known, held.accepted))) {
        session = held.session;
      } else if (held !== null) {
        this.forget(known.uploadId);
      }
    }
    return this.drive(
      file,
      parentId,
      session ?? (await this.open(file, parentId)),
      options.conflictBehavior ?? "fail",
    );
  }

  private remembered(file: File, parentId: string): ResumableUpload | null {
    if (!this.storage) return null;
    const stored = this.records();
    const entry = stored[fingerprint(file, parentId)];
    if (!entry) return null;
    // A session another file of this tab is already sending into belongs to
    // that file. The digest check below would usually catch that too, but not
    // when the two files share a first part and differ later, and this costs
    // nothing to ask.
    return this.driving.has(entry.uploadId) ? null : entry;
  }

  /**
   * Whether every part the server is holding is this file's.
   *
   * Those are exactly the parts the pump will skip, so those are the parts that
   * have to be proved. They are hashed one at a time and in order, and the
   * first disagreement ends it — a file that differs in its opening part costs
   * one part read, not the whole accepted prefix. A part the record cannot
   * name is a disagreement too: nothing in the browser can say whose bytes it
   * is. Slices are taken at the REMEMBERED part size, so the two digests
   * describe the same window.
   *
   * A session holding nothing is trivially agreed: there is no part to skip, so
   * there is nothing a wrong file could inherit.
   */
  private async holdsTheSameBytes(
    file: File,
    known: ResumableUpload,
    accepted: readonly number[],
  ): Promise<boolean> {
    const remembered = known.partDigests ?? {};
    for (const partNo of [...accepted].sort((first, second) => first - second)) {
      const digest = remembered[String(partNo)];
      if (digest === undefined || digest === "") return false;
      const from = (partNo - 1) * known.partSize;
      if (from >= file.size) return false;
      try {
        const slice = file.slice(from, Math.min(from + known.partSize, file.size));
        if ((await this.digest(slice)) !== digest) return false;
      } catch {
        // A file this browser can no longer read is not a file it may resume.
        return false;
      }
    }
    return true;
  }

  /** A remembered session is only usable if the SERVER still has it; a expired
   *  or swept one is forgotten and the upload starts again. The parts it is
   *  holding come back with it, because those are what a resume would skip. */
  private async rejoin(known: ResumableUpload): Promise<Rejoined> {
    try {
      const status = await this.status(known.uploadId);
      // A session the server still holds but will not complete (a refused
      // commit aborted it) is no resume: rejoined, its completion is refused
      // for as long as the server keeps it. It is let go and the file starts
      // over, the same as one the server swept.
      if (typeof status.state === "string" && !LIVE_SESSION_STATES.has(status.state)) {
        throw new ApiError(410, { code: SESSION_STATE, message: `The upload session is ${status.state}.` });
      }
      return {
        session: {
          uploadId: known.uploadId,
          partSize: known.partSize,
          partsTotal: status.partsTotal,
          expiresAt: "",
        },
        accepted: status.acceptedParts,
      };
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
        this.forget(known.uploadId);
        throw error;
      }
      throw error;
    }
  }

  private async open(file: File, parentId: string): Promise<OpenResponse> {
    const opened = await this.send<OpenResponse>("POST", "/api/v1/files/uploads", {
      body: JSON.stringify({
        declaredSize: file.size,
        name: file.name,
        parentId,
        mime: file.type || null,
      }),
      headers: { "content-type": "application/json" },
    });
    this.remember(file, parentId, opened);
    return opened;
  }

  private drive(
    file: File,
    parentId: string,
    opened: OpenResponse,
    initialBehaviour: "fail" | "rename",
  ): UploadHandle {
    // Both move when a name collision is answered: the refused commit aborted
    // the session it was asked of, so the answer is carried by a NEW session
    // opened for it rather than by a second completion of the dead one.
    let session = opened;
    this.driving.add(session.uploadId);
    let behaviour: "fail" | "rename" | "replace" = initialBehaviour;
    let precondition: string | null = null;
    // Where the refusal came from decides how the answer is carried. A
    // completion refused ON THE CALL leaves the session back in `uploading`,
    // so the answer is a second completion of it. A completion the server
    // ACCEPTED and then refused inside the queued commit has already released
    // the session, so the answer needs a new one and the bytes again.
    let sessionSpent = false;
    // What each part was hashed to on its way out, so the completion assembles
    // the digests it already holds instead of reading the file again. A hex
    // string per part and never the bytes, so the cost is the same for a 10 GB
    // file as for a 10 MB one; a part this browser did not send has no entry
    // and is hashed at completion, once.
    const sentChecksums = new Map<number, string>();
    let paused = false;
    let cancelled = false;
    // How many pumps are live on this session, and whether a Resume was
    // refused while one was.
    let pumps = 0;
    let again = false;
    // What a cancel reaches into: the hash of the part in flight, the request
    // carrying it, and any backoff between two tries. Without it `cancelled` is
    // read once per part, so a cancel during a 128 MiB hash or a 30 s wait was
    // answered only when that finished — and never at all when it could not.
    const aborter = new AbortController();
    let settle: (progress: UploadProgress) => void = () => {};
    const finished = new Promise<UploadProgress>((resolve) => {
      settle = (final) => {
        // The session is nobody's now: a file that arrives later with the same
        // four facts may pick the remembered one up, which is what resume is.
        this.driving.delete(session.uploadId);
        resolve(final);
      };
    });

    const progress: UploadProgress = {
      uploadId: opened.uploadId,
      name: file.name,
      sent: 0,
      total: file.size,
      partsDone: 0,
      partsTotal: session.partsTotal,
      state: "uploading",
    };

    const emit = (): void => this.onProgress?.({ ...progress });

    /** What every backing-off request says to the row — a part, the completion
     *  and the operation read alike, because from the row they are one wait. */
    const waiting = (): void => {
      progress.state = "waiting";
      emit();
    };

    /** And what ends that wait, once the request it was for has landed. */
    const waitedOut = (): void => {
      if (progress.state !== "waiting") return;
      progress.state = "uploading";
      emit();
    };

    const pump = async (): Promise<void> => {
      sessionSpent = false;
      // What the SERVER already holds decides where this run starts — a resume
      // after a reload and a resume after a pause take the same path.
      const status = await this.status(session.uploadId, waiting, aborter.signal);
      const accepted = new Set(status.acceptedParts);
      progress.partsDone = status.partsDone;
      progress.sent = status.offset;
      progress.partsTotal = status.partsTotal;
      waitedOut();
      emit();

      for (let part = 1; part <= status.partsTotal; part += 1) {
        if (cancelled || paused) break;
        if (accepted.has(part)) continue;
        const from = (part - 1) * session.partSize;
        const slice = file.slice(from, Math.min(from + session.partSize, file.size));
        // The slice itself, never its bytes: one Blob handed to both the digest
        // and the body, each streaming it a window at a time, so no part is
        // ever resident. The file is therefore read twice per part — accepted,
        // because the single pass would need the body to be a `ReadableStream`
        // tee'd through the hasher, and that costs a buffer of the whole part
        // to save a disk read.
        const checksum = await this.digestPart(file, slice, aborter.signal);
        await this.putPart(session.uploadId, part, slice, checksum, waiting, aborter.signal);
        sentChecksums.set(part, checksum);
        // What the server now holds for this part, recorded the moment it has
        // it. Without it the record describes a destination and a label, and a
        // later visit cannot tell this file from another one that agrees with
        // it on both.
        this.rememberDigest(session.uploadId, part, checksum);
        waitedOut();
        progress.partsDone += 1;
        progress.sent = Math.min(progress.sent + slice.size, file.size);
        emit();
      }

      if (cancelled) return;
      if (paused) {
        progress.state = "paused";
        emit();
        return;
      }
      progress.state = "completing";
      emit();
      const queued = asOperation(
        await this.complete(
          file,
          session,
          sentChecksums,
          behaviour,
          precondition,
          waiting,
          aborter.signal,
        ),
      );
      // A 202 is the server agreeing to commit, not the commit. Whatever the
      // commit decided is on the operation, and throwing it here puts it on the
      // same path as a refusal that arrived synchronously.
      if (queued !== null) {
        const settled = await this.follow(queued, waiting, aborter.signal);
        const refusal = operationRefusal(settled);
        if (refusal !== null) {
          sessionSpent = true;
          throw refusal;
        }
        const landed = operationResult(settled);
        if (landed !== null) progress.result = landed;
      }
      this.forget(session.uploadId);
      progress.state = "done";
      emit();
      settle({ ...progress });
    };

    const recover = async (error: unknown): Promise<void> => {
      if (error instanceof ApiError && isNameTaken(error) && this.onConflict) {
        // `progress.uploadId` and not `session.uploadId`: a rename answered
        // earlier reopens the session, and the row keeps the id it was created
        // under. The answer has to reach the row, not the session.
        let answer: ConflictAnswer;
        try {
          answer = await this.onConflict(file.name, progress.uploadId);
        } catch (abandoned: unknown) {
          // Nobody will answer (the tray went away). A session the refused
          // commit already ended is let go, so the next visit does not offer
          // to continue an upload that cannot complete; a live one stays
          // remembered, and dropping the file again asks the question again.
          if (sessionSpent) this.forget(session.uploadId);
          throw abandoned;
        }
        const chosen = behaviourFor(answer);
        if (chosen === null) {
          // Skip. A session the queued commit already released has nothing
          // left to abort; a live one is given back here.
          if (sessionSpent) {
            this.forget(session.uploadId);
            progress.state = "cancelled";
            emit();
            settle({ ...progress });
          } else {
            await handle.cancel();
          }
          return;
        }
        const fence = preconditionFor(answer);
        if (chosen === "replace" && fence === null) {
          // The server refuses a `replace` with no `If-Match`, and refusing
          // here says which version was missing instead of turning the
          // server's 428 into an unattributable failure.
          fail(
            new ApiError(428, {
              code: "files.if_match_required",
              message: REPLACE_NEEDS_ETAG,
            }),
          );
          return;
        }
        behaviour = chosen;
        precondition = fence;
        if (!sessionSpent) {
          try {
            const answered = asOperation(
              await this.complete(
                file,
                session,
                sentChecksums,
                behaviour,
                precondition,
                waiting,
                aborter.signal,
              ),
            );
            if (answered !== null) {
              const settled = await this.follow(answered, waiting, aborter.signal);
              const refused = operationRefusal(settled);
              if (refused !== null) throw refused;
              const landed = operationResult(settled);
              if (landed !== null) progress.result = landed;
            }
            this.forget(session.uploadId);
            progress.state = "done";
            emit();
            settle({ ...progress });
          } catch (retried: unknown) {
            fail(retried);
          }
          return;
        }
        try {
          this.forget(session.uploadId);
          this.driving.delete(session.uploadId);
          session = await this.open(file, parentId);
          this.driving.add(session.uploadId);
          // A new session may cut the file at a different part size, so the
          // digests of the old one describe windows that no longer exist.
          sentChecksums.clear();
        } catch (reopened: unknown) {
          fail(reopened);
          return;
        }
        progress.partsDone = 0;
        progress.sent = 0;
        progress.partsTotal = session.partsTotal;
        progress.state = "uploading";
        emit();
        launch();
        return;
      }
      fail(error);
    };

    /** Every path out of a run ends on a row: the upload's own failures through
     *  `recover`, and anything `recover` itself threw — a conflict hook that
     *  rejected, a cancel refused by the network — through the second catch.
     *  A run that ends anywhere else is a row stuck at its last percentage and
     *  an unhandled rejection in the console.
     *
     *  `launch` is the unguarded door, for the one caller that is already
     *  inside the chain the guard counts: `recover` restarting its own run
     *  after a name collision was answered. */
    const launch = (): void => {
      pumps += 1;
      pump()
        .catch(recover)
        .catch((error: unknown) => fail(error))
        .finally(() => {
          pumps -= 1;
          // A Resume that arrived while this pump was still winding down is
          // honoured here rather than dropped: the row would otherwise read
          // "uploading" with nothing uploading it.
          if (pumps === 0 && again && !paused && !TERMINAL_UPLOAD_STATES.has(progress.state)) {
            again = false;
            launch();
          }
        });
    };

    /** Start a run, unless one is already going. Two pumps on one session send
     *  the same part twice and settle the row twice (two clicks on Resume). */
    const run = (): void => {
      if (pumps > 0) {
        again = true;
        return;
      }
      again = false;
      launch();
    };

    const fail = (error: unknown): void => {
      // A session deleted on purpose refuses everything still in flight against
      // it. That refusal is the cancel working, not news, and it must not turn
      // a cancelled row back into a failed one.
      if (cancelled) return;
      // A session that can no longer be completed must not be remembered, or
      // the retry the row offers rejoins it and is refused the same way, for
      // as long as the server keeps the session. Every other failure leaves the
      // record alone: a dropped connection is exactly what resume is for.
      if (sessionIsSpent(error)) this.forget(session.uploadId);
      progress.state = "failed";
      progress.error = error instanceof ApiError ? error : undefined;
      emit();
      settle({ ...progress });
    };

    const handle: UploadHandle = {
      uploadId: session.uploadId,
      pause: () => {
        paused = true;
      },
      resume: async () => {
        // Same rule as cancel: a row that has settled keeps what it settled
        // as, so a late Resume does not repaint "Uploaded" as "uploading".
        if (TERMINAL_UPLOAD_STATES.has(progress.state)) return;
        paused = false;
        progress.state = "uploading";
        emit();
        run();
      },
      cancel: async () => {
        // A row that has already settled keeps what it settled as. Cancel is
        // reachable on one for a frame — the click lands as the last part
        // lands — and repainting it "cancelled" took a failed row's sentence
        // and its Retry away, and told a person a file that IS in Files is not.
        if (TERMINAL_UPLOAD_STATES.has(progress.state)) return;
        cancelled = true;
        // Stop the hash and the request the run is inside FIRST: the DELETE
        // below can take as long as the network does, and a cancel that only
        // frees the row once the server has answered is one the person watches
        // do nothing. The abort is also what a hung worker answers to.
        aborter.abort();
        try {
          // Deliberately not aborted: this is the request that gives the
          // session's quota hold back, and it is the one request a cancel
          // exists to make.
          await this.send<unknown>("DELETE", `/api/v1/files/uploads/${session.uploadId}`, {});
        } catch {
          // The server keeping a session this browser has let go is a sweep's
          // problem, never the reader's: a cancel that rejects leaves the row
          // running forever and its queue slot with it.
        }
        this.forget(session.uploadId);
        progress.state = "cancelled";
        emit();
        settle({ ...progress });
      },
      done: () => finished,
    };

    run();
    return handle;
  }

  /**
   * Make one request, waiting out the refusals that mean "not now".
   *
   * `onWaiting` runs before each wait so the row can say what it is doing: a
   * shed request looks exactly like a stalled one otherwise, and neither the
   * percentage nor the spinner moves while the client backs off.
   */
  private async waitingOut<T>(
    onWaiting: () => void,
    attempt: () => Promise<T>,
    signal?: AbortSignal,
  ): Promise<T> {
    for (let tries = 1; ; tries += 1) {
      try {
        return await attempt();
      } catch (error: unknown) {
        if (tries >= PART_RETRY_ATTEMPTS || !canWaitOut(error)) throw error;
        onWaiting();
        await delay(waitBefore(tries, error), signal);
        // A cancel during the wait ends the run with what it was waiting on
        // rather than spending the remaining tries on a session that is gone.
        if (signal?.aborted === true) throw error;
      }
    }
  }

  /**
   * The digest of one part, with a read failure named for the person.
   *
   * `File` is a handle, so every read of it can fail for a reason nothing in
   * this client did: the file moved, was deleted, the drive was unplugged. Left
   * bare, that arrives as a `NotReadableError` on a row with no error to show.
   */
  private async digestPart(file: File, part: Blob, signal?: AbortSignal): Promise<string> {
    try {
      return await this.digest(part, signal);
    } catch (error: unknown) {
      if (isAbort(error)) throw error;
      throw new UploadReadError(file.name, error);
    }
  }

  private async putPart(
    uploadId: string,
    partNo: number,
    body: BodyInit,
    checksum: string,
    onWaiting: () => void,
    signal?: AbortSignal,
  ): Promise<void> {
    await this.waitingOut(
      onWaiting,
      () =>
        this.send<unknown>("PUT", `/api/v1/files/uploads/${uploadId}/parts/${partNo}`, {
          body,
          headers: {
            "content-type": "application/octet-stream",
            "X-Part-Checksum": checksum,
          },
          signal,
        }),
      signal,
    );
  }

  private async complete(
    file: File,
    session: OpenResponse,
    sentChecksums: ReadonlyMap<number, string>,
    conflictBehavior: "fail" | "rename" | "replace",
    ifMatch: string | null,
    onWaiting: () => void,
    signal?: AbortSignal,
  ): Promise<unknown> {
    // WHICH parts the server holds is the server's to say; re-reading them beats
    // re-deriving them from a client-side tally that a resume could disagree
    // with. What each of them hashes to is already known for every part this
    // browser sent, and re-deriving THAT is a second full read of the file and a
    // second full hashing pass with every byte already landed. So the digests
    // are carried from the send, and only a part this run did not send — the
    // tail another tab or an earlier page load put there — is read here.
    const status = await this.status(session.uploadId, onWaiting, signal);
    const parts: { partNo: number; size: number; checksum: string }[] = [];
    for (const part of status.acceptedParts) {
      const from = (part - 1) * session.partSize;
      const to = Math.min(from + session.partSize, file.size);
      const known = sentChecksums.get(part);
      parts.push({
        partNo: part,
        size: to - from,
        // A part this run did not send is read here, and that read can fail the
        // same way the sending one can — named, so a completion refused because
        // the drive went away says so.
        checksum: known ?? (await this.digestPart(file, file.slice(from, to), signal)),
      });
    }
    // Waited out rather than failed, as the status read above it is and for the
    // same reason: every byte is already on the server, so a refusal about the
    // moment would lose a file that is entirely uploaded. Only the POST is
    // repeated — the digests it carries are computed once.
    return await this.waitingOut(
      onWaiting,
      () =>
        this.send<unknown>("POST", `/api/v1/files/uploads/${session.uploadId}/complete`, {
          body: JSON.stringify({ parts, conflictBehavior }),
          headers: {
            "content-type": "application/json",
            // Only `replace` writes onto a node that already exists, so only
            // `replace` has a version to fence against.
            ...(ifMatch === null ? {} : { "If-Match": ifMatch }),
          },
          signal,
        }),
      signal,
    );
  }

  /**
   * Read the queued commit back until it has settled.
   *
   * The only ends are a terminal state, a refusal the read itself answers with
   * (a 404 for an operation that is gone, a 403 for one this session may no
   * longer read), and the backstop deadline. Anything else and the client would
   * be guessing at a commit it can see the truth of — including a read the
   * server sheds, which says nothing about the commit it is a read of.
   */
  private async follow(
    queued: OperationSnapshot,
    onWaiting: () => void,
    signal?: AbortSignal,
  ): Promise<OperationSnapshot> {
    if (this.driveId === null) return queued;
    const path = `/api/v1/files/drives/${this.driveId}/operations/${queued.id}`;
    const stopAt = Date.now() + OPERATION_POLL_LIMIT_MS;
    let current = queued;
    for (let look = 0; !SETTLED_STATES.has(current.state); look += 1) {
      if (Date.now() > stopAt) {
        throw new ApiError(0, {
          code: "files.commit_pending",
          message: "This is still being committed. Check the folder in a few minutes.",
        });
      }
      await delay(OPERATION_POLL_MS[Math.min(look, OPERATION_POLL_MS.length - 1)] ?? 0, signal);
      // A poll is the one loop a cancel could otherwise be stuck behind for the
      // full ten minutes, because nothing in it is a request to abort.
      if (signal?.aborted === true) throw abortError();
      current = await this.waitingOut(
        onWaiting,
        () => this.send<OperationSnapshot>("GET", path, { signal }),
        signal,
      );
    }
    return current;
  }

  /**
   * What the server holds for this session.
   *
   * Waited out on the same terms as a part and the completion, whenever the
   * caller can say so to the row: a read the process sheds says nothing about
   * the session it is a read of, and giving up on one fails an upload whose
   * bytes are entirely on the server. `onWaiting` is absent only before a run
   * exists to report a wait on.
   */
  private async status(
    uploadId: string,
    onWaiting?: () => void,
    signal?: AbortSignal,
  ): Promise<StatusResponse> {
    const read = (): Promise<StatusResponse> =>
      this.send<StatusResponse>("GET", `/api/v1/files/uploads/${uploadId}`, { signal });
    return onWaiting ? await this.waitingOut(onWaiting, read, signal) : await read();
  }

  private async send<T>(
    method: string,
    path: string,
    init: { body?: BodyInit; headers?: Record<string, string>; signal?: AbortSignal },
  ): Promise<T> {
    const headers: Record<string, string> = { ...(init.headers ?? {}) };
    // Every non-GET on the session API needs one; the server's answer to a
    // missing key is 428, and a retry that re-used this call would reuse the
    // key with it.
    if (method !== "GET") headers["Idempotency-Key"] = uploadKey();
    const response = await this.fetchImpl(`${apiBaseUrl}${path}`, {
      method,
      headers,
      body: init.body,
      credentials: "include",
      // A cancelled upload's part is a 128 MiB body still going out; without
      // this it keeps the connection and the person's bandwidth until it lands.
      ...(init.signal ? { signal: init.signal } : {}),
    });
    uploadConcurrency.observe(response);
    if (!response.ok) {
      const body: unknown = await response.json().catch(() => null);
      // A 503 (no in-flight byte budget) and a 429 come with the server's own
      // idea of when to come back; every other refusal is final and carries no
      // wait to honour.
      if (response.status === 503 || response.status === 429) {
        throw new BusyError(response.status, body, parseRetryAfter(response.headers.get("retry-after")));
      }
      throw forTheReader(new ApiError(response.status, body, REQUEST_FAILED, response.headers));
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  private records(): Record<string, ResumableUpload> {
    if (!this.storage) return {};
    try {
      const raw = this.storage.getItem(this.storageKey);
      const parsed: unknown = raw ? JSON.parse(raw) : {};
      return parsed && typeof parsed === "object"
        ? (parsed as Record<string, ResumableUpload>)
        : {};
    } catch {
      return {};
    }
  }

  private write(records: Record<string, ResumableUpload>): void {
    try {
      this.storage?.setItem(this.storageKey, JSON.stringify(records));
    } catch {
      // A private window that refuses to store costs a resume, never an upload.
    }
  }

  private remember(file: File, parentId: string, session: OpenResponse): void {
    const records = this.records();
    records[fingerprint(file, parentId)] = {
      uploadId: session.uploadId,
      name: file.name,
      size: file.size,
      lastModified: file.lastModified,
      parentId,
      partSize: session.partSize,
    };
    this.write(records);
  }

  /** Note what one of a live session's parts hashed to. Nothing to do when the
   *  record is already gone — the upload finished, or another tab swept it. */
  private rememberDigest(uploadId: string, partNo: number, digest: string): void {
    const records = this.records();
    let found = false;
    for (const entry of Object.values(records)) {
      if (entry.uploadId !== uploadId) continue;
      entry.partDigests = { ...entry.partDigests, [String(partNo)]: digest };
      found = true;
    }
    if (found) this.write(records);
  }

  private forget(uploadId: string): void {
    const records = this.records();
    for (const [key, entry] of Object.entries(records)) {
      if (entry.uploadId === uploadId) delete records[key];
    }
    this.write(records);
  }
}

function uploadKey(): string {
  const webCrypto = globalThis.crypto;
  if (webCrypto && typeof webCrypto.randomUUID === "function") return webCrypto.randomUUID();
  return `alk-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

/** The shared guarded store, in the `Storage` shape the injectable seam takes —
 *  so a caller that supplies its own fake and the production default are the
 *  same three methods. Storage throws outright in some embedded and privacy
 *  contexts; there it degrades to "no resume", never to no upload. */
function safeLocalStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  const store = guardedLocalStorage();
  return {
    getItem: (key) => store.get(key),
    setItem: (key, value) => {
      store.set(key, value);
    },
    removeItem: (key) => {
      store.remove(key);
    },
  };
}
