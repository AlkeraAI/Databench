// A pending file tool has no input yet: the model is still streaming the
// arguments, and on the wire that phase is nearly the call's whole visible
// lifetime. The step must render head-only until the path lands -- an open
// well with nothing in it reads as a broken card. These fail if the guard is
// removed, because the body would exist for every state again.

import { describe, expect, it } from "vitest";

import { renderBody, stepOf, toolPart } from "./_steps";

// The second entry is the verb the head carries WHILE THE CALL IS STILL OUT:
// in the present, because nothing has been read or edited yet.
const FILE_TOOLS = [
  ["write", "Writing…", { filePath: "src/fib.py", content: "def fib(n):\n    return n" }],
  ["read", "Reading", { filePath: "src/fib.py" }],
  ["edit", "Editing", { filePath: "src/fib.py", oldString: "a", newString: "b" }],
] as const;

describe("file tool pending guard", () => {
  it("holds the body back until the path lands", () => {
    for (const [name, pendingVerb, input] of FILE_TOOLS) {
      const bare = stepOf(toolPart(name, { state: "pending" }));
      expect(bare.body, name).toBeUndefined();
      expect(bare.verb, name).toBe(pendingVerb);

      // Arguments are still streaming: an input object exists, the path does not.
      expect(stepOf(toolPart(name, { state: "pending", input: {} })).body, name).toBeUndefined();

      const landed = stepOf(toolPart(name, { state: "running", input: { ...input } }));
      expect(landed.body, name).toBeDefined();
      expect(renderBody(landed).querySelector(".chat-tool-band__text")?.textContent, name).toBe("src/fib.py");
    }
  });
});
