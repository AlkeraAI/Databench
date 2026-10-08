// A failed request is told to a person through `refusalSentence` (api/errors.ts): the
// caller's copy, the sentence the server wrote for a refusal it explained, or the generic
// sentence. Reading `error.message` straight onto the screen shows whatever the error
// carries, which for an `ApiError` the server did not explain is the client's own
// diagnostic ("could not join the organization (500)"). Today's raw reads are counted
// here; the count may only shrink.

import { describe, expect, it } from "vitest";

import { EXTRA_ALLOWLIST, productSources, readSource } from "./scan";

const SCANNED_DIRS = ["apps/web/src/pages", "apps/web/src/app", ...(EXTRA_ALLOWLIST.rawErrorMessageDirs ?? [])];

/** The raw reads left today. Lower it when one is converted; never raise it. */
const RAW_ERROR_MESSAGE_CEILING = 2 + (EXTRA_ALLOWLIST.rawErrorMessageCeiling ?? 0);

/** How many times `source` reads an error's own `message` in code (comment lines skipped). */
function rawErrorMessages(source: string): number {
  return source
    .split("\n")
    .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line))
    .reduce((n, line) => n + (line.match(/\berror\.message\b/g)?.length ?? 0), 0);
}

describe("raw error message scanner", () => {
  it.each([
    ["a JSX child", "<Callout>{memberships.error.message}</Callout>", 1],
    ["a prop", "details={switchOrg.isError ? switchOrg.error.message : undefined}", 1],
    ["a bare error", 'return error.message || "That did not go through.";', 1],
    ["two on one line", "a ? leave.error.message : switchOrg.error.message", 2],
    ["the sentence helper", "<Callout>{refusalSentence(memberships.error)}</Callout>", 0],
    ["a field named like it", "errorMessage: firstError.errorMessage", 0],
    ["a comment", " * raw `error.message` is not evidence that the server said anything", 0],
    ["a line comment", "// error.message is the client's diagnostic", 0],
  ])("counts %s", (_why, source, expected) => {
    expect(rawErrorMessages(source)).toBe(expected);
  });
});

describe("raw error messages under pages and app", () => {
  const found = SCANNED_DIRS.flatMap(productSources).map((file) => ({
    file,
    count: rawErrorMessages(readSource(file)),
  }));
  const total = found.reduce((n, f) => n + f.count, 0);

  it("scans the product tree", () => {
    expect(found.length).toBeGreaterThan(100);
  });

  it("adds no new raw read: say a failure through refusalSentence", () => {
    const where = found.filter((f) => f.count > 0).map((f) => `${f.file}: ${f.count}`);
    expect(total, `raw error.message reads:\n${where.join("\n")}`).toBeLessThanOrEqual(RAW_ERROR_MESSAGE_CEILING);
  });

  it("records the ceiling at today's count, so a conversion lowers it", () => {
    expect(total, "lower RAW_ERROR_MESSAGE_CEILING to the new count").toBe(RAW_ERROR_MESSAGE_CEILING);
  });
});
