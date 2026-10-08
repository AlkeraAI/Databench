import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { BlobPage } from "@alkera/chat-model";

import { BlobTable } from "./BlobTable";
import { registerReferenceRenderer, resolveReferenceRenderer } from "./registry";

afterEach(cleanup);

function rowsPage(rows: unknown[][], columns: string[]): BlobPage {
  return {
    kind: "rows",
    columns,
    rows,
    text: "",
    offset: 0,
    limit: 50,
    total: rows.length,
    returned: rows.length,
    hasMore: false,
    nextOffset: null,
  };
}

function textPage(text: string): BlobPage {
  return {
    kind: "text",
    columns: [],
    rows: [],
    text,
    offset: 0,
    limit: 8000,
    total: text.length,
    returned: text.length,
    hasMore: false,
    nextOffset: null,
  };
}

describe("reference renderer registry", () => {
  it("resolves by declared refType, else page kind, else the text fallback", () => {
    expect(resolveReferenceRenderer("rows", "text").type).toBe("rows");
    expect(resolveReferenceRenderer(undefined, "rows").type).toBe("rows");
    expect(resolveReferenceRenderer(undefined, "text").type).toBe("text");
    // Unknown type + unknown kind → never a dead end.
    expect(resolveReferenceRenderer("totally-unknown", "also-unknown").type).toBe("text");
  });

  it("lets a new type register and win", () => {
    registerReferenceRenderer({
      type: "image",
      icon: null,
      describe: () => "an image",
      Preview: () => null,
      FullView: () => null,
    });
    expect(resolveReferenceRenderer("image", "rows").type).toBe("image");
  });

  it("describes a page by its size, singular-aware", () => {
    const rows = resolveReferenceRenderer(undefined, "rows");
    expect(rows.describe(rowsPage([[1]], ["only"]))).toBe("1 row × 1 col");
    expect(rows.describe({ ...rowsPage([[1, 2]], ["a", "b"]), total: 1200 })).toBe("1,200 rows × 2 cols");

    const text = resolveReferenceRenderer(undefined, "text");
    expect(text.describe(textPage("x"))).toBe("1 char");
    expect(text.describe(textPage("hello"))).toBe("5 chars");
  });

  it("text Preview truncates where FullView does not", () => {
    const body = "x".repeat(700);
    const text = resolveReferenceRenderer(undefined, "text");
    const { container: previewBox } = render(<>{text.Preview({ page: textPage(body) })}</>);
    expect(previewBox.querySelector(".alk-blobv-text")?.textContent).toHaveLength(600);
    const { container: fullBox } = render(<>{text.FullView({ page: textPage(body) })}</>);
    expect(fullBox.querySelector(".alk-blobv-text")?.textContent).toHaveLength(700);
  });

  it("a rows grid formats its non-finite cells", () => {
    const page = rowsPage(
      [[Number.NaN, "ok"], [Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY]],
      ["x", "y"],
    );
    render(<>{resolveReferenceRenderer(undefined, "rows").FullView({ page })}</>);
    expect(screen.getByText("NaN")).toBeInTheDocument();
    expect(screen.getByText("−∞")).toBeInTheDocument();
    expect(screen.getByText("∞")).toBeInTheDocument();
    expect(screen.getByText("ok")).toBeInTheDocument();
  });
});

describe("BlobTable", () => {

  it("bounds the preview to maxRows / maxCols with a +N affordance", () => {
    const rows = Array.from({ length: 100 }, (_, i) => [i, `row-${i}`]);
    const page = rowsPage(rows, ["id", "label"]);
    const { container } = render(<BlobTable page={page} maxRows={3} maxCols={1} />);
    // 3 body rows only.
    expect(container.querySelectorAll("tbody tr")).toHaveLength(3);
    // 1 shown column + the "+N more columns" affordance.
    expect(within(container).getByText("+1")).toBeInTheDocument();
  });
});
