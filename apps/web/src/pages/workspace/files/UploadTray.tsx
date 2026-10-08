// The tray: what the drop is doing. A tray whose every upload finished cleanly
// clears itself a moment later (the hook owns that clock); one with a failure
// stays until Dismiss, because the sentence on the failed row is the only place
// the person learns the file is not in Files.
//
// It renders what {@link useUploads} already decided — it holds no upload state
// of its own — so the same tray serves a fresh drop, a resumed session and a
// conflict prompt without three code paths.

import { Button } from "@alkera/ui";

import { filesErrorCopy } from "@/lib/files/errors";
import { shownName } from "@/lib/files/shownName";

import {
  TERMINAL_STATES,
  NAME_REFUSED,
  retryName,
  type BatchProgress,
  type ConflictAnswer,
  type ConflictPrompt,
  type UploadRow,
} from "./useUploads";
import type { ResumableUpload } from "@/api/filesUpload";

import "./upload-tray.css";

/** Replace is a *content* mode the upload session does not offer yet, so the
 *  reason is printed with the remaining choices rather than parked in the
 *  `title` of a dead button: a hover-only explanation reaches nobody on a
 *  touch screen, nobody using a keyboard, and nobody using a screen reader,
 *  which is every way of reading it except a mouse resting on a control that
 *  looks broken. When the session grows the mode this sentence is replaced by
 *  the button, not the other way round. */
/** A Replace whose version moved on. Nothing was written, which is the part
 *  the server's own "node … moved on" never says. */
export const STALE_PRECONDITION =
  "That file changed while you were uploading. Nothing was replaced. Try again.";

/** What Replace does, said before it is clicked: it is the one choice that
 *  changes a file that is already there, and a new version is recoverable
 *  while the wrong answer is not obvious afterwards. */
const REPLACE_HINT = "Replace keeps the file and adds your upload as a new version.";

export interface UploadTrayProps {
  rows: readonly UploadRow[];
  skippedSidecars: number;
  /** Copies of a file this drop already sent under the same name. */
  identicalCopies: number;
  alreadyInFiles: number;
  finished: number;
  refusal: string | null;
  /** The dropped batch as a whole — how far through it is, and what it is on. */
  batch?: BatchProgress | null;
  conflicts: readonly ConflictPrompt[];
  resumable: readonly ResumableUpload[];
  onPause(uploadId: string): void;
  onResume(uploadId: string): void;
  onCancel(uploadId: string): void;
  /** Settle one upload's collision, named by the upload rather than by the
   *  file: two files of one name each ask their own question. */
  onAnswerConflict(uploadId: string, answer: ConflictAnswer): void;
  /** Send one failed file again. */
  onRetry?(uploadId: string): void;
  /** Send every failed file again, and only those. */
  onRetryFailed?(): void;
  /** Clear the finished rows. Offered once nothing is still moving. */
  onDismiss?(): void;
}

function percent(row: UploadRow): number {
  if (row.total <= 0) return 0;
  return Math.min(100, Math.round((row.sent / row.total) * 100));
}

/** Said on the row the open question is about.
 *
 *  A commit the server queued decides a taken name long after the bytes are up
 *  — tens of seconds on a real deployment — and for all of it the row is in
 *  `completing`. "Finishing" is what a commit nobody has to do anything about
 *  says; this one is waiting on the reader, and without saying so the question
 *  above it reads as unrelated to the upload below it. */
const WAITING_FOR_ANSWER = "Waiting for your answer";

function label(row: UploadRow, asked: boolean): string {
  if (asked && !TERMINAL_STATES.has(row.state)) return WAITING_FOR_ANSWER;
  switch (row.state) {
    case "uploading":
      return `${percent(row)}%`;
    // The server sheds a part it has no room for right now, and the client
    // waits it out. Neither the percentage nor the bar moves meanwhile, so the
    // row says why rather than reading as a stall.
    case "waiting":
      return "Waiting for the server…";
    case "paused":
      return "Paused";
    case "completing":
      return "Finishing";
    case "done":
      return "Uploaded";
    case "cancelled":
      return "Cancelled";
    case "failed":
      return "Failed";
  }
}

/** Why a row failed, printed on its own line under the row so the sentence has
 *  room and the columns above it never move to make some. */
