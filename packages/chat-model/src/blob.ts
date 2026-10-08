// Blob paging — the wire contract for tool-produced blobs (big tables, long text/JSON)
// fetched page-by-page from the daemon, plus the decoder for the JSON-safe non-finite
// float encoding the Python side emits. React-free: the renderers live in the UI layer;
// this is the data they consume.

/** One fetched page of a blob. `rows` pages by row, `text` by character. */
export interface BlobPage {
  kind: "rows" | "text";
  columns: string[];
  rows: unknown[][];
  text: string;
  offset: number;
  limit: number;
  total: number;
  returned: number;
  hasMore: boolean;
  nextOffset: number | null;
}

/** Fetch one page of a blob by handle. `limit` omitted ⇒ the daemon's per-kind
 *  default page size (rows page by row, text by character). Injected so the UI
 *  layer stays free of host/RPC coupling. */
export type BlobFetcher = (handle: string, offset: number, limit?: number) => Promise<BlobPage>;

// Standard JSON has no NaN / Infinity, so `alkera_core.json_safe.json_safe` wraps them
// as `{"$nonfinite": "nan" | "inf" | "-inf"}` before serializing (otherwise
// vscode-jsonrpc / JSON.parse reject the bare tokens). `decodeNonFinite` is the TS
// inverse of `json_restore`.

const NONFINITE_KEY = "$nonfinite";

function nonFiniteFromTag(tag: unknown): number | undefined {
  if (tag === "nan") return Number.NaN;
  if (tag === "inf") return Number.POSITIVE_INFINITY;
  if (tag === "-inf") return Number.NEGATIVE_INFINITY;
  return undefined;
}

/** Recursively replace `{"$nonfinite": …}` wrappers with NaN / ±Infinity.
 *  Leaves everything else untouched; returns a new structure for objects/arrays
 *  and the input by value for primitives. */
export function decodeNonFinite(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(decodeNonFinite);
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record);
    if (keys.length === 1 && keys[0] === NONFINITE_KEY) {
      const decoded = nonFiniteFromTag(record[NONFINITE_KEY]);
      if (decoded !== undefined) return decoded;
    }
    const out: Record<string, unknown> = {};
    for (const key of keys) out[key] = decodeNonFinite(record[key]);
    return out;
  }
  return value;
}

/** Render a decoded cell value as display text: NaN → "NaN", ±Infinity → the
 *  math symbols, null/undefined → "", objects → compact JSON, everything else
 *  via String(). */
export function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") {
    if (Number.isNaN(value)) return "NaN";
    if (value === Number.POSITIVE_INFINITY) return "∞";
    if (value === Number.NEGATIVE_INFINITY) return "−∞";
    return String(value);
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}
