// Two files in one drop that want the same name in the same folder.
//
// A folder holds one live node per name, so a drop carrying `notes.md` twice is
// asking for something the store cannot do — and the one outcome that must
// never happen is the second set of bytes going nowhere with nothing said. The
// drop therefore decides, BEFORE a session is opened:
//
//   * identical bytes → one upload. Nothing is lost by sending them once, and
//     the tray says how many copies it folded away.
//   * different bytes → every one of them is uploaded, the first under the name
//     it asked for and the rest under `rename`, which is the same commit
//     behaviour the person's own "Keep both" answer sends. A drop of three
//     hundred colliding files must not become three hundred questions, and the
//     row says what its answer was.
//
// Names are compared byte for byte, because that is how the folder compares
// them: `Note.txt` and `note.txt` are two names and two nodes, and folding them
// here would lose one of the two files the person chose.

import type { DroppedFile } from "./dropHandlers";

/** One file of a drop, with the commit behaviour its place in the drop earned.
 *  `rename` is only ever set for a file that a file EARLIER in the same drop is
 *  already taking the name of. */
export interface PlannedFile {
  dropped: DroppedFile;
  conflictBehavior: "fail" | "rename";
}

export interface BatchPlan {
  /** What to upload, in the order the drop offered it. */
  files: PlannedFile[];
  /** Copies not uploaded because a file of the same name in the same folder
   *  held the same bytes. */
  identicalCopies: number;
}

/** The digest of a whole file, injected so a test can pin it and so the drop
 *  can share the page's hashing pool rather than starting its own. */
export type ContentKey = (file: File) => Promise<string>;

/** The folder-and-name a file is competing for. The directory is the drop's own
 *  relative path, which is what decides the destination folder. */
function collisionKey(dropped: DroppedFile): string {
  return `${dropped.directory}\u0000${dropped.file.name}`;
}

/**
 * Settle every same-name collision a drop carries with itself.
 *
 * Only a group of two or more is hashed, and inside it only files that agree on
 * a size: bytes of different lengths are different bytes, so the cheap fact is
 * asked first and a drop with no collisions reads nothing at all. A file whose
 * digest cannot be taken — moved, unplugged, a drive that went away — is never
 * folded away on a guess; it is uploaded under `rename`, which costs a second
 * copy at worst and loses nothing.
 */
export async function planNames(
  files: readonly DroppedFile[],
  contentKey: ContentKey,
): Promise<BatchPlan> {
  const groups = new Map<string, DroppedFile[]>();
  for (const dropped of files) {
    const key = collisionKey(dropped);
    const held = groups.get(key);
    if (held) held.push(dropped);
    else groups.set(key, [dropped]);
  }

  // What each colliding file's bytes are, for the groups that have a collision.
  const content = new Map<DroppedFile, string>();
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    const bySize = new Map<number, DroppedFile[]>();
    for (const dropped of group) {
      const sized = bySize.get(dropped.file.size);
      if (sized) sized.push(dropped);
      else bySize.set(dropped.file.size, [dropped]);
    }
    for (const [size, sized] of bySize) {
      if (sized.length < 2) continue;
      for (const dropped of sized) {
        try {
          content.set(dropped, `${size}:${await contentKey(dropped.file)}`);
        } catch {
          // Unreadable here is not "identical": it is uploaded like any other.
        }
      }
    }
  }

  const kept = new Set<DroppedFile>();
  const renamed = new Set<DroppedFile>();
  let identicalCopies = 0;
  for (const group of groups.values()) {
    // The bytes each distinct upload in this group is carrying, so the second
    // file holding them is folded into the first rather than sent again.
    const seen = new Map<string, DroppedFile>();
    let distinct = 0;
    for (const dropped of group) {
      const bytes = content.get(dropped);
      if (bytes !== undefined && seen.has(bytes)) {
        identicalCopies += 1;
        continue;
      }
      if (bytes !== undefined) seen.set(bytes, dropped);
      kept.add(dropped);
      // Everything after the first distinct upload in a group is landing beside
      // a name this drop is already taking.
      if (distinct > 0) renamed.add(dropped);
      distinct += 1;
    }
  }

  return {
    files: files
      .filter((dropped) => kept.has(dropped))
      .map((dropped) => ({
        dropped,
        conflictBehavior: renamed.has(dropped) ? ("rename" as const) : ("fail" as const),
      })),
    identicalCopies,
  };
}
