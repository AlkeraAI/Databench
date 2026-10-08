// Turning a result the agent produced into something the org keeps.
//
// Two different things come off the same card, and the difference matters:
//
// * SAVING A RESULT pins the numbers as they were. The cloud only records the
//   intent — the machine that ran the query uploads the payload afterwards —
//   because the full rows never crossed to the cloud in the first place (the
//   transcript carries a preview and a handle, never the data);
// * SAVING A QUERY keeps the STATEMENT, with its `{expr}` slots named as
//   parameters, so it can be re-run later against a different customer or date
//   range. That re-run happens on the machine too; the cloud never executes SQL.

import type { ToolConversationPart } from "@alkera/chat-model";

import type { PromoteColumn } from "../../../api/cloudChat/transport";
import { PARAM_TYPES, parametersFromLiterals, slotsOf, type ParamType } from "../../../lib/querySlots";

import type { PreviewCell } from "./chartDerive";

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

function resultOf(part: ToolConversationPart): Record<string, unknown> {
  const output = part.output;
  if (!isRecord(output)) return {};
  const inner = output.result;
  return isRecord(inner) ? inner : output;
}

function stringField(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : "";
}

/** The statement a result came from, wherever the tool put it. */
export function sqlOf(part: ToolConversationPart): string {
  const input = isRecord(part.input) ? part.input : {};
  return stringField(input, "sql") || stringField(input, "query");
}

/** The columns the preview came back with, as a promotion names them. */
export function columnsOf(part: ToolConversationPart): PromoteColumn[] {
  const result = resultOf(part);
  const columns = result.columns;
  if (!Array.isArray(columns)) return [];
  return columns.map((column) => ({ name: String(column), label: null }));
}

/** The grid the card showed: its columns and the preview rows behind them. The
 *  chart offer is derived from these, so it is derived from exactly what the
 *  reader was looking at when they pressed save. */
export function previewOf(part: ToolConversationPart): {
  columns: string[];
  rows: PreviewCell[][];
} {
  const result = resultOf(part);
  const columns = Array.isArray(result.columns)
    ? result.columns.map((column) => String(column))
    : [];
  const raw = result.preview_rows;
  const rows = Array.isArray(raw)
    ? raw.map((row) =>
        (Array.isArray(row) ? row : []).map((cell): PreviewCell => {
          if (cell === null || cell === undefined) return null;
          if (typeof cell === "string" || typeof cell === "number" || typeof cell === "boolean") {
            return cell;
          }
          return String(cell);
        }),
      )
    : [];
  return { columns, rows };
}

/** A title to offer for a saved object: what the tool called the result, else
 *  the relation it read, else nothing (the reader is asked). */
export function suggestedTitle(part: ToolConversationPart): string {
  const result = resultOf(part);
  const input = isRecord(part.input) ? part.input : {};
  return (
    stringField(result, "result_name") ||
    stringField(input, "result_name") ||
    stringField(input, "table")
  );
}

/** The parameter types a saved query may declare. The names are the compiler's
 *  own vocabulary (`ParamType` in `alkera_core/schemas/objects/specs.py`), and
 *  `querySpecSeam.test.ts` pins this list equal to it — a type this side
 *  invented would be refused at the write. */
export const QUERY_PARAM_TYPES = PARAM_TYPES;
export type QueryParamType = ParamType;

/** The engines a saved query may name: exactly those the compiler has a
 *  bound-parameter path for (`QueryEngine`, same module, same pin). A query
 *  against anything else is refused at the write rather than saved with
 *  placeholders its driver would never bind. */
export const QUERY_ENGINES = [
  "postgres",
  "redshift",
  "clickhouse",
  "tinybird",
  "duckdb",
  "duckdb_local",
  "sqlite",
] as const;

/** One declared slot of a saved query, as `QueryParam` spells it.
 *
 *  A type alias rather than an interface, here and below, so it stays
 *  assignable to the SDK's free-form `spec` (`{[key: string]: unknown}`) —
 *  TypeScript gives an object type alias the implicit index signature an
 *  interface never gets. */
export type QueryParamDraft = {
  name: string;
  type: QueryParamType;
};

/** A saved query, as `QuerySpec` spells it. Every key here is a field the
 *  server declares: the spec model allows extras (a newer writer's field must
 *  survive an older reader), which means a MISSPELLED key is stored silently
 *  and read back as a default — so what this builder emits is pinned against
 *  the real reader by a cross-seam test rather than by review. */
