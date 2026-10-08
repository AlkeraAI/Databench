// The fallback: a tool with no card of its own still returns structure, so the
// card reads its JSON as a ledger of records instead of dumping it as one
// paragraph of braces.
//
// Every word on screen is a key or a value the payload carried. The head's
// figure is the one array the result is built around, named by its own key.
// Below, each item becomes a numbered record with its keys down the left and its
// values colored by type, so a reader can scan one field across every record.
// Nothing here is written for the card, which is what makes it safe to point at
// a tool nobody has designed for yet.
import type { ReactElement } from "react";
import type { ToolConversationPart } from "@alkera/chat-model";
import { isRecord } from "./alkeraPayload";
import { EmptyLine } from "./shared";
import type { CardHead } from "./step";
import "./shared.css";
import s from "./generic.module.css";
/** Nesting past this reads as a blob whatever the furniture, so it renders as
 *  compact JSON and stops pretending. */
const MAX_DEPTH = 3;
function isScalar(value: unknown): boolean {
  return value === null || typeof value !== "object";
}
function parsePayload(part: ToolConversationPart): unknown {
  const out = part.output ?? part.content;
  if (typeof out !== "string") return out ?? null;
  try {
    return JSON.parse(out);
  } catch {
    return out;
  }
}
function scalarText(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") return value;
  return String(value);
}
function scalarType(value: unknown): string {
  if (value === null) return "empty";
  return typeof value;
}
function Scalar({ value }: { value: unknown }): ReactElement {
  return (
    <span className={`${s.val} chat-tool-mono`} data-type={scalarType(value)}>
      {scalarText(value)}
    </span>
  );
}
function Field({ name, value, depth }: { name: string; value: unknown; depth: number }): ReactElement {
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return (
        <div className="chat-tool-row">
          <span className={`${s.key} chat-tool-mono`}>{name}</span>
          <span className={s.val} data-type="empty">
            none
          </span>
        </div>
      );
    }
    if (value.every(isScalar)) {
      return (
        <div className="chat-tool-row">
          <span className={`${s.key} chat-tool-mono`}>{name}</span>
          <span className={`${s.val} chat-tool-mono`} data-type="string">
            {value.map(scalarText).join(", ")}
          </span>
        </div>
      );
    }
    if (depth >= MAX_DEPTH) return <Compact name={name} value={value} />;
    return (
      <div className={s.block}>
        <p className="chat-tool-row">
          <span className={`${s.key} chat-tool-mono`}>{name}</span>
        </p>
        {value.map((item, i) => (
          <div key={i} className={s.rec}>
            <span className={s.recN}>{i + 1}</span>
            {isRecord(item) ? <Fields value={item} depth={depth + 1} /> : <Scalar value={item} />}
          </div>
        ))}
      </div>
    );
  }
  if (isRecord(value)) {
    if (depth >= MAX_DEPTH) return <Compact name={name} value={value} />;
    return (
      <div className={s.block}>
        <p className="chat-tool-row">
          <span className={`${s.key} chat-tool-mono`}>{name}</span>
        </p>
        <div className={s.rec}>
          <Fields value={value} depth={depth + 1} />
        </div>
      </div>
    );
  }
  return (
    <div className="chat-tool-row">
      <span className={`${s.key} chat-tool-mono`}>{name}</span>
      <Scalar value={value} />
    </div>
  );
}
function Compact({ name, value }: { name: string; value: unknown }): ReactElement {
  return (
    <div className="chat-tool-row">
      <span className={`${s.key} chat-tool-mono`}>{name}</span>
      <span className={`${s.val} chat-tool-mono`}>{JSON.stringify(value)}</span>
    </div>
  );
}
function Fields({ value, depth }: { value: Record<string, unknown>; depth: number }): ReactElement {
  return (
    <>
      {Object.entries(value).map(([key, item]) => (
        <Field key={key} name={key} value={item} depth={depth} />
      ))}
    </>
  );
}
/** The array a result is built around, if it has exactly one. */
function principalArray(payload: unknown): { key: string; items: unknown[] } | null {
  if (!isRecord(payload)) return null;
  const arrays = Object.entries(payload).filter(([, value]) => Array.isArray(value));
  if (arrays.length !== 1) return null;
  return { key: arrays[0][0], items: arrays[0][1] as unknown[] };
}
function figureOf(payload: unknown): string | null {
  const principal = principalArray(payload);
  if (principal) return `${principal.items.length} ${principal.key}`;
  if (Array.isArray(payload)) return `${payload.length} items`;
  if (!isRecord(payload)) return null;
  const fields = Object.keys(payload).length;
  return `${fields} ${fields === 1 ? "field" : "fields"}`;
}
/** Whether a call has arguments to show yet. Until the model finishes streaming
 *  them the input is absent or `{}`, and an empty brace pair is not a record a
 *  reader learns anything from. */
