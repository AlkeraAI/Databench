import { describe, expectTypeOf, it } from "vitest";

import type { ComposerSendMeta } from "./contracts";

// A contract is a type, so its proof is a type: `tsc` (the typecheck gate)
// refuses this file when the field goes, and vitest runs the assertions as a
// no-op on the way past.
describe("ComposerSendMeta", () => {
  it("carries the linked Files node ids as an optional list of strings", () => {
    expectTypeOf<ComposerSendMeta>().toHaveProperty("attachments");
    expectTypeOf<ComposerSendMeta["attachments"]>().toEqualTypeOf<string[] | undefined>();
    const meta: ComposerSendMeta = { mode: "default", model: "m", attachments: ["node-1"] };
    expectTypeOf(meta.attachments).toEqualTypeOf<string[] | undefined>();
  });

  it("stays sendable with nothing attached", () => {
    const meta: ComposerSendMeta = { mode: "default", model: "m" };
    expectTypeOf(meta).toMatchTypeOf<ComposerSendMeta>();
  });
});
