import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { ConnectorMark, connectorBrand } from "./ConnectorMark";
import { CONNECTOR_BRANDS } from "../../assets/icons/connectors";

afterEach(cleanup);

// Pins the contracts a refactor can break silently. A catalog id missing from the registry
// wears the generic plug, which is how hex, sigma, fivetran, and then looker shipped
// logo-less. Repainted artwork and Illustrator's document-global <style> classes or
// duplicate ids are the failure modes of the build-time SVG pipeline. Composition itself
// is judged by eye.
// Derived from the registry's own keys, so a mark added or dropped there is proven here with
// no edit.
const BRANDED = Object.keys(CONNECTOR_BRANDS);

it.each(BRANDED)("%s renders the vendor's drawing, plus a dark one only when declared", (id) => {
  const { container } = render(<ConnectorMark id={id} />);
  const mark = container.querySelector(`[data-connector="${id}"]`);
  expect(mark).toBeTruthy();
  expect(mark).not.toHaveAttribute("data-fallback");
  expect(connectorBrand(id)?.title).toBeTruthy();

  // svgr prefixes every id with its file stem, so a drawing comparison strips that and the class.
  const svgs = [...mark!.querySelectorAll("svg")];
  const drawings = svgs.map((svg) =>
    svg.outerHTML.replace(/ class="[^"]*"/, "").replace(/[\w-]+_svg__/g, ""),
  );
  expect(svgs.map((svg) => svg.getAttribute("class"))).toEqual(
    connectorBrand(id)!.Dark ? ["alk-connmark__light", "alk-connmark__dark"] : [null],
  );
  expect(new Set(drawings).size).toBe(drawings.length);
});

// Named one by one, not derived: the registry-keyed case above passes just as
// happily with a connector missing from it. These are the ids a catalog serves
// that must never fall back.
it.each(["tinybird", "planetscale", "databricks", "snowflake"])(
  "%s is a registered brand, so the catalog never serves it the plug",
  (id) => {
    const { container } = render(<ConnectorMark id={id} />);
    const mark = container.querySelector(`[data-connector="${id}"]`);
    expect(mark).toBeTruthy();
    expect(mark).not.toHaveAttribute("data-fallback");
    expect(mark!.querySelector("svg")).toBeTruthy();
    expect(connectorBrand(id)?.title).toBeTruthy();
  },
);

it("an unregistered id wears the plug fallback instead of a wrong brand", () => {
  for (const [i, id] of ["generic_sql", "some-future-plugin"].entries()) {
    const { container } = render(<ConnectorMark id={id} className={i ? "caller" : undefined} />);
    const plug = container.querySelector(`svg[data-connector="${id}"]`);
    expect(plug).toHaveAttribute("data-fallback");
    // The class sets the plug's size in a flex row. No entry, no dark file, so no scheme class.
    expect(plug!.getAttribute("class")).toBe(i ? "alk-connmark caller" : "alk-connmark");
    expect(connectorBrand(id)).toBeUndefined();
  }
});

it("each scheme gets the vendor's own file, near-black duck in light and yellow-bodied in dark", () => {
  // A currentColor substitution once turned the duck theme-ink. The dark variant must be
  // the vendor's separate file, #fff100 body and no #1a1a1a.
  const { container } = render(<ConnectorMark id="duckdb_local" />);
  const [light, dark] = container.querySelectorAll("[data-connector] svg");
  expect(light.outerHTML).toContain("#1a1a1a");
  expect(dark.outerHTML).toContain("#fff100");
  expect(dark.outerHTML).not.toContain("#1a1a1a");
  expect(light.outerHTML).not.toContain("currentColor");
  expect(dark.outerHTML).not.toContain("currentColor");
});

it("marks stay inert side by side, no <style> blocks and no duplicate ids", () => {
  const { container } = render(
    <>
      {BRANDED.map((id) => (
        <ConnectorMark key={id} id={id} />
      ))}
    </>,
  );
  expect(container.querySelector("style")).toBeNull();
  const ids = [...container.querySelectorAll("[id]")].map((n) => n.getAttribute("id"));
  expect(new Set(ids).size).toBe(ids.length);
});
