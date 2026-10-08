// Fail closed: a `can_*` field the server did not send is not a grant. Reading
// a missing answer as a yes offers a control the server then refuses, so every
// read goes through `granted()` in `src/lib/capabilities.ts`.

import { describe, expect, it } from "vitest";

import { countPerFile, failOpenCapabilities, productSources } from "./scan";

const SCANNED = [...productSources("apps/web/src"), ...productSources("packages/ui/src")];

// Today's fail-open reads. A file may only lose entries.
const FAIL_OPEN_ALLOWED: Record<string, number> = {
  // The template read carries no `can_*` of its own, so the brief editor reads
  // the Files node's `can_write`, which is absent when Files is off. Closing it
  // needs the template read to say whether the reader may edit it.
  "apps/web/src/pages/workspace/templates/ChatTemplatePage.tsx": 1,
};

describe("fail-open capability scanner", () => {
  it("finds a missing answer read as a yes, in each spelling", () => {
    const source = [
      "const a = row.can_delete !== false;",
      "const b = false !== chat.data?.can_send;",
      "const c = raw.can_switch ?? true;",
      "const d = row.can_share != false;",
    ].join("\n");
    expect(failOpenCapabilities(source)).toBe(4);
  });

  it("leaves a fail-closed read alone", () => {
    const source = [
      "const a = row.can_delete === true;",
      "const b = granted(chat.data?.can_send);",
      "const c = raw.can_switch ?? false;",
      "const d = row.can_share !== true;",
    ].join("\n");
    expect(failOpenCapabilities(source)).toBe(0);
  });
});

describe("no capability read fails open in the portal or the shared UI", () => {
  it("only today's files, at today's counts", () => {
    expect(countPerFile(SCANNED, failOpenCapabilities)).toEqual(FAIL_OPEN_ALLOWED);
  });
});
