// Glob: a path list is mostly repeated prefix, so the card says the shared
// directory once and gives the rest of the room to the filenames. The search
// root the caller passed is dropped, the directory a run of paths shares is
// hoisted into a header, and the tool's own newest-first order stays intact,
// because for glob that order is the answer to "what changed most recently".
import type { ReactElement } from "react";
import { LanguageIcon, Text } from "../sharedUi";
import type { ToolConversationPart } from "@alkera/chat-model";
import { count, groupRuns, str } from "./alkeraPayload";
import { Band, EmptyLine, splitLeaf } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import s from "./glob.module.css";
/** The paths the run returned, in the order it returned them, each stripped of
 *  the search root the caller passed: the band states that root once. */
function matchedPaths(part: ToolConversationPart): string[] {
  const root = str(part.input?.path).replace(/\/+$/, "");
  return (str(part.output) || str(part.content))
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .map((line) => (root && line.startsWith(`${root}/`) ? line.slice(root.length + 1) : line));
}
/** The well's interior: the pattern under the root it ran against, then the
 *  filenames grouped by the directory they share. It paints no ground or edge;
 *  the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const pattern = str(part.input?.pattern);
  const root = str(part.input?.path).replace(/\/+$/, "");
  const paths = matchedPaths(part);
  return (
    <div data-tool="glob">
      <Band>
        <span className="chat-tool-band__text chat-tool-mono">
          {root ? <span className={`${s.root} chat-tool-dir`}>{root}/</span> : null}
          {pattern}
        </span>
      </Band>
      {paths.length === 0 ? (
        <EmptyLine>No file matched this pattern.</EmptyLine>
      ) : (
        <div className={s.tree}>
          {groupRuns(paths, (path) => splitLeaf(path).dir).map((group) => (
            <div key={group.key} className={s.treeGroup}>
              <p className={`${s.treeDir} chat-tool-mono`}>{group.key || "."}</p>
              {group.items.map((path) => {
                const name = splitLeaf(path).leaf;
                return (
                  <div key={name} className={`${s.treeRow} chat-tool-line`}>
                    <span className="chat-tool-glyph" aria-hidden="true">
                      <LanguageIcon path={name} size={16} />
                    </span>
                    {/* The row shows the name alone, so the tip is the only place
                        the match's whole path is available. */}
                    <Text className="chat-tool-mono chat-tool-clip" tooltip="truncate" tooltipLabel={path}>
                      {name}
                    </Text>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part) => ({
  object: str(part.input?.pattern),
  data: { kind: "count", text: count(matchedPaths(part).length, "file", "files") },
  body: <Body part={part} />,
});
