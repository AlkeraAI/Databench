// Read: a read is a window onto a file, so the card keeps the file's own line
// numbers and says exactly where the window sits. The tool returns a
// `<content>` envelope whose gutter carries real file line numbers and whose
// closing note states the file's true length; both are kept. The head's figure
// is the file's size, the footer is the window, and the two never say the same
// thing twice. The preview highlights by the file's extension.
//
// The same call also reads a directory, and the envelope declares which subject
// it got. A directory answers with an `<entries>` listing, so the card counts
// entries and shows the names, each kept exactly as the tool wrote it -- a
// trailing separator is how the tool marks a child directory.
import type { ReactElement } from "react";
import { IconFolder } from "@tabler/icons-react";
import { Text } from "../sharedUi";
import type { ToolConversationPart } from "@alkera/chat-model";
import { codeLeaves, languageFor, paintLeaves } from "../syntax";
import { count, str } from "./alkeraPayload";
import { EntryMark, PathBand } from "./shared";
import type { CardHead, StepEnvironment } from "./step";
import "./shared.css";
import s from "./read.module.css";
interface SourceLine {
  n: number;
  code: string;
}
interface Entry {
  name: string;
  dir: boolean;
}
interface FileView {
  kind: "file";
  lines: SourceLine[];
  /** The file's true length, from the envelope's closing note. */
  total: number | null;
}
interface DirView {
  kind: "directory";
  entries: Entry[];
  /** The directory's true size, from the listing's closing note. */
  total: number | null;
}
type View = FileView | DirView;
/** Strip the `<content>` envelope back to numbered lines plus the length note. */
function parseRead(output: string): FileView {
  const lines: SourceLine[] = [];
  let total: number | null = null;
  for (const raw of output.split("\n")) {
    const note = /\(File has (\d+) lines/.exec(raw);
    if (note) {
      total = Number(note[1]);
      continue;
    }
    if (raw === "<content>" || raw === "</content>") continue;
    const numbered = /^(\d+):\s?(.*)$/.exec(raw);
    if (numbered) lines.push({ n: Number(numbered[1]), code: numbered[2] });
  }
  return { kind: "file", lines, total };
}
/** Strip the `<entries>` envelope back to the names it holds. A name is kept
 *  verbatim, trailing separator and all, because that separator is the tool's
 *  own mark for a child directory. */
function parseListing(output: string): DirView {
  const entries: Entry[] = [];
  let total: number | null = null;
  let inside = false;
  for (const raw of output.split("\n")) {
    const line = raw.replace(/\r$/, "");
    if (line === "<entries>" || line === "</entries>") {
      inside = line === "<entries>";
      continue;
    }
    if (!inside || line.trim().length === 0) continue;
    // Both closing notes state the directory's real size: `(14 entries)` when
    // the listing is whole, `(Showing 14 of 200 entries...)` when it is a window.
    const note = /^\((?:Showing \d+ of )?(\d+) entries/.exec(line.trim());
    if (note) {
      total = Number(note[1]);
      continue;
    }
    entries.push({ name: line, dir: line.endsWith("/") });
  }
  return { kind: "directory", entries, total };
}
/** The envelope names its own subject. The path cannot: a file and a directory
 *  arrive through the same argument, spelled the same way. */
function declaredType(output: string): string {
  return str(/<type>([^<]*)<\/type>/.exec(output)?.[1])
    .trim()
    .toLowerCase();
}
function filePath(part: ToolConversationPart): string {
  return str(part.input?.filePath) || str(part.input?.path);
}
function view(part: ToolConversationPart): View {
  const output = str(part.output) || str(part.content);
  return declaredType(output) === "directory" ? parseListing(output) : parseRead(output);
}
/** The file's own lines under their own numbers. */
function Window({ lines, path }: { lines: SourceLine[]; path: string }): ReactElement {
  const language = languageFor(path);
  const last = lines[lines.length - 1]?.n;
  // The gutter is exactly as wide as the largest number it has to hold.
  const gutter = `${String(last ?? lines.length).length}ch`;
  return (
    <div className="chat-tool-src chat-tool-mono">
      {lines.map((line) => (
        <div key={line.n} className="chat-tool-src__line">
          <span className="chat-tool-src__n" style={{ width: gutter }} aria-hidden="true">
            {line.n}
          </span>
          <span className="chat-tool-src__code">{paintLeaves(codeLeaves(line.code, language))}</span>
        </div>
      ))}
    </div>
  );
}
/** The directory's children in the order the tool listed them, each name under
 *  the glyph for what it is. */
function Listing({ entries }: { entries: Entry[] }): ReactElement {
  return (
    <div className={s.list} data-cap="260">
      {entries.map((entry) => (
        <div key={entry.name} className={`${s.listRow} chat-tool-line`}>
          <span className="chat-tool-glyph" aria-hidden="true">
            <EntryMark name={entry.name} dir={entry.dir} />
          </span>
          <Text className="chat-tool-mono chat-tool-clip" tooltip="truncate">
            {entry.name}
          </Text>
        </div>
      ))}
    </div>
  );
}
/** The payload in the shape the envelope declared. One that parses to nothing
 *  renders nothing, so no empty frame stands in for an answer. */
function Interior({ shown, path }: { shown: View; path: string }): ReactElement | null {
  if (shown.kind === "directory") return shown.entries.length > 0 ? <Listing entries={shown.entries} /> : null;
  return shown.lines.length > 0 ? <Window lines={shown.lines} path={path} /> : null;
}
/** What a settled read that parsed to nothing says. A call still running has no
 *  output to parse yet, and a failed one has the group's failure line. */
function emptyNote(shown: View, state: ToolConversationPart["state"]): string | null {
  if (state !== "completed") return null;
  if (shown.kind === "directory") return shown.entries.length === 0 ? "This directory is empty." : null;
  return shown.lines.length === 0 ? "This read returned nothing to show." : null;
}
/** What the read returned: the path it came from, then the file's lines under
 *  their own numbers or the directory's entries. It paints no ground, edge, or
 *  radius; the group's well owns those. */
function Body({ part, env }: { part: ToolConversationPart; env?: StepEnvironment }): ReactElement {
  const path = filePath(part);
  const shown = view(part);
  const note = emptyNote(shown, part.state);
  return (
    <div data-tool="read">
      <PathBand path={path} relativeTo={env?.workspaceRoot} onOpen={env?.onOpenPath && path ? () => env.onOpenPath?.(path) : undefined} />
      <Interior shown={shown} path={path} />
      {note ? <p className="chat-tool-note">{note}</p> : null}
    </div>
  );
}
/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part, env) => {
  const path = filePath(part);
  const shown = view(part);
  // A pending call has no input yet (the arguments are still streaming),
  // so there is no file to open a window on.
  const body = path ? <Body part={part} env={env} /> : undefined;
  if (shown.kind === "directory") {
    const size = shown.total ?? shown.entries.length;
    return {
      object: path,
      icon: IconFolder,
      disclosure: "entries",
      data: path ? { kind: "count", text: count(size, "entry", "entries") } : undefined,
      body,
      footer: shown.entries.length < size ? `${shown.entries.length} of ${size} entries` : undefined,
    };
  }
  const size = shown.total ?? shown.lines.length;
  const first = shown.lines[0]?.n;
  const last = shown.lines[shown.lines.length - 1]?.n;
  const partial = shown.total !== null && shown.lines.length < shown.total;
  return {
    object: path,
    data: path ? { kind: "count", text: count(size, "line", "lines") } : undefined,
    body,
    footer:
      partial && first !== undefined && last !== undefined ? `lines ${first} to ${last} of ${shown.total}` : undefined,
  };
};
