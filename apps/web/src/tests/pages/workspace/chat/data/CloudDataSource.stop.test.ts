// Stop, on the browser's path.
//
// The portal has no engine channel, so the stop goes out on the chat's own
// REST surface and is relayed to the box running it.

import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import type { DocHandle } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

function build(stopTurn: (chatId: string) => Promise<void>) {
  const rest = { stopTurn: vi.fn(stopTurn) };
  const source = new CloudDataSource({
    rest: rest as never,
    openDoc: () => ({ subscribe: () => () => {}, close: () => {} }) as unknown as DocHandle<never>,
    acquire: () => () => {},
    clientId: () => "client-1",
  });
  return { source, rest };
}

describe("CloudDataSource.cancelTurn", () => {
  it("posts the stop for the chat it was given and reports the turn stopped", async () => {
    const { source, rest } = build(async () => {});

    const outcome = await source.cancelTurn("c1");

    expect(rest.stopTurn).toHaveBeenCalledWith("c1");
    expect(outcome.stopped).toBe(true);
  });

  it("says the turn is still running when the stop did not reach the server", async () => {
    // A reader told "stopped" by a Stop that never landed would sit watching a
    // turn nobody ended — so a refused post is reported as what it is.
    const { source } = build(async () => {
      throw new Error("the request to /api/v1/chats/c1/stop failed");
    });

    const outcome = await source.cancelTurn("c1");

    expect(outcome.stopped).toBe(false);
    expect(outcome.reason).toMatch(/still running/i);
  });

  it.each([403, 429, 500])(
    "hands a %i back to the caller rather than calling it a stop that never landed",
    async (status) => {
      // The server answered, so the stop DID reach the workspace and was refused.
      // Folding that into the transport sentence tells the reader the opposite of
      // what happened, and buries the status the surface decides its copy from.
      const refusal = new ApiError(status, { error: { code: "forbidden", message: "No." } });
      const { source } = build(async () => {
        throw refusal;
      });

      await expect(source.cancelTurn("c1")).rejects.toBe(refusal);
    },
  );
});
