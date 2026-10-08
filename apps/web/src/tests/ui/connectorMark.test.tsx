// A vendor mark imported with `?react` must reach React as a COMPONENT. When the
// suffix fell through to Vite's plain asset handling the import became a data-URL
// string, React took the URL for a tag name, and every tool step carrying a mark
// disappeared from the transcript with nothing logged. The mark renders inside a
// tool step's glyph, so a mark that throws takes the whole step with it.
//
// This lives in apps/web rather than packages/ui because the failure is a build
// question, not a component question: packages/ui's own suite stayed green on its
// own vitest while this app shipped strings. What is under test here is the
// resolution apps/web's plugin chain performs -- the same chain behind both of
// this app's builds, the browser portal and the webview.

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CONNECTOR_BRANDS, ConnectorMark, connectorBrand } from "@alkera/ui";

const IDS = Object.keys(CONNECTOR_BRANDS);

describe("connector marks resolve through the ?react importer", () => {
  it("finds the registry to test", () => {
    expect(IDS.length).toBeGreaterThan(1);
  });

  it.each(IDS)("%s mounts one svg component per declared file, never a URL string", (id) => {
    const brand = connectorBrand(id);
    expect(brand, `${id} is declared in the registry but absent from the barrel`).toBeDefined();
    // A string here is the regression itself: React would call it a tag name.
    expect(typeof brand!.Light).toBe("function");

    const { container } = render(<ConnectorMark id={id} />);
    const marks = container.querySelectorAll(`[data-connector="${id}"] svg`);
    expect(marks).toHaveLength(brand!.Dark === undefined ? 1 : 2);
    // Inlined vendor artwork, not a placeholder: svgo is configured to keep the
    // viewBox so a mark scales to whatever box renders it.
    for (const mark of marks) expect(mark.getAttribute("viewBox")).toBeTruthy();
  });
});
