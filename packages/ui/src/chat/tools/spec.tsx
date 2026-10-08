// THE SPEC SHEET. A tool's output describes a resource, so it sets as a
// specification: fields under quiet section labels, each name against its value
// in a shared measured column, arrays as inline runs, and a collection as a run
// of numbered stacked mini specs. Rank comes from rule weight, ink, case, and
// air at one type size. Field names are retitled from the vendor's own keys, so
// `usename` reads Usename and `query_start` reads Query start; the only
// per-tool work is a section order for a record worth grouping.
//
// A boolean is a presence mark, filled for yes and hollow for no. It reads fast
// down the seven booleans of the RDS spec, and it fails where the boolean IS
// the whole answer: postgres.cancel_query returns `{requested, pid, cancelled:
// false}`, and a well holding one hollow ring answers nothing. So an
// acknowledgment earns no well here. Its outcome goes in the head, in words, on
// the figure the frame already tints for a failure. The mark survives only
// where it has peers to be compared against.
import { Fragment, type ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { IconCheck, IconX } from "@tabler/icons-react";
import { connectorBrand, ConnectorMark } from "../../brand/ConnectorMark";
import { codeLeaves, highlightBash, paintLeaves, sqlLines } from "../syntax";
import { count, isRecord, readResult, records, str } from "./alkeraPayload";
import { FailLine } from "./shared";
import type { CardHead, StepFigure } from "./step";
import "./shared.css";
import "./spec.css";
/** Keys that read as words, not as an initialism, when a label is derived. */
const ACRONYM = new Set([
  "id",
  "ids",
  "az",
  "kms",
  "iam",
  "ca",
  "vpc",
  "iops",
  "pid",
  "sql",
  "arn",
  "uri",
  "url",
  "dns",
  "ip",
  "aws",
  "cpu",
  "ttl",
]);
/** A trailing unit belongs beside the number, not in the field's name. */
const UNIT: Record<string, string> = {
  gb: "GB",
  tb: "TB",
  mb: "MB",
  days: "days",
  seconds: "seconds",
  ms: "ms",
  bytes: "bytes",
};
/** Flags that only mean something when set: a warning about the record, not
 *  one of a run of comparable booleans. Unset, the field is left out, because
 *  a hollow mark beside "Output outdated" reads as a dismiss button rather
 *  than as "no". Set, it is said in words under its own label. */
const FLAG_WORDS: Record<string, { label: string; yes: string }> = {
  output_outdated: { label: "Output", yes: "Outdated: the code or environment changed since it ran" },
};
/** A field the sheet leaves out: a warning flag that is not set. */
function quiet(key: string, value: unknown): boolean {
  return FLAG_WORDS[key] !== undefined && value !== true;
}
function labelOf(key: string, value?: unknown): string {
  const flag = FLAG_WORDS[key];
  if (flag !== undefined && value === true) return flag.label;
  let parts = key.split("_");
  const last = parts[parts.length - 1];
  if (parts.length > 1 && typeof value === "number" && UNIT[last] !== undefined) parts = parts.slice(0, -1);
  if (parts.length > 1 && typeof value === "boolean" && last === "enabled") parts = parts.slice(0, -1);
  const words = parts.map((part) => (ACRONYM.has(part) ? part.toUpperCase() : part));
  if (!ACRONYM.has(parts[0])) words[0] = words[0].charAt(0).toUpperCase() + words[0].slice(1);
  return words.join(" ");
}
function unitOf(key: string, value: unknown): string {
  if (typeof value !== "number") return "";
  const parts = key.split("_");
  if (parts.length < 2) return "";
  const unit = UNIT[parts[parts.length - 1]];
  return unit === undefined ? "" : ` ${unit}`;
}
/** The connector registry is keyed by this same vocabulary, so a mark is one
 *  lookup away. Parent-hosted tools have no vendor and keep the bench glyph. */
export function vendorId(name: string): string | null {
  const head = name.split(".")[0];
  return connectorBrand(head) ? head : null;
}
const STATEMENT =
  /^(select|insert|update|delete|merge|call|refresh|with|create|alter|drop|grant|revoke|copy|vacuum|analyze|explain|truncate|show|describe|set|begin|commit|rollback|use|unload|values)\b/i;
/** A statement can open with comments or parens before its verb. A verb
 *  followed by nothing but a number is a label like `grant 2`, not SQL. */
function isStatement(value: string): boolean {
  let head = value.trim();
  for (;;) {
    const shorter = head.replace(/^(?:--[^\n]*|\/\*[\s\S]*?\*\/|#[^\n]*)\s*/, "").replace(/^\(\s*/, "");
    if (shorter === head) break;
    head = shorter;
  }
  return STATEMENT.test(head) && !/^\w+\s+\d+$/.test(head);
}
type Grammar = "sql" | "bash" | "json";
/** The key names that mean the value IS code whatever it looks like, so the card
 *  reads the vendor's own naming instead of sniffing the text. Only names that
 *  mean one thing across all 126 tools: `sql`, `query_text`, `where` (a SQL
 *  predicate) and `command`. `query` is NOT here -- it carries a statement on
 *  bigquery and a search phrase on web.search, context_search, lineage_find and
 *  search_tools, and painting a person's search as SQL is the worse mistake. A
 *  real statement under `query` is caught by `isStatement` anyway. The match is
 *  exact, so `query_id` and `query_start` stay plain. */
const CODE_KEY: Record<string, Grammar> = {
  sql: "sql",
  query_text: "sql",
  where: "sql",
  command: "bash",
};
function timeish(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}T/.test(value) || /^([a-z]{3}:)?\d{2}:\d{2}-([a-z]{3}:)?\d{2}:\d{2}$/.test(value);
}
/** Mono only where the content IS code. A version is a number, a warehouse name
 *  in shouting caps is a name, and a timestamp is a figure. */
function codeish(value: string): boolean {
  if (value === "") return false;
  if (/\s/.test(value)) return isStatement(value);
  if (/^\d+(\.\d+)*$/.test(value)) return false;
  if (/^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$/.test(value)) return false;
  if (timeish(value)) return false;
  if (/^[0-9a-f]{12,}$/i.test(value)) return true;
  return /[-_:/.@]/.test(value);
}
type Section = readonly [string, readonly string[]];
/** The only per-tool work the sheet asks for: which fields belong together and
 *  in what order. A key no map mentions lands in a trailing section, so a field
 *  the backend adds tomorrow is never dropped. */
const SECTIONS: Record<string, readonly Section[]> = {
  "aws.rds.describe_instance": [
    ["Instance", ["identifier", "status", "engine", "engine_version", "cluster_identifier", "created_at"]],
    ["Connection", ["host", "port", "database", "master_username"]],
    [
      "Placement",
      ["availability_zone", "secondary_availability_zone", "multi_az", "vpc_id", "subnet_group", "subnets"],
    ],
    ["Capacity", ["instance_class", "allocated_storage_gb", "max_allocated_storage_gb", "storage_type", "iops"]],
    [
      "Security",
      [
        "publicly_accessible",
        "storage_encrypted",
        "kms_key_id",
        "iam_auth_enabled",
        "deletion_protection",
        "ca_certificate_identifier",
        "security_groups",
      ],
    ],
    [
      "Maintenance and backups",
      [
        "preferred_maintenance_window",
        "preferred_backup_window",
        "backup_retention_days",
        "auto_minor_version_upgrade",
      ],
    ],
    [
      "Observability",
      [
        "monitoring_interval_seconds",
        "performance_insights_enabled",
        "performance_insights_retention_days",
        "log_exports",
      ],
    ],
    ["Engine configuration", ["parameter_groups", "option_group"]],
    ["Tags", ["tags"]],
  ],
  "databricks.get_table": [
    ["Definition", ["full_name", "table_type", "data_source_format", "owner", "storage_location"]],
    ["Notes", ["comment"]],
    ["Columns", ["columns"]],
  ],
};
/** A call can arrive under an MCP prefix, so the map matches on the suffix. */
function sectionsFor(name: string): readonly Section[] | null {
  const tool = Object.keys(SECTIONS).find((key) => name.endsWith(key));
  return tool === undefined ? null : SECTIONS[tool];
}
function isRecordList(value: unknown): value is Record<string, unknown>[] {
  return Array.isArray(value) && value.length > 0 && records(value).length === value.length;
}
function scalarText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
/** A boolean answers yes or no, so the mark says which in the plainest signs
 *  there are: a check for yes, an X for no. */
function Mark({ on }: { on: boolean }): ReactElement {
  const word = on ? "Yes" : "No";
  return (
    <span className="chat-spec-mk" data-on={on ? "1" : "0"} role="img" aria-label={word} title={word}>
      {on ? <IconCheck size={13} stroke={2.2} /> : <IconX size={13} stroke={2.2} />}
    </span>
  );
}
/** A code value painted as the language it is. A key that declares its grammar
 *  names it; otherwise only a statement is painted, because a bare value
 *  carries no filename and no fence, and sniffing the value's own text for one
 *  paints a path's extension as a keyword. A statement spans lines, so it keeps
 *  them; anything else is mono in one ink. */
function Code({ text, grammar }: { text: string; grammar: Grammar | null }): ReactElement {
  if (grammar === "bash") return <span className="chat-tool-mono">{highlightBash(text)}</span>;
  if (grammar === "json") return <span className="chat-tool-mono">{paintLeaves(codeLeaves(text, "json"))}</span>;
  if (grammar === null && !isStatement(text)) return <span className="chat-tool-mono">{text}</span>;
  return (
    <span className="chat-spec-sql chat-tool-mono">
      {sqlLines(text).map((line, index) => (
        <span key={index} className="chat-spec-sql__l">
          {paintLeaves(line)}
        </span>
      ))}
    </span>
  );
}
function Scalar({ name, value }: { name: string; value: unknown }): ReactElement {
  const flag = FLAG_WORDS[name];
  if (flag !== undefined && value === true) return <>{flag.yes}</>;
  if (typeof value === "boolean") return <Mark on={value} />;
  if (value === null || value === undefined || value === "") return <span className="chat-tool-quiet">None</span>;
  if (typeof value === "number") return <span className="chat-tool-num">{`${value}${unitOf(name, value)}`}</span>;
  const text = scalarText(value);
  // A nested object's text is this card's own, made by `JSON.stringify`, so its
  // grammar is known rather than guessed.
  if (typeof value === "object") return <Code text={text} grammar="json" />;
  if (typeof value !== "string") return <>{text}</>;
  const declared = CODE_KEY[name];
  if (declared !== undefined) return <Code text={text} grammar={declared} />;
  if (codeish(value)) return <Code text={text} grammar={null} />;
  if (timeish(value)) return <span className="chat-tool-num">{text}</span>;
  return <>{text}</>;
}
/** An array is an inline run of small items. Small means tight, never smaller. */
function Value({ name, value }: { name: string; value: unknown }): ReactElement {
  if (!Array.isArray(value)) return <Scalar name={name} value={value} />;
  if (value.length === 0) return <span className="chat-tool-quiet">None</span>;
  return (
    <span className="chat-spec-chips">
      {value.map((item, index) => (
        <span key={index} className="chat-spec-chip chat-tool-chip">
          <Scalar name={name} value={item} />
        </span>
      ))}
    </span>
  );
}
/** A field: its name set against its value in the block's shared column. A raw
 *  label is a key nobody named (a tag, a vendor's own map), so it keeps its
 *  literal form in mono. */
function Field({ name, value, raw }: { name: string; value: unknown; raw?: boolean }): ReactElement | null {
  if (quiet(name, value)) return null;
  return (
    <div className="chat-spec-f chat-tool-row">
      <span className={raw && FLAG_WORDS[name] === undefined ? "chat-spec-f__k chat-tool-mono" : "chat-spec-f__k"}>
        {raw && FLAG_WORDS[name] === undefined ? name : labelOf(name, value)}
      </span>
      <span className="chat-spec-f__v">
        <Value name={name} value={value} />
      </span>
    </div>
  );
}
function Rec({
  item,
  ordinal,
  rule,
  raw,
}: {
  item: Record<string, unknown>;
  ordinal?: number;
  rule?: boolean;
  raw?: boolean;
}): ReactElement {
  const entries = Object.entries(item).filter(([key, value]) => !quiet(key, value));
  return (
    <div className="chat-spec-rec chat-tool-row">
      {rule ? <span className="chat-spec-rule" /> : null}
      {/* The ordinal holds one gutter cell against all of its record's rows, so
          the fields beside it keep the run's shared label column. */}
      <span className="chat-spec-n" style={{ gridRow: `span ${Math.max(entries.length, 1)}` }}>
        {ordinal ?? ""}
      </span>
      {entries.map(([key, value]) => (
        <Field key={key} name={key} value={value} raw={raw} />
      ))}
    </div>
  );
}
/** A list of records is a run of mini specs. Their labels share one column, so
 *  a field reads straight down the run. */
function Run({
  items,
  ordinals = true,
  raw,
}: {
  items: Record<string, unknown>[];
  ordinals?: boolean;
  raw?: boolean;
}): ReactElement {
  return (
    <div className="chat-spec-run">
      {items.map((item, index) => (
        <Rec key={index} item={item} ordinal={ordinals ? index + 1 : undefined} rule={index > 0} raw={raw} />
      ))}
    </div>
  );
}
/** A block of fields against one measured label column. When a section head
 *  already names the block's single key, that key's own keys become the fields
 *  and the head carries the name. */
function Spec({
  entries,
  namedByHead,
  className,
}: {
  entries: [string, unknown][];
  namedByHead?: boolean;
  className?: string;
}): ReactElement {
  return (
    <div className={className === undefined ? "chat-spec-spec" : `chat-spec-spec ${className}`}>
      {entries.map(([key, value]) => {
        if (isRecordList(value)) {
          return (
            <Fragment key={key}>
              {namedByHead ? null : <p className="chat-spec-blockk">{labelOf(key, value)}</p>}
              <Run items={value} />
            </Fragment>
          );
        }
        if (isRecord(value)) {
          const inner = Object.entries(value);
          if (namedByHead) {
            return (
              <Fragment key={key}>
                {inner.map(([innerKey, innerValue]) => (
                  <Field key={innerKey} name={innerKey} value={innerValue} raw />
                ))}
              </Fragment>
            );
          }
          return (
            <Fragment key={key}>
              <p className="chat-spec-blockk">{labelOf(key, value)}</p>
              <Run items={[value]} ordinals={false} raw />
            </Fragment>
          );
        }
        return <Field key={key} name={key} value={value} />;
      })}
    </div>
  );
}
function grouped(output: Record<string, unknown>, map: readonly Section[] | null): [string, string[]][] {
  if (map === null) return [["", Object.keys(output)]];
  const groups: [string, string[]][] = [];
  const seen = new Set<string>();
  for (const [name, keys] of map) {
    const present = keys.filter((key) => key in output);
    for (const key of present) seen.add(key);
    if (present.length > 0) groups.push([name, present]);
  }
  const rest = Object.keys(output).filter((key) => !seen.has(key));
  if (rest.length > 0) groups.push(["Other", rest]);
  return groups;
}
type Shape = "collection" | "record" | "ack" | "failure";
interface SpecView {
  shape: Shape;
  output: Record<string, unknown> | null;
  map: readonly Section[] | null;
  listKey: string;
  items: Record<string, unknown>[];
  fields: number;
  sections: number;
  truncated: boolean;
  requested: string;
  /** The ask's reported outcome, when the payload carries one. */
  outcome: boolean | null;
  subject: string;
  errorText: string;
}
/** What the head names: the resource the payload identifies, else the single
 *  argument the call was made with, else the tool. */
const ID_KEYS = ["identifier", "full_name", "urn", "arn", "name", "id"];
function subjectOf(part: ToolConversationPart, output: Record<string, unknown> | null): string {
  for (const key of ID_KEYS) {
    const value = output?.[key];
    if (typeof value === "string" && value !== "") return value;
  }
  const args = Object.entries(part.input ?? {});
  if (args.length === 1) return `${labelOf(args[0][0], args[0][1])} ${scalarText(args[0][1])}`;
  return part.name;
}
function derive(part: ToolConversationPart): SpecView {
  const output = readResult(part.output);
  const errorText = str(part.errorText);
  const map = sectionsFor(part.name);
  const keys = output === null ? [] : Object.keys(output);
  const listKey = output === null ? "" : (keys.find((key) => isRecordList(output[key])) ?? "");
  const items = listKey === "" || output === null ? [] : (output[listKey] as Record<string, unknown>[]);
  const requested = output === null ? "" : str(output.requested);
  const outcomeKey = output === null ? undefined : keys.find((key) => typeof output[key] === "boolean");
  // A stopped call ended without a result; that is not the tool failing, and
  // the group states the stop itself.
  const stopped = part.state === "stopped";
  const failed = !stopped && (part.state === "error" || errorText !== "" || output === null);
  // A collection is one run of records that scalars only qualify; a record with
  // a list riding along keeps its sections.
  const collection = listKey !== "" && keys.length - 1 <= 2;
  return {
    shape: failed ? "failure" : requested !== "" ? "ack" : collection ? "collection" : "record",
    output,
    map,
    listKey,
    items,
    fields: keys.filter((key) => key !== listKey).length,
    sections: output === null ? 0 : grouped(output, map).length,
    truncated: output?.truncated === true,
    requested,
    outcome: outcomeKey === undefined || output === null ? null : (output[outcomeKey] as boolean),
    subject: subjectOf(part, output),
    errorText,
  };
}
/** A vendor key as its own plural and singular, so a figure agrees with its
 *  count without a lexicon. */
function words(key: string): { one: string; many: string } {
  const many = key.replace(/_/g, " ");
  const one = /ies$/.test(many) ? many.replace(/ies$/, "y") : many.replace(/s$/, "");
  return { one, many };
}
function figureOf(view: SpecView): StepFigure | undefined {
  if (view.shape === "failure") return { kind: "count", text: "Failed", tone: "fail" };
  if (view.shape === "ack") {
    if (view.outcome === false) return { kind: "count", text: "Did not take", tone: "fail" };
    if (view.outcome === true) return { kind: "count", text: "Done", tone: "pass" };
    return undefined;
  }
  const listed = words(view.listKey);
  if (view.shape === "collection") {
    const truncated = view.truncated ? ", truncated" : "";
    return { kind: "count", text: `${count(view.items.length, listed.one, listed.many)}${truncated}` };
  }
  const tail =
    view.listKey !== ""
      ? `, ${count(view.items.length, listed.one, listed.many)}`
      : view.sections > 1
        ? `, ${count(view.sections, "section", "sections")}`
        : "";
  return { kind: "count", text: `${count(view.fields, "field", "fields")}${tail}` };
}
function verbOf(view: SpecView): string {
  if (view.shape === "ack") return `Requested ${view.requested}`;
  if (view.shape === "failure") return "Read";
  return view.shape === "collection" ? "Listed" : "Described";
}
const DISCLOSURE: Record<Shape, string> = {
  collection: "result",
  record: "spec",
  ack: "receipt",
  failure: "cause",
};
/** A harness reason is stamped on the step and rendered by the well, so the only
 *  failure this body owns is the one the harness never named. */
function Failure({ text }: { text: string }): ReactElement | null {
  return text === "" ? <FailLine>The call returned no result.</FailLine> : null;
}
function Output({ view }: { view: SpecView }): ReactElement | null {
  const { output } = view;
  if (output === null) return null;
  if (view.shape === "collection") {
    const tail = Object.entries(output).filter(([key]) => key !== view.listKey);
    return (
      <div className="chat-spec-out">
        <Run items={view.items} />
        {tail.length > 0 ? <Spec entries={tail} className="chat-spec-tail" /> : null}
      </div>
    );
  }
  return (
    <div className="chat-spec-out">
      {grouped(output, view.map).map(([name, keys]) => (
        <section key={name} className="chat-spec-sec">
          {name === "" ? null : <p className="chat-spec-sec__h">{name}</p>}
          <Spec entries={keys.map((key) => [key, output[key]] as [string, unknown])} namedByHead={keys.length === 1} />
        </section>
      ))}
    </div>
  );
}
/** The well's interior: the call's own arguments as a specification too, then
 *  the result as one. It paints no ground, edge, radius, or outer pad; the
 *  group's well owns those, the failure tint included. */
function Body({ part, view }: { part: ToolConversationPart; view: SpecView }): ReactElement {
  const args = Object.entries(part.input ?? {});
  return (
    <div data-tool="spec">
      {args.length > 0 ? (
        <div className="chat-tool-band chat-spec-band">
          {args.map(([key, value]) => (
            <span key={key} className="chat-spec-arg">
              <span className="chat-spec-arg__k chat-tool-quiet chat-tool-pin">{labelOf(key, value)}</span>
              <span className="chat-spec-arg__v">
                <Scalar name={key} value={value} />
              </span>
            </span>
          ))}
        </div>
      ) : null}
      {view.shape === "failure" ? <Failure text={view.errorText} /> : <Output view={view} />}
    </div>
  );
}
function vendorOf(name: string): string {
  const head = name.split(".")[0];
  return head === name ? "" : labelOf(head);
}
/** The step this tool contributes to the transcript's tool group. Every word of
 *  it is read off the payload: what the call did, what the sheet holds, and
 *  whose service answered. */
export const head: CardHead = (part) => {
  const view = derive(part);
  const stopped = part.state === "stopped";
  const vendor = vendorOf(part.name);
  const mark = vendorId(part.name);
  return {
    // A record is the sheet's own voice, held in its registry row; the other
    // three shapes depart from it.
    // A stopped call described nothing: it reads as the call it was.
    verb: stopped ? "Called" : view.shape === "record" ? undefined : verbOf(view),
    object: view.subject,
    data: figureOf(view),
    // A card for a third-party service wears that service's own mark. The
    // parent-hosted tools have no vendor and keep the row's bench glyph.
    glyph: mark === null ? undefined : <ConnectorMark id={mark} size={16} />,
    disclosure: stopped ? "arguments" : view.shape === "record" ? undefined : DISCLOSURE[view.shape],
    loneSummary: vendor === "" ? undefined : `Ran 1 ${vendor} call`,
    // A receipt has no interior worth opening: its whole outcome is in the head.
    body: view.shape === "ack" ? undefined : <Body part={part} view={view} />,
  };
};
