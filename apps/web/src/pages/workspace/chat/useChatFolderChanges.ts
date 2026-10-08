// When the transcript has to look its own files up again.
//
// A message names a file by a path inside the chat's folder, and the shell
// answers where that path is — once per path, because a transcript naming the
// same report in five messages must not cost five walks of the drive. That memo
// is right for a folder that is standing still and wrong for one a machine is
// writing into: a reference the agent wrote a second BEFORE the file landed is
// answered "nowhere", and without something to drop the answer the reader is
// told the file is not in the chat for as long as they keep the chat open.
//
// So the folder itself is watched. A node frame names the folder the changed
// node sits in, and the two folders a reference can be answered from are the
// chat's own node and the working directory it names — so a frame for either is
// the drive saying "ask again", and a frame for anybody else's folder costs
// nothing. The count it returns is the invalidation the renderer keys its memo
// on; a shell with no Files API behind it (the editor's webview) passes `null`
// and this asks for nothing at all.
//
// A machine's own work arrives differently, and watching only for node frames
// missed most of it. A chat's folder is MOUNTED to its box under a lease, and
// the live plane announces itself as one frame for the whole leased subtree.
// Landing new bytes does also raise a node frame carrying the parent — but a
// re-save of identical bytes raises nothing at all, and a rename, a new folder
// and a delete raise a node frame with no parent on it, which this cannot match.
// So the file the agent had just put there raised nothing this hook was
// listening for, the memo kept its "nowhere", and the reader was told the file
// was not in the chat until they reloaded. A lease frame naming either of the
// same two folders says the same thing a node frame does: ask again. (The Files
// tab beside the transcript has always read both, for the same reason — see
// `files/live/useFolderLiveness.ts`.)

import { useCallback, useEffect, useRef, useState } from "react";

import { useChat } from "@/api/chats";
import type { RealtimeEventFrame } from "@/api/events/eventMap";
import { useFrames } from "@/api/events/frameBus";
import { isMachineRate } from "@/api/events/machineRefresh";
import { useDrive, useItem } from "@/api/files";
import { MACHINE_REFRESH_MS } from "@/lib/limits";
import { chatFilesNodeOf } from "@/lib/files/chatFolder";

/** A machine saving continuously emits one frame per file per save. Frames
 *  landing inside this window raise one invalidation between them, so twenty
 *  saves in a quarter-second cost the transcript one pass rather than twenty. */
export const CHAT_FOLDER_COALESCE_MS = 250;

/**
 * How many times the chat's folder has changed under this transcript.
 *
 * `chatId` is `null` on a shell that cannot read the drive, which leaves every
 * read here disabled — the hook then answers a number that never moves.
 */
export function useChatFolderChanges(chatId: string | null): number {
  const chat = useChat(chatId ?? undefined);
  const drive = useDrive({ enabled: chatId !== null });
  const chatNodeId = chat.data?.files_node_id ?? undefined;
  // The same reads the page beside this surface already holds, under the same
  // keys, so knowing which folders to watch costs no request of its own.
  const chatNode = useItem(drive.data?.id, chatNodeId);
  const workingNodeId = chatFilesNodeOf(chatNode.data) ?? undefined;

  const [changes, setChanges] = useState(0);
  // Read through a ref inside the predicate: the ids arrive after the reads
  // settle, and re-subscribing on each would drop the frames landing in the gap.
  const folders = useRef<readonly (string | undefined)[]>([]);
  folders.current = [chatNodeId, workingNodeId];

  // A person's change (a rename, a trash) is answered within the coalescing
  // window. A machine saving raises a frame per save for minutes on end, so
  // after one pass those wait out `MACHINE_REFRESH_MS` from it, and the rest
  // of the burst lands in one more pass.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastPass = useRef(-Infinity);
  const due = useRef(0);
  const bump = useCallback((frame: RealtimeEventFrame) => {
    const machine = isMachineRate(frame) ? lastPass.current + MACHINE_REFRESH_MS - Date.now() : 0;
    const wait = Math.max(CHAT_FOLDER_COALESCE_MS, machine);
    if (timer.current !== null) {
      // A pass already waiting covers this frame, unless it waits out a
      // machine's window and this is a person's change, which goes sooner.
      if (due.current <= Date.now() + wait) return;
      clearTimeout(timer.current);
    }
    due.current = Date.now() + wait;
    timer.current = setTimeout(
      () => {
        timer.current = null;
        lastPass.current = Date.now();
        setChanges((count) => count + 1);
      },
      wait,
    );
  }, []);

  // Both folders are `undefined` until their reads settle, so a frame naming no
  // folder must be matched against neither: an unresolved id is not a wildcard.
  const watched = (named: string | undefined): boolean =>
    named !== undefined && folders.current.includes(named);

  useFrames((frame) => {
    if (frame.type === "file_node.changed") {
      return watched(frame.parent_id);
    }
    // The leased node is named in the payload; an older server names it only as
    // the entity, which is read the same way.
    if (frame.type === "file_lease.changed") {
      return watched(frame.lease_node_id ?? frame.entity_id);
    }
    return false;
  }, bump);

  // A pass queued for a transcript that is gone is a re-render nobody is
  // waiting for.
  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = null;
    },
    [],
  );

  return changes;
}
