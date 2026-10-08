// Delimited text in a chart spec. Vega reads CSV, TSV and other delimited
// text with a parser that compiles a row function at run time, which no
// Alkera surface allows (no `eval` under any of its policies). A spec that
// asks for it is refused with an output error before Vega is run; data
// arrives as JSON records instead (`alkera.chart` converts CSV in Python).

const DELIMITED = new Set(["csv", "tsv", "dsv"]);

/** The delimited format a spec asks Vega to parse, at any depth, or null. */
export function delimitedFormat(spec: unknown, depth = 0): string | null {
  if (depth > 64 || typeof spec !== "object" || spec === null) return null;
  if (Array.isArray(spec)) {
    for (const item of spec) {
      const found = delimitedFormat(item, depth + 1);
      if (found) return found;
    }
    return null;
  }
  const record = spec as Record<string, unknown>;
  const format = record.format;
  if (typeof format === "object" && format !== null && !Array.isArray(format)) {
    const type = (format as Record<string, unknown>).type;
    if (typeof type === "string" && DELIMITED.has(type.toLowerCase())) return type.toLowerCase();
  }
  for (const [key, value] of Object.entries(record)) {
    // Inline rows are data, not spec: a column may be called "format".
    if (key === "values") continue;
    const found = delimitedFormat(value, depth + 1);
    if (found) return found;
  }
  return null;
}

/** What the reader is told when a spec carries delimited text. */
export const DELIMITED_REFUSAL = "This chart reads CSV or TSV text, which charts cannot parse here. Pass the data as records instead.";
