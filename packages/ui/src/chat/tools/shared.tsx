// The interior atoms every tool body shares: the input band, the failure line,
// the empty line, and the path treatment that reads a leaf one tier brighter
// than the trail locating it.

import type { ReactElement, ReactNode } from "react";

import { IconCircle, IconCircleCheck, IconCircleX, IconFolder, IconPlayerStop, IconProgress, IconX } from "@tabler/icons-react";

import { LanguageIcon, Text } from "../sharedUi";
import type { StepAction } from "./step";

import "./shared.css";

/** The call's full input, above its output, in every well. */
export function Band({ children, className }: { children: ReactNode; className?: string }): ReactElement {
  return <div className={className ? `chat-tool-band ${className}` : "chat-tool-band"}>{children}</div>;
}

/** The height caps a capped body may take; each was settled against the content
 *  it holds, so the scale enumerates them rather than rounding them together.
 *  Shared sheets carry rules through 360; a card using a taller cap declares
 *  that height in its own sheet. */
export type CapHeight = 260 | 280 | 300 | 340 | 360 | 440;

/** A bounded, scrolling body. Content past the cap scrolls inside the card,
 *  and scroll-edge shadows say so; a row sliced at the fold reads as a scroll
 *  edge, not a defect. `cue` turns the shadows off for a body whose own ground
 *  would fight them. */
export function Capped({
  cap,
  cue = true,
  className,
  children,
}: {
  cap: CapHeight;
  cue?: boolean;
  className?: string;
  children: ReactNode;
}): ReactElement {
  return (
    <div className={className} data-cap={cap} data-cue={cue ? undefined : "off"}>
      {children}
    </div>
  );
}

/** Split a path at its last separator: the trail that locates, the leaf that
 *  names. Both separator families count -- the extension ships on Windows, and
 *  a backslash path must still yield its base name, not the whole path. */
export function splitLeaf(path: string, sep = "/"): { dir: string; leaf: string } {
  const cut = Math.max(path.lastIndexOf(sep), path.lastIndexOf("\\"), path.lastIndexOf("/"));
  if (cut < 0 || cut === path.length - 1) return { dir: "", leaf: path };
  return { dir: path.slice(0, cut + 1), leaf: path.slice(cut + 1) };
}

export function LeafPath({ path, sep = "/" }: { path: string; sep?: string }): ReactElement {
  const { dir, leaf } = splitLeaf(path, sep);
  return (
    <>
      {dir ? <span className="chat-tool-dir">{dir}</span> : null}
      <span className="chat-tool-leaf">{leaf}</span>
    </>
  );
}

/** A path inside the workspace reads relative -- the form the reader uses
 *  everywhere else in the editor. One outside it keeps its absolute form,
 *  because relativizing across a root boundary would name the wrong file.
 *  Separators normalize before comparing so a Windows root still matches. */
export function displayPath(path: string, root?: string, sep = "/"): string {
  if (!root) return path;
  const norm = (value: string): string => value.replaceAll("\\", sep);
  const cleanRoot = norm(root);
  const base = cleanRoot.endsWith(sep) ? cleanRoot : cleanRoot + sep;
  const cleanPath = norm(path);
  return cleanPath.startsWith(base) && cleanPath.length > base.length ? cleanPath.slice(base.length) : path;
}

/** The file band a path-bearing well opens with: the file's language icon and
 *  its path on one clipped line, workspace-relative when `relativeTo` holds
 *  the root. Wired with `onOpen` it is a button that opens the file in the
 *  host's editor; without it, a plain label. The title keeps the full path. */
export function PathBand({
  path,
  relativeTo,
  onOpen,
  action,
}: {
  path: string;
  relativeTo?: string;
  onOpen?: () => void;
  /** One control at the band's right end, apart from the band's own door
   *  (a notebook output's "Go to cell"). */
  action?: StepAction;
}): ReactElement {
  const band = <PathBandFace path={path} relativeTo={relativeTo} onOpen={onOpen} />;
  if (!action) return band;
  return (
    <div className="chat-tool-band-row">
      {band}
      <button type="button" className="chat-tool-band__action" aria-label={action.ariaLabel} onClick={action.onAction}>
        {action.label}
      </button>
    </div>
  );
}

