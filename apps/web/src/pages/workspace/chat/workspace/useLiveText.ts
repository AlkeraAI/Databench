// A live document's text, as it stands, for a view that draws it rather than
// edits it: a Markdown note rendered beside its source, a CSV drawn as a table.
//
// It holds the same per-file live handle the editor does (`acquireLiveFile`),
// so a preview and an editor on one file in two groups are one document: what
// is typed into the editor is in the preview on the next change event, with no
// write back to the drive in between.

import { useEffect, useState } from "react";

import type { LiveSocket } from "@/api/realtime/crdt/channel";
import { acquireLiveFile } from "@/api/realtime/crdt/liveFile";
import type { LoroApi } from "@/api/realtime/crdt/loro";
import type { AccountScope } from "@/lib/accountScope";

export type LiveText =
  /** Not asked for: no socket here, or the view does not draw live text. */
  | { kind: "off" }
  | { kind: "pending" }
  | { kind: "live"; text: string; revision: number }
  /** The file cannot be held live; the caller draws the drive's copy, and
   *  offers back `unacknowledged` (typed here, never taken) when there is any. */
  | { kind: "fallback"; reason: string; unacknowledged: string };

const OFF: LiveText = { kind: "off" };

export function useLiveText(
  nodeId: string | undefined,
  socket: LiveSocket | undefined,
  options: { enabled: boolean; account: AccountScope | null; loadLoro?: () => Promise<LoroApi> },
): LiveText {
  const { enabled, account, loadLoro } = options;
  const [text, setText] = useState<LiveText>(OFF);

  useEffect(() => {
    if (!enabled || account === null || nodeId === undefined || socket === undefined) {
      setText(OFF);
      return;
    }
    setText({ kind: "pending" });
    const { file, release } = acquireLiveFile(nodeId, { socket, loadLoro, account });
    let revision = 0;
    let stopDoc: (() => void) | null = null;
    let stopChannel: (() => void) | null = null;

    const read = (): void => {
      const doc = file.channel.doc;
      const name = file.channel.textName;
      if (doc === null || name === null) return;
      revision += 1;
      setText({ kind: "live", text: doc.getText(name).toString(), revision });
    };
    const bind = (): void => {
      stopDoc?.();
      const doc = file.channel.doc;
      // Local commits and imported changes both land here: an edit typed in an
      // editor on this file, in any group of this tab, and one typed elsewhere.
      stopDoc = doc === null ? null : (doc.subscribe(() => read()) as () => void);
      read();
    };

    const stopFile = file.subscribe((state) => {
      if (state.kind === "live") {
        if (stopChannel === null) stopChannel = file.channel.listen({ replaced: () => bind() });
        bind();
      } else if (state.kind === "fallback") {
        setText({ kind: "fallback", reason: state.reason, unacknowledged: state.unacknowledged });
      }
    });
    return () => {
      stopFile();
      stopDoc?.();
      stopChannel?.();
      release();
    };
  }, [account, enabled, loadLoro, nodeId, socket]);

  return text;
}
