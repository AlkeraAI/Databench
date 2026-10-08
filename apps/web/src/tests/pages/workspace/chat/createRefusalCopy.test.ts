// What a refused new chat says: plain copy for the refusals this build knows,
// the fallback for an API refusal it does not, never the server's own words.

import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import { CREATE_REFUSALS, createErrorTurn } from "@/pages/workspace/chat/controller/useChatTranscript";

const textOf = (err: unknown): string => {
  const part = createErrorTurn(err).parts[0] as { text: string };
  return part.text;
};

describe("a refused new chat", () => {
  it("reads a leased folder as plain copy, not the server's sentence", () => {
    const text = textOf(new ApiError(409, { code: "files.leased", message: "the folder is leased" }));
    expect(text).toBe(CREATE_REFUSALS["files.leased"]);
    expect(text).not.toMatch(/the folder is leased/);
  });

  it.each(["workspace_holds_one_chat", "workspaces_multi_chat_disabled"])("has copy for %s", (code) => {
    expect(textOf(new ApiError(409, { detail: { code, message: "raw words" } }))).toBe(CREATE_REFUSALS[code]);
  });

  it("falls back for an API refusal it has no copy for, rather than show the server's words", () => {
    const text = textOf(new ApiError(409, { code: "files.something_new", message: "operator words" }));
    expect(text).toBe("The chat could not be started. Try again in a moment.");
  });

  it("keeps a sentence written for the reader that carries no code", () => {
    expect(textOf({ message: "Open a workspace folder for Alkera to work in." })).toBe(
      "Couldn't start a new conversation: Open a workspace folder for Alkera to work in.",
    );
  });
});
