// The addresses the chat area answers on, and the backend it reads on mount.
// Shared by the route tests that mount `<ChatUnderTest>`.

import { vi } from "vitest";

/** A chat id the routes accept — they reject anything that is not one. */
export const CHAT_ID = "6f1a1c2e-6d41-4e2a-9a77-2b1f0c9d4e55";
export const PART_ID = "part-1";

/** Every address the chat area answers on, cold. */
export const CHAT_PATHS = [
  "/chat",
  `/chat/${CHAT_ID}`,
  `/chat/${CHAT_ID}/compaction/${PART_ID}`,
  `/chat/${CHAT_ID}/results`,
  `/chat/${CHAT_ID}/plan/${PART_ID}`,
] as const;

/** The backend the chat area reads on mount, answered well enough that every
 *  surface gets to its own chrome. */
export function scriptChatFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      const at = new URL(url, "http://x").pathname;
      const json = (body: unknown): Response =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (at.includes("/auth/me")) return json({ id: "u1", email: "dana@example.com" });
      if (at.includes("/machines/current")) {
        return json({ machine_id: "m1", status: "ready", name: "box-1", reason: null });
      }
      if (at.endsWith("/messages")) return json({ items: [], next_cursor: null });
      if (/^\/api\/v1\/chats\/[^/]+$/.test(at)) {
        return json({
          id: CHAT_ID,
          title: "A long one",
          owner_user_id: "u1",
          machine_id: "m1",
          machine_status: "ready",
          machine_refusal_reason: null,
          permission_mode: "default",
          last_seq: 0,
          created_at: "2026-09-06T12:00:00Z",
          updated_at: "2026-09-06T12:00:00Z",
        });
      }
      if (at.startsWith("/api/v1/chats")) return json({ items: [], next_cursor: null });
      return json({});
    }),
  );
}
