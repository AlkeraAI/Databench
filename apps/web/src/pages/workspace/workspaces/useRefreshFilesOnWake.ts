// The Files pane catches up the moment a chat wakes.
//
// While a chat sleeps its files are the saved copy on the drive; once its box
// takes the chat back they are live again. The pane learns that from the
// folder's lease, and a lease taken while the pane was already open could
// leave it on "Saved" until a reload. So the transition the server reports
// (asleep, waking or queued, then awake or working) re-reads the Files family once.

import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { keys } from "../../../api/keys";

import type { ChatSessionRead } from "../../../api/chats";

/** Where a chat's agent session stands, as the server says it. */
export type SessionState = NonNullable<ChatSessionRead["session_state"]>;

const SLEEPING: ReadonlySet<SessionState | null> = new Set<SessionState | null>([
  "asleep",
  "starting",
  "waking",
  "queued",
]);
const SERVED: ReadonlySet<SessionState | null> = new Set<SessionState | null>(["awake", "working"]);

export function useRefreshFilesOnWake(life: SessionState | null): void {
  const queryClient = useQueryClient();
  const last = useRef(life);
  useEffect(() => {
    const was = last.current;
    last.current = life;
    if (SLEEPING.has(was) && SERVED.has(life)) {
      void queryClient.invalidateQueries({ queryKey: keys.files.all });
    }
  }, [life, queryClient]);
}
