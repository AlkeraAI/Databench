// Repo overview: the result is three sections in one text blob -- a header of
// `Key: value` facts, an optional entrypoint list, and an indented tree where two
// spaces are one level and a trailing slash marks a directory. The header is what
// a reader wants first (which stacks this repo is built on, where the code starts),
// so the profile leads and the tree follows it. The tool stops listing at its own
// cap, so the extent line says when the tree is only a sample.
import type { CSSProperties, ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { Text } from "../sharedUi";
import { IconGitBranch } from "@tabler/icons-react";
import { count, field, num, str } from "./alkeraPayload";
import { Band, EmptyLine, EntryMark, LeafPath } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import "./repo_overview.css";
interface Entrypoint {
  /** What package.json called this path (`main`, `bin`, `exports`), or `file`
   *  for one the scan recognized by name. */
  role: string;
  value: string;
}
interface TreeRow {
  name: string;
  level: number;
  dir: boolean;
}
interface Overview {
  target: string;
  branch: string;
  ecosystems: string[];
  packageManager: string;
  dependencies: string[];
  entrypoints: Entrypoint[];
  tree: TreeRow[];
  truncated: boolean;
  files: number;
}
interface Sections {
  header: string[];
  entrypoints: string[];
  tree: string[];
  truncated: boolean;
}
function split(text: string): Sections {
  const sections: Sections = { header: [], entrypoints: [], tree: [], truncated: false };
  let into: keyof Omit<Sections, "truncated"> = "header";
  for (const raw of text.split("\n")) {
    const trimmed = raw.trim();
    if (trimmed.length === 0) continue;
    if (trimmed === "Likely entrypoints:") {
      into = "entrypoints";
      continue;
    }
    if (trimmed === "Top-level structure:") {
      into = "tree";
      continue;
    }
    if (trimmed === "(Structure truncated)") {
      sections.truncated = true;
      continue;
    }
    // The tree's own indentation is its nesting, so those lines keep it.
    if (into === "tree") sections.tree.push(raw);
    else sections[into].push(trimmed);
  }
  return sections;
}
function commaList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}
function readEntrypoints(lines: string[]): Entrypoint[] {
  return lines.map((line) => {
    const body = line.startsWith("- ") ? line.slice(2) : line;
    const colon = body.indexOf(":");
    if (colon === -1) return { role: "", value: body };
    return { role: body.slice(0, colon).trim(), value: body.slice(colon + 1).trim() };
  });
}
function readTree(lines: string[]): TreeRow[] {
  return lines.map((raw) => {
    const name = raw.trim();
    const indent = raw.length - raw.trimStart().length;
    const dir = name.endsWith("/");
    return { name: dir ? name.slice(0, -1) : name, level: Math.floor(indent / 2), dir };
  });
}
function deriveOverview(part: ToolConversationPart): Overview {
  const sections = split(str(part.output) || str(part.content));
  const tree = readTree(sections.tree);
  return {
    target: str(part.input?.repository) || field(sections.header, "repository") || field(sections.header, "path"),
    branch: field(sections.header, "branch"),
    ecosystems: commaList(field(sections.header, "ecosystems")),
    packageManager: field(sections.header, "package manager"),
    dependencies: commaList(field(sections.header, "dependency files")),
    entrypoints: readEntrypoints(sections.entrypoints),
    tree,
    truncated: sections.truncated,
    files: tree.filter((row) => !row.dir).length,
  };
}
/** What the repository is built on, in one row of chips. */
function Profile({ view }: { view: Overview }): ReactElement | null {
  const chips = [...view.ecosystems, ...(view.packageManager ? [view.packageManager] : [])];
  if (chips.length === 0 && view.dependencies.length === 0 && !view.branch) return null;
  return (
    <p className="chat-repo-profile">
      {view.branch ? (
        <span className="chat-repo-chip chat-tool-chip" data-chip="branch">
          <IconGitBranch size={13} stroke={1.7} aria-hidden="true" />
          {view.branch}
        </span>
      ) : null}
      {chips.map((chip) => (
        <span key={chip} className="chat-repo-chip chat-tool-chip" data-chip="stack">
          {chip}
        </span>
      ))}
      {view.dependencies.map((file) => (
        <span key={file} className="chat-repo-chip chat-tool-chip chat-tool-mono" data-chip="dep">
          {file}
        </span>
      ))}
    </p>
  );
}
function Entrypoints({ entrypoints }: { entrypoints: Entrypoint[] }): ReactElement | null {
  if (entrypoints.length === 0) return null;
  return (
    <>
      <p className="chat-repo-cap">Entrypoints</p>
      <ul className="chat-repo-entries">
        {entrypoints.map((entry, index) => (
          <li key={`${entry.role}-${index}`} className="chat-repo-entry">
            {entry.role ? <span className="chat-repo-entry__role">{entry.role}</span> : null}
            <Text className="chat-tool-mono chat-tool-clip" tooltip="truncate">
              {entry.value}
            </Text>
          </li>
        ))}
      </ul>
    </>
  );
}
function Tree({ rows }: { rows: TreeRow[] }): ReactElement | null {
  if (rows.length === 0) return null;
  return (
    <>
      <p className="chat-repo-cap">Structure</p>
      <div className="chat-repo-tree" data-cap="260">
        {rows.map((row, index) => (
          <div
            key={`${row.name}-${index}`}
            className="chat-repo-tree__row chat-tool-line"
            data-dir={row.dir ? "" : undefined}
            style={{ "--chat-repo-level": row.level } as CSSProperties}
          >
            <span className="chat-repo-tree__icon chat-tool-glyph" aria-hidden="true">
              <EntryMark name={row.name} dir={row.dir} />
            </span>
            <Text className="chat-repo-tree__name chat-tool-mono chat-tool-clip" tooltip="truncate">
              {row.name}
            </Text>
          </div>
        ))}
      </div>
    </>
  );
}
/** The well's interior: what the repository is, then how it is laid out. It
 *  paints no ground, edge, radius, or outer pad; the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const view = deriveOverview(part);
  const depth = num(part.input?.depth);
  const bare =
    view.tree.length === 0
    && view.entrypoints.length === 0
    && view.ecosystems.length === 0
    && view.dependencies.length === 0
    && !view.branch
    && !view.packageManager;
  return (
    <div data-tool="repo_overview">
      <Band>
        <Text className="chat-tool-band__text chat-tool-band__text--one chat-tool-mono" tooltip="truncate" tooltipLabel={view.target}>
          <LeafPath path={view.target} />
        </Text>
        {depth === null ? null : <span className="chat-tool-band__param">depth {depth}</span>}
      </Band>
      {bare ? <EmptyLine>The overview came back empty.</EmptyLine> : null}
      <Profile view={view} />
      <Entrypoints entrypoints={view.entrypoints} />
      <Tree rows={view.tree} />
    </div>
  );
}
/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part) => {
  const view = deriveOverview(part);
  return {
    object: view.target,
    data: { kind: "count", text: count(view.files, "file", "files") },
    body: <Body part={part} />,
    footer: view.truncated ? "structure truncated" : undefined,
  };
};
