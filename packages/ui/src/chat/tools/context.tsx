// The knowledge base's three single-item tools render one object between them:
// a KB item. `context_get` returns the whole card; `context_note` carries the
// note it just wrote plus what the store made of it; `context_edit` carries only
// the fields it changed. A field the payload does not carry is simply absent.
//
// Trust is three states, not two: the store's `trust` + `trust_source` say
// whether a person confirmed the item, an agent merely asserted it, or nobody
// has checked it. A note asked to be shared can also come back private, when
// there was no team to share to; that downgrade tints the head's figure and
// states itself under the visibility it contradicts.
import type { ReactElement } from "react";
import { toTrustGrade, type ToolConversationPart, type TrustGrade } from "@alkera/chat-model";
import {
  IconAlertTriangle,
  IconLock,
  IconShield,
  IconShieldCheck,
  IconShieldOff,
  IconUsers,
} from "@tabler/icons-react";
import { Text } from "../sharedUi";
import { paintLeaves, sqlLines } from "../syntax";
import { count, num, readResult, str, strings } from "./alkeraPayload";
import { Band, LeafPath, PathBand } from "./shared";
import type { CardHead, StepEnvironment, StepFigure } from "./step";
import "./shared.css";
import "./context.css";
type Mode = "note" | "edit" | "get";
/** How far a reader may lean on the item, rendered in the card's own voice. */
const GRADE_WORD: Record<TrustGrade, string> = {
  verified: "verified",
  agent: "agent asserted",
  unverified: "unverified",
};
/** The store's kind vocabulary, shared with the map card so both name a kind the
 *  same way. */
export const KIND_WORD: Record<string, string> = {
  learned_fact: "learned fact",
  reference_sql: "reference SQL",
  schema_card: "schema card",
};
/** An edit sends only what it changes, so the fields present name the change. */
const CHANGE_WORD: Record<string, string> = {
  body: "body",
  title: "title",
  subject_path: "subject",
  file_sources: "sources",
  trusted: "trust",
  visibility: "visibility",
};
function modeOf(name: string): Mode {
  if (name.endsWith("context_edit")) return "edit";
  if (name.endsWith("context_get")) return "get";
  return "note";
}
/** The store titles an untitled note by its first line, so the card names it the
 *  same way rather than showing an item id the reader cannot place. */
function firstLine(text: string): string {
  return text.trim().split("\n")[0]?.slice(0, 80) ?? "";
}

/** What the item is called. A write receipt still names an item the call never titled. */
function titleOf(
  from: Record<string, unknown> | null,
  result: Record<string, unknown> | null,
): string {
  return str(from?.title) || str(result?.title);
}
interface ItemView {
  mode: Mode;
  /** The store answered. Until it does, the card states no trust and no
   *  visibility, because the call's request is not the store's decision. */
  answered: boolean;
  itemId: string;
  heading: string;
  title: string;
  body: string;
  kind: string;
  subject: string;
  sources: string[];
  stale: string[];
  grade: TrustGrade;
  visibility: string;
  downgraded: boolean;
  graduation: boolean;
  sharedBy: string;
  updatedAgo: string;
  confirmations: number | null;
  changed: string[];
}
function deriveItem(part: ToolConversationPart): ItemView {
  const mode = modeOf(part.name);
  const get = mode === "get";
  const result = readResult(part.output);
  const input: Record<string, unknown> = part.input ?? {};
  // A get renders what came back; every other mode renders what was sent.
  const from = get ? result : input;
  const title = titleOf(from, result);
  const body = get
    ? str(result?.body)
    : str(mode === "note" ? input.text : input.body);
  const visibility = str(result?.visibility);
  const changed = Object.keys(CHANGE_WORD)
    .filter((field) => input[field] !== undefined && input[field] !== null)
    .map((field) => CHANGE_WORD[field]);
  return {
    mode,
    answered: result !== null,
    itemId: str(result?.item_id) || str(input.item_id),
    heading:
      title || firstLine(body) || str(result?.item_id) || str(input.item_id),
    title,
    body,
    kind: str(from?.kind),
    subject: str(from?.subject_path),
    sources: strings(from?.file_sources),
    stale: get ? strings(result?.stale_sources) : [],
    grade: toTrustGrade(str(result?.trust), str(result?.trust_source)),
    visibility,
    downgraded:
      !get && str(input.visibility) === "shared" && visibility === "private",
    graduation:
      mode === "edit" && input.trusted === true && !input.body && !input.title,
    sharedBy:
      get && result?.is_mine === false
        ? str(result?.shared_by) || str(result?.shared_by_email)
        : "",
    updatedAgo: get ? str(result?.updated_ago) : "",
    confirmations: get ? num(result?.confirmations) : null,
    changed: mode === "edit" ? changed : [],
  };
}
function GradeGlyph({ grade }: { grade: TrustGrade }): ReactElement {
  if (grade === "verified") return <IconShieldCheck size={13} stroke={1.8} />;
  if (grade === "agent") return <IconShield size={13} stroke={1.8} />;
  return <IconShieldOff size={13} stroke={1.8} />;
}

