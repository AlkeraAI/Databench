// The permission presentation: one surface-neutral description of a pending
// permission ask, which every surface renders.
//
// This is the TypeScript twin of `alkera_core.permission_presentation`. The
// words, registers and redaction rules are NOT written here — they are the
// generated registry (`generated/permissionPresentation.ts`), exported from the
// Python one — and the algorithm is held to the Python one's exact output by
// the committed conformance vectors (`permissionPresentation.test.ts`). A new
// kind of ask is one registry entry on the Python side; the web and Slack both
// present it from there.

import {
  NATIVE_TOOL_NAMES,
  PERMISSION_PRESENTATION_REGISTRY as REGISTRY,
} from "./generated/permissionPresentation";
import { effortLabel } from "./effort";
import { formatUsd } from "./money";
import { alkeraToolName, canonicalToolName, nativeToolName } from "./toolNames";
import type { PermissionConversationPart, PermissionSubjectView } from "./conversation";

export const PRESENTATION_VERSION: string = REGISTRY.version;
export const EXEC_TITLE: string = REGISTRY.execTitle;
export const EXACT_ALWAYS_LABEL: string = REGISTRY.exactAlwaysLabel;
export { NATIVE_TOOL_NAMES };

// ---------------------------------------------------------------------------
// The shapes (camelCase, identical to the Python models' JSON)
// ---------------------------------------------------------------------------

export interface AskTarget {
  kind: string;
  name: string;
  connection?: string;
}

export interface AskCost {
  usd?: number;
  bytesScanned?: number;
  rowsScanned?: number;
  currency?: string;
}

export interface AskSubject {
  capability?: string;
  effect?: string;
  confidence?: string;
  operation?: string;
  targets: AskTarget[];
  cost?: AskCost;
  scope?: string;
}

/** Why a cell in a notebook run executes: the agent asked for it, a target
 *  reads what it defines, or it reads what a target defines. */
export type NotebookCellRole = "target" | "dependency" | "dependent";

/** A notebook tool's ask, as the machine holding the notebook words it. */
export interface AskNotebook {
  lead: string;
  fileName: string;
  filePath: string;
  tail: string;
  cells: { name: string; code: string; role: NotebookCellRole }[];
  packages: string[];
}

export interface AskPreview {
  kind: string;
  title?: string;
  content?: string;
  truncated: boolean;
  notebook?: AskNotebook;
}

export interface AskCall {
  name: string;
  input: Record<string, unknown>;
}

export interface PermissionAsk {
  permissionKind: string;
  canonicalKind: string;
  patterns: string[];
  subjectPending: boolean;
  subject?: AskSubject;
  preview?: AskPreview;
  options: { optionId: string; name: string }[];
  call?: AskCall;
}

export type SubjectFormat = "command" | "path" | "url" | "code" | "sentence";
export type DecisionRole = "allow" | "always" | "deny" | "other";
export type PrimaryField =
  | "title"
  | "subject"
  | "note"
  | "change"
  | "details"
  | "notebook"
  | "facts"
  | "decisions";

/** A cell as a notebook ask lists it: its name and the first lines of its code. */
export interface PresentedNotebookCell {
  name: string;
  previewLines: string[];
  role: NotebookCellRole;
}

/** A notebook ask: `title` is `${lead} ${fileName}${tail}`, and a surface that
 *  can open files renders `fileName` as a link to `filePath`. */
export interface PresentedNotebook {
  lead: string;
  fileName: string;
  filePath: string;
  tail: string;
  /** Why the ask needs approval, in plain words, said once. */
  reason?: string;
  /** The cells the agent asked to run, in notebook order. */
  cells: PresentedNotebookCell[];
  /** The cells that run only because of them, in notebook order. */
  related: PresentedNotebookCell[];
  /** The one quiet line standing for `related`. */
  relatedLine?: string;
  packages: string[];
}

