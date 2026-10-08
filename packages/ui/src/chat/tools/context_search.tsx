// Context search, designed around the question a reader actually has about a
// retrieved note: can I act on this? The two lanes (team knowledge, catalog)
// stay apart and each entry carries its warrant: the trust word with its
// source, when it was last touched, how many people confirmed it, and its
// subject. A stale source is the one thing that can make a verified note
// wrong, so it gets its own tinted line.
import type { ReactElement } from "react";
import {
  toTrustGrade,
  type ToolConversationPart,
  type TrustGrade,
} from "@alkera/chat-model";
import {
  IconAlertTriangle,
  IconBook2,
  IconDatabase,
  IconNote,
  IconShield,
  IconShieldCheck,
} from "@tabler/icons-react";
import { Text } from "../sharedUi";
import { count, num, records, str, strings, readResult } from "./alkeraPayload";
import { Band, Capped, EmptyLine, LeafPath } from "./shared";
import type { CardHead, StepEnvironment } from "./step";
import "./shared.css";
import s from "./context_search.module.css";
interface Card {
  itemId: string;
  title: string;
  body: string;
  kind: string;
  grade: TrustGrade;
  trustWord: string;
  trustSource: string;
  origin: string;
  subjectPath: string;
  updatedAgo: string;
  confirmations: number | null;
  staleSources: string[];
}
/** Project open search payloads through the canonical trust grade. */
function readCards(value: unknown): Card[] {
  return records(value).map((entry) => {
    const trustSource = str(entry.trust_source);
    const grade = toTrustGrade(str(entry.trust), trustSource);
    return {
      itemId: str(entry.item_id),
      title: str(entry.title) || str(entry.subject_path),
      body: str(entry.body),
      kind: str(entry.kind),
      grade,
      trustWord: grade === "agent" ? "agent asserted" : grade,
      trustSource,
      origin: str(entry.origin),
      subjectPath: str(entry.subject_path),
      updatedAgo: str(entry.updated_ago),
      confirmations: num(entry.confirmations),
      staleSources: strings(entry.stale_sources),
    };
  });
}
function KindGlyph({ kind }: { kind: string }): ReactElement {
  if (kind === "runbook") return <IconBook2 size={14} stroke={1.6} />;
  if (kind === "asset") return <IconDatabase size={14} stroke={1.6} />;
  return <IconNote size={14} stroke={1.6} />;
}
/** A subject is a repo path or an asset URN; both locate their leaf the same way. */
function Subject({ path }: { path: string }): ReactElement {
  return <LeafPath path={path} sep={path.includes("://") ? "." : "/"} />;
}
/** The keyboard-accessible name of a result, wired only when it has a destination. */
function ItemName({
  card,
  open,
}: {
  card: Card;
  open?: () => void;
}): ReactElement {
  const name = (
    <Text className="chat-tool-clip" tooltip="truncate">
      {card.title}
    </Text>
  );
  if (!open) return name;
  return (
    <button
      type="button"
      className={s.contextSearchKnowOpen}
      aria-label={`Open ${card.title} in project knowledge`}
      onClick={open}
    >
      {name}
    </button>
  );
}

/** Let the whole result row open while preserving any nested control's own action. */
function rowPress(open: () => void) {
  return (event: { target: unknown }) => {
    if (!(event.target as HTMLElement).closest("button,a")) open();
  };
}