function reason(row: UploadRow): string | null {
  if (row.state !== "failed") return null;
  // A refused precondition is the one refusal whose server sentence names a
  // node id rather than what happened. Replace fences against the version
  // the person was shown, so a 412 means somebody else wrote first — and
  // the thing they need to know is that nothing was overwritten.
  if (row.error?.status === 412) return STALE_PRECONDITION;
  // A 0-byte file is one empty part, so the server's own sentence for it is
  // "a part may not be empty" — a unit of an upload the person never chose,
  // about a file they did. It is the one refusal read out of the copy table.
  // A name with whitespace at either end: the copy table's sentence, plus what
  // to do when there is no name left to offer instead.
  // A name refused for what it is: the copy table's sentence, never the
  // server's lowercase rule ("a name may not exceed 243 bytes"), and — when
  // the product has no name to offer instead — what the person does next,
  // because Retry would send the same name into the same refusal.
  if (row.error?.code?.startsWith(NAME_REFUSED) === true) {
    const { title } = filesErrorCopy(row.error);
    return retryName(row) === null ? `${title} Rename the file and upload it again.` : title;
  }
  if (row.error?.code === "files.empty_part") {
    const copy = filesErrorCopy(row.error);
    return `${copy.title} ${copy.detail ?? ""}`.trim();
  }
  // Otherwise the server's sentence, not its code: "part checksum mismatch …"
  // tells the person whether to retry, where `files.part_checksum_mismatch`
  // tells them nothing. Nothing when the refusal carried no message: the
  // column already says Failed.
  return row.error?.message || null;
}

/** How many names the resumable line prints before it counts the rest. Three
 *  names still read as a sentence; a fourth turns the line into a list. */
const NAMED_RESUMABLE = 3;

/** The files a previous visit left half-sent, by name.
 *
 *  A session can only continue against the same bytes, and the browser cannot
 *  re-open a file it was handed once — so the reader has to hand it over again.
 *  The line names WHICH files, because "1 upload" left them guessing, and it
 *  names both ways of handing a file over: the drop the old copy assumed, and
 *  the Upload files button most people actually used. */
export function resumableLine(names: readonly string[]): string {
  const one = names.length === 1;
  const shown = names.slice(0, NAMED_RESUMABLE);
  const rest = names.length - shown.length;
  const listed = rest > 0 ? [...shown, `${rest} more`] : shown;
  const subject =
    listed.length === 1
      ? listed[0]!
      : `${listed.slice(0, -1).join(", ")} and ${listed[listed.length - 1]!}`;
  return `${subject} from your last visit can continue. Drop or choose the same ${one ? "file" : "files"}.`;
}

/** One line for the whole drop: the count, and the file it is on while it runs.
 *  A batch stopped short says how many never started, because those files are
 *  still only on the laptop and nothing else in the tray says so. */
export function batchLine(batch: BatchProgress): string {
  const { total, done, failed, current, stopped } = batch;
  if (stopped !== null) {
    const left = total - done;
    return left > 0
      ? `Stopped after ${done} of ${total}, ${left} not uploaded`
      : `Stopped with ${failed} of ${total} failed`;
  }
  if (done >= total) {
    // The summary the tray leaves on screen before it clears: what landed,
    // said as a count of files rather than as a fraction of itself.
    return failed > 0
      ? `${total - failed} of ${total} uploaded, ${failed} failed`
      : `${total} ${total === 1 ? "file" : "files"} uploaded`;
  }
  return current ? `Uploading ${done} of ${total}: ${current}` : `Uploading ${done} of ${total}`;
}

/** The head's one line, for everything the tray is showing.
 *
 *  The batch describes the LAST drop, but the list keeps every row a person has
 *  not dismissed: seven files from earlier drops and one from the latest. A line
 *  that counted only the latest drop read "1 of 1 uploaded" over eight rows. So
 *  the count is over the rows shown, widened to the drop when files the drop
 *  expanded to have no row yet; what the batch alone knows (why it stopped,
 *  files it settled without a row) still rides along. */
export function headLine(rows: readonly UploadRow[], batch: BatchProgress | null): string | null {
  const total = Math.max(rows.length, batch?.total ?? 0);
  if (total === 0) return null;
  const terminal = rows.filter((row) => TERMINAL_STATES.has(row.state)).length;
  const failedRows = rows.filter((row) => row.state === "failed").length;
  // A row that is finishing has every byte up and is waiting on the server's
  // commit. Counting it as not yet uploaded read "Uploading 0 of 3" over three
  // full bars; it counts as up, and the file being named is one still sending.
  const finishing = rows.filter((row) => row.state === "completing").length;
  const sending = rows.find((row) => !TERMINAL_STATES.has(row.state) && row.state !== "completing");
  const done = Math.max(terminal, batch?.done ?? 0);
  const stopped = batch?.stopped ?? null;
  if (stopped === null && sending === undefined && finishing > 0 && done < total) {
    return `Finishing ${finishing} ${finishing === 1 ? "file" : "files"}`;
  }
  return batchLine({
    total,
    done: done < total ? Math.min(total, done + finishing) : done,
    failed: Math.max(failedRows, batch?.failed ?? 0),
    current: sending ? shownName(sending.name) : batch?.current ? shownName(batch.current) : null,
    stopped,
  });
}

