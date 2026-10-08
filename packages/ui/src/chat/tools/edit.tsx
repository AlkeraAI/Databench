// Edit: an edit returns a CHANGE, and a change is only readable where it
// landed, so the patch is parsed into located hunks with the file's own line
// numbers. The `@@` header seeds both counters; an insertion advances the new
// file only, a deletion the old file only, context both. The gutter prints the
// old number on a deletion and the new number everywhere else, so a line's
// number is the number it has in the file the reader will open (the product's
// own diff-numbering rule, carried over unchanged). A patch with no `@@`
// header carries no positions and shows no numbers.
import type { CSSProperties, ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { Text } from "../sharedUi";
import { toolPath } from "./alkeraPayload";
import { EmptyLine, PathBand } from "./shared";
import type { CardHead, StepEnvironment } from "./step";
import "./shared.css";
import s from "./edit.module.css";
interface DiffLine {
  sign: "+" | "-" | " ";
  text: string;
  /** The line's number in the file, or null when the patch carried no `@@`. */
  number: number | null;
}
interface Hunk {
  /** The first line of the changed file this hunk covers, off the `@@` header. */
  start: number;
  /** The enclosing context the patch header already names. Empty when the tool
   *  emitted none. */
  context: string;
  lines: DiffLine[];
}
const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$/;
/** Split a unified patch into located hunks, numbering every line off the `@@`
 *  positions. The file header is dropped: the well's own band already names the
 *  file. */
function parsePatch(patch: string): Hunk[] {
  const hunks: Hunk[] = [];
  let oldLine: number | null = null;
  let newLine: number | null = null;
  for (const raw of patch.split("\n")) {
    const header = HUNK.exec(raw);
    if (header) {
      oldLine = Number(header[1]);
      newLine = Number(header[2]);
      hunks.push({ start: newLine, context: header[3].trim(), lines: [] });
      continue;
    }
    if (
      raw.startsWith("---") ||
      raw.startsWith("+++") ||
      raw.startsWith("diff ") ||
      raw.startsWith("index ")
    ) {
      continue;
    }
    const current = hunks[hunks.length - 1];
    if (!current) continue;
    const sign = raw.startsWith("+") ? "+" : raw.startsWith("-") ? "-" : " ";
    // A deletion is numbered in the file it left, everything else in the file the
    // reader will open.
    current.lines.push({
      sign,
      text: raw.slice(1),
      number: sign === "-" ? oldLine : newLine,
    });
    if (sign !== "+" && oldLine !== null) oldLine += 1;
    if (sign !== "-" && newLine !== null) newLine += 1;
  }
  return hunks;
}
/** The gutter is as wide as the largest number it prints, so the code column
 *  starts at one x for the whole patch. */
function gutterWidth(hunks: Hunk[]): number {
  const widest = hunks
    .flatMap((hunk) => hunk.lines)
    .reduce(
      (max, line) =>
        Math.max(max, line.number === null ? 0 : String(line.number).length),
      0,
    );
  return Math.max(widest, 2);
}
function changed(part: ToolConversationPart): { path: string; hunks: Hunk[] } {
  const resource = part.resources?.[0];
  const path = toolPath(part.input) || resource?.path || resource?.target || "";
  return { path, hunks: parsePatch(resource?.preview?.content ?? "") };
}
function HunkBlock({ hunk }: { hunk: Hunk }): ReactElement {
  return (
    <div>
      {/* The gutter states the lines, so the caption is only the block
          the change sits in; a patch with no context falls back to it. */}
      <p className={s.hunkAt}>
        {hunk.context ? (
          <Text className={s.hunkCtx} tooltip="truncate">
            {hunk.context}
          </Text>
        ) : (
          <span className={s.hunkLine}>Line {hunk.start}</span>
        )}
      </p>
      <div
        className={`${s.diff} chat-tool-mono`}
        role="group"
        aria-label={`Change at line ${hunk.start}`}
      >
        {hunk.lines.map((line, i) => (
          <div
            key={i}
            className={`${s.diffLine} chat-tool-diff__line`}
            data-tone={
              line.sign === "+" ? "add" : line.sign === "-" ? "del" : "ctx"
            }
          >
            <span className={s.diffNo} aria-hidden="true">
              {line.number ?? ""}
            </span>
            <span className="chat-tool-diff__sign" aria-hidden="true">
              {line.sign}
            </span>
            <span className="chat-tool-diff__code">{line.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Diff({ hunks }: { hunks: Hunk[] }): ReactElement {
  const gutter = gutterWidth(hunks);
  return (
    <div
      className="chat-tool-ruled"
      data-cap="300"
      style={{ "--chat-edit-gutter": `${gutter}ch` } as CSSProperties}
    >
      {hunks.map((hunk, h) => (
        <HunkBlock key={h} hunk={hunk} />
      ))}
    </div>
  );
}

/** Render a unified patch with the same located-hunk treatment as an edit card. */
export function PatchDiff({ patch }: { patch: string }): ReactElement {
  const hunks = parsePatch(patch);
  return hunks.length === 0 ? (
    <EmptyLine>The edit carries no located diff.</EmptyLine>
  ) : (
    <Diff hunks={hunks} />
  );
}

/** The well's interior: the file the change landed in, then the located hunks.
 *  It paints no ground, edge, radius, or outer pad; the group's well owns those. */
function Body({
  part,
  env,
}: {
  part: ToolConversationPart;
  env?: StepEnvironment;
}): ReactElement {
  const { path, hunks } = changed(part);
  return (
    <div data-tool="edit">
      <PathBand
        path={path}
        relativeTo={env?.workspaceRoot}
        onOpen={
          env?.onOpenPath && path ? () => env.onOpenPath?.(path) : undefined
        }
      />
      {hunks.length === 0 ? (
        <EmptyLine>
          {part.state === "error"
            ? "Nothing was changed."
            : "The edit landed without a diff to show."}
        </EmptyLine>
      ) : (
        <Diff hunks={hunks} />
      )}
    </div>
  );
}
/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part, env) => {
  const insertions = part.resources?.[0]?.diff?.insertions ?? 0;
  const deletions = part.resources?.[0]?.diff?.deletions ?? 0;
  const { path } = changed(part);
  return {
    object: path,
    data: path
      ? { kind: "diff", added: insertions, removed: deletions }
      : undefined,
    // A pending call has no input yet (the arguments are still streaming),
    // so there is no diff to show -- no body until the path lands.
    body: path ? <Body part={part} env={env} /> : undefined,
  };
};