function hasArguments(part: ToolConversationPart): boolean {
  return isRecord(part.input) && Object.keys(part.input).length > 0;
}
/** Whether a result exists at all. A call in flight has not returned, and one
 *  that answered with nothing -- absent, null, or an empty container -- has no
 *  ledger to draw and no figure to count. Printed verbatim, `null` and `{}`
 *  read as an answer the call never gave. */
function hasResult(part: ToolConversationPart): boolean {
  const out = part.output ?? part.content;
  if (out === undefined || out === null || out === "") return false;
  const payload = parsePayload(part);
  if (payload === null) return false;
  if (Array.isArray(payload)) return payload.length > 0;
  if (isRecord(payload)) return Object.keys(payload).length > 0;
  return true;
}
/** The well's interior: the call's own arguments, then the result read as a
 *  ledger of records. The frame around it belongs to whoever renders the step,
 *  so nothing here paints a ground, an edge, or a radius. */
function Body({ part }: { part: ToolConversationPart }): ReactElement {
  const payload = parsePayload(part);
  const structured = isRecord(payload) || Array.isArray(payload);
  const answered = hasResult(part);
  return (
    <div data-tool="generic">
      {hasArguments(part) ? (
        <div className="chat-tool-band">
          <span className="chat-tool-band__text chat-tool-mono">{JSON.stringify(part.input)}</span>
        </div>
      ) : null}
      {answered ? (
        <div className={s.json} data-cap="280">
          {isRecord(payload) ? <Fields value={payload} depth={1} /> : null}
          {Array.isArray(payload)
            ? payload.map((item, i) => (
                <div key={i} className={s.rec}>
                  <span className={s.recN}>{i + 1}</span>
                  {isRecord(item) ? <Fields value={item} depth={2} /> : <Scalar value={item} />}
                </div>
              ))
            : null}
          {!structured ? <p className={`${s.plain} chat-tool-mono`}>{scalarText(payload)}</p> : null}
        </div>
      ) : null}
      {/* A call that finished with nothing to show says so. One still in flight
          says nothing, because its result has not happened yet; one that failed
          or was stopped has no result to be empty, and the group already names
          why beside the head. */}
      {!answered && part.state === "completed" ? <EmptyLine>This call returned nothing.</EmptyLine> : null}
    </div>
  );
}
/** The step this tool contributes to a transcript's tool group. A call that has
 *  not answered speaks in the present tense, carries no result figure, and --
 *  until its arguments land -- has no interior to open at all. */
export const head: CardHead = (part) => {
  const inFlight = part.state === "pending" || part.state === "running";
  const answered = hasResult(part);
  const figure = inFlight || !answered ? null : figureOf(parsePayload(part));
  // Only a completed call's empty result is something to show; any other call
  // with no arguments and no result has no interior to open.
  const empty = !hasArguments(part) && !answered && part.state !== "completed";
  return {
    object: part.name,
    verb: inFlight ? "Calling" : undefined,
    data: figure === null ? undefined : { kind: "count", text: figure },
    // A stopped call never produced a result, so what opens is its arguments.
    disclosure: part.state === "stopped" ? "arguments" : undefined,
    body: empty ? undefined : <Body part={part} />,
  };
};
