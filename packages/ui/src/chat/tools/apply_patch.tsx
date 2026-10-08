// Apply patch: one call rewrites several files, and only the call's own
// `patchText` carries the changes, so the roster is parsed out of the envelope
// (`*** Add|Update|Delete File:`, an optional `*** Move to:`, then `@@`
// sections). Those `@@` lines name the enclosing block rather than positions, so
// a hunk here has no line numbers to print and the sign column is the whole
// gutter.
//
// The well is a ledger of file rows -- the file's language mark, the change
// word, the path, and the diff figure -- and each row opens to its own diff, so
// a wide patch is navigated file by file instead of read as one stack. The
// language server's complaints attach to the file they name: a mark on the row,
// the callout inside its diff, because a patch that lands with a fresh type
// error is the one thing worth reading twice.

import { useState, type ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";
import { IconAlertTriangle, IconArrowUpRight } from "@tabler/icons-react";

import { LanguageIcon, Text } from "../sharedUi";

import { count, str } from "./alkeraPayload";
import { Capped, EmptyLine, LeafPath } from "./shared";
import type { CardHead, StepEnvironment } from "./step";
import "./shared.css";
import s from "./apply_patch.module.css";

type PatchOp = "add" | "update" | "delete";
/** What the section did to its file, with a rename told apart from a plain edit. */
type Change = "added" | "updated" | "renamed" | "deleted";

interface PatchLine {
  sign: "+" | "-" | " ";
  text: string;
}

interface PatchHunk {
  /** The enclosing block the `@@` line names. Empty when the section carried none. */
  context: string;
  lines: PatchLine[];
}

interface PatchFile {
  op: PatchOp;
  path: string;
  movedTo: string;
  hunks: PatchHunk[];
  added: number;
  removed: number;
}

const FILE_HEAD = /^\*\*\* (Add|Update|Delete) File: (.+)$/;
const MOVE = /^\*\*\* Move to: (.+)$/;
const DIAGNOSTIC_HEAD = /^LSP errors detected in (.+), please fix:$/;

const OP_BY_MARKER: Record<string, PatchOp> = { Add: "add", Update: "update", Delete: "delete" };
const CHANGE_WORD: Record<Change, string> = {
  added: "Added",
  updated: "Updated",
  renamed: "Renamed",
  deleted: "Deleted",
};

function changeOf(file: PatchFile): Change {
  if (file.op === "add") return "added";
  if (file.op === "delete") return "deleted";
  return file.movedTo ? "renamed" : "updated";
}

/** The path the reader will open: a rename lands at its new name. */
function landing(file: PatchFile): string {
  return file.movedTo || file.path;
}

/** An Add section carries only `+` lines with no `@@` above them, so its first
 *  line opens a hunk of its own. */
function hunkFor(file: PatchFile): PatchHunk {
  const last = file.hunks[file.hunks.length - 1];
  if (last) return last;
  const opened: PatchHunk = { context: "", lines: [] };
  file.hunks.push(opened);
  return opened;
}

function addLine(file: PatchFile, raw: string): void {
  const marked = raw.startsWith("+") || raw.startsWith("-") || raw.startsWith(" ");
  const sign = raw.startsWith("+") ? "+" : raw.startsWith("-") ? "-" : " ";
  // Only a marked line owns its first character; an unmarked one is context a
  // writer left bare, and slicing it would eat the code.
  hunkFor(file).lines.push({ sign, text: marked ? raw.slice(1) : raw });
  if (sign === "+") file.added += 1;
  if (sign === "-") file.removed += 1;
}

function parseEnvelope(patch: string): PatchFile[] {
  const files: PatchFile[] = [];
  for (const raw of patch.split("\n")) {
    const head = FILE_HEAD.exec(raw);
    if (head) {
      files.push({ op: OP_BY_MARKER[head[1]], path: head[2].trim(), movedTo: "", hunks: [], added: 0, removed: 0 });
      continue;
    }
    const file = files[files.length - 1];
    if (!file) continue;
    const move = MOVE.exec(raw);
    if (move) {
      file.movedTo = move[1].trim();
      continue;
    }
    // The envelope's own markers, and the padding between sections, are not content.
    if (raw.startsWith("***") || raw.length === 0) continue;
    if (raw.startsWith("@@")) {
      file.hunks.push({ context: raw.slice(2).trim(), lines: [] });
      continue;
    }
    addLine(file, raw);
  }
  return files;
}

/** The language server's complaints, keyed by the path the result named them under. */
function readDiagnostics(text: string): Map<string, string[]> {
  const byPath = new Map<string, string[]>();
  let current: string[] | null = null;
  for (const raw of text.split("\n")) {
    const head = DIAGNOSTIC_HEAD.exec(raw.trim());
    if (head) {
      current = [];
      byPath.set(head[1], current);
      continue;
    }
    if (current && raw.trim().length > 0) current.push(raw.trim());
  }
  return byPath;
}

/** The result names files relative to the worktree while the envelope names them
 *  as the model wrote them, so either can be the other's tail. */
function diagnosticsFor(byPath: Map<string, string[]>, file: PatchFile): string[] {
  const target = landing(file);
  for (const [path, lines] of byPath) {
    if (path === target || path.endsWith(target) || target.endsWith(path)) return lines;
  }
  return [];
}

/** The diff figure a row trails with: the same spelling the step head prints. */
function Figure({ file }: { file: PatchFile }): ReactElement | null {
  if (file.added === 0 && file.removed === 0) return null;
  return (
    <span className={`${s.patchOpFig} chat-tool-mono`}>
      <span className={s.patchOpAdd}>+{file.added}</span>
      {file.removed > 0 ? <> <span className={s.patchOpDel}>−{file.removed}</span></> : null}
    </span>
  );
}

function Hunks({ file }: { file: PatchFile }): ReactElement {
  return (
    <>
      {file.hunks.map((hunk, index) => (
        <div key={index} className="chat-patch-hunk chat-tool-rule">
          {hunk.context ? (
            <Text as="p" className={`${s.patchHunkAt} chat-tool-mono`} tooltip="truncate">
              {hunk.context}
            </Text>
          ) : null}
          <div className={`${s.patchDiff} chat-tool-mono`} role="group" aria-label={hunk.context || "Change"}>
            {hunk.lines.map((line, i) => (
              <div
                key={i}
                className="chat-patch-diff__line chat-tool-diff__line"
                data-tone={line.sign === "+" ? "add" : line.sign === "-" ? "del" : "ctx"}
              >
                <span className="chat-patch-diff__sign chat-tool-diff__sign" aria-hidden="true">
                  {line.sign}
                </span>
                <span className="chat-patch-diff__code chat-tool-diff__code">{line.text}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </>
  );
}

function Diagnostics({ lines }: { lines: string[] }): ReactElement | null {
  if (lines.length === 0) return null;
  return (
    <div className={s.patchDiag}>
      <p className={s.patchDiagHead}>
        <IconAlertTriangle size={14} stroke={1.8} aria-hidden="true" />
        The language server flagged this file after the patch landed.
      </p>
      {lines.map((line, index) => (
        <p key={index} className={`${s.patchDiagLine} chat-tool-mono`}>
          {line}
        </p>
      ))}
    </div>
  );
}

function FileMark({ path }: { path: string }): ReactElement {
  return (
    <span className="chat-tool-band__icon" aria-hidden="true">
      <LanguageIcon path={path} size={16} />
    </span>
  );
}

function FlagMark(): ReactElement {
  return (
    <span className={s.patchRowFlag} title="The language server flagged this file." aria-label="Flagged by the language server">
      <IconAlertTriangle size={13} stroke={1.9} />
    </span>
  );
}

function RenamedFrom({ file }: { file: PatchFile }): ReactElement | null {
  if (!file.movedTo) return null;
  return (
    <Text as="p" className={s.patchFrom} tooltip="truncate" tooltipLabel={file.path}>
      from{" "}
      <span className="chat-tool-mono">
        <LeafPath path={file.path} />
      </span>
    </Text>
  );
}

/** The summary row's own button: mark, change word, path, the flag when the
 *  language server complained, the figure, and the worded disclosure. */
function RowHead({
  file,
  flagged,
  open,
  expandable,
  onToggle,
}: {
  file: PatchFile;
  flagged: boolean;
  open: boolean;
  expandable: boolean;
  onToggle: () => void;
}): ReactElement {
  const path = landing(file);
  return (
    <button type="button" className={s.patchRowHead} aria-expanded={expandable ? open : undefined} disabled={!expandable} onClick={onToggle}>
      <FileMark path={path} />
      <span className={s.patchOpWord}>{CHANGE_WORD[changeOf(file)]}</span>
      <Text className={`${s.patchRowPath} chat-tool-mono`} tooltip="truncate" tooltipLabel={path}>
        <LeafPath path={path} />
      </Text>
      {flagged ? <FlagMark /> : null}
      <Figure file={file} />
      {expandable ? <span className={s.patchRowDisclose}>{open ? "Hide diff" : "Show diff"}</span> : null}
    </button>
  );
}

function FileDiff({ file, lines }: { file: PatchFile; lines: string[] }): ReactElement {
  return (
    <div className={s.patchFileBody}>
      <RenamedFrom file={file} />
      <Hunks file={file} />
      <Diagnostics lines={lines} />
    </div>
  );
}

/** One file of the patch: the summary row is the door to its diff, and the
 *  trailing arrow opens the file itself in the editor. */
function FileRow({
  file,
  lines,
  index,
  open,
  onToggle,
  onOpenPath,
}: {
  file: PatchFile;
  lines: string[];
  index: number;
  open: boolean;
  onToggle: (index: number) => void;
  onOpenPath?: (path: string) => void;
}): ReactElement {
  const flagged = lines.length > 0;
  const expandable = file.hunks.length > 0 || flagged;
  return (
    <div className={s.patchFile} data-change={changeOf(file)}>
      <div className={s.patchRow}>
        <RowHead file={file} flagged={flagged} open={open} expandable={expandable} onToggle={() => onToggle(index)} />
        {onOpenPath ? (
          <button type="button" className={s.patchRowOpen} aria-label={`Open ${landing(file)}`} title="Open file" onClick={() => onOpenPath(landing(file))}>
            <IconArrowUpRight size={14} stroke={1.8} />
          </button>
        ) : null}
      </div>
      {open && expandable ? <FileDiff file={file} lines={lines} /> : null}
    </div>
  );
}

/** The well's interior: the patch as a ledger of file rows, each opening to its
 *  own diff. It paints no ground, edge, radius, or outer pad; the group's well
 *  owns those. */
function Body({ part, env }: { part: ToolConversationPart; env?: StepEnvironment }): ReactElement {
  const files = parseEnvelope(str(part.input?.patchText));
  const diagnostics = readDiagnostics(str(part.output) || str(part.content));
  // A patch of one file IS its diff, so that one opens itself; a batch waits
  // behind its rows.
  const [open, setOpen] = useState<ReadonlySet<number>>(() => new Set(files.length === 1 ? [0] : []));
  const toggle = (index: number): void => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };
  const rows = files.map((file, index) => (
    <FileRow
      key={`${file.path}-${index}`}
      file={file}
      lines={diagnosticsFor(diagnostics, file)}
      index={index}
      open={open.has(index)}
      onToggle={toggle}
      onOpenPath={env?.onOpenPath}
    />
  ));
  if (files.length === 0) {
    return (
      <div data-tool="apply_patch">
        <EmptyLine>The patch carried no file sections.</EmptyLine>
      </div>
    );
  }
  return (
    <div data-tool="apply_patch">
      <Capped cap={340} className="chat-patch-files chat-tool-ruled">
        {rows}
      </Capped>
    </div>
  );
}

/** The step this tool contributes to a transcript's tool group. */
export const head: CardHead = (part, env) => {
  const files = parseEnvelope(str(part.input?.patchText));
  const added = files.reduce((sum, file) => sum + file.added, 0);
  const removed = files.reduce((sum, file) => sum + file.removed, 0);
  const lone = files.length === 1 ? files[0] : null;
  return {
    // One file reads as the file; several read as their number, since the row
    // shows a path by its leaf alone and several leaves would say nothing.
    object: lone ? landing(lone) : count(files.length, "file", "files"),
    objectKind: lone ? "path" : "pattern",
    data: files.length === 0 ? undefined : { kind: "diff", added, removed },
    body: <Body part={part} env={env} />,
  };
};