export interface PermissionPresentation {
  version: string;
  presenter: string;
  label: string;
  title: string;
  subject?: { text: string; format: SubjectFormat; language?: string };
  missingSubject?: string;
  waiting?: string;
  note?: { title?: string; body: string; truncated: boolean };
  change?: { title?: string; content: string };
  details?: { tool: string; input: string };
  notebook?: PresentedNotebook;
  facts: string[];
  effect?: string;
  alwaysScope?: string;
  decisions: { optionId: string; label: string; role: DecisionRole }[];
  primary: PrimaryField[];
}

// ---------------------------------------------------------------------------
// The registry, typed
// ---------------------------------------------------------------------------

interface TitleRule {
  text: string;
  effect: string | null;
  operation: string | null;
  named: boolean | null;
}

interface Presenter {
  key: string;
  layer: "canonical" | "capability" | "mechanism";
  matches: readonly string[];
  label: string | null;
  titles: readonly TitleRule[];
  titlesWhenCanonical: readonly string[];
  format: SubjectFormat | null;
  language: string | null;
  waiting: string | null;
  namesTool: boolean;
  inputKeys: readonly string[];
  scopeNoun: string;
  scopePhrases: Readonly<Record<string, string>>;
  scopeFallback: string | null;
}

const PRESENTERS = REGISTRY.presenters as unknown as readonly Presenter[];
const BY_SLOT = new Map<string, Presenter>();
for (const presenter of PRESENTERS) {
  for (const match of presenter.matches) BY_SLOT.set(`${presenter.layer}:${match}`, presenter);
}

function lookup(layer: Presenter["layer"], key: string): Presenter | undefined {
  return BY_SLOT.get(`${layer}:${key}`);
}

const GENERIC = lookup("canonical", "other") as Presenter;

/** Every registered presenter key, in registration order. */
export const PRESENTER_KEYS: readonly string[] = PRESENTERS.map((p) => p.key);

/** The permission modes every surface offers, in order. */
export interface PermissionModeEntry {
  value: string;
  label: string;
  description: string;
  short: string | null;
  aliases: readonly string[];
}
export const PERMISSION_MODES = REGISTRY.modes as unknown as readonly PermissionModeEntry[];

/** The mode values alone, in the order every surface offers them: the set the
 *  server accepts for a chat, and the one a mode string is checked against. */
export const PERMISSION_MODE_VALUES: readonly string[] = PERMISSION_MODES.map((mode) => mode.value);

// ---------------------------------------------------------------------------
// Settled asks and mode changes (`alkera_core.permission_presentation.outcome`)
// ---------------------------------------------------------------------------

const OUTCOMES = REGISTRY.outcomes;