/** Show the full title only when the band spent its line locating the item. */
function TitleLine({ item }: { item: ItemView }): ReactElement | null {
  if (!item.title || !item.subject) return null;
  return <p className="chat-context-title">{item.title}</p>;
}
/** A reference_sql item's body IS a query, so it reads as one. */
function Note({ body, kind }: { body: string; kind: string }): ReactElement {
  if (kind === "reference_sql") {
    return (
      <div
        className="chat-context-body chat-context-body--sql chat-tool-mono"
        data-cap="260"
      >
        {sqlLines(body).map((line, index) => (
          <p key={index} className="chat-context-sqlline">
            {paintLeaves(line)}
          </p>
        ))}
      </div>
    );
  }
  return (
    <p className="chat-context-body" data-cap="260">
      {body}
    </p>
  );
}

/** The band names an attachment, otherwise the item title, with the raw id as last resort. */
function BandName({ item }: { item: ItemView }): ReactElement {
  if (item.subject) return <LeafPath path={item.subject} />;
  if (item.title)
    return <span className="chat-context-name">{item.title}</span>;
  return <>{item.itemId}</>;
}

/** The item band becomes a knowledge-base door only when the host supplies that destination. */
function ItemBand({
  item,
  env,
}: {
  item: ItemView;
  env?: StepEnvironment;
}): ReactElement {
  const mono = item.subject || !item.title ? " chat-tool-mono" : "";
  const inner = (
    <>
      <Text
        className={`chat-tool-band__text chat-tool-band__text--one${mono}`}
        tooltip="truncate"
        tooltipLabel={item.subject || item.itemId}
      >
        <BandName item={item} />
      </Text>
      {item.kind ? (
        <span className="chat-tool-band__param">
          {KIND_WORD[item.kind] ?? item.kind}
        </span>
      ) : null}
    </>
  );
  const open = env?.onOpenKnowledgeItem;
  if (!open || !item.itemId) return <Band>{inner}</Band>;
  return (
    <button
      type="button"
      className="chat-tool-band"
      aria-label={`Open ${item.heading} in project knowledge`}
      onClick={() => open(item.itemId)}
    >
      {inner}
    </button>
  );
}
/** The well's interior: where the item lives, what it says, and how far to trust
 *  it. It paints no ground, edge, radius, or outer pad; the group's well owns
 *  those. */
