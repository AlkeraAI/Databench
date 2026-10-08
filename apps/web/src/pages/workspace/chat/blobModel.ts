// What a chat's turns say about the large tool results it produced. Shared by
// the Results page, which lists them, and the chat chrome, which only counts
// them -- both read the transcript already in cache, so neither costs an RPC.

import {
  alkeraToolName,
  toolResultRecord,
  unwrapCallTool,
  type BlobReference,
  type ConversationTurn,
} from "@alkera/chat-model";

/** The file each held result was written out to in the chat's folder, by
 *  handle: every completed `blob.materialize` call in the chat, with the path
 *  the tool reported (the box's absolute path — the renderer checks it is this
 *  chat's). A result written out twice keeps the latest file.
 *
 *  This is what lets `[label](blob:<handle>)` open on a surface that cannot
 *  reach the held result itself: the agent's brief says to name a result that
 *  way, and it writes the result out before working on it, so the file is the
 *  same data under a name the Files pane can open. It reads only what the tool
 *  said — never a name guessed from the handle, which the agent may override. */
export function materializedResults(turns: ConversationTurn[]): Map<string, string> {
  const files = new Map<string, string>();
  for (const turn of turns) {
    for (const raw of turn.parts) {
      if (raw.kind !== "tool" || raw.state !== "completed") continue;
      const part = unwrapCallTool(raw);
      if (alkeraToolName(part.name) !== "blob.materialize") continue;
      const handle = part.input?.handle;
      const path = toolResultRecord(part.output)?.path;
      if (typeof handle === "string" && handle !== "" && typeof path === "string" && path !== "") {
        files.set(handle, path);
      }
    }
  }
  return files;
}

/** Every distinct blob referenced by a tool part across the chat, newest first,
 *  deduped by handle (a result may be referenced more than once). */
export function collectBlobs(turns: ConversationTurn[]): BlobReference[] {
  const seen = new Set<string>();
  const out: BlobReference[] = [];
  for (const turn of turns) {
    for (const part of turn.parts) {
      if (part.kind !== "tool" || !part.references) continue;
      for (const reference of part.references) {
        if (reference.handle && !seen.has(reference.handle)) {
          seen.add(reference.handle);
          out.push(reference);
        }
      }
    }
  }
  out.reverse(); // newest results first
  return out;
}
