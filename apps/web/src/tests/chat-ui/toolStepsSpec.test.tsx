// A spec sheet's value is a bare string. It arrives with no filename and no
// fence to name a grammar, so the card must not sniff one out of the text: a
// path sniffed as SQL paints its extension as a keyword, and a `//` in a bucket
// URI swallows the rest of the line as a comment. Only a value that opens as a
// statement gets painted.
//
// The subjects below are read off the shipped tokenizer, not invented: each one
// is a path that Prism's SQL grammar really does find a token in, so the pin
// fails the moment the sniffing comes back.

import { describe, expect, it } from "vitest";

import { renderBody, stepOf, toolPart } from "./_steps";

/** A record-shaped spec payload with one field under test. */
function cellOf(value: string): HTMLElement {
  const body = renderBody(
    stepOf(toolPart("databricks.get_table", { output: JSON.stringify({ storage_location: value }) })),
  );
  const cell = body.querySelector<HTMLElement>(".chat-spec-out");
  if (cell === null) throw new Error("the spec card rendered no output section");
  return cell;
}

const highlights = (cell: HTMLElement) => [...cell.querySelectorAll('[class*="chat-tok--"]')]; // same-author-ok: mechanical class-prefix rename (r- -> chat-)

describe("a spec sheet paints only what is a statement", () => {
  it("leaves a path unpainted and whole", () => {
    const paths = [
      // The extension is the classic false keyword.
      "dbt/models/staging/stg_orders.sql",
      // `//` opens a SQL line comment: everything after the scheme would vanish
      // into comment ink.
      "s3://alkera-lake/warehouse/orders/part-0001.parquet",
      // A directory name that happens to be a reserved word.
      "src/webview/data/fixtures.ts",
      // No token at all, but it must still skip the painter's wrapper.
      "/var/log/alkera/daemon.log",
    ];
    for (const path of paths) {
      const cell = cellOf(path);
      expect(cell.querySelector(".chat-spec-sql"), path).toBeNull();
      expect(highlights(cell), path).toEqual([]);
      expect(cell.textContent, path).toContain(path);
    }
  });

  it("still paints a value that really is a statement", () => {
    const cell = cellOf("select order_id, sum(net_revenue) from analytics.orders group by 1");
    expect(cell.querySelector(".chat-spec-sql")).not.toBeNull();
    expect(highlights(cell).length).toBeGreaterThan(0);
    expect(highlights(cell).map((span) => span.textContent)).toContain("select");
  });

  it("paints a call argument by the same rule", () => {
    const body = renderBody(
      stepOf(
        toolPart("databricks.get_table", {
          input: { path: "models/marts/core/fct_orders.sql", statement: "show tables in analytics" },
          output: JSON.stringify({ rows: 12 }),
        }),
      ),
    );
    const args = [...body.querySelectorAll<HTMLElement>(".chat-spec-arg")];
    const cellFor = (key: string) =>
      args.find((arg) => arg.querySelector(".chat-spec-arg__k")?.textContent?.toLowerCase().includes(key));
    const path = cellFor("path");
    const statement = cellFor("statement");
    expect(path).toBeDefined();
    expect(statement).toBeDefined();
    expect(highlights(path!)).toEqual([]);
    expect(highlights(statement!).length).toBeGreaterThan(0);
  });
});
