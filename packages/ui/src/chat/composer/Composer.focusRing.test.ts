// The composer field turns its own outline off and paints a mode-hued halo on the unit around it.
// That halo fires on plain `:focus`, so it is the same for a click as for a Tab, and it is not the
// ring the rest of the product uses — a keyboard reader arriving at the composer gets no indicator
// they recognise. The shared focus ring rides alongside it, on `:focus-visible` only.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const CSS = readFileSync(join(HERE, "composer.css"), "utf8");

describe("the composer's keyboard focus ring", () => {
  it("wears the shared focus ring when the field is reached by keyboard", () => {
    const at = CSS.indexOf(".chat-composer-unit:has(.chat-composer-field:focus-visible)");
    expect(at).toBeGreaterThan(-1);
    const block = CSS.slice(at, CSS.indexOf("}", at));
    expect(block).toMatch(/outline:\s*2px solid var\(--alkFocusRing\)/);
  });
});
