// Inline uploads: the pure half. A reader pastes, drops or picks a file; the
// composer puts a pill in the field — `[Image 1]`, `[File 1]`, numbered per
// kind — stages the bytes, and uploads them at once through the shell's
// uploader. On send each pill becomes the transcript's one form for a chat
// file: an image `![Image 1](uploads/paste-1-ab12.png)`, a file `[File 1: q.csv]
// (uploads/file-1-ab12.csv)`, both paths relative to the chat's effective root (the
// agent's working directory), so the model reads exactly what the renderer
// shows and the file is readable at that path. Everything here is data in,
// data out; the composer owns the state and the DOM.

import {
  UPLOAD_ACCEPTED_EXTENSIONS as ACCEPTED_EXTENSIONS,
  UPLOAD_ACCEPTED_SUMMARY as ACCEPTED_SUMMARY,
  UPLOAD_ATTEMPTS as ATTEMPTS,
  UPLOAD_RETRY_BASE_MS,
} from "../../theme/limits";
import { doublingDelay } from "../../backoff";

export type UploadKind = "image" | "file";
/** `held`: staged, uploads on send (an uploader with `timing: "on-send"`). */
export type UploadState = "held" | "uploading" | "uploaded" | "failed";

export interface StagedUpload {
  /** Stable for the life of the pill. */
  id: string;
  kind: UploadKind;
  /** The pill's number within its kind, 1-based; renumbered on removal. */
  n: number;
  file: File;
  /** The name the reader picked — what a file pill's link says. */
  name: string;
  state: UploadState;
  /** 0-100 while the bytes go up, when the uploader reports it. */
  progress?: number;
  /** Where the bytes landed, relative to the chat root, once `uploaded`. */
  path?: string;
  error?: string;
  /** An object URL for an image pill's thumbnail; revoked with the pill. */
  previewUrl?: string;
}

export interface ComposerUploader {
  /** Send one file into the chat's root. Resolves with the path the message
   *  should reference (relative to that root); rejects with the reason. Every
   *  attempt gets the same `File`, never a consumed stream, so a retry sends
   *  the whole body again. */
  upload(
    file: File,
    hint: { kind: UploadKind; n: number; onProgress?: (percent: number) => void },
  ): Promise<{ path: string }>;
  /** Pause before attempt `attempt` (2-based). Defaults to a short backoff;
   *  tests pin it to zero. */
  retryDelayMs?: (attempt: number) => number;
  /** When the bytes go up. Absent, or `at-once`: the moment the pill lands,
   *  with Send held until every upload has. `on-send`: the file is held in
   *  the field and Send stays open; when the reader commits, every held pill
   *  is uploaded first and the message goes out once each has landed — for a
   *  composer whose chat does not exist yet and is only opened for a message,
   *  so nothing is uploaded into a chat that may never be written to. */
  timing?: "at-once" | "on-send";
  /** Once per commit with held pills, before any of them is uploaded, with
   *  the message as it stands: where an `on-send` uploader opens the chat the
   *  files go into. Rejecting keeps the text and every pill in the field and
   *  states the reason; nothing is uploaded and nothing is sent. */
  beforeSend?(message: string): Promise<void>;
  /** The largest file THIS transport can carry, when it has a bound of its
   *  own. A shell that uploads through the Files session API has none: its cap
   *  is the deployment's published file ceiling, which the server refuses
   *  against before a byte moves and in words the reader is shown verbatim, so
   *  restating it here would be a second ceiling that drifts. A shell whose
   *  transport is genuinely smaller — the VS Code webview stages a file as one
   *  base64 JSON-RPC message held whole in memory — declares that bound here
   *  and the composer refuses over it without an attempt. */
  maxBytes?: number;
  /** The file extensions this transport takes, lower case and without the dot,
   *  overriding the composer's own list. For a deployment that publishes one.
   *  An empty array accepts everything, which is what a host that has no list
   *  and wants none should pass. */
  accept?: readonly string[];
}

/** How many times one file is sent before its pill is withdrawn. */
export const UPLOAD_ATTEMPTS = ATTEMPTS;

export function defaultRetryDelayMs(attempt: number): number {
  return doublingDelay(attempt - 2, UPLOAD_RETRY_BASE_MS);
}

/** An upload as it is spoken: "Image 1", "File 2". */
export function uploadTokenLabel(kind: UploadKind, n: number): string {
  return kind === "image" ? `Image ${n}` : `File ${n}`;
}

/** The pill an upload is in the field: `[Image 1]`, `[File 2]`. */
export function uploadToken(kind: UploadKind, n: number): string {
  return `[${uploadTokenLabel(kind, n)}]`;
}

const TOKEN = /\[(Image|File) (\d+)\]/g;

/** Uploads renumbered 1..k within each kind, in their existing order. */
export function numberedUploads(uploads: StagedUpload[]): StagedUpload[] {
  const next: Record<UploadKind, number> = { image: 0, file: 0 };
  return uploads.map((u) => {
    next[u.kind] += 1;
    return u.n === next[u.kind] ? u : { ...u, n: next[u.kind] };
  });
}

/** `text` with every pill of `before` rewritten to its number in `after`
 *  (matched by id). A single pass over the text, so `[Image 2]` → `[Image 1]`
 *  never collides with an `[Image 1]` that is also moving. */
export function renumberTokens(text: string, before: StagedUpload[], after: StagedUpload[]): string {
  const to = new Map<string, string>();
  for (const old of before) {
    const now = after.find((u) => u.id === old.id);
    if (now && now.n !== old.n) to.set(uploadToken(old.kind, old.n), uploadToken(now.kind, now.n));
  }
  if (to.size === 0) return text;
  return text.replace(TOKEN, (token) => to.get(token) ?? token);
}

