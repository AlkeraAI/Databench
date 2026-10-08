// Tool NAME resolution: from a harness wire spelling to the tool it denotes.
// Rendering stays in the UI packages; this layer is the shared vocabulary both
// frontends (and any future surface) route by, so it lives with the model.
//
// The alkera tool surface is the generated `AlkeraToolName` union (from
// packages/shared-openapi/tool-manifest.json). opencode-native tools
// (read/glob/grep/bash/...) are vendored TypeScript with plain-text output —
// they are NOT in the generated manifest, so their (small, stable) name set is
// owned by the permission presentation registry and generated alongside it.

import {
  NATIVE_TOOL_NAMES,
  PERMISSION_PRESENTATION_REGISTRY,
} from "./generated/permissionPresentation";
import { ALKERA_TOOL_NAMES, type AlkeraToolName } from "./generated/toolManifest";

/** opencode-native tools — vendored TS, plain-text output, NOT in the manifest.
 *  The list and its aliases are owned by the permission presentation registry
 *  (`alkera_core.permission_presentation.registry`), so the server resolves a
 *  wire key to the same tool the browser does. */
export const OPENCODE_TOOL_NAMES = NATIVE_TOOL_NAMES;
export type OpencodeToolName = (typeof OPENCODE_TOOL_NAMES)[number];

/** Wire-name variants that mean the same native tool → its canonical id.
 *  `web_search` / `web_fetch` are NOT here: they are opencode's sanitized
 *  spelling of the alkera tools `web.search` / `web.fetch`, and an alias would
 *  claim them before `alkeraToolName` could, handing an adapter the wrong
 *  identity the day those tools earn their own interior. */
const OPENCODE_ALIASES: Readonly<Record<string, OpencodeToolName>> =
  PERMISSION_PRESENTATION_REGISTRY.nativeToolAliases;

/** Strip the MCP server prefix (`mcp__alkera__…`) and the bare `alkera_`/`alkera-`
 *  prefix the loopback server adds, leaving the tool's own registered name.
 *
 *  This is load-bearing for BACKWARD COMPATIBILITY, not just tidiness: a tool's
 *  wire name is persisted verbatim into each chat's on-disk JSONL, so a stored
 *  transcript replays under whatever spelling was current when it was written.
 *  The shell tool alone has four live spellings that must all reach the terminal
 *  card — `alkera_bash` (opencode, before it was advertised under the native
 *  name), `mcp__alkera__bash` (claude, still current), and bare `bash` (opencode
 *  today, the Windows native, and the name `runtime.py` synthesizes for
 *  background-job cards). Pinned in toolNames.test.ts. The PREFIX STRIPPING is
 *  mirrored on the TUI side by `canonical_tool_name` in
 *  `alkera_cli/ui/tui/tunables/tools.py`; resolving opencode's sanitized spelling
 *  back to a registered name (`alkeraToolName` below) is webview-only, so the TUI
 *  still reads `snowflake_running_queries` as an unknown tool. */
export function canonicalToolName(name: string): string {
  return name
    .trim()
    .replace(/^mcp__[^_]+__/i, "")
    .replace(/^alkera[_-]/i, "");
}

/** The case-folded key to look a canonical name up by. Every id in the tables
 *  below is lower-case, and a model echoing its trained-on capitalization (`Bash`
 *  rather than `bash`) must still hit its card — opencode itself repairs the case
 *  of a mis-cased call, so the name can reach us either way. Kept separate from
 *  `canonicalToolName` so the display fallback (`humanizeToolName`) still sees the
 *  original casing and renders an unknown `MyTool` as "MyTool", not "Mytool". */
function lookupKey(canonical: string): string {
  return canonical.toLowerCase();
}

/** Every wire spelling of an alkera tool, mapped to its generated name.
 *
 *  A dotted tool reaches us under two spellings and the registries are keyed on
 *  only one of them. Opencode composes an MCP tool as `<client>_<tool>` with
 *  every character outside `[A-Za-z0-9_-]` replaced, so `blob.profile` arrives
 *  as `alkera_blob_profile`; claude keeps the dots. 102 of the 126 tools are
 *  dotted, so matching one spelling drops most of the surface onto the generic
 *  card. Both spellings are built here from the generated names -- nothing is
 *  hand-typed, and across all 126 the two spellings collide on nothing. */
const ALKERA_BY_SPELLING = new Map<string, AlkeraToolName>(
  ALKERA_TOOL_NAMES.flatMap((name): [string, AlkeraToolName][] => [
    [lookupKey(name), name],
    [lookupKey(name.replace(/[^a-zA-Z0-9_-]/g, "_")), name],
  ]),
);

/** The generated tool name a wire spelling denotes, or null when it names no
 *  alkera tool. The one door from a harness name to the tool surface. */
export function alkeraToolName(wireName: string): AlkeraToolName | null {
  return ALKERA_BY_SPELLING.get(lookupKey(canonicalToolName(wireName))) ?? null;
}

/** The opencode-native tool a wire spelling denotes, or null. The native half of
 *  the same door, so the case-fold and the alias table have one owner rather
 *  than a copy per lookup site. */
export function nativeToolName(wireName: string): OpencodeToolName | null {
  const key = lookupKey(canonicalToolName(wireName));
  const native = OPENCODE_ALIASES[key] ?? key;
  // Membership is the NAME list, not a card table: the step registry resolves
  // through here too, and the vocabulary must outlive any one set of renderers.
  return NATIVE_NAMES.has(native) ? (native as OpencodeToolName) : null;
}

const NATIVE_NAMES = new Set<string>(OPENCODE_TOOL_NAMES);
