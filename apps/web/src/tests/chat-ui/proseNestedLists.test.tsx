// A chat reply's nested list renders inside its item: the sub-items indent
// under their parent and a numbered list under a sub-item keeps its numbers.
// The parser has carried nested lists for a while; the chat's own renderer had
// dropped them on the floor, so a reply lost every indented line.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Prose } from "@alkera/ui";

const NESTED = [
  "- **Ingest**",
  "  - pull the raw tables",
  "  - land them as parquet",
  "- **Transform**",
  "  - build the marts",
  "  - test the marts",
  "- **Serve**",
  "  - publish the dashboards",
  "  - Refresh the dashboards",
  "    1. warm the cache",
  "    2. flip the alias",
  "    3. tell the channel",
].join("\n");

/** An item's own words, without the text of the lists nested in it. */
function ownText(li: Element): string {
  const clone = li.cloneNode(true) as Element;
  clone.querySelectorAll("ul, ol").forEach((list) => list.remove());
  return (clone.textContent ?? "").trim();
}

describe("Prose renders a nested list inside its item", () => {
  it("keeps every sub-item under its parent and a numbered list under a sub-item", () => {
    const { container } = render(<Prose content={NESTED} />);
    const top = container.querySelector("ul");
    expect(top).not.toBeNull();
    const items = Array.from(top?.children ?? []);
    expect(items.map(ownText)).toEqual(["Ingest", "Transform", "Serve"]);

    const ingest = items[0].querySelector("ul");
    expect(Array.from(ingest?.children ?? []).map(ownText)).toEqual([
      "pull the raw tables",
      "land them as parquet",
    ]);

    const serve = items[2].querySelector("ul");
    const serveItems = Array.from(serve?.children ?? []);
    expect(serveItems.map(ownText)).toEqual(["publish the dashboards", "Refresh the dashboards"]);
    const ordered = serveItems[1].querySelector("ol");
    expect(Array.from(ordered?.children ?? []).map(ownText)).toEqual([
      "warm the cache",
      "flip the alias",
      "tell the channel",
    ]);

    // Nothing the model wrote is missing: 3 + 6 + 3 items.
    expect(container.querySelectorAll("li")).toHaveLength(12);
    // The parent's emphasis survives beside its nested list.
    expect(items[0].querySelector("strong")?.textContent).toBe("Ingest");
  });

  it("a flat list nests nothing", () => {
    const { container } = render(<Prose content={"- one\n- two"} />);
    expect(container.querySelectorAll("ul")).toHaveLength(1);
    expect(container.querySelectorAll("li")).toHaveLength(2);
  });
});
