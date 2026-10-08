// Every REST call the browser chat makes, checked against the server's own
// schema.
//
// The generated SDK pins the SHAPES (`components["schemas"][...]`), so a field
// that changes name fails the build. It pins nothing about the ADDRESS: the
// path and the method are string literals in `cloudChat/transport.ts`, and a
// route that moves — or was never spelled the way the browser spells it —
// shows up as a 404 in a tab, not as a red build.
//
// So each call is driven for real with `fetch` recorded, and the method + path
// it produced is looked up in `openapi.json`. Path parameters are matched
// against the template the schema declares, the way a router does.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import * as transport from "@/api/cloudChat/transport";

const OPENAPI = resolve(process.cwd(), "../../packages/shared-openapi/openapi.json");

interface Spec {
  paths: Record<string, Record<string, unknown>>;
}

const spec = JSON.parse(readFileSync(OPENAPI, "utf8")) as Spec;

/** The schema template a concrete path belongs to, or null when the server
 *  declares no such route. `/api/v1/chats/abc/messages` matches
 *  `/api/v1/chats/{chat_id}/messages`. */
function templateFor(path: string): string | null {
  const parts = path.split("/");
  for (const template of Object.keys(spec.paths)) {
    const declared = template.split("/");
    if (declared.length !== parts.length) continue;
    const matches = declared.every(
      (segment, i) => (segment.startsWith("{") && segment.endsWith("}")) || segment === parts[i],
    );
    if (matches) return template;
  }
  return null;
}

let calls: { method: string; path: string }[] = [];

beforeEach(() => {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), "http://portal.test");
      calls.push({ method: (init?.method ?? "GET").toUpperCase(), path: url.pathname });
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const CHAT = "1f1b6f16-0000-4000-8000-000000000001";
const OBJECT = "1f1b6f16-0000-4000-8000-000000000002";

const CALLS: [string, () => Promise<unknown>][] = [
  ["listChats", () => transport.listChats()],
  ["createChat", () => transport.createChat("hello")],
  ["getChat", () => transport.getChat(CHAT)],
  ["listMessages", () => transport.listMessages(CHAT)],
  ["postMessage", () => transport.postMessage(CHAT, { text: "hi", client_id: "c1" })],
  [
    "promoteResult",
    () =>
      transport.promoteResult(CHAT, {
        event_id: "e1",
        title: "t",
        columns: null,
        chart_spec: null,
      }),
  ],
  ["currentMachine", () => transport.currentMachine()],
  ["listObjects", () => transport.listObjects({ type: "query" })],
  [
    "createObject",
    () => transport.createObject({ type: "result", title: "t", spec: {}, client_id: "c1" }),
  ],
  ["getObject", () => transport.getObject(OBJECT)],
  ["objectRows", () => transport.objectRows(OBJECT)],
  // `rerunObject` is deliberately absent: the re-run route was retired with the
  // saved-query kind, so there is no declaration for it to match. It is listed
  // again the day the call is either restored or removed with its page.
];

describe("the cloud chat's REST calls", () => {
  it.each(CALLS)("%s addresses a route the server declares", async (_name, call) => {
    await call().catch(() => undefined);
    expect(calls).toHaveLength(1);
    const { method, path } = calls[0]!;
    const template = templateFor(path);
    expect(template, `no route in openapi.json matches ${method} ${path}`).not.toBeNull();
    expect(
      Object.keys(spec.paths[template!]!),
      `openapi.json declares no ${method} on ${template}`,
    ).toContain(method.toLowerCase());
  });

  it("downloads a CSV from a route the server declares", () => {
    const path = new URL(transport.objectCsvUrl(OBJECT), "http://portal.test").pathname;
    const template = templateFor(path);
    expect(template, `no route in openapi.json matches GET ${path}`).not.toBeNull();
    expect(Object.keys(spec.paths[template!]!)).toContain("get");
  });
});
