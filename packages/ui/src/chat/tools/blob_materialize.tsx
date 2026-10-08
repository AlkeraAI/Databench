// blob.materialize turns a held result into a real file the agent can run its own
// code over, so the well reads as that conversion: the handle it came from above
// the file it became. The file band is the card's one actionable thing, so it is
// wired to open in the host's editor.

import type { ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";
import { IconFileExport } from "@tabler/icons-react";

import { count, num, readResult, str, strings } from "./alkeraPayload";
import { bytes, ColumnChips, HandleBand } from "./blob";
import { PathBand } from "./shared";
import type { CardHead, StepEnvironment } from "./step";
import "./shared.css";
import "./blob.css";

interface FileView {
  handle: string;
  format: string;
  path: string;
  rowCount: number;
  charCount: number;
  columns: string[];
  size: number;
}

function deriveFile(part: ToolConversationPart): FileView {
  const result = readResult(part.output);
  return {
    handle: str(part.input?.handle),
    format: str(result?.format) || str(part.input?.format),
    path: str(result?.path),
    rowCount: num(result?.row_count) ?? 0,
    charCount: num(result?.char_count) ?? 0,
    columns: strings(result?.columns),
    size: num(result?.bytes) ?? 0,
  };
}

/** The well's interior: the result the file came from, the file itself, and the
 *  columns it carries. It paints no ground, edge, radius, or outer pad; the
 *  group's well owns those. */
function Body({ part, env }: { part: ToolConversationPart; env?: StepEnvironment }): ReactElement {
  const view = deriveFile(part);
  return (
    <div data-tool="blob">
      <HandleBand
        handle={view.handle}
        glyph={<IconFileExport size={14} stroke={1.6} />}
        param={view.format || undefined}
      />
      {view.path ? (
        <PathBand
          path={view.path}
          relativeTo={env?.workspaceRoot}
          onOpen={env?.onOpenPath ? () => env.onOpenPath?.(view.path) : undefined}
        />
      ) : null}
      <ColumnChips columns={view.columns} />
    </div>
  );
}

/** The step this tool contributes to the transcript's tool group. */
export const head: CardHead = (part, env) => {
  const view = deriveFile(part);
  // A text blob writes verbatim and reports characters; a tabular one reports rows.
  const written =
    view.charCount > 0 ? count(view.charCount, "character", "characters") : count(view.rowCount, "row", "rows");
  return {
    object: view.path,
    data: { kind: "count", text: `${written}, ${bytes(view.size)}` },
    body: <Body part={part} env={env} />,
  };
};
