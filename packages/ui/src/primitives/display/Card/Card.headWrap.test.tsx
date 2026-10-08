// A card header with a toggle and two buttons (the platform console's Machines card) did not wrap at
// 390 px: the actions row was fixed at its full width, so it pushed "Provision machine" past the
// clipped card edge and squeezed the title beside it to zero width. jsdom does not lay out, so the
// structure that decides the wrap is pinned through the real stylesheet's cascade on a rendered card.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { cleanup, render } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { Card } from "./Card";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(join(HERE, "card.css"), "utf8");

let sheet: HTMLStyleElement;
beforeAll(() => {
  sheet = document.createElement("style");
  sheet.textContent = CSS;
  document.head.append(sheet);
});
afterAll(() => sheet.remove());
afterEach(cleanup);

function renderConsoleHeader() {
  const { container } = render(
    <Card
      title="Machines"
      icon={<svg aria-hidden="true" />}
      actions={
        <>
          <label>
            <input type="checkbox" /> Show released and failed
          </label>
          <button type="button">Register a box</button>
          <button type="button">Provision machine</button>
        </>
      }
    >
      rows
    </Card>,
  );
  const part = (selector: string) => getComputedStyle(container.querySelector(selector)!);
  return { head: part(".alk-card__head"), id: part(".alk-card__id"), actions: part(".alk-card__actions") };
}

describe("a card header at phone width", () => {
  it("drops the actions to a line of their own rather than overflowing", () => {
    expect(renderConsoleHeader().head.flexWrap).toBe("wrap");
  });

  it("lets the actions row shrink to the header and wrap its own controls", () => {
    const { actions } = renderConsoleHeader();
    // A row that cannot shrink keeps its full width on its own line too, and the last button is
    // still clipped by the card.
    expect(actions.flexShrink).toBe("1");
    expect(actions.minWidth).toMatch(/^0(px)?$/);
    expect(actions.maxWidth).toBe("100%");
    expect(actions.flexWrap).toBe("wrap");
  });

  it("reserves the title a real width before the actions may share its line", () => {
    const { id } = renderConsoleHeader();
    // The wrap measures each item by its basis. A content-sized (auto) basis of min-width 0 is what
    // let the title collapse to nothing; a fixed basis forces the wrap before that can happen.
    expect(id.flexBasis).toBe("10rem");
    expect(id.flexGrow).toBe("1");
  });
});