/** How much of a filename the tray shows before it elides the middle. Long
 *  enough for the names people actually upload, short enough that one row does
 *  not push the percentage and the controls off the tray. */
export const NAME_BUDGET = 44;

/**
 * A filename short enough for the row, elided in the MIDDLE.
 *
 * The end of a filename is the half that distinguishes it — `qa-bulk-047.txt`
 * from `qa-bulk-048.txt`, `.csv` from `.csv.gz` — so a name cut from the right
 * is the one shape that cannot attribute a failure to a file. The whole name
 * still rides the row's `title`.
 */
export function middleEllipsis(name: string, budget = NAME_BUDGET): string {
  const glyphs = [...name];
  if (glyphs.length <= budget) return name;
  const tail = Math.floor((budget - 1) / 2);
  const head = budget - 1 - tail;
  return `${glyphs.slice(0, head).join("")}…${glyphs.slice(glyphs.length - tail).join("")}`;
}

/** The question a taken name raises, under the row it is about: the sentence,
 *  what Replace does, and the three choices in one line. */
function Question({
  prompt,
  onAnswer,
}: {
  prompt: ConflictPrompt;
  onAnswer(uploadId: string, answer: ConflictAnswer): void;
}) {
  return (
    <div
      className="alk-files-uploads__conflict"
      role="group"
      aria-label={`Name taken: ${shownName(prompt.name)}`}
    >
      <p>
        <span className="alk-code">{shownName(prompt.name)}</span> already exists here.
      </p>
      <p className="alk-meta">{REPLACE_HINT}</p>
      <div className="alk-files-uploads__choices">
        <Button size="sm" fill="ghost" onClick={() => onAnswer(prompt.uploadId, "replace")}>
          Replace
        </Button>
        <Button size="sm" fill="ghost" onClick={() => onAnswer(prompt.uploadId, "keep-both")}>
          Keep both
        </Button>
        <Button size="sm" fill="ghost" onClick={() => onAnswer(prompt.uploadId, "skip")}>
          Skip
        </Button>
      </div>
    </div>
  );
}

/** A failed row's one way forward. A name refused for whitespace at its ends
 *  is offered under the trimmed name, and a name refused for what it is offers
 *  no retry at all — sending it as it was would be refused
 *  again — and a name that is nothing but whitespace offers nothing. */
function RetryControl({ row, onRetry }: { row: UploadRow; onRetry(uploadId: string): void }) {
  const trimmed = retryName(row);
  if (trimmed === null) return null;
  if (trimmed === undefined) {
    return (
      <Button size="sm" fill="ghost" aria-label={`Retry ${shownName(row.name)}`} onClick={() => onRetry(row.uploadId)}>
        Retry
      </Button>
    );
  }
  return (
    <Button size="sm" fill="ghost" title={shownName(trimmed)} onClick={() => onRetry(row.uploadId)}>
      {`Upload as \u201c${middleEllipsis(shownName(trimmed))}\u201d`}
    </Button>
  );
}

