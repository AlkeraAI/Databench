// @vitest-environment node
//
// The client's protocol vocabulary is pinned against the committed OpenAPI document: the
// envelope kinds, doc types, op intents and ticket path the server publishes through
// `RealtimeProtocolDescriptor` and `WsTicketResponse`.
//
// The close codes cannot be pinned there. `RealtimeProtocolDescriptor.close_codes` is a
// free-form `dict[str, int]` in the schema, so a code the server grows leaves openapi.json
// byte-identical and a client table written out by hand goes on looking correct -- which is
// how 4503 came to be missing from this one. They are pinned instead against the module
// `GET /api/v1/ws/protocol` serves verbatim, and the README that documents it.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { CLOSE_CODES, DOC_TYPES, ENVELOPE_KINDS, OP_INTENTS, channelOf } from "@/api/realtime/wsClient";

const repoFile = (path: string): string =>
  fileURLToPath(new URL(`../../../../../../${path}`, import.meta.url));

const OPENAPI_PATH = repoFile("packages/shared-openapi/openapi.json");
const CLOSE_CODES_PATH = repoFile("apps/backend/backend/services/realtime/close_codes.py");
const REALTIME_README_PATH = repoFile("packages/api-core/alkera_core/schemas/realtime/README.md");

interface Schema {
  properties?: Record<string, { items?: { enum?: string[] }; default?: unknown; enum?: string[] }>;
}

function schemas(): Record<string, Schema> {
  return (JSON.parse(readFileSync(OPENAPI_PATH, "utf-8")) as { components: { schemas: Record<string, Schema> } })
    .components.schemas;
}

const enumOf = (schema: Schema, prop: string): string[] => {
  const items = schema.properties?.[prop]?.items?.enum;
  if (!items) throw new Error(`${prop} carries no enum in openapi.json`);
  return items;
};

/** The mapping the descriptor route hands out as `close_codes` (`dict(CLOSE_CODES)`),
 *  read from the module that declares it. Each entry resolves a name in the table to the
 *  constant it points at, so a renamed constant is an error rather than a silent drop. */
function servedCloseCodes(): Record<string, number> {
  const source = readFileSync(CLOSE_CODES_PATH, "utf-8");
  const constants = new Map(
    [...source.matchAll(/^([A-Z_]+): Final = (\d{4})$/gm)].map(([, name, code]) => [name, Number(code)]),
  );
  const table = /CLOSE_CODES: Final\[dict\[str, int\]\] = \{([^}]*)\}/.exec(source);
  if (!table?.[1]) throw new Error("close_codes.py no longer declares a CLOSE_CODES mapping");
  const entries = [...table[1].matchAll(/"([A-Z_]+)":\s*([A-Z_]+),/g)].map(([, name, constant]) => {
    const code = constants.get(constant);
    if (code === undefined) throw new Error(`CLOSE_CODES names ${constant}, which is not a code`);
    return [name, code] as const;
  });
  if (entries.length === 0) throw new Error("close_codes.py served no close codes");
  return Object.fromEntries(entries);
}

/** The close-code table in the realtime README, the contract a client is written from. */
function documentedCloseCodes(): Record<string, number> {
  const source = readFileSync(REALTIME_README_PATH, "utf-8");
  const rows = [...source.matchAll(/^\|\s*(4\d{3})\s*\|\s*([A-Z_]+)\s*\|/gm)].map(
    ([, code, name]) => [name, Number(code)] as const,
  );
  if (rows.length === 0) throw new Error("the realtime README no longer tabulates its close codes");
  return Object.fromEntries(rows);
}

describe("the realtime protocol vocabulary", () => {
  it("envelope kinds, doc types and op intents equal the server's descriptor enums", () => {
    const descriptor = schemas().RealtimeProtocolDescriptor;
    expect([...ENVELOPE_KINDS]).toEqual(enumOf(descriptor, "envelope_kinds"));
    expect([...DOC_TYPES]).toEqual(enumOf(descriptor, "doc_types"));
    expect([...OP_INTENTS]).toEqual(enumOf(descriptor, "op_intents"));
  });

  it("the ticket's default path is the socket path the client connects to", () => {
    expect(schemas().WsTicketResponse.properties?.path?.default).toBe("/api/v1/ws");
  });

  it("the close codes are the set the gateway serves, name for name", () => {
    expect({ ...CLOSE_CODES }).toEqual(servedCloseCodes());
  });

  it("the README documents the set the gateway serves", () => {
    expect(documentedCloseCodes()).toEqual(servedCloseCodes());
  });

  it("a channel is doc:<type>:<id>, the grammar the server enforces", () => {
    expect(channelOf("artifact", "0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f")).toBe(
      "doc:artifact:0f2b6c1e-6d1a-4a6d-9f1e-2c3b4a5d6e7f",
    );
    expect(channelOf("chat", "sess-01J7Q3M8")).toBe("doc:chat:sess-01J7Q3M8");
  });
});