function Entry({
  card,
  env,
}: {
  card: Card;
  env?: StepEnvironment;
}): ReactElement {
  const verified = card.grade === "verified";
  const warrant = card.trustSource || card.origin;
  const seam = env?.onOpenKnowledgeItem;
  const open = seam && card.itemId ? () => seam(card.itemId) : undefined;
  return (
    <article
      className={s.contextSearchKnow}
      data-door={open ? "" : undefined}
      onClick={open ? rowPress(open) : undefined}
    >
      <p className={s.contextSearchKnowTitle}>
        <span className={s.contextSearchKnowGlyph} aria-hidden="true">
          <KindGlyph kind={card.kind} />
        </span>
        <ItemName card={card} open={open} />
      </p>
      {card.body ? (
        <Text as="p" className={s.contextSearchKnowBody} tooltip="truncate">
          {card.body}
        </Text>
      ) : null}
      <p className={s.contextSearchKnowMeta}>
        <span className={s.contextSearchTrust} data-trust={card.grade}>
          {verified ? (
            <IconShieldCheck size={14} stroke={1.8} />
          ) : (
            <IconShield size={14} stroke={1.8} />
          )}
          {card.trustWord}
        </span>
        {warrant ? (
          <Text className={s.contextSearchKnowSrc} tooltip="truncate">
            {warrant}
          </Text>
        ) : null}
        {card.confirmations ? (
          <span>
            {count(card.confirmations, "confirmation", "confirmations")}
          </span>
        ) : null}
      </p>
      {card.subjectPath ? (
        <Text
          as="p"
          className={`${s.contextSearchKnowSubject} chat-tool-mono`}
          tooltip="truncate"
          tooltipLabel={card.subjectPath}
        >
          <Subject path={card.subjectPath} />
          {card.updatedAgo ? (
            <span className={s.contextSearchKnowAged}> {card.updatedAgo}</span>
          ) : null}
        </Text>
      ) : null}
      {card.staleSources.map((source) => (
        <p key={source} className={s.contextSearchKnowStale}>
          <IconAlertTriangle size={14} stroke={1.8} />
          {source}
        </p>
      ))}
    </article>
  );
}
function Lane({
  label,
  cards,
  env,
}: {
  label: string;
  cards: Card[];
  env?: StepEnvironment;
}): ReactElement | null {
  if (cards.length === 0) return null;
  return (
    <section className={s.contextSearchLane}>
      <p className={`${s.contextSearchLaneHead} chat-tool-lane-head`}>
        {label}
        <span className="chat-tool-num chat-tool-quiet">{cards.length}</span>
      </p>
      {cards.map((card, index) => (
        <Entry key={card.itemId || `${label}-${index}`} card={card} env={env} />
      ))}
    </section>
  );
}
interface SearchView {
  team: Card[];
  catalog: Card[];
  query: string;
  k: number | null;
}
function deriveSearch(part: ToolConversationPart): SearchView {
  const result = readResult(part.output);
  return {
    team: readCards(result?.team_knowledge),
    catalog: readCards(result?.catalog),
    query: str(part.input?.query),
    k: num(part.input?.k),
  };
}
/** The well's interior: the query band, then the two lanes. It paints no ground,
 *  edge, radius, or outer pad; the group's well owns those. */
function Body({
  part,
  env,
}: {
  part: ToolConversationPart;
  env?: StepEnvironment;
}): ReactElement {
  const { team, catalog, query, k } = deriveSearch(part);
  return (
    <div data-tool="context_search">
      <Band>
        <Text
          className="chat-tool-band__text chat-tool-band__text--one"
          tooltip="truncate"
        >
          {query}
        </Text>
        {k === null ? null : (
          <span className="chat-tool-band__param">top {k}</span>
        )}
      </Band>
      {team.length === 0 && catalog.length === 0 ? (
        <EmptyLine>No team knowledge or catalog item matched.</EmptyLine>
      ) : (
        <Capped cap={340}>
          <Lane label="Team knowledge" cards={team} env={env} />
          <Lane label="Catalog" cards={catalog} env={env} />
        </Capped>
      )}
    </div>
  );
}
/** The step this tool contributes to the transcript's tool group. */
export const head: CardHead = (part, env) => {
  const { team, catalog, query } = deriveSearch(part);
  return {
    object: query,
    data: {
      kind: "count",
      text: count(team.length + catalog.length, "match", "matches"),
    },
    body: <Body part={part} env={env} />,
  };
};
