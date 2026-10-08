import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { DescList, DescRow } from "./DescList";

// Tests for the @alkera/ui DescList / DescRow — the `fixed-label | value` definition list behind
// the knowledge drawer's provenance block.
//
// They assert the OBSERVABLE contract: it renders a real <dl> of <dt>/<dd> pairs (the semantic seam a
// screen reader's definition-list navigation reads), each term is paired with its value, arbitrary
// value nodes render, and a caller className survives the merge. The two-column grid needs a real
// browser; jsdom has no layout.

afterEach(cleanup);

describe("DescList", () => {
  it("renders a definition list of term/value pairs", () => {
    const { container } = render(
      <DescList>
        <DescRow label="Descent">Imported</DescRow>
        <DescRow label="Visibility">Shared</DescRow>
      </DescList>,
    );
    const dl = container.querySelector("dl");
    expect(dl).not.toBeNull();
    const terms = container.querySelectorAll("dt");
    const defs = container.querySelectorAll("dd");
    expect(terms).toHaveLength(2);
    expect(defs).toHaveLength(2);
    expect(terms[0]).toHaveTextContent("Descent");
    expect(defs[0]).toHaveTextContent("Imported");
    expect(terms[1]).toHaveTextContent("Visibility");
    expect(defs[1]).toHaveTextContent("Shared");
  });

  it("merges a caller className onto the <dl>", () => {
    const { container } = render(
      <DescList className="page-prov">
        <DescRow label="A">b</DescRow>
      </DescList>,
    );
    expect(container.querySelector("dl")).toHaveClass("alk-desclist", "page-prov");
  });
});
