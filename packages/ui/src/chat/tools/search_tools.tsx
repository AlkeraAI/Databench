// What the agent can call, and with what. `search_tools` answers a query with
// matching tools; `list_agent_types` answers with every agent it can spawn. The
// payloads are the same roster of name, description, and input schema, so they
// render as one card.
//
// The argument signature is the half a reader cannot get from the name. It rides
// each row while the rows differ, and moves to a line of its own when every row
// takes the same arguments, which is what a list of agents always looks like.

import type { ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";
import { Text } from "../sharedUi";

import { count, isRecord, num, readResult, records, str, strings } from "./alkeraPayload";
import { Band, EmptyLine } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import s from "./search_tools.module.css";

interface Arg {
  name: string;
  required: boolean;
}

interface Capability {
  name: string;
  description: string;
  app: string;
  args: Arg[];
}

interface RosterView {
  agents: boolean;
  cards: Capability[];
  /** The one signature every row shares, hoisted out of the rows. */
  shared: Arg[];
  query: string;
  app: string;
  k: number | null;
}

function argsOf(schema: unknown): Arg[] {
  if (!isRecord(schema)) return [];
  const properties = schema.properties;
  if (!isRecord(properties)) return [];
  const required = new Set(strings(schema.required));
  return Object.keys(properties).map((name) => ({ name, required: required.has(name) }));
}

function signature(args: Arg[]): string {
  return args.map((arg) => `${arg.name}${arg.required ? "*" : ""}`).join(", ");
}

function deriveRoster(part: ToolConversationPart): RosterView {
  const agents = part.name.endsWith("list_agent_types");
  const result = readResult(part.output);
  const cards = records(agents ? result?.agents : result?.tools).map((entry) => ({
    name: str(entry.name),
    description: str(entry.description),
    app: str(entry.app),
    args: argsOf(entry.input_schema),
  }));
  const first = cards[0] ? signature(cards[0].args) : "";
  const uniform = cards.length > 1 && first.length > 0 && cards.every((card) => signature(card.args) === first);
  return {
    agents,
    cards,
    shared: uniform ? cards[0].args : [],
    query: str(part.input?.query),
    app: str(part.input?.app),
    k: num(part.input?.k),
  };
}

function Args({ args }: { args: Arg[] }): ReactElement {
  return (
    <span className={`${s.searchToolsArgs} chat-tool-mono`}>
      {args.map((arg, index) => (
        <span key={arg.name} className={s.searchToolsArg} data-required={arg.required ? "" : undefined}>
          {index > 0 ? ", " : null}
          {arg.name}
          {arg.required ? "*" : null}
        </span>
      ))}
    </span>
  );
}

/** The well's interior: what was asked, then what can be called. It paints no
 *  ground, edge, radius, or outer pad; the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const view = deriveRoster(part);
  return (
    <div data-tool="search_tools">
      {view.agents ? null : (
        <Band>
          <Text className="chat-tool-band__text chat-tool-band__text--one" tooltip="truncate">
            {view.query}
          </Text>
          {view.app ? <span className="chat-tool-band__param">in {view.app}</span> : null}
          {view.k === null ? null : <span className="chat-tool-band__param">top {view.k}</span>}
        </Band>
      )}
      {view.shared.length > 0 ? (
        <p className={s.searchToolsShared}>
          Each takes <Args args={view.shared} />
        </p>
      ) : null}
      {view.cards.length === 0 ? (
        <EmptyLine>{view.agents ? "No agent types are available." : "No tool matched this query."}</EmptyLine>
      ) : (
        <div className="chat-tool-ruled" data-cap="360">
          {view.cards.map((card) => (
            <div key={card.name} className={s.searchToolsCard}>
              <p className={`${s.searchToolsCardHead} chat-tool-line chat-tool-line--base`}>
                <span className={`${s.searchToolsCardName} chat-tool-mono`}>{card.name}</span>
                {card.app ? <span className={`${s.searchToolsCardApp} chat-tool-chip`}>{card.app}</span> : null}
              </p>
              {card.description ? (
                <Text as="p" className={s.searchToolsCardDesc} tooltip="truncate">
                  {card.description}
                </Text>
              ) : null}
              {view.shared.length === 0 && card.args.length > 0 ? <Args args={card.args} /> : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** The step a tool search contributes to the transcript's tool group. */
export const headSearch: CardHead = (part) => {
  const view = deriveRoster(part);
  return {
    object: view.query,
    data: { kind: "count", text: count(view.cards.length, "tool", "tools") },
    body: <Body part={part} />,
  };
};

/** The step an agent-type roster contributes to the transcript's tool group. */
export const headAgents: CardHead = (part) => {
  const view = deriveRoster(part);
  return {
    object: "agent types",
    data: { kind: "count", text: count(view.cards.length, "agent", "agents") },
    body: <Body part={part} />,
  };
};
