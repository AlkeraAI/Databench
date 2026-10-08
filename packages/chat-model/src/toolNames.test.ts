// A tool's wire name is persisted verbatim into each chat's on-disk JSONL, so a
// stored transcript replays under whatever spelling was current when it was
// written. Opencode advertises an MCP tool as `<client>_<tool>` with every
// character outside `[A-Za-z0-9_-]` replaced, so `blob.profile` arrives as
// `alkera_blob_profile`; claude keeps the dots behind `mcp__alkera__`. Most of
// the manifest is dotted, so a resolver matching only the dotted spelling drops
// most of the tool surface onto the generic card -- green, with no type error.
//
// The two compositions below are the HARNESSES' rules, restated on purpose.
// Asking the resolver to build its own input would prove nothing about the wire.

import { describe, expect, it } from "vitest";

import { ALKERA_TOOL_NAMES, type AlkeraToolName } from "./generated/toolManifest";

import { alkeraToolName, canonicalToolName, nativeToolName } from "./toolNames";

/** Opencode's composition: the MCP client name, an underscore, and the tool name
 *  with every character outside its allowed class replaced. */
function opencode(name: string): string {
  return `alkera_${name.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}

/** Claude's composition: the server prefix, the tool's own name untouched. */
function claude(name: string): string {
  return `mcp__alkera__${name}`;
}

const NAMES = [...ALKERA_TOOL_NAMES];

describe("canonicalToolName strips transport namespacing and nothing else", () => {
  it.each([
    ["bash", "bash"],
    ["alkera_bash", "bash"],
    ["alkera-bash", "bash"],
    ["mcp__alkera__bash", "bash"],
    ["mcp__web__search", "search"],
    ["  bash  ", "bash"],
    // Only the LEADING prefix is peeled, and only once.
    ["alkera_alkera_bash", "alkera_bash"],
    ["sql.query", "sql.query"],
  ])("%s -> %s", (raw, expected) => {
    expect(canonicalToolName(raw)).toBe(expected);
  });
});

describe("every wire spelling resolves to the tool it was composed from", () => {
  // Both spellings of every tool live in one Map, and a Map keeps only its last
  // writer: the day an `a.b` / `a_b` pair enters the manifest, one of them starts
  // answering for the other. This round-trip is what would catch it.
  it.each(NAMES)("%s", (name) => {
    expect(alkeraToolName(name)).toBe(name);
    expect(alkeraToolName(opencode(name))).toBe(name);
    expect(alkeraToolName(claude(name))).toBe(name);
  });
});

describe("the shell tool answers to every spelling it has shipped under", () => {
  it.each([
    "bash",
    "alkera_bash", // historical: opencode loopback-MCP prefix
    "mcp__alkera__bash", // claude MCP composition, still current
    "alkera-bash", // historical: hyphenated loopback prefix
    "shell",
    "terminal",
    "Bash", // a model echoing its trained-on capitalization
    "mcp__alkera__Bash",
  ])("%s is the native bash tool", (wire) => {
    expect(nativeToolName(wire)).toBe("bash");
  });

  it.each(["sql.query", "lineage_impact", "alkera_sql.query", "web.search", "read"])(
    "%s is not swallowed by a shell alias",
    (wire) => {
      expect(nativeToolName(wire)).not.toBe("bash");
    },
  );
});

describe("an alkera tool is never mistaken for a native one", () => {
  // `web_search` / `web_fetch` are opencode's spelling of two ALKERA tools and
  // were once native aliases, so an alias would answer first and hand the call to
  // the vendor tool's card. The annotation makes a backend rename a `tsc` error.
  const WEB: AlkeraToolName[] = ["web.search", "web.fetch"];

  it.each(WEB)("%s under every spelling", (name) => {
    for (const wire of [name, opencode(name), claude(name), name.replace(".", "_")]) {
      expect(alkeraToolName(wire)).toBe(name);
      expect(nativeToolName(wire)).toBeNull();
    }
  });

  it("an unknown tool names neither surface and keeps its casing", () => {
    expect(alkeraToolName("alkera_MysteryTool")).toBeNull();
    expect(nativeToolName("alkera_MysteryTool")).toBeNull();
    expect(canonicalToolName("mcp__alkera__MysteryTool")).toBe("MysteryTool");
  });
});
