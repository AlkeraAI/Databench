// The notebook editor's document and cell-text ports over the live Loro
// notebook: the document is the `NotebookDocument` itself (it already speaks
// the port), and each cell's editor is a Loro CodeMirror binding on that
// cell's `source` text, sharing the notebook's one history and its carets.

import type { CellTextBinding, CellTextPort } from "@alkera/notebook-ui";

import type { LiveDocChannel } from "@/api/realtime/crdt/channel";
import { LoroCodeMirrorBinding } from "@/api/realtime/crdt/codeMirrorBinding";
import type { LoroApi } from "@/api/realtime/crdt/loro";
import type { NotebookDocument } from "@/api/realtime/crdt/notebookDoc";
import { liveCarets } from "@/pages/workspace/chat/workspace/liveCarets";

import type { NotebookCarets } from "./notebookCarets";

export function loroCellText(opts: {
  channel: LiveDocChannel;
  loro: LoroApi;
  doc: NotebookDocument;
  carets: NotebookCarets;
  hueOf: (who: { userId: string; email: string }) => number;
}): CellTextPort {
  const { channel, loro, doc, carets, hueOf } = opts;
  return {
    bind(cellId: string): CellTextBinding {
      const binding = new LoroCodeMirrorBinding({
        channel,
        loro,
        hueOf,
        carets: liveCarets,
        text: (loroDoc) => doc.sourceText(loroDoc, cellId),
        history: doc.sharedHistory,
        sharedCarets: true,
        onSelection: () => carets.publish(),
      });
      const detach = carets.attach(cellId, binding);
      return {
        text: () => binding.text(),
        extension: [binding.extension, binding.keys()],
        dispose: () => {
          detach();
          binding.dispose();
        },
      };
    },
  };
}
