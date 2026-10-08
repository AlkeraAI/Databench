// What a drop onto the browser means, decided before anything is sent.
//
// Two shapes arrive on the same event and must not be confused:
//
//  * a drag that started inside the browser carries {@link MOVE_MIME} and is a
//    MOVE onto the folder row or breadcrumb segment under the pointer;
//  * a drag from the desktop carries entries, and is an UPLOAD.
//
// The upload path exists because a browser lies about folders: `FileList` from
// a `webkitdirectory` picker or a naive drop omits EMPTY directories entirely,
// so a dropped tree would silently lose them. `webkitGetAsEntry` does not — it
// hands back a directory entry whose reader simply yields nothing. So the
// expansion walks entries, records every directory it meets (empty or not) and
// only then collects files, which is what lets one `tree` call recreate the
// skeleton exactly as it sat on the laptop.
//
// The directory reader is also quietly hostile: `readEntries` returns a BATCH,
// not the whole listing, and signals the end with an empty batch. It is drained
// in a loop for that reason, never called once.

import { acceptsWrites } from "./writeAccess";

/** The desktop junk that must never become a node. macOS writes `.DS_Store`
 *  beside every browsed folder and `._name` AppleDouble sidecars onto foreign
 *  filesystems; both describe the Finder's view of a file, not content. */
export function isSidecar(name: string): boolean {
  return name === ".DS_Store" || name.startsWith("._");
}

/** The drag type an in-browser move carries. A desktop drag never has it, which
 *  is the whole test for "is this a move or an upload". */
export const MOVE_MIME = "application/x-alkera-files-move";

/** One file the drop is offering, with the folder it belongs in. */
export interface DroppedFile {
  file: File;
  /** Slash-joined, relative to the drop target. `photos/a.csv`, or `a.csv`. */
  relativePath: string;
  /** The folder part of {@link relativePath}; `""` for a file dropped at the top. */
  directory: string;
}

export interface DropExpansion {
  files: DroppedFile[];
  /** Every directory the drop contains, parents before children, including the
   *  ones with nothing in them. Slash-joined and relative, like the files. */
  directories: string[];
  /** Sidecars folded away. Counted so the tray can say so instead of pretending
   *  the drop was smaller than it was. */
  skippedSidecars: number;
}

/** The subset of `FileSystemEntry` this walk needs. Structural on purpose: the
 *  real DOM entry satisfies it, and a test can hand over a plain object without
 *  a jsdom shim. */
export interface DropEntry {
  readonly isFile: boolean;
  readonly isDirectory: boolean;
  readonly name: string;
  file?(onSuccess: (file: File) => void, onError?: (error: unknown) => void): void;
  createReader?(): DropDirectoryReader;
}

export interface DropDirectoryReader {
  readEntries(onSuccess: (entries: DropEntry[]) => void, onError?: (error: unknown) => void): void;
}

/** The subset of `DataTransferItem` the expansion reads. */
export interface DropItem {
  readonly kind: string;
  webkitGetAsEntry?(): DropEntry | null;
  getAsFile?(): File | null;
}

function entryFile(entry: DropEntry): Promise<File | null> {
  const read = entry.file;
  if (!read) return Promise.resolve(null);
  return new Promise((resolve) => {
    read.call(
      entry,
      (file) => resolve(file),
      () => resolve(null),
    );
  });
}

/** Drain one directory. `readEntries` answers a batch at a time and marks the
 *  end with an empty one, so a single call would silently truncate a big
 *  folder. */
async function readAll(reader: DropDirectoryReader): Promise<DropEntry[]> {
  const all: DropEntry[] = [];
  for (;;) {
    const batch = await new Promise<DropEntry[]>((resolve) => {
      reader.readEntries(
        (entries) => resolve(entries),
        () => resolve([]),
      );
    });
    if (batch.length === 0) return all;
    all.push(...batch);
  }
}

/**
 * Walk everything a drop offers.
 *
 * Breadth-first through an explicit queue rather than recursion, so a
 * `node_modules`-deep tree cannot blow the stack, and so directories are
 * emitted parents-first — the order `POST …/tree` needs to create them.
 */
