// A failure of the trip never reaches a reader in the browser's words. A new
// chat that could not reach the server read "Couldn't start a new
// conversation — Failed to fetch"; it now says what happened and that the
// message is still in the box, and every surface that turns a failure into
// copy through `errorText` gets the same sentences.
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import { createErrorTurn } from "@/pages/workspace/chat/controller";
import { errorText } from "@/pages/workspace/chat/data";

const abort = (): Error => {
  const err = new Error("The user aborted a request.");
  err.name = "AbortError";
  return err;
};

describe("errorText", () => {
  it.each([
    ["a fetch that never reached the server", new TypeError("Failed to fetch"), "Couldn't reach the server."],
    ["Firefox's spelling", new TypeError("NetworkError when attempting to fetch resource."), "Couldn't reach the server."],
    ["Safari's spelling", new TypeError("Load failed"), "Couldn't reach the server."],
    ["an aborted request", abort(), "The request was cancelled before an answer came back."],
    ["a 502", new ApiError(502, null), "The server couldn't finish the request."],
    ["a 500 with a body", new ApiError(500, { detail: "Internal Server Error" }), "The server couldn't finish the request."],
  ])("says %s in a sentence", (_what, err, says) => {
    expect(errorText(err)).toBe(says);
  });

  it.each([
    ["a refusal with its own reason", new ApiError(409, { detail: "The chat is being archived." }), "The chat is being archived."],
    ["a daemon rejection", { message: "Open a workspace folder for Alkera to work in.", code: -32000 }, "Open a workspace folder for Alkera to work in."],
    ["a 5xx that names its own code", new ApiError(500, { error: { code: "internal_error", message: "The chat's box is unreachable." } }), "The chat's box is unreachable."],
    ["a type error that is not the network", new TypeError("x is not a function"), "x is not a function"],
  ])("keeps %s in its own words", (_what, err, says) => {
    expect(errorText(err)).toBe(says);
  });
});

describe("an API refusal the server explained no further", () => {
  // The message such an error carries is this client's fallback, written for a
  // developer: it names the route (and the chat's id in it) and the status.
  const developer = "the request to /api/v1/chats/0000000a-0000-4000-8000-00000000c4a7/model failed";

  it.each([
    ["a 429", 429, "The server is limiting how often it will answer."],
    ["a 404", 404, "The request didn't go through."],
    ["a 422", 422, "The request didn't go through."],
  ])("says %s in a sentence, never the route or the status", (_what, status, says) => {
    const text = errorText(new ApiError(status, null, developer));
    expect(text).toBe(says);
    expect(text).not.toMatch(/\/api\/|\(\d{3}\)/);
  });
});

describe("a new chat that could not reach the server", () => {
  it("says so, and that the message is still in the box", () => {
    const part = createErrorTurn(new TypeError("Failed to fetch")).parts[0];
    if (part.kind !== "system") throw new Error("a create failure no longer reads as a system line");
    expect(part.text).toBe("Couldn't reach the server. Your message is still in the box.");
    expect(part.text).not.toContain("Failed to fetch");
  });
});