function Body({
  part,
  env,
}: {
  part: ToolConversationPart;
  env?: StepEnvironment;
}): ReactElement {
  const item = deriveItem(part);
  const shared = item.visibility === "shared";
  return (
    <div data-tool="context">
      <ItemBand item={item} env={env} />
      {item.sources.map((path, index) => (
        <PathBand
          key={`${path}-${index}`}
          path={path}
          relativeTo={env?.workspaceRoot}
          onOpen={env?.onOpenPath ? () => env.onOpenPath?.(path) : undefined}
        />
      ))}
      {item.stale.length > 0 ? (
        <p className="chat-context-stale">
          <IconAlertTriangle size={14} stroke={1.8} aria-hidden="true" />
          <span className="chat-tool-break">
            Changed after this was written: {item.stale.join(", ")}
          </span>
        </p>
      ) : null}
      <TitleLine item={item} />
      {item.body ? <Note body={item.body} kind={item.kind} /> : null}
      {item.changed.length > 0 ? (
        <p className="chat-context-changed">
          Changed {item.changed.join(", ")}.
        </p>
      ) : null}
      {item.answered ? (
        <p className="chat-context-strip">
          <span
            className="chat-context-chip chat-tool-chip"
            data-trust={item.grade}
          >
            <GradeGlyph grade={item.grade} />
            {GRADE_WORD[item.grade]}
          </span>
          {item.visibility ? (
            <span
              className="chat-context-chip chat-tool-chip"
              data-vis={item.visibility}
            >
              {shared ? (
                <IconUsers size={13} stroke={1.8} />
              ) : (
                <IconLock size={13} stroke={1.8} />
              )}
              {item.visibility.charAt(0).toUpperCase() + item.visibility.slice(1)}
            </span>
          ) : null}
          {item.sharedBy ? (
            <Text className="chat-tool-clip" tooltip="truncate">
              Shared by {item.sharedBy}
            </Text>
          ) : null}
          {item.updatedAgo ? (
            <Text className="chat-tool-clip" tooltip="truncate">
              {item.updatedAgo}
            </Text>
          ) : null}
          {item.confirmations ? (
            <span>
              {count(item.confirmations, "confirmation", "confirmations")}
            </span>
          ) : null}
        </p>
      ) : null}
      {item.downgraded ? (
        <p className="chat-context-warn">
          <IconAlertTriangle size={14} stroke={1.8} aria-hidden="true" />
          <span className="chat-tool-break">
            Asked to share this, saved it private. There was no team to share
            to.
          </span>
        </p>
      ) : null}
    </div>
  );
}
/** What the call left behind: for a read, how far to trust what came back; for a
 *  write, where the item now lives. A downgrade outranks both. */
function figureOf(item: ItemView): StepFigure | undefined {
  if (!item.answered) return undefined;
  if (item.downgraded)
    return { kind: "count", text: "saved private", tone: "fail" };
  if (item.mode === "get") {
    const stale = item.stale.length > 0;
    const text = [GRADE_WORD[item.grade], stale ? "stale" : null]
      .filter(Boolean)
      .join(", ");
    return {
      kind: "count",
      text,
      tone: stale ? "fail" : item.grade === "verified" ? "pass" : undefined,
    };
  }
  const text = [
    item.visibility,
    item.grade === "unverified" ? null : GRADE_WORD[item.grade],
  ]
    .filter(Boolean)
    .join(", ");
  return text ? { kind: "count", text } : undefined;
}
/** The step a read of the store contributes to the transcript's tool group. */
export const headGet: CardHead = (part, env) => {
  const item = deriveItem(part);
  return {
    object: item.heading,
    data: figureOf(item),
    body: <Body part={part} env={env} />,
  };
};
/** The step a written note contributes. A write that carries no prose has
 *  nothing to show. */
export const headNote: CardHead = (part, env) => {
  const item = deriveItem(part);
  return {
    object: item.heading,
    data: figureOf(item),
    expanded: item.body.length > 0,
    body: <Body part={part} env={env} />,
  };
};
/** The step an edit contributes. Raising an item's trust without touching its
 *  prose is a graduation, not a refinement. */
export const headEdit: CardHead = (part, env) => {
  const item = deriveItem(part);
  return {
    verb: item.graduation ? "Graduated" : "Refined",
    object: item.heading,
    data: figureOf(item),
    expanded: item.body.length > 0,
    body: <Body part={part} env={env} />,
  };
};