/** `text` without `token`, closing the gap it leaves (one adjacent space). */
export function removeUploadToken(text: string, token: string): string {
  const at = text.indexOf(token);
  if (at === -1) return text;
  let end = at + token.length;
  let start = at;
  if (text[end] === " ") end += 1;
  else if (start > 0 && text[start - 1] === " ") start -= 1;
  return text.slice(0, start) + text.slice(end);
}

/** The transcript form of one uploaded pill. */
export function formatUploadMarkdown(upload: StagedUpload): string {
  const path = upload.path ?? "";
  // The pill's brackets are the field's; the transcript form carries the
  // label bare (`![Image 1](…)`), which is what the block parser reads back.
  return upload.kind === "image"
    ? `![Image ${upload.n}](${path})`
    : `[File ${upload.n}: ${upload.name.replace(/[\][]/g, "")}](${path})`;
}

/** The outgoing text: every uploaded pill replaced by its markdown. A pill
 *  that never finished has already been withdrawn from the text; a held one
 *  stays as its token, which is why a commit uploads the held pills first.
 *
 *  A pill on screen is a file on the message, whatever became of its token:
 *  the token only says WHERE it sits in the words. A pill whose token the
 *  reader typed over or deleted rides after the words, one per line, in pill
 *  order — dropping it sent a message without the files the reader could see
 *  attached, and left them uploaded into the chat with nothing pointing at
 *  them. */
export function replaceUploadTokens(text: string, uploads: StagedUpload[]): string {
  let out = text;
  const trailing: string[] = [];
  for (const u of uploads) {
    if (u.state !== "uploaded" || !u.path) continue;
    const token = uploadToken(u.kind, u.n);
    if (out.includes(token)) out = out.split(token).join(formatUploadMarkdown(u));
    else trailing.push(formatUploadMarkdown(u));
  }
  if (trailing.length === 0) return out;
  const words = out.trimEnd();
  return `${words}${words ? "\n\n" : ""}${trailing.join("\n")}`;
}

export function describeUploadSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const LIMIT_UNITS = ["B", "KB", "MB", "GB"] as const;

/** A ceiling as the product states it: decimal units FLOORED to a whole one —
 *  the same rendering the daemon's own refusal uses (`stage_file_size_label`),
 *  so one bound is never quoted to the same reader as two different figures.
 *
 *  Floored rather than rounded because the figure has to be a size the bound
 *  actually accepts: rounding the 10 MiB stage-file cap up read as "10.5 MB",
 *  which names ~14 KB of sizes that are in fact refused. A pill's tooltip keeps
 *  `describeUploadSize` — that is a file's size, which browsers report the
 *  binary way readers are used to. */
export function describeUploadLimit(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < LIMIT_UNITS.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${Math.floor(value)} ${LIMIT_UNITS[unit]}`;
}

/** Why a file cannot go on the message, or null when it can.
 *
 *  Only a transport that declares a bound of its own refuses here. Without one
 *  there is exactly one ceiling — the deployment's, refused by the server at
 *  the moment a session is opened and before any byte is sent — and the reader
 *  is shown the sentence the server wrote rather than a guess made locally. */
export function refuseUpload(
  file: File,
  maxBytes?: number,
  accept: readonly string[] = ACCEPTED_EXTENSIONS,
): string | null {
  if (maxBytes !== undefined && file.size > maxBytes) {
    return `${file.name} exceeded the maximum upload size of ${describeUploadLimit(maxBytes)}.`;
  }
  if (accept.length > 0 && !isAcceptedUpload(file.name, accept)) {
    // The rule is the file's type, read from its extension, so the sentence
    // names the type that was refused rather than only the kinds allowed.
    const ext = uploadExtension(file.name);
    const kind = ext === null ? "a file with no extension" : `a .${ext} file`;
    const takes = accept === ACCEPTED_EXTENSIONS ? ACCEPTED_SUMMARY : accept.map((e) => `.${e}`).join(", ");
    return `${file.name} can't be attached. A message takes ${takes}, not ${kind}.`;
  }
  return null;
}

/** Whether a name ends in one of the accepted extensions, case-insensitively.
 *  A name with no extension at all is refused: there is nothing to decide on,
 *  and silently uploading it is how a `.exe` became an attachment. */
export function isAcceptedUpload(
  name: string,
  accept: readonly string[] = ACCEPTED_EXTENSIONS,
): boolean {
  const ext = uploadExtension(name);
  return ext !== null && accept.includes(ext);
}

/** A name's extension, lower case and without the dot, or null for a name
 *  with none (`Makefile`, `trailing.`, a dotfile like `.gitignore`). */
export function uploadExtension(name: string): string | null {
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return null;
  return name.slice(dot + 1).toLowerCase();
}

/** What makes two picks the same file. A reader dropping a folder twice, or
 *  picking and then dropping, hands over the same bytes under the same name —
 *  two pills for one file is two uploads and two tokens in the message. */
export function uploadIdentity(file: File): string {
  return `${file.name}\u0000${String(file.size)}\u0000${String(file.lastModified)}`;
}

/** The picks worth staging: each distinct file once, in the order it arrived.
 *  `already` is what the field is holding, so a second drop of a file already
 *  pilled is dropped too. */
export function dedupeUploads(files: File[], already: readonly File[] = []): File[] {
  const seen = new Set(already.map(uploadIdentity));
  const out: File[] = [];
  for (const file of files) {
    const id = uploadIdentity(file);
    if (seen.has(id)) continue;
    seen.add(id);
    out.push(file);
  }
  return out;
}
