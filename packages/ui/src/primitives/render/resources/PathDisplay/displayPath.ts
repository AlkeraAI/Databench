/** Workspace-aware display form for file paths, the same rule the terminal
 * client applies. Keep the two in lockstep when semantics change. */

export interface WorkspacePathContext {
  /** Absolute workspace/project root (the folder holding the project). */
  root?: string | null;
  /** Absolute user home directory, when the host knows it. */
  home?: string | null;
}

interface ParsedPath {
  /** "" for relative, "/" for POSIX-absolute, "X:/" (original case) for drive-absolute. */
  anchor: string;
  segments: string[];
}

const DRIVE_ANCHOR = /^[A-Za-z]:\//;

function parse(input: string): ParsedPath {
  let anchor = "";
  let rest = input;
  if (DRIVE_ANCHOR.test(input)) {
    anchor = input.slice(0, 3);
    rest = input.slice(3);
  } else if (input.startsWith("/")) {
    anchor = "/";
    rest = input.slice(1);
  }
  const segments: string[] = [];
  for (const piece of rest.split("/")) {
    if (!piece || piece === ".") continue;
    if (piece === "..") {
      if (segments.length && segments[segments.length - 1] !== "..") segments.pop();
      else if (!anchor) segments.push("..");
      continue;
    }
    segments.push(piece);
  }
  return { anchor, segments };
}

/** A root/home is usable only when absolute with at least one segment — a bare
 * "/" (or "C:/") base would relativize every absolute path into nonsense. */
function parseBase(value: string | null | undefined): ParsedPath | null {
  if (!value) return null;
  const parsed = parse(value.replace(/\\/g, "/"));
  if (!parsed.anchor || parsed.segments.length === 0) return null;
  return parsed;
}

function resolveAgainst(base: ParsedPath, relative: string[]): ParsedPath {
  const segments = [...base.segments];
  for (const piece of relative) {
    if (piece === "..") {
      if (segments.length) segments.pop();
      continue;
    }
    segments.push(piece);
  }
  return { anchor: base.anchor, segments };
}

function anchorsEqual(a: string, b: string): boolean {
  return a === b || (a.length === 3 && b.length === 3 && a.toUpperCase() === b.toUpperCase());
}

function isDriveAnchor(anchor: string): boolean {
  return anchor.length === 3;
}

// Drive-anchored (Windows) paths compare case-insensitively — NTFS is — while
// POSIX comparisons stay case-sensitive.
function segmentEqual(a: string, b: string, fold: boolean): boolean {
  return fold ? a.toLowerCase() === b.toLowerCase() : a === b;
}

function startsWithSegments(whole: string[], prefix: string[], fold: boolean): boolean {
  return prefix.length < whole.length && prefix.every((segment, i) => segmentEqual(whole[i], segment, fold));
}

function sameSegments(a: string[], b: string[], fold: boolean): boolean {
  return a.length === b.length && a.every((segment, i) => segmentEqual(segment, b[i], fold));
}

/** A path as the UI should read it: relative when inside the workspace root
 * (`models/staging/x.sql`), `~`-abbreviated when under home (`~/other/y.py`) —
 * never an escaping `../../..`. The root itself reads `~/<root-name>`. A relative
 * input is resolved against the root (and `..` collapsed) first. Purely lexical
 * (no filesystem); `\` separators are normalized to `/` for display and
 * drive-anchored (Windows) paths compare case-insensitively throughout — NTFS
 * is — so Windows hosts shorten too. URLs and other scheme-carrying strings
 * pass through verbatim. Empty in / empty out. */
export function displayPath(path: string, ctx: WorkspacePathContext = {}): string {
  if (!path) return "";
  if (path.includes("://")) return path;
  let here = parse(path.replace(/\\/g, "/"));
  const root = parseBase(ctx.root);
  if (!here.anchor && root) here = resolveAgainst(root, here.segments);
  if (root && anchorsEqual(here.anchor, root.anchor)) {
    const fold = isDriveAnchor(root.anchor);
    if (sameSegments(here.segments, root.segments, fold)) {
      return `~/${root.segments[root.segments.length - 1]}`;
    }
    if (startsWithSegments(here.segments, root.segments, fold)) {
      return here.segments.slice(root.segments.length).join("/");
    }
  }
  const home = parseBase(ctx.home);
  if (home && anchorsEqual(here.anchor, home.anchor)) {
    const fold = isDriveAnchor(home.anchor);
    if (sameSegments(here.segments, home.segments, fold)) return "~";
    if (startsWithSegments(here.segments, home.segments, fold)) {
      return `~/${here.segments.slice(home.segments.length).join("/")}`;
    }
  }
  return here.anchor ? here.anchor + here.segments.join("/") : path;
}