function lookupWord(table: Readonly<Record<string, string>>, key: string | null | undefined): string | undefined {
  return key != null && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

/** Where a person was when they acted, when the transcript says. */
export type ActionSurface = "web" | "slack";

/** The line a settled ask shows in place of its buttons, on every surface:
 *  "Allowed on web by Dana Okafor", "Denied in Slack by Sam Lee",
 *  "Decided by the new mode". A decider or surface the resolution does not
 *  name is left out rather than guessed. */
export function resolutionLine(
  optionId: string | null | undefined,
  decidedBy: string | null | undefined,
  deciderName: string | null | undefined,
  surface: string | null | undefined,
): string {
  const decider = lookupWord(OUTCOMES.deciderLines, decidedBy);
  if (decider !== undefined) return decider;
  const verb = lookupWord(OUTCOMES.resolutionVerbs, optionId) ?? OUTCOMES.answered;
  const where = lookupWord(OUTCOMES.surfacePhrases, surface);
  const who = deciderName ? `by ${deciderName}` : undefined;
  return [verb, where, who].filter(Boolean).join(" ");
}

/** A mode change as a card: `title` leads, `change` shows old → new,
 *  `attribution` who changed it and where. */
export interface ModeChangeView {
  title: string;
  change: string;
  attribution: string;
}

function modeLabel(value: string | null | undefined): string | undefined {
  if (!value) return undefined;
  return PERMISSION_MODES.find((mode) => mode.value === value)?.label ?? value;
}

/** Who changed a chat's setting and where: "Changed on web by Dana Okafor".
 *  A person or surface the record does not name is left out. Every change
 *  card (the mode, the model) ends with it. */
export function changeAttribution(
  changerName: string | null | undefined,
  surface: string | null | undefined,
): string {
  const where = lookupWord(OUTCOMES.surfacePhrases, surface);
  return OUTCOMES.changedBy
    .replace("{where}", where ? ` ${where}` : "")
    .replace("{who}", changerName ? ` by ${changerName}` : "");
}

export function modeChangePresentation(
  mode: string,
  previousMode: string | null | undefined,
  changerName: string | null | undefined,
  surface: string | null | undefined,
): ModeChangeView {
  const label = modeLabel(mode) ?? mode;
  const before = modeLabel(previousMode);
  return {
    title: OUTCOMES.modeSetTitle.replace("{label}", label),
    change: before && before !== label ? `${before} → ${label}` : label,
    attribution: changeAttribution(changerName, surface),
  };
}

/** A model or effort change, as the transcript's `model.changed` row names it. */
export interface ModelChangeFacts {
  modelId: string;
  displayName?: string | null;
  effort?: string | null;
  previousModelId?: string | null;
  previousDisplayName?: string | null;
  previousEffort?: string | null;
  changerName?: string | null;
  surface?: string | null;
}

/** A model change as a card, in the mode card's shape: the new model (or, for
 *  an effort change alone, the new effort) leading, old → new, and who changed
 *  it from where. The effort rides beside each model only when it moved too. */
export function modelChangePresentation(facts: ModelChangeFacts): ModeChangeView {
  const name = facts.displayName || facts.modelId;
  const before = facts.previousDisplayName || facts.previousModelId || null;
  const effort = facts.effort ? effortLabel(facts.effort) : null;
  const beforeEffort = facts.previousEffort ? effortLabel(facts.previousEffort) : null;
  const modelMoved = facts.previousModelId != null && facts.previousModelId !== facts.modelId;
  const effortMoved = effort !== beforeEffort;
  const attribution = changeAttribution(facts.changerName, facts.surface);
  if (!modelMoved && effortMoved && effort) {
    return {
      title: `Effort set to ${effort}`,
      change: beforeEffort ? `${beforeEffort} → ${effort}` : effort,
      attribution,
    };
  }
  const side = (model: string, level: string | null): string =>
    effortMoved && level ? `${model} · ${level}` : model;
  return {
    title: `Model set to ${name}`,
    change: modelMoved && before ? `${side(before, beforeEffort)} → ${side(name, effort)}` : side(name, effort),
    attribution,
  };
}

/** The cases this twin and the Python one are both held to. */
export const OUTCOME_VECTORS = OUTCOMES.vectors;

// ---------------------------------------------------------------------------
// Redaction
// ---------------------------------------------------------------------------

const RULES = REGISTRY.redactionRules.map(
  (rule) => new RegExp(rule.pattern, rule.ignoreCase ? "gi" : "g"),
);
const SECRET_KEY = new RegExp(REGISTRY.secretKeyPattern, "i");

/** `text` with every secret the rules recognise replaced by `[redacted]`. */
export function redact(text: string): string {
  let out = text;
  for (const rule of RULES) {
    out = out.replace(
      rule,
      (_m: string, keep: string, _secret: string, after: string | undefined) =>
        `${keep}${REGISTRY.redacted}${after ?? ""}`,
    );
  }
  return out;
}

function redactValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [
        key,
        SECRET_KEY.test(key) ? REGISTRY.redacted : redactValue(item),
      ]),
    );
  }
  return value;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"];

