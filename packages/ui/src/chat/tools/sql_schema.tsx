// sql.schema answers in two shapes off one wire: the relations on a connection
// (`mode: "list"`), or one table's columns (`mode: "describe"`). The payload
// discriminates itself, so the card reads the result rather than the request and
// a call that failed before answering still renders the shape it asked for.

import type { ReactElement } from "react";

import type { ToolConversationPart } from "@alkera/chat-model";
import { Text } from "../sharedUi";
import { IconDatabase, IconEye, IconTable, IconTableColumn } from "@tabler/icons-react";

import { count, groupRuns, readResult, records, str, tally } from "./alkeraPayload";
import { Band, LeafPath, splitLeaf } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import s from "./sql_schema.module.css";

interface Relation {
  urn: string;
  name: string;
  kind: string;
}

interface Column {
  name: string;
  dataType: string;
  nullable: boolean;
}

interface SchemaView {
  describe: boolean;
  connection: string;
  table: string;
  relations: Relation[];
  columns: Column[];
}

function deriveSchema(part: ToolConversationPart): SchemaView {
  const result = readResult(part.output);
  const columns: Column[] = records(result?.columns).map((column) => ({
    name: str(column.name),
    dataType: str(column.data_type),
    nullable: column.nullable !== false,
  }));
  const answered = str(result?.table);
  return {
    describe: str(part.input?.mode) === "describe" || columns.length > 0 || answered.length > 0,
    connection: str(part.input?.connection),
    table: answered || str(part.input?.table),
    relations: records(result?.relations).map((relation) => ({
      urn: str(relation.urn),
      name: str(relation.name),
      kind: str(relation.kind) || "table",
    })),
    columns,
  };
}

/** The kinds the connection answered with, most numerous first: "42 tables, 6
 *  views" tells a reader more about a warehouse than one total does. */
function kindTally(relations: Relation[]): string {
  return tally(relations, (relation) => relation.kind)
    .map(([kind, n]) => count(n, kind, `${kind}s`))
    .join(", ");
}

/** The dotted namespace a relation sits in, which the listing names once above
 *  the relations that share it. */
function namespaceOf(relation: Relation): string {
  return splitLeaf(relation.name, ".").dir.replace(/\.$/, "");
}

/** A view is a lens over tables, so it wears one; everything else reads as the
 *  table it is and names its kind only when that kind is not "table". */
function KindMark({ kind }: { kind: string }): ReactElement {
  if (kind === "view") return <IconEye size={14} stroke={1.6} />;
  return <IconTable size={14} stroke={1.6} />;
}

/** A table's contract: its columns in ordinal order, each with the type a query
 *  must respect and, where the schema constrains it, the NOT NULL that says the
 *  column can be joined without guarding. */
function Columns({ columns }: { columns: Column[] }): ReactElement {
  return (
    <div className={s.sqlSchemaCols} data-cap="260" role="table" aria-label="Table columns">
      {columns.map((column, index) => (
        <div key={`${index}-${column.name}`} className="chat-sql-schema-col chat-tool-row" role="row">
          <span className={`${s.sqlSchemaColN} chat-tool-mono`} role="rowheader">
            {index + 1}
          </span>
          <span className={`${s.sqlSchemaColName} chat-tool-mono`} role="cell">
            {column.name}
          </span>
          <span className={`${s.sqlSchemaColType} chat-tool-mono`} role="cell">
            {column.dataType}
          </span>
          <span className={s.sqlSchemaColBound} role="cell">
            {column.nullable ? "" : "not null"}
          </span>
        </div>
      ))}
    </div>
  );
}

function Relations({ relations }: { relations: Relation[] }): ReactElement {
  return (
    <div className={s.sqlSchemaRels} data-cap="280">
      {groupRuns(relations, namespaceOf).map((group) => (
        <div key={group.key} className={s.sqlSchemaNs}>
          {group.key ? <p className={`${s.sqlSchemaNsHead} chat-tool-mono`}>{group.key}</p> : null}
          {group.items.map((relation, index) => (
            // Position, not identity: a listing can repeat a name (the same
            // relation reached through two schemas, an engine's own tables), and
            // a repeated key makes React drop a row of the listing.
            <div key={`${index}-${relation.urn || relation.name}`} className={s.sqlSchemaRel}>
              <span className="chat-sql-schema-rel__mark chat-tool-glyph" aria-hidden="true">
                <KindMark kind={relation.kind} />
              </span>
              {/* The row shows the leaf, so the tip carries what locates it. */}
              <Text className="chat-sql-schema-rel__name chat-tool-mono chat-tool-clip" tooltip="truncate" tooltipLabel={relation.urn || relation.name}>
                {splitLeaf(relation.name, ".").leaf}
              </Text>
              {relation.kind === "table" ? null : <span className="chat-sql-schema-rel__kind chat-tool-end chat-tool-quiet">{relation.kind}</span>}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

/** The well's interior: the connection the question went to, then either the
 *  table's columns or the connection's relations. It paints no ground, edge,
 *  radius, or outer pad; the group's well owns those. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const view = deriveSchema(part);
  const subject = view.describe && view.table ? `${view.connection} ${view.table}` : view.connection;
  return (
    <div data-tool="sql_schema">
      <Band>
        <span className="chat-tool-band__icon" aria-hidden="true">
          <IconDatabase size={14} stroke={1.6} />
        </span>
        <Text className="chat-tool-band__text chat-tool-band__text--one chat-tool-mono" tooltip="truncate" tooltipLabel={subject}>
          <span className="chat-sql-schema-conn chat-tool-quiet">{view.connection}</span>
          {view.describe && view.table ? (
            <>
              <span className={s.sqlSchemaSep} aria-hidden="true">
                /
              </span>
              <LeafPath path={view.table} sep="." />
            </>
          ) : null}
        </Text>
      </Band>
      {view.describe ? <Columns columns={view.columns} /> : <Relations relations={view.relations} />}
    </div>
  );
}

/** The step this tool contributes to the transcript's tool group. The card's
 *  voice is the listing one; a describe call answers about a single table, so it
 *  speaks its own words off the same wire. */
export const head: CardHead = (part) => {
  const view = deriveSchema(part);
  if (view.describe) {
    const bound = view.columns.filter((column) => !column.nullable).length;
    const columns = count(view.columns.length, "column", "columns");
    return {
      verb: "Described",
      object: view.table,
      objectKind: "graph",
      data: { kind: "count", text: bound > 0 ? `${columns}, ${bound} not null` : columns },
      // A column list is bounded and it IS the answer, so it shows itself.
      expanded: true,
      icon: IconTableColumn,
      disclosure: "columns",
      loneSummary: "Described 1 table",
      body: <Body part={part} />,
    };
  }
  return {
    object: view.connection,
    data: { kind: "count", text: kindTally(view.relations) || count(0, "relation", "relations") },
    body: <Body part={part} />,
  };
};
