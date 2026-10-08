// The contract between a tool's adapter and the activity group that renders
// it: the head facts every step carries, plus the tool-owned well interior.

import type { ComponentType, ReactNode } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";

/** `stopped` is a call its turn's Stop cut off: it ended without a result, and
 *  the group says so in its own words rather than counting it as a failure. */
export type StepStatus = "running" | "done" | "error" | "pending" | "stopped";

/** What a step's object names, so a renderer can mark artifacts (paths, graph
 *  nodes) differently from commands and search patterns. Presentation only:
 *  the argument's typography, never a semantic axis. */
export type StepObjectKind = "pattern" | "path" | "graph" | "command";

/** The result figure beside a step's verb line. The adapter states the
 *  meaning; the group only chooses type and ink for it. */
export type StepFigure =
  | { kind: "diff"; added: number; removed: number }
  | { kind: "count"; text: string; tone?: "pass" | "fail" };

export interface CardStep {
  /** Stable identity (the tool call's id) that openness survives on. Stamped
   *  by `resolveStep`; a host-synthesized step supplies its own. */
  id: string;
  verb: string;
  object: string;
  objectKind: StepObjectKind;
  data?: StepFigure;
  status: StepStatus;
  /** Openness belongs to the payload: a bounded answer that IS the answer shows
   *  itself; an unbounded stream waits behind its word. */
  expanded?: boolean;
  /** The tool's own glyph, worn as the step's badge. Every tool has one; the
   *  group renders no kind-derived stand-in. */
  glyph: ReactNode;
  /** The well's interior (input band, output, tally), in the tool's own
   *  register; the well frame, rail, and status ground stay the group's. */
  body?: ReactNode;
  /** The call was refused before it ran. The group's digest counts it apart
   *  from a failure: a person's or the policy's no is not something that broke. */
  refused?: boolean;
  /** An ask still waiting on a person holds the call: it is in flight but not
   *  running, so no loading sweep claims work is under way. */
  waiting?: boolean;
  /** Why the call failed, in the harness's words. Stamped by `resolveStep` and
   *  rendered by the group, never by a card. A stopped call carries none: its
   *  status is the whole of what the group says about it. */
  failure?: string;
  /** One control beside the disclosure that takes the reader somewhere the
   *  step names (a notebook output's "Go to cell"). It is its own button, never
   *  part of the head's toggle. */
  action?: StepAction;
  /** Names what opens ("Show <disclosure>"). */
  disclosure?: string;
  /** The extent line under a sampled body; a whole body carries none. */
  footer?: string;
  /** The digest header for a run of one, in the tool's own words ("Ran 1
   *  query"). */
  loneSummary: string;
}

/** A step's one control. */
export interface StepAction {
  label: string;
  /** The accessible name when the label alone is ambiguous in a long transcript. */
  ariaLabel?: string;
  onAction: () => void;
}

/** What reading an output a notebook holds came to. */
export type StoredOutputRead<T> =
  | { kind: "ready"; value: T }
  /** This reader may not open the notebook. */
  | { kind: "refused" }
  /** The notebook no longer holds the output: its cell ran again, or was deleted. */
  | { kind: "gone" };

/** Host seams a step body may need; every field optional so a plain render works. */
export interface StepEnvironment {
  /** Route an external link through the host (a webview cannot open a tab itself). */
  onOpenUrl?: (url: string) => void;
  /** Open a workspace file in the host's editor; a path-bearing body's path
   *  band is a button only when this is wired. */
  onOpenPath?: (path: string) => void;
  /** The workspace root; a path band under it renders workspace-relative,
   *  one outside it keeps its absolute form. */
  workspaceRoot?: string;
  /** Open a graph node in the host's full lineage surface, centered on this
   *  URN. A card's mini graph offers its nodes as buttons only when this is
   *  wired; unwired, they stay read-only marks. */
  onOpenLineageNode?: (urn: string) => void;
  /** Open the knowledge base focused on this item. */
  onOpenKnowledgeItem?: (itemId: string) => void;
  /** An image a notebook's cell shows, as its bytes, by the notebook's path
   *  as the tool answered it and the image's hash. A step asks only once it
   *  is on screen. Unwired, an image output says to open the notebook. */
  notebookImage?: (notebookPath: string, sha256: string) => Promise<StoredOutputRead<Blob>>;
  /** The spec of a chart a notebook stores beside itself (one too large to
   *  carry in the tool's reply), by the notebook's path and the stored file's
   *  hash. Asked and answered as an image is. */
  notebookChartSpec?: (notebookPath: string, sha256: string) => Promise<StoredOutputRead<unknown>>;
  /** Open a notebook in the workspace and bring one cell into view, by the
   *  notebook's path as the tool answered it and the cell's stable id. A step
   *  offers "Go to cell" only where this is wired. */
  onOpenNotebookCell?: (notebookPath: string, cellId: string) => void;
  /** A permission ask in the same turn is waiting on a person, so the turn's
   *  unanswered calls are held rather than running. */
  awaitingApproval?: boolean;
}

/** What an adapter contributes. The registry stamps the id and the failure off
 *  the call, so neither is in an adapter's reach to set or to forget. */
export type ToolStep = Omit<CardStep, "id" | "failure">;

/** A tool part's state as the step status the group renders. */
export function stepStatus(state: ToolConversationPart["state"]): StepStatus {
  return state === "completed" ? "done" : state;
}

/** A card's fixed voice: the words and the mark it always uses. Held as data in
 *  one registry so the whole tool surface reads in one place, and so a card
 *  file carries only what actually varies with the call. */
export interface CardVoice {
  /** The badge glyph, drawn at one size for every tool. A card whose mark
   *  depends on the call (the spec sheet wears the vendor's) returns `glyph`
   *  from its head and leaves this out. */
  icon?: ComponentType<{ size?: number; stroke?: number }>;
  verb: string;
  /** Names what opens ("Show <of>"). */
  of: string;
  /** The digest header for a run of one, in the card's own words. */
  lone: string;
  /** Openness belongs to the payload: a bounded answer that IS the answer
   *  shows itself. Default closed. */
  open?: boolean;
  /** Default `pattern`, the kind that renders the object verbatim. */
  kind?: StepObjectKind;
}

/** What a card computes from the call: the object, and whatever the payload
 *  decides. A voice field named here overrides the row for this call only, so a
 *  head states a word ONLY where the call departs from the card's own. */
export type CardHead = (
  part: ToolConversationPart,
  env?: StepEnvironment,
) => Partial<ToolStep> & { object: string; icon?: CardVoice["icon"] };

/** One card: its voice, then the part of it that is really code. */
export type Card = CardVoice & { head: CardHead };

/** The step a card contributes, with the voice filled in around it. */
export function cardStep(
  card: Card,
  part: ToolConversationPart,
  env?: StepEnvironment,
): ToolStep {
  const { icon, ...head } = card.head(part, env);
  const Mark = icon ?? card.icon;
  const voice: ToolStep = {
    verb: card.verb,
    object: head.object,
    objectKind: card.kind ?? "pattern",
    disclosure: card.of,
    loneSummary: card.lone,
    expanded: card.open ?? false,
    glyph: Mark ? <Mark size={16} stroke={1.6} /> : null,
    status: stepStatus(part.state),
  };
  // Only what the head actually named: a field left undefined keeps the card's
  // own word rather than blanking it, so a lookup that misses cannot erase a verb.
  const named = Object.fromEntries(
    Object.entries(head).filter(([, value]) => value !== undefined),
  );
  return { ...voice, ...(named as Partial<ToolStep>) };
}