export async function expandDrop(items: readonly DropItem[]): Promise<DropExpansion> {
  const files: DroppedFile[] = [];
  const directories: string[] = [];
  let skippedSidecars = 0;

  const queue: { entry: DropEntry; prefix: string }[] = [];

  for (const item of items) {
    if (item.kind !== "file") continue;
    const entry = item.webkitGetAsEntry?.() ?? null;
    if (entry) {
      queue.push({ entry, prefix: "" });
      continue;
    }
    // A browser (or a paste) with no entry API still gives the flat file.
    const flat = item.getAsFile?.() ?? null;
    if (!flat) continue;
    if (isSidecar(flat.name)) {
      skippedSidecars += 1;
      continue;
    }
    files.push({ file: flat, relativePath: flat.name, directory: "" });
  }

  while (queue.length > 0) {
    const next = queue.shift();
    if (!next) break;
    const { entry, prefix } = next;
    const relativePath = prefix ? `${prefix}/${entry.name}` : entry.name;

    if (isSidecar(entry.name)) {
      skippedSidecars += 1;
      continue;
    }

    if (entry.isDirectory) {
      // Recorded BEFORE its children are read, and recorded even when the read
      // yields nothing — an empty folder is the case the FileList path loses.
      directories.push(relativePath);
      const reader = entry.createReader?.();
      if (!reader) continue;
      for (const child of await readAll(reader)) {
        queue.push({ entry: child, prefix: relativePath });
      }
      continue;
    }

    const file = await entryFile(entry);
    if (!file) continue;
    files.push({ file, relativePath, directory: prefix });
  }

  return { files, directories, skippedSidecars };
}

/** What a folder row or breadcrumb segment must look like to accept a drop. */
export interface DropTarget {
  id: string;
  name: string;
  kind?: string;
  capabilities?: { can_write?: boolean } | null;
  /** A machine holds the folder under a lease and the surface shows it
   *  read-only for as long as that lasts. The grant is untouched; the writer
   *  is elsewhere. */
  leased?: boolean;
}

export type DropVerdict =
  | { accepted: true }
  | { accepted: false; reason: string };

/** Why nothing lands in a folder a machine is holding, named for the folder. */
export function leasedFolderRefusal(name: string): string {
  return `${name} is leased and read-only until the lease ends.`;
}

/**
 * Whether this target can be dropped onto, and the sentence to show when it
 * cannot. Refusal is decided here rather than by the request failing, so
 * nothing is ever sent to a folder the caller has no write on. A missing
 * grant is named before a lease: it is the refusal that outlives the lease.
 */
export function dropVerdict(target: DropTarget | null): DropVerdict {
  if (!target) return { accepted: false, reason: "Drop onto a folder to put files there." };
  if (target.kind !== undefined && target.kind !== "folder") {
    return { accepted: false, reason: `${target.name} is not a folder.` };
  }
  if (!acceptsWrites(target)) {
    return { accepted: false, reason: `You do not have permission to add to ${target.name}.` };
  }
  if (target.leased === true) {
    return { accepted: false, reason: leasedFolderRefusal(target.name) };
  }
  return { accepted: true };
}

/** One row being dragged. The `etag` rides along because the move it becomes
 *  is a precondition write, and the drop target has no other way to know the
 *  version the user was looking at when they picked the row up. */
export interface MoveSubject {
  id: string;
  etag: string;
}

/** Mark a drag that started on a row, so the drop knows it is a move. */
export function writeMovePayload(
  transfer: Pick<DataTransfer, "setData">,
  subjects: readonly MoveSubject[],
): void {
  transfer.setData(MOVE_MIME, JSON.stringify(subjects));
}

/** The rows an in-browser drag is carrying, or `null` for a desktop drop. */
export function readMovePayload(transfer: Pick<DataTransfer, "getData">): MoveSubject[] | null {
  const raw = transfer.getData(MOVE_MIME);
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed)) return null;
  const subjects = parsed.filter(
    (entry): entry is MoveSubject =>
      typeof entry === "object" &&
      entry !== null &&
      typeof (entry as MoveSubject).id === "string" &&
      typeof (entry as MoveSubject).etag === "string",
  );
  return subjects.length > 0 ? subjects : null;
}

/** Pair the paths that were asked for with the nodes the `tree` call created,
 *  so each dropped file knows which folder id it belongs under. */
export function indexTree(
  paths: readonly string[],
  items: readonly { id: string; name: string; path?: string | null }[],
): Record<string, string> {
  const byPath: Record<string, string> = {};
  for (const wanted of paths) {
    const leaf = wanted.split("/").pop() ?? wanted;
    const match =
      items.find((item) => {
        const full = item.path ?? "";
        return full === wanted || full.endsWith(`/${wanted}`);
      }) ?? items.find((item) => item.name === leaf);
    if (match) byPath[wanted] = match.id;
  }
  return byPath;
}