export function UploadTray(props: UploadTrayProps) {
  const {
    rows,
    skippedSidecars,
    identicalCopies,
    alreadyInFiles,
    finished,
    refusal,
    batch = null,
    conflicts,
    resumable,
    onPause,
    onResume,
    onCancel,
    onAnswerConflict,
    onRetry,
    onRetryFailed,
    onDismiss,
  } = props;

  const empty =
    rows.length === 0 &&
    conflicts.length === 0 &&
    resumable.length === 0 &&
    refusal === null &&
    batch === null;
  if (empty) return null;

  const settled = rows.length > 0 && rows.every((row) => TERMINAL_STATES.has(row.state));
  // Each open question sits under the row it is about; a question whose row is
  // not listed (a resumed session's) is asked above the list instead.
  const asking = new Map(conflicts.map((prompt) => [prompt.uploadId, prompt]));
  const listed = new Set(rows.map((row) => row.uploadId));
  const unlisted = conflicts.filter((prompt) => !listed.has(prompt.uploadId));
  // Only the failures a retry can land: a name the server refuses for what it
  // is is refused however often it is sent.
  const failures = rows.filter((row) => row.state === "failed" && retryName(row) !== null).length;
  const line = headLine(rows, batch);

  return (
    <section className="alk-stack alk-files-uploads" aria-label="Uploads">
      <div className="alk-files-uploads__head">
        <h2 className="alk-t-label">Uploads</h2>
        <span className="alk-files-uploads__head-side">
          {line === null ? null : (
            <span
              className="alk-meta alk-files-uploads__batch"
              role="status"
              data-stopped={batch?.stopped != null ? "true" : undefined}
            >
              {line}
            </span>
          )}
          {/* Offered only once nothing is moving: a retry that runs beside the
              lanes still finishing would race them for the same folder. */}
          {settled && failures > 1 && onRetryFailed ? (
            <Button size="sm" fill="ghost" onClick={onRetryFailed}>
              Retry {failures} failed
            </Button>
          ) : null}
          {settled && onDismiss ? (
            <Button size="sm" fill="ghost" onClick={onDismiss}>
              Dismiss
            </Button>
          ) : null}
        </span>
      </div>

      {refusal === null ? null : (
        <p className="alk-warning" role="alert">
          {refusal}
        </p>
      )}

      {resumable.length === 0 ? null : (
        <p className="alk-meta">{resumableLine(resumable.map((entry) => shownName(entry.name)))}</p>
      )}

      {unlisted.map((prompt) => (
        <Question key={prompt.uploadId} prompt={prompt} onAnswer={onAnswerConflict} />
      ))}

      <ul className="alk-files-uploads__list">
        {rows.map((row) => {
          const prompt = asking.get(row.uploadId) ?? null;
          // The name as the browser handed it over, which no server has
          // escaped yet: a right-to-left override would make a `.exe` read
          // as a `.png` in the one place a person watches it land.
          const name = shownName(row.name);
          return (
            <li
              key={row.uploadId}
              className="alk-files-uploads__row"
              data-asking={prompt === null ? undefined : "true"}
            >
              {/* The whole name in `title`, and the middle elided rather than the
                  tail: the name sits in a fixed grid track, and a batch cut from
                  the right leaves every row in it reading the same. */}
              <span className="alk-name alk-files-uploads__name" title={name}>
                {middleEllipsis(name)}
              </span>
              {/* The bar carries its own state: a finished upload painted the browser's default
                  progress element, which is RED — the product's colour for failure — behind the
                  word "Uploaded". The tokens are chosen by `data-state`, in CSS. */}
              <progress
                className="alk-files-uploads__bar"
                data-state={row.state}
                value={row.sent}
                max={row.total}
                aria-label={`${name} progress`}
              />
              <span className="alk-meta alk-tnum alk-files-uploads__status">
                {label(row, prompt !== null)}
              </span>
              {/* Always rendered, even empty: it is a fixed grid track, so the bar and the
                  word sit in the same place on a row that still has Pause and Cancel and on
                  one that has finished. */}
              <span className="alk-files-uploads__actions">
                {row.state === "uploading" || row.state === "completing" ? (
                  <Button size="sm" fill="ghost" onClick={() => onPause(row.uploadId)}>
                    Pause
                  </Button>
                ) : null}
                {row.state === "paused" ? (
                  <Button size="sm" fill="ghost" onClick={() => onResume(row.uploadId)}>
                    Resume
                  </Button>
                ) : null}
                {/* A failure is the one terminal state with something left to do:
                    the session is resumed where the server still holds it and
                    started over where it does not, so one control covers both. */}
                {row.state === "failed" && onRetry ? (
                  <RetryControl row={row} onRetry={onRetry} />
                ) : null}
                {TERMINAL_STATES.has(row.state) ? null : (
                  <Button
                    size="sm"
                    fill="ghost"
                    variant="destructive"
                    onClick={() => onCancel(row.uploadId)}
                  >
                    Cancel
                  </Button>
                )}
              </span>
              {reason(row) === null ? null : (
                <span className="alk-files-uploads__why">{reason(row)}</span>
              )}
              {row.outcome === undefined ? null : (
                <span className="alk-files-uploads__why">{row.outcome}</span>
              )}
              {prompt === null ? null : <Question prompt={prompt} onAnswer={onAnswerConflict} />}
            </li>
          );
        })}
      </ul>

      {alreadyInFiles > 0 && finished > 0 ? (
        <p className="alk-meta">
          {alreadyInFiles} of {finished} already in Files
        </p>
      ) : null}

      {skippedSidecars > 0 ? (
        <p className="alk-meta">
          {skippedSidecars === 1
            ? "1 system file skipped"
            : `${skippedSidecars} system files skipped`}
        </p>
      ) : null}

      {/* A copy is folded away rather than uploaded twice, so this line is the
          only place the drop's own arithmetic adds up. */}
      {identicalCopies > 0 ? (
        <p className="alk-meta">
          {identicalCopies === 1
            ? "1 identical copy was skipped"
            : `${identicalCopies} identical copies were skipped`}
        </p>
      ) : null}
    </section>
  );
}