export function formatBytes(size: number): string {
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? String(Math.round(value)) : value.toFixed(value < 10 ? 1 : 0)} ${BYTE_UNITS[unit]}`;
}

function costFacts(cost: AskCost | undefined): string[] {
  if (!cost) return [];
  const facts: string[] = [];
  if (cost.usd) {
    facts.push(
      !cost.currency || cost.currency === "usd"
        ? formatUsd(cost.usd)
        : `${cost.usd} ${cost.currency}`,
    );
  }
  if (cost.bytesScanned) facts.push(`${formatBytes(cost.bytesScanned)} scanned`);
  else if (cost.rowsScanned) facts.push(`${cost.rowsScanned.toLocaleString("en-US")} rows scanned`);
  return facts;
}

/** The target kinds a notebook ask's classifier names: the notebook file itself. */
const NOTEBOOK_TARGETS: ReadonlySet<string> = new Set(["notebook", "file"]);

function factsOf(subject: AskSubject | undefined, skip: ReadonlySet<string> = new Set()): string[] {
  if (!subject) return [];
  const targets = subject.targets
    .filter((target) => target.name && !skip.has(target.kind))
    .map((target) => target.name)
    .join(", ");
  return [targets, ...costFacts(subject.cost)].filter(Boolean);
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

/** The tool a wire spelling denotes, or null when it names no tool. */
export function resolveTool(wire: string): string | null {
  return alkeraToolName(wire) ?? nativeToolName(wire) ?? null;
}

// ---------------------------------------------------------------------------
// The presentation
// ---------------------------------------------------------------------------

function capabilityOf(ask: PermissionAsk): string | undefined {
  return ask.subject?.capability || (ask.permissionKind === "sql" ? "sql" : undefined);
}

function connectionOf(ask: PermissionAsk): string | undefined {
  return (ask.subject?.targets ?? []).find((target) => target.connection)?.connection;
}

interface TitleArgs {
  canonical: string;
  effect: string | undefined;
  operation: string;
  named: boolean;
  tool: string | null;
  connection: string | undefined;
}

function titleOf(presenter: Presenter | undefined, args: TitleArgs): string | undefined {
  if (!presenter) return undefined;
  if (
    presenter.titlesWhenCanonical.length > 0 &&
    !presenter.titlesWhenCanonical.includes(args.canonical)
  ) {
    return undefined;
  }
  for (const rule of presenter.titles) {
    if (rule.effect !== null && rule.effect !== args.effect) continue;
    if (rule.operation !== null && rule.operation !== args.operation) continue;
    if (rule.named !== null && rule.named !== args.named) continue;
    if (rule.text.includes("{tool}") && !args.tool) continue;
    if (rule.text.includes("{connection}") && !args.connection) continue;
    return rule.text
      .replaceAll("{tool}", args.tool ?? "")
      .replaceAll("{connection}", args.connection ?? "");
  }
  return undefined;
}

function recoveredSubject(ask: PermissionAsk, layers: Presenter[]): string | undefined {
  if (!ask.call) return undefined;
  const keys = [...layers.flatMap((layer) => layer.inputKeys), ...REGISTRY.subjectInputKeys];
  for (const key of keys) {
    const value = ask.call.input[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

/** The one line saying what a standing grant under `label` would cover, or
 *  undefined when the classifier named nothing to say it about. */
export function alwaysScope(
  label: string,
  subject: AskSubject | PermissionSubjectView | undefined,
): string | undefined {
  if (subject?.scope === "command" || label === EXACT_ALWAYS_LABEL) {
    return `${label === EXACT_ALWAYS_LABEL ? "Always allow" : label} covers this exact command.`;
  }
  const capability = subject?.capability;
  const operation = subject?.operation;
  if (!capability || !operation || operation === "unknown") return undefined;
  const lane = (lookup("capability", capability) ?? lookup("capability", "*")) as Presenter;
  if (Object.keys(lane.scopePhrases).length > 0 || lane.scopeFallback) {
    const phrase = lane.scopePhrases[operation] ?? lane.scopeFallback ?? operation;
    return `${label} covers every ${phrase}.`;
  }
  const words = operation.replace(/_/gu, " ");
  return `${label} covers every ${lane.scopeNoun ? `${words} ${lane.scopeNoun}` : words}.`;
}

/** The line naming the tool that raised an ask, where the title does not. */
export function requestedBy(ask: Pick<PermissionAsk, "canonicalKind" | "permissionKind">): string | undefined {
  const base = lookup("canonical", ask.canonicalKind) ?? GENERIC;
  if (base.namesTool || lookup("mechanism", ask.permissionKind)) return undefined;
  const tool = resolveTool(ask.permissionKind);
  return tool ? `Requested by the ${tool} tool.` : undefined;
}

// ---------------------------------------------------------------------------
// Notebook asks
// ---------------------------------------------------------------------------

const EFFECT_REASONS: Readonly<Record<string, string>> = REGISTRY.effectReasons;
const OPERATION_REASONS: Readonly<Record<string, string>> = REGISTRY.operationReasons;

/** Why an action needs approval, in plain words: what the operation does where
 *  the effect words would misdescribe it, else that the classifier could not
 *  tell, else the effect it found. */
export function effectReason(subject: AskSubject | undefined): string | undefined {
  if (!subject) return undefined;
  const byOperation = lookupWord(OPERATION_REASONS, subject.operation ?? "");
  if (byOperation !== undefined) return byOperation;
  if (subject.confidence === "unknown") return REGISTRY.unsureReason;
  return lookupWord(EFFECT_REASONS, subject.effect ?? "");
}

/** Counted in code points, as the Python twin counts a string. */
function clipLine(line: string, width: number): string {
  const points = Array.from(line);
  return points.length <= width ? line : `${points.slice(0, width - 1).join("")}…`;
}

const TRIM_END = /[ \t\r\f\v]+$/u;
const BLANK = /^[ \t\r\f\v]*$/u;

/** The first lines of a cell's code, enough to recognise it: blank lines
 *  skipped, each line cut to the preview width, and an ellipsis on the last
 *  line shown when more follow. */
export function codePreview(code: string): string[] {
  const lines = redact(code)
    .split("\n")
    .filter((line) => !BLANK.test(line))
    .map((line) => line.replace(TRIM_END, ""));
  const max = REGISTRY.notebookPreviewLines;
  const shown = lines.slice(0, max).map((line) => clipLine(line, REGISTRY.notebookPreviewWidth));
  const last = shown.length - 1;
  if (lines.length > max && last >= 0 && !shown[last].endsWith("…")) shown[last] = `${shown[last]} …`;
  return shown;
}

function countOf(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The quiet line standing for the cells that run only because of the targets. */
export function relatedLine(targets: number, dependencies: number, dependents: number): string | undefined {
  const single = targets === 1;
  const parts: string[] = [];
  if (dependencies) {
    parts.push(`${countOf(dependencies, "cell", "cells")} ${single ? "it depends on" : "they depend on"}`);
  }
  if (dependents) {
    const verb = dependents === 1 ? "depends" : "depend";
    parts.push(`${countOf(dependents, "cell", "cells")} that ${verb} on ${single ? "it" : "them"}`);
  }
  return parts.length > 0 ? `Also runs ${parts.join(" and ")}` : undefined;
}

function notebookCell(cell: AskNotebook["cells"][number]): PresentedNotebookCell {
  return { name: redact(cell.name), previewLines: codePreview(cell.code), role: cell.role };
}

function presentNotebook(ask: AskNotebook, subject: AskSubject | undefined): PresentedNotebook {
  let cells = ask.cells.filter((cell) => cell.role === "target").map(notebookCell);
  let related = ask.cells.filter((cell) => cell.role !== "target").map(notebookCell);
  if (cells.length === 0) {
    // Nothing was asked for by name: every cell is the run itself.
    cells = related;
    related = [];
  }
  const dependencies = related.filter((cell) => cell.role === "dependency").length;
  const reason = effectReason(subject);
  const line = relatedLine(cells.length, dependencies, related.length - dependencies);
  return {
    lead: redact(ask.lead),
    fileName: ask.fileName,
    filePath: ask.filePath,
    tail: redact(ask.tail),
    ...(reason !== undefined ? { reason } : {}),
    cells,
    related,
    ...(line !== undefined ? { relatedLine: line } : {}),
    packages: ask.packages.map(redact),
  };
}

/** What a standing grant on a notebook ask remembers: the action as the
 *  machine words it, whatever the cells hold. */
export function notebookAlwaysScope(label: string, raw: string): string {
  return `${label} skips this question whenever the agent asks to ${raw.charAt(0).toLowerCase()}${raw.slice(1)}.`;
}

const ROLES: Record<string, DecisionRole> = {
  allow_once: "allow",
  allow_always: "always",
  reject_once: "deny",
  reject_always: "deny",
  cancelled: "deny",
};

/** The presentation of one pending ask. */
export function presentPermission(ask: PermissionAsk): PermissionPresentation {
  const base = lookup("canonical", ask.canonicalKind) ?? GENERIC;
  const mechanism = lookup("mechanism", ask.permissionKind);
  const capability = capabilityOf(ask);
  const lane = capability
    ? (lookup("capability", capability) ?? lookup("capability", "*"))
    : undefined;
  const subject = ask.subject;
  const effect = subject?.effect;
  const tool = mechanism ? null : resolveTool(ask.permissionKind);

  const layers = [lane, base].filter((p): p is Presenter => p !== undefined);
  const raw = ask.patterns[0] ? ask.patterns[0] : recoveredSubject(ask, layers);
  const args: TitleArgs = {
    canonical: ask.canonicalKind,
    effect,
    operation: subject?.operation ?? "",
    named: Boolean(raw),
    tool,
    connection: connectionOf(ask),
  };

  let chosen: Presenter;
  let title: string;
  if (mechanism) {
    chosen = mechanism;
    title = mechanism.titles[0].text;
  } else if (effect === "exec") {
    chosen = lane ?? base;
    title = EXEC_TITLE;
  } else {
    const laneTitle = titleOf(lane, args);
    if (lane && laneTitle !== undefined) {
      chosen = lane;
      title = laneTitle;
    } else {
      chosen = base;
      title = titleOf(base, args) ?? "Allow this action?";
    }
  }

  const format: SubjectFormat =
    (lane?.format && base === GENERIC ? lane.format : base.format) ?? "code";
  const language = lane?.language || base.language || undefined;

  let note: PermissionPresentation["note"];
  let change: PermissionPresentation["change"];
  let notebook: PresentedNotebook | undefined;
  const preview = ask.preview;
  if (preview?.notebook) {
    notebook = presentNotebook(preview.notebook, subject);
    title = `${notebook.lead} ${notebook.fileName}${notebook.tail}`;
  } else if (preview && preview.kind === "text" && preview.content !== undefined) {
    note = {
      ...(preview.title ? { title: redact(preview.title) } : {}),
      body: redact(preview.content),
      truncated: preview.truncated,
    };
  } else if (preview?.content) {
    change = {
      ...(preview.title !== undefined ? { title: preview.title } : {}),
      content: redact(preview.content),
    };
  }

  let presented: PermissionPresentation["subject"];
  if (raw && !note && !notebook) {
    const text = redact(raw);
    presented = {
      text: format === "sentence" ? text.charAt(0).toUpperCase() + text.slice(1) : text,
      format,
      ...(language ? { language } : {}),
    };
  }

  const waiting = ask.subjectPending && !raw ? (base.waiting ?? undefined) : undefined;
  const missing =
    !presented && !note && !notebook && waiting === undefined ? requestedBy(ask) : undefined;

  let details: PermissionPresentation["details"];
  if (
    ask.call &&
    Object.keys(ask.call.input).length > 0 &&
    !presented &&
    !note &&
    !change &&
    !notebook &&
    waiting === undefined
  ) {
    const shown =
      resolveTool(ask.call.name) || canonicalToolName(ask.call.name) || tool || "tool";
    details = {
      tool: shown,
      input: redact(JSON.stringify(redactValue(ask.call.input), null, 2)),
    };
  }

  const always = ask.options.find((option) => option.optionId === "allow_always");
  const decisions = ask.options.map((option) => ({
    optionId: option.optionId,
    label: option.name,
    role: ROLES[option.optionId] ?? "other",
  }));
  // A notebook ask names its notebook itself; the targets the classifier
  // recorded for it are paths on the machine, which mean nothing to a reader.
  const facts = factsOf(subject, notebook ? NOTEBOOK_TARGETS : undefined);
  const primary: PrimaryField[] = ["title"];
  if (presented) primary.push("subject");
  if (note) primary.push("note");
  if (change) primary.push("change");
  if (details) primary.push("details");
  if (notebook) primary.push("notebook");
  if (facts.length > 0) primary.push("facts");
  if (decisions.length > 0) primary.push("decisions");

  const scope = !always
    ? undefined
    : notebook && raw && subject?.scope === "command"
      ? notebookAlwaysScope(always.name, raw)
      : alwaysScope(always.name, subject);
  return {
    version: PRESENTATION_VERSION,
    presenter: chosen.key,
    label: chosen.label ?? base.label ?? GENERIC.label ?? "",
    title,
    ...(presented ? { subject: presented } : {}),
    ...(missing !== undefined ? { missingSubject: missing } : {}),
    ...(waiting !== undefined ? { waiting } : {}),
    ...(note ? { note } : {}),
    ...(change ? { change } : {}),
    ...(details ? { details } : {}),
    ...(notebook ? { notebook } : {}),
    facts,
    ...(effect !== undefined ? { effect } : {}),
    ...(scope !== undefined ? { alwaysScope: scope } : {}),
    decisions,
    primary,
  };
}

// ---------------------------------------------------------------------------
// Reading the ask off the folded conversation part
// ---------------------------------------------------------------------------

function askSubject(subject: PermissionSubjectView | undefined): AskSubject | undefined {
  if (!subject) return undefined;
  const cost = subject.cost;
  return {
    ...(subject.capability ? { capability: subject.capability } : {}),
    ...(subject.effect ? { effect: subject.effect } : {}),
    ...(subject.confidence ? { confidence: subject.confidence } : {}),
    ...(subject.operation ? { operation: subject.operation } : {}),
    targets: (subject.targets ?? []).map((target) => ({
      kind: target.kind,
      name: target.name,
      ...(target.connection ? { connection: target.connection } : {}),
    })),
    ...(cost
      ? {
          cost: {
            ...(cost.usd !== undefined ? { usd: cost.usd } : {}),
            ...(cost.bytesScanned !== undefined ? { bytesScanned: cost.bytesScanned } : {}),
            ...(cost.rowsScanned !== undefined ? { rowsScanned: cost.rowsScanned } : {}),
            ...(cost.currency !== undefined ? { currency: cost.currency } : {}),
          },
        }
      : {}),
    ...(subject.scope ? { scope: subject.scope } : {}),
  };
}

/** The ask a folded permission part carries — the same fields the server reads
 *  off the `permission.request` event — plus the gated call when known. */
export function askFromPart(part: PermissionConversationPart, call?: AskCall): PermissionAsk {
  const preview = part.preview;
  const notebook = part.notebook;
  return {
    permissionKind: part.permissionKind,
    canonicalKind: part.canonicalKind,
    patterns: part.patterns,
    subjectPending: part.subjectPending === true,
    ...(part.subject ? { subject: askSubject(part.subject) } : {}),
    ...(notebook
      ? {
          preview: {
            kind: "notebook",
            ...(preview?.title !== undefined ? { title: preview.title } : {}),
            truncated: false,
            notebook,
          },
        }
      : preview
        ? {
            preview: {
              kind: preview.kind,
              ...(preview.title !== undefined ? { title: preview.title } : {}),
              ...(preview.content !== undefined ? { content: preview.content } : {}),
              truncated: preview.truncated === true,
            },
          }
        : {}),
    options: part.options.map((option) => ({ optionId: option.optionId, name: option.name })),
    ...(call ? { call } : {}),
  };
}
