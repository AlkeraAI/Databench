// `application/json`: a collapsible tree. Each container is a disclosure button that
// toggles it (Enter and Space through the native button), with
// `aria-expanded` saying which way it is.

import { useState } from "react";

import type { OutputRenderer, OutputRendererProps } from "./types";

/** Children shown before a "Show more" button. */
export const JSON_CHILD_LIMIT = 100;

type Container = unknown[] | Record<string, unknown>;

function isContainer(value: unknown): value is Container {
  return typeof value === "object" && value !== null;
}

function summary(value: Container): string {
  if (Array.isArray(value)) return `Array(${value.length})`;
  const count = Object.keys(value).length;
  return `Object(${count} ${count === 1 ? "key" : "keys"})`;
}

function Scalar({ value }: { value: unknown }) {
  if (value === null) return <span className="nb-json-null">null</span>;
  switch (typeof value) {
    case "string":
      return <span className="nb-json-string">{JSON.stringify(value)}</span>;
    case "number":
    case "bigint":
      return <span className="nb-json-number">{String(value)}</span>;
    case "boolean":
      return <span className="nb-json-boolean">{String(value)}</span>;
    default:
      return <span className="nb-json-null">{String(value)}</span>;
  }
}

function JsonNode({ label, value, depth, openDepth }: { label: string | null; value: unknown; depth: number; openDepth: number }) {
  const [open, setOpen] = useState(depth < openDepth);
  const [limit, setLimit] = useState(JSON_CHILD_LIMIT);
  const key = label === null ? null : <span className="nb-json-key">{label}: </span>;
  if (!isContainer(value)) {
    return (
      <li className="nb-json-item">
        {key}
        <Scalar value={value} />
      </li>
    );
  }
  const entries: [string, unknown][] = Array.isArray(value)
    ? value.map((item, index) => [String(index), item])
    : Object.entries(value);
  return (
    <li className="nb-json-item">
      <button type="button" className="nb-json-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="nb-json-caret" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
        {key}
        <span className="nb-json-summary">{summary(value)}</span>
      </button>
      {open && entries.length > 0 && (
        <ul className="nb-json-children">
          {entries.slice(0, limit).map(([childKey, child]) => (
            <JsonNode key={childKey} label={childKey} value={child} depth={depth + 1} openDepth={openDepth} />
          ))}
          {entries.length > limit && (
            <li className="nb-json-item">
              <button type="button" className="nb-json-more" onClick={() => setLimit((n) => n + JSON_CHILD_LIMIT)}>
                Show {Math.min(JSON_CHILD_LIMIT, entries.length - limit)} more
              </button>
            </li>
          )}
        </ul>
      )}
    </li>
  );
}

export function JsonTree({ value, openDepth = 1 }: { value: unknown; openDepth?: number }) {
  return (
    <ul className="nb-output-json" aria-label="JSON value">
      <JsonNode label={null} value={value} depth={0} openDepth={openDepth} />
    </ul>
  );
}

function JsonOutput({ data }: OutputRendererProps) {
  return <JsonTree value={data} />;
}

export const jsonRenderer: OutputRenderer = {
  id: "alkera.json",
  mimes: ["application/json"],
  rank: 0,
  place: "app",
  Component: JsonOutput,
};
