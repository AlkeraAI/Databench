// sql.connections is the "what can I query" call, and its answer is a roster of
// targets. Each row leads with the handle, because the handle is the string every
// later call has to pass; dialect and environment qualify it. A prod target is
// the one qualifier that changes how carefully the next query should be written,
// so it is the only one that carries ink.
import type { ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { ConnectorMark, Text } from "../sharedUi";
import { count, readResult, records, str, tally } from "./alkeraPayload";
import { EmptyLine } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import s from "./sql_connections.module.css";
interface Connection {
  handle: string;
  dialect: string;
  environment: string;
}
function deriveConnections(part: ToolConversationPart): Connection[] {
  return records(readResult(part.output)?.connections).map((connection) => ({
    handle: str(connection.handle),
    dialect: str(connection.dialect),
    environment: str(connection.environment),
  }));
}
/** A dialect names an engine; the connector registry files that engine's mark
 *  under the plugin catalog's id. These four spellings differ between the two
 *  vocabularies -- every other dialect already IS the registry's own word, and
 *  an engine with no mark at all (sqlserver, oracle, the neutral `sql`) takes
 *  the component's plug. */
const MARK_ALIAS: Record<string, string> = {
  duckdb: "duckdb_local",
  mariadb: "mysql",
  postgresql: "postgres",
  presto: "trino",
};
function markId(dialect: string): string {
  const id = dialect.trim().toLowerCase();
  return MARK_ALIAS[id] ?? id;
}
/** What the connections speak, most common first: "2 snowflake, 1 duckdb" says
 *  which SQL the next query has to be written in. Empty when no connection
 *  declared a dialect. */
function dialectTally(connections: Connection[]): string {
  const declared = connections.filter((connection) => connection.dialect);
  return tally(declared, (connection) => connection.dialect)
    .map(([dialect, n]) => `${n} ${dialect}`)
    .join(", ");
}
/** The well's interior. The tool takes no arguments, so the well opens on its
 *  answer instead of on an empty input band. It paints no ground, edge, radius,
 *  or outer pad; the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const connections = deriveConnections(part);
  return (
    <div data-tool="sql_connections">
      {connections.length === 0 ? (
        <EmptyLine>No data connection is configured yet.</EmptyLine>
      ) : (
        <div className={`${s.sqlConnectionsList} chat-tool-ruled`} data-cap="260">
          {connections.map((connection) => (
            <div key={connection.handle} className={`${s.sqlConnectionsConn} chat-tool-line`}>
              <ConnectorMark id={markId(connection.dialect)} size={14} className={s.sqlConnectionsConnMark} />
              <Text className="chat-tool-mono chat-tool-clip" tooltip="truncate">
                {connection.handle}
              </Text>
              {connection.dialect ? <span className="chat-tool-quiet chat-tool-pin">{connection.dialect}</span> : null}
              {connection.environment ? (
                <span className={`${s.sqlConnectionsConnEnv} chat-tool-end chat-tool-quiet`} data-env={connection.environment}>
                  {connection.environment}
                </span>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
/** The step this tool contributes to the transcript's tool group. */
export const head: CardHead = (part) => {
  const connections = deriveConnections(part);
  return {
    object: "data connections",
    data: {
      kind: "count",
      text: dialectTally(connections) || count(connections.length, "connection", "connections"),
    },
    body: <Body part={part} />,
  };
};
