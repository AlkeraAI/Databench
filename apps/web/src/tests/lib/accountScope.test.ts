// Where the person and org a remembered value is filed under come from.

import { describe, expect, it } from "vitest";

import { accountKey } from "@alkera/ui/storage";

import { chatAccountScope, isAccountKeyOf, userScope } from "@/lib/accountScope";

describe("userScope", () => {
  it("files under the user and the org the session is acting in", () => {
    expect(userScope({ id: "u1", org_team_id: "o1" })).toEqual({ userId: "u1", orgId: "o1" });
  });

  it("keys a user with no org apart from every real org", () => {
    expect(userScope({ id: "u1", org_team_id: null })).toEqual({ userId: "u1", orgId: "" });
  });

  it.each([null, undefined, { id: "", org_team_id: "o1" }])("names nobody before the user is known (%o)", (user) => {
    expect(userScope(user)).toBeNull();
  });
});

describe("chatAccountScope", () => {
  it("prefers the user id and org the browser names", () => {
    expect(chatAccountScope({ email: "dana@example.com", userId: "u1", orgId: "o1" })).toEqual({
      userId: "u1",
      orgId: "o1",
    });
  });

  it("keys a shell that names only the email by the email, with no org", () => {
    expect(chatAccountScope({ email: "dana@example.com" })).toEqual({ userId: "dana@example.com", orgId: "" });
    expect(chatAccountScope({ email: "dana@example.com", userId: null, orgId: null })).toEqual({
      userId: "dana@example.com",
      orgId: "",
    });
  });

  it.each([null, undefined, { email: null }, { email: null, userId: "", orgId: "o1" }])(
    "names nobody while the shell knows nobody (%o)",
    (account) => {
      expect(chatAccountScope(account)).toBeNull();
    },
  );
});

describe("isAccountKeyOf", () => {
  it("recognises a family's key for any person in any org", () => {
    expect(isAccountKeyOf(accountKey("u1", "o1", "chat.last"), "chat.last")).toBe(true);
    expect(isAccountKeyOf(accountKey("u2", "", "chat.last"), "chat.last")).toBe(true);
    expect(isAccountKeyOf(`${accountKey("u1", "o1", "crdt.unsent")}:doc:x`, "crdt.unsent")).toBe(true);
  });

  it("does not claim another family's key, a longer family, or an unrelated key", () => {
    expect(isAccountKeyOf(accountKey("u1", "o1", "chat.layout"), "chat.last")).toBe(false);
    expect(isAccountKeyOf(accountKey("u1", "o1", "chat.lastSeen"), "chat.last")).toBe(false);
    expect(isAccountKeyOf("alkera.size:rail", "chat.last")).toBe(false);
  });
});
