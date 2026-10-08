// Write: a create returns a FILE, so the card is that file. The head carries
// the insertion count off the resource's own diff, and the well opens on the
// file itself under a numbered gutter, highlighted by the file's extension.
// A written file is bounded and IS the answer, so it opens itself, whole, with
// no sampling footer.

import type { ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";

import { codeLines, languageFor, paintLeaves } from "../syntax";
import { str, toolPath } from "./alkeraPayload";
import { PathBand } from "./shared";
import type { CardHead, StepEnvironment } from "./step";
import "./shared.css";

function authored(part: ToolConversationPart): {
  path: string;
  content: string;
} {
  const resource = part.resources?.[0];
  const path = toolPath(part.input) || resource?.path || resource?.target || "";
  const content = str(part.input?.content) || resource?.preview?.content || "";
  return { path, content };
}

/** The well's interior: the file the write landed in, then the text it landed
 *  there. It paints no ground, edge, radius, or outer pad; the group's well
 *  owns those. */
function Body({
  part,
  env,
}: {
  part: ToolConversationPart;
  env?: StepEnvironment;
}): ReactElement {
  const { path, content } = authored(part);
  const lines = content.length > 0 ? codeLines(content, languageFor(path)) : [];
  return (
    <div data-tool="write">
      <PathBand
        path={path}
        relativeTo={env?.workspaceRoot}
        onOpen={
          env?.onOpenPath && path ? () => env.onOpenPath?.(path) : undefined
        }
      />
      <div className="chat-tool-src chat-tool-mono">
        {lines.map((line, i) => (
          <div key={i} className="chat-tool-src__line">
            <span className="chat-tool-src__n" aria-hidden="true">
              {i + 1}
            </span>
            <span className="chat-tool-src__code">{paintLeaves(line)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** The card's word for a call still in flight. A write that has not run yet
 *  must not read as a file already on disk, and the whole streaming window sits
 *  in that state, so the verb stays in progress until the call settles.
 *  `undefined` keeps the card's own settled word. */
function inFlightVerb(state: ToolConversationPart["state"]): string | undefined {
  return state === "pending" || state === "running" ? "Writing…" : undefined;
}

/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part, env) => {
  const insertions = part.resources?.[0]?.diff?.insertions ?? 0;
  const deletions = part.resources?.[0]?.diff?.deletions ?? 0;
  const { path } = authored(part);
  return {
    object: path,
    verb: inFlightVerb(part.state),
    data: path
      ? { kind: "diff", added: insertions, removed: deletions }
      : undefined,
    // A pending call has no input yet (the model is still streaming the
    // arguments), so there is no file to show -- an empty open well would
    // read as a broken card for the whole streaming window.
    body: path ? <Body part={part} env={env} /> : undefined,
  };
};