export type QuerySpecDraft = {
  sql_template: string;
  params: QueryParamDraft[];
  connection_id: string | null;
  source_chat_id: string | null;
  engine?: string;
  defaults?: Record<string, unknown>;
};

/** A declarable parameter name. The compiler accepts mixed case in a template,
 *  but a DECLARATION must be lower-case, so a `{Customer}` slot is left
 *  undeclared and its type is inferred from the value at re-run. */
const DECLARABLE = /^[a-z_][a-z0-9_]*$/;

/**
 * The parameters a statement declares, in the order they first appear and
 * without repeats — the shared slot grammar (`@/lib/querySlots`), which reads
 * both `{customer}` and Tinybird's own `{{String(customer)}}`, minus the names
 * a declaration may not spell.
 */
export function paramsOf(sql: string): QueryParamDraft[] {
  return slotsOf(sql)
    .filter((slot) => DECLARABLE.test(slot.name))
    .map((slot) => ({ name: slot.name, type: slot.type }));
}

/** The values the statement ran with, as the tool was called with them. They
 *  become the re-run form's starting point, so the reader changes the one
 *  parameter they came to change. Only the slots the template actually
 *  declares are kept — anything else is a value with nothing to bind to. */
function defaultsOf(part: ToolConversationPart, params: readonly QueryParamDraft[]): Record<string, unknown> {
  const input = isRecord(part.input) ? part.input : {};
  const recorded = isRecord(input.params) ? input.params : {};
  const kept: Record<string, unknown> = {};
  for (const param of params) {
    const value = recorded[param.name];
    if (value !== undefined && value !== null) kept[param.name] = value;
  }
  return kept;
}

/** The spec a saved query is stored as: the statement, its declared slots, the
 *  connection it was written against, and the chat that has the machine to run
 *  it in. No result data — a query is the question, not an answer.
 *
 *  `chatId` is not optional in spirit: a query with no chat is a query that can
 *  never be re-run (the cloud executes no SQL — R-H), which is why the server
 *  refuses to create one. */
export function querySpecOf(
  part: ToolConversationPart,
  chatId: string | undefined,
): QuerySpecDraft {
  const input = isRecord(part.input) ? part.input : {};
  const result = resultOf(part);
  const sql = sqlOf(part);
  // An executed statement is stamped with a PROVENANCE block, and that is where
  // the tool puts the engine it ran on and the cloud id of the connection it ran
  // against — the card's own source line reads them from there. Read flat only
  // as a fallback, for a result recorded before the block existed: a saved query
  // whose connection is null and whose engine defaults to postgres can never be
  // re-run, and a Tinybird statement re-run as Postgres binds nothing.
  const provenance = isRecord(result.provenance) ? result.provenance : {};
  const engine =
    stringField(provenance, "engine") ||
    stringField(result, "engine") ||
    stringField(input, "engine");
  const connectionId =
    stringField(provenance, "connection_id") ||
    stringField(result, "connection_id") ||
    stringField(input, "connection_id");
  // A statement that declares slots is saved as its author wrote it. One that
  // declares none is OFFERED its own filters as parameters, with the literals
  // it was run with as their values — otherwise a query saved from a real
  // card has nothing to change before re-running, because an agent
  // asked a plain question writes `WHERE run_day >= toDate('2026-08-31')`,
  // not a slot.
  const declared = paramsOf(sql);
  const derived = declared.length === 0 ? parametersFromLiterals(sql) : null;
  const params = derived && derived.params.length > 0 ? derived.params : declared;
  const template = derived && derived.params.length > 0 ? derived.template : sql;
  const defaults = { ...(derived?.defaults ?? {}), ...defaultsOf(part, params) };
  return {
    sql_template: template,
    params,
    connection_id: connectionId || null,
    source_chat_id: chatId ?? null,
    // Omitted rather than guessed when the tool named none: the server's own
    // default is the one place that decision belongs.
    ...(engine ? { engine } : {}),
    // Likewise omitted when the statement bound nothing, so a save off a card
    // with no parameters writes exactly the bytes it always did.
    ...(Object.keys(defaults).length > 0 ? { defaults } : {}),
  };
}

/** The event a promotion names — the transcript entry the machine uploads the
 *  payload for. A tool part's call id IS that entry's id. */
export function eventIdOf(part: ToolConversationPart): string {
  return part.callId;
}
