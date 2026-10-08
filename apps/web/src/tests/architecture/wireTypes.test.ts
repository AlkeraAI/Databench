// A wire shape the backend publishes comes from the generated SDK
// (`components["schemas"]` in packages/ts-sdk/src/schema.d.ts). An exported type
// in src/api that reuses a schema's name but is not a plain alias of it is a hand
// copy that drifts silently when the server changes.

import { describe, expect, it } from "vitest";

import { EXTRA_ALLOWLIST, productSources, readSource, schemaNames, shadowedSchemaTypes } from "./scan";

const SCHEMAS = schemaNames(readSource("packages/ts-sdk/src/schema.d.ts"));

// Today's hand-typed copies. A file may only lose entries.
const SHADOWS_ALLOWED: Record<string, string[]> = {
  "apps/web/src/api/chats.ts": ["ChatWorkspaceRead"],
  "apps/web/src/api/cloudChat/transport.ts": ["ChatSpareState"],
  ...EXTRA_ALLOWLIST.schemaShadows,
};

describe("schema shadow scanner", () => {
  const names = new Set(["UserRead", "TeamRead", "OrgRead"]);

  it("reports a hand-typed shape and an extended alias", () => {
    const source = [
      "export interface UserRead { id: string }",
      'export type TeamRead = components["schemas"]["TeamRead"] & { extra: true };',
    ].join("\n");
    expect(shadowedSchemaTypes(source, names)).toEqual(["TeamRead", "UserRead"]);
  });

  it("allows a plain alias of the same schema", () => {
    const source = [
      'export type UserRead = components["schemas"]["UserRead"];',
      'export type OrgRead = Schemas["OrgRead"];',
      "export interface Unrelated { id: string }",
    ].join("\n");
    expect(shadowedSchemaTypes(source, names)).toEqual([]);
  });

  it("reads the schema names out of the generated SDK", () => {
    expect(SCHEMAS.has("UserRead")).toBe(true);
    expect(SCHEMAS.size).toBeGreaterThan(100);
  });
});

describe("no hand-typed wire shape in src/api", () => {
  it("only today's copies remain", () => {
    const found: Record<string, string[]> = {};
    for (const file of productSources("apps/web/src/api")) {
      const shadows = shadowedSchemaTypes(readSource(file), SCHEMAS);
      if (shadows.length > 0) found[file] = shadows;
    }
    expect(found).toEqual(SHADOWS_ALLOWED);
  });
});
