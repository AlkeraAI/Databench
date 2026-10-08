// One key per read. Every surface asking the daemon the same question must land
// in the same cache entry, or a delete refreshes half the screen; and a refresh
// aimed at one read must not sweep another's. Both halves run through a real
// query client, because the cache's own matcher is what decides them.

import { describe, expect, it } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { activityKeys, chatKeys } from "./chatKeys";

/** Every read the chat family makes. Each is seeded with its own key path, so
 *  a value read back through a SECOND call of the builder proves both that the
 *  builder is stable and that it did not land on a neighbor's entry. */
const READS = [
  () => chatKeys.chats(),
  () => chatKeys.models(),
  () => chatKeys.chatDefaults(),
  () => chatKeys.commands(),
  () => chatKeys.turns("c1"),
  () => chatKeys.modelOptions("c1"),
  () => activityKeys.decisions("c1"),
  () => activityKeys.safety("c1"),
  () => activityKeys.cost("c1"),
  () => activityKeys.ledger("c1"),
];

const seedOf = (key: readonly unknown[]): string => key.join("/");

describe("chat cache keys", () => {
  it("omits the retired preferences read while keeping key builders stable", () => {
    expect(Object.keys(chatKeys).sort()).toEqual([
      "chatDefaults",
      "chats",
      "commands",
      "modelOptions",
      "models",
      "turns",
    ]);

    const client = createQueryClient();
    for (const read of READS) client.setQueryData(read(), seedOf(read()));

    for (const read of READS) expect(client.getQueryData(read())).toBe(seedOf(read()));
  });

  it("refreshing the whole workspace namespace reaches every chat read", () => {
    const client = createQueryClient();
    for (const read of READS) client.setQueryData(read(), seedOf(read()));

    void client.invalidateQueries({ queryKey: ["ide"] });

    for (const read of READS) expect(client.getQueryState(read())?.isInvalidated).toBe(true);
  });

  it("refreshing the chats a surface lists leaves a chat's activity page alone", () => {
    const client = createQueryClient();
    for (const read of READS) client.setQueryData(read(), seedOf(read()));

    void client.invalidateQueries({ queryKey: chatKeys.chats() });

    expect(client.getQueryState(chatKeys.chats())?.isInvalidated).toBe(true);
    expect(client.getQueryState(activityKeys.ledger("c1"))?.isInvalidated).toBe(false);
    expect(client.getQueryState(activityKeys.cost("c1"))?.isInvalidated).toBe(false);
    expect(client.getQueryState(chatKeys.turns("c1"))?.isInvalidated).toBe(false);
  });

  it("saving one chat's caps leaves another chat's ledger alone", () => {
    const client = createQueryClient();
    client.setQueryData(activityKeys.ledger("c1"), "mine");
    client.setQueryData(activityKeys.ledger("c2"), "a neighbor's");

    void client.invalidateQueries({ queryKey: activityKeys.ledger("c1") });

    expect(client.getQueryState(activityKeys.ledger("c1"))?.isInvalidated).toBe(true);
    expect(client.getQueryState(activityKeys.ledger("c2"))?.isInvalidated).toBe(false);
  });

  it.each([null, undefined, ""])(
    "a page holding no chat id (%p) keeps its own entry, never a real chat's",
    (chatId) => {
      const client = createQueryClient();
      client.setQueryData(chatKeys.turns("c1"), "a real chat's turns");
      client.setQueryData(chatKeys.turns(chatId), "nothing yet");

      expect(client.getQueryData(chatKeys.turns("c1"))).toBe("a real chat's turns");
      expect(client.getQueryData(chatKeys.turns(chatId))).toBe("nothing yet");
    },
  );

  it("each chat keeps its own transcript entry", () => {
    const client = createQueryClient();
    client.setQueryData(chatKeys.turns("c1"), "first");
    client.setQueryData(chatKeys.turns("c2"), "second");

    expect(client.getQueryData(chatKeys.turns("c1"))).toBe("first");
    expect(client.getQueryData(chatKeys.turns("c2"))).toBe("second");
  });
});