function PathBandFace({
  path,
  relativeTo,
  onOpen,
}: {
  path: string;
  relativeTo?: string;
  onOpen?: () => void;
}): ReactElement {
  const shown = displayPath(path, relativeTo);
  // The label is the FULL path, not the shortened reading: the band clips a long
  // path AND may render it workspace-relative, so the tip is the only place the
  // whole thing is available. LeafPath renders nodes, so the label is explicit.
  const inner = (
    <>
      <span className="chat-tool-band__icon" aria-hidden="true">
        <LanguageIcon path={path} size={16} />
      </span>
      <Text className="chat-tool-band__text chat-tool-band__text--one chat-tool-mono" tooltip="truncate" tooltipLabel={path}>
        <LeafPath path={shown} />
      </Text>
    </>
  );
  if (!onOpen) {
    return <div className="chat-tool-band chat-tool-band--path">{inner}</div>;
  }
  return (
    <button type="button" className="chat-tool-band chat-tool-band--path" aria-label={`Open ${path}`} onClick={onOpen}>
      {inner}
    </button>
  );
}

/** The mark a filesystem row leads with: a directory reads as a folder, and a
 *  file reads as the language it is written in. */
export function EntryMark({ name, dir }: { name: string; dir: boolean }): ReactElement {
  return dir ? <IconFolder size={16} stroke={1.6} /> : <LanguageIcon path={name} size={16} />;
}

/** A harness reason often leads with a shouted code: `TIMEOUT: the warehouse
 *  did not answer`. The code sets in the machine register so the sentence after
 *  it reads as a sentence. The space is a real character, not a margin: a reader
 *  who selects or hears the line gets the two halves as two words either way. */
function splitCode(children: ReactNode): ReactNode {
  if (typeof children !== "string") return children;
  const split = /^([A-Z][A-Z0-9_]+):\s+([\s\S]+)$/.exec(children);
  if (split === null) return children;
  return (
    <>
      <span className="chat-spec-code chat-tool-mono">{split[1]}</span> <span className="chat-spec-fail__text">{split[2]}</span>
    </>
  );
}

/** What a card says where its results would have been when there are none. A
 *  search that found nothing did its job, so this reads in the locating
 *  register and never in the failure one. Each card supplies its own sentence,
 *  about the thing it looks for. */
export function EmptyLine({ children }: { children: ReactNode }): ReactElement {
  return <p className="chat-tool-empty">{children}</p>;
}

/** The failure line: sans, above the machine register, so the cause reads first.
 *  A cause that spans lines keeps them: a database error sets its `LINE 1:` echo
 *  and the caret under it on lines of their own, and run together the caret
 *  points at nothing. */
export function FailLine({ children }: { children: ReactNode }): ReactElement {
  const text = typeof children === "string" ? children.replace(/\s+$/, "") : children;
  const lines = typeof text === "string" && text.includes("\n");
  return (
    <p className="chat-tool-fail">
      <span aria-hidden="true">
        <IconX size={13} stroke={2.4} />
      </span>
      <span className={lines ? "chat-tool-fail__text chat-tool-fail__text--lines" : "chat-tool-fail__text"}>{splitCode(text)}</span>
    </p>
  );
}

/** The stopped line: a call its turn's Stop cut off. The same shape as the
 *  failure line, in the quiet ink, because nothing went wrong with the tool. */
export function StopLine({ children }: { children: ReactNode }): ReactElement {
  return (
    <p className="chat-tool-stop">
      <span aria-hidden="true">
        <IconPlayerStop size={13} stroke={2.2} />
      </span>
      <span className="chat-tool-stop__text">{children}</span>
    </p>
  );
}

/** One glyph per work status, shared by every task-shaped list. */
export type WorkStatus = "pending" | "in_progress" | "completed" | "cancelled";

export function StatusGlyph({ status }: { status: WorkStatus }): ReactElement {
  if (status === "completed") return <IconCircleCheck size={16} stroke={1.7} />;
  if (status === "in_progress") return <IconProgress size={16} stroke={1.7} />;
  if (status === "cancelled") return <IconCircleX size={16} stroke={1.7} />;
  return <IconCircle size={16} stroke={1.7} />;
}
