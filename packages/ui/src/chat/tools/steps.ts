// The per-tool step registry. A tool name resolves to a CARD, and a card is one
// row of voice -- the mark it wears and the words it says -- plus the head that
// reads the call. Holding the voice as data keeps a card file to what actually
// varies, and puts the whole surface's wording in one readable place.

import type { AlkeraToolName, ToolConversationPart } from "@alkera/chat-model";
import {
  IconBinoculars,
  IconBulb,
  IconChartHistogram,
  IconChecklist,
  IconCode,
  IconDatabase,
  IconFileDiff,
  IconFileExport,
  IconFilePlus,
  IconFileSearch,
  IconFileStack,
  IconFileText,
  IconFilter,
  IconFolderSearch,
  IconGitFork,
  IconLayoutDashboard,
  IconListCheck,
  IconNote,
  IconPencil,
  IconPencilPlus,
  IconPlug,
  IconPlugConnected,
  IconPresentationAnalytics,
  IconPuzzle,
  IconRobot,
  IconScript,
  IconSql,
  IconStack2,
  IconStackPush,
  IconTable,
  IconTerminal2,
  IconNotebook,
  IconTools,
  IconTrash,
  IconWorldDownload,
  IconWorldSearch,
} from "@tabler/icons-react";
import { alkeraToolName, canonicalToolName, isRefusal, nativeToolName, unwrapCallTool, type OpencodeToolName } from "@alkera/chat-model";

import { head as apply_patchHead } from "./apply_patch";
import { head as bashHead } from "./bash";
import { head as blobHead, headDelete as blob_deleteHead } from "./blob";
import { head as blob_materializeHead } from "./blob_materialize";
import { head as blob_profileHead } from "./blob_profile";
import { headDerive as blob_deriveHead, headQuery as blob_queryHead } from "./blob_query";
import { headEdit as context_editHead, headGet as context_getHead, headNote as context_noteHead } from "./context";
import { head as context_searchHead } from "./context_search";
import { head as editHead } from "./edit";
import { head as fetch_resultHead } from "./fetch_result";
import { head as genericHead } from "./generic";
import { head as globHead } from "./glob";
import { head as grepHead } from "./grep";
import { head as list_pluginsHead } from "./list_plugins";
import { headDashboards as looker_dashboardsHead, headTiles as looker_tilesHead } from "./looker";
import { head as lspHead } from "./lsp";
import { head as readHead } from "./read";
import { head as repo_cloneHead } from "./repo_clone";
import { head as repo_overviewHead } from "./repo_overview";
import { headAgents as list_agent_typesHead, headSearch as search_toolsHead } from "./search_tools";
import { head as notebookHead } from "./notebook";
import { head as notebook_showHead } from "./notebook_show";
import { head as skillHead } from "./skill";
import { head as specHead } from "./spec";
import { head as sql_connectionsHead } from "./sql_connections";
import { head as sql_queryHead } from "./sql_query";
import { head as sql_schemaHead } from "./sql_schema";
import { head as tasksHead } from "./tasks";
import { head as todowriteHead } from "./todowrite";
import { head as webfetchHead } from "./webfetch";
import { head as websearchHead } from "./websearch";
import { head as writeHead } from "./write";
import { cardStep, type Card, type CardStep, type StepEnvironment, type ToolStep } from "./step";
import { registeredCard } from "./toolCards";

export type StepAdapter = (part: ToolConversationPart, env?: StepEnvironment) => ToolStep;

/** THE TOOL VOICE, one row per card. A head may override any of these when the
 *  payload decides them; `icon` is absent where the card draws its own mark. */
const VOICE = {
  apply_patch: { icon: IconFileStack, verb: "Patched", of: "patch", lone: "Ran 1 patch", open: true, head: apply_patchHead },
  bash: { icon: IconTerminal2, verb: "Ran", of: "output", lone: "Ran 1 terminal command", kind: "command", head: bashHead },
  blob_create: { icon: IconStackPush, verb: "Stored", of: "result", lone: "Stored 1 result", head: blobHead },
  blob_delete: { icon: IconTrash, verb: "Deleted", of: "outcome", lone: "Deleted 1 result", head: blob_deleteHead },
  blob_derive: { icon: IconFilter, verb: "Reshaped", of: "result", lone: "Reshaped 1 result", open: true, head: blob_deriveHead },
  blob_info: { icon: IconStack2, verb: "Inspected", of: "shape", lone: "Inspected 1 result", open: true, head: blobHead },
  blob_materialize: { icon: IconFileExport, verb: "Wrote", of: "file", lone: "Wrote 1 result file", open: true, kind: "path", head: blob_materializeHead },
  blob_profile: { icon: IconChartHistogram, verb: "Profiled", of: "profile", lone: "Profiled 1 result", open: true, head: blob_profileHead },
  blob_query: { icon: IconSql, verb: "Queried", of: "result", lone: "Ran 1 result query", open: true, head: blob_queryHead },
  context_edit: { icon: IconPencil, verb: "Refined", of: "note", lone: "Edited 1 note", head: context_editHead },
  context_get: { icon: IconNote, verb: "Opened", of: "item", lone: "Opened 1 knowledge item", open: true, head: context_getHead },
  context_note: { icon: IconPencilPlus, verb: "Noted", of: "note", lone: "Wrote 1 note", head: context_noteHead },
  context_search: { icon: IconBulb, verb: "Searched", of: "matches", lone: "Ran 1 search", open: true, head: context_searchHead },
  edit: { icon: IconFileDiff, verb: "Edited", of: "diff", lone: "Ran 1 write", open: true, kind: "path", head: editHead },
  fetch_result: { icon: IconStack2, verb: "Fetched", of: "page", lone: "Fetched 1 page", head: fetch_resultHead },
  generic: { icon: IconPuzzle, verb: "Called", of: "result", lone: "Ran 1 tool call", open: true, head: genericHead },
  glob: { icon: IconFolderSearch, verb: "Matched", of: "files", lone: "Ran 1 search", head: globHead },
  grep: { icon: IconFileSearch, verb: "Searched", of: "matches", lone: "Ran 1 search", head: grepHead },
  list_agent_types: { icon: IconRobot, verb: "Listed", of: "agents", lone: "Listed the agent types", open: true, head: list_agent_typesHead },
  list_plugins: { icon: IconPlug, verb: "Listed", of: "plugins", lone: "Listed the plugins", open: true, head: list_pluginsHead },
  looker_dashboards: { icon: IconPresentationAnalytics, verb: "Listed dashboards on", of: "dashboards", lone: "Listed the Looker dashboards", head: looker_dashboardsHead },
  looker_tiles: { icon: IconLayoutDashboard, verb: "Inspected", of: "tiles", lone: "Inspected 1 dashboard", open: true, kind: "graph", head: looker_tilesHead },
  lsp: { icon: IconCode, verb: "Queried", of: "results", lone: "Ran 1 code lookup", kind: "path", head: lspHead },
  read: { icon: IconFileText, verb: "Read", of: "preview", lone: "Ran 1 read", kind: "path", head: readHead },
  repo_clone: { icon: IconGitFork, verb: "Cloned", of: "details", lone: "Ran 1 repository clone", open: true, kind: "path", head: repo_cloneHead },
  repo_overview: { icon: IconBinoculars, verb: "Explored", of: "overview", lone: "Ran 1 repository overview", kind: "path", head: repo_overviewHead },
  notebook: { icon: IconNotebook, verb: "Used", of: "notebook", lone: "Used 1 notebook tool", head: notebookHead },
  notebook_show: { icon: IconNotebook, verb: "Showed", of: "output", lone: "Showed 1 notebook output", open: true, head: notebook_showHead },
  search_tools: { icon: IconTools, verb: "Searched tools for", of: "tools", lone: "Ran 1 tool search", head: search_toolsHead },
  skill: { icon: IconScript, verb: "Loaded", of: "skill", lone: "Loaded 1 skill", head: skillHead },
  spec: { icon: IconDatabase, verb: "Described", of: "spec", lone: "Ran 1 tool call", open: true, head: specHead },
  sql_connections: { icon: IconPlugConnected, verb: "Listed", of: "connections", lone: "Listed the data connections", open: true, head: sql_connectionsHead },
  sql_query: { icon: IconDatabase, verb: "Queried", of: "result", lone: "Ran 1 query", open: true, head: sql_queryHead },
  sql_schema: { icon: IconTable, verb: "Listed relations on", of: "relations", lone: "Listed the connection's relations", head: sql_schemaHead },
  tasks: { icon: IconChecklist, verb: "Updated", of: "tasks", lone: "Updated the task list", open: true, head: tasksHead },
  todowrite: { icon: IconListCheck, verb: "Planned", of: "todos", lone: "Wrote the todo list", open: true, head: todowriteHead },
  webfetch: { icon: IconWorldDownload, verb: "Fetched", of: "page", lone: "Ran 1 fetch", head: webfetchHead },
  websearch: { icon: IconWorldSearch, verb: "Searched", of: "results", lone: "Ran 1 search", open: true, head: websearchHead },
  write: { icon: IconFilePlus, verb: "Wrote", of: "file", lone: "Ran 1 write", open: true, kind: "path", head: writeHead },

} satisfies Record<string, Card>;

type CardKey = keyof typeof VOICE;

const adapt = (key: CardKey): StepAdapter => (part, env) => cardStep(VOICE[key], part, env);

/** opencode-native tools. Tools without a designed interior take the generic
 *  ledger -- a deliberate, typed decision per tool, never a fall-through. */
const OPENCODE_STEPS: Record<OpencodeToolName, CardKey> = {
  read: "read",
  write: "write",
  edit: "edit",
  glob: "glob",
  grep: "grep",
  bash: "bash",
  webfetch: "webfetch",
  websearch: "websearch",
  apply_patch: "apply_patch",
  lsp: "lsp",
  repo_clone: "repo_clone",
  repo_overview: "repo_overview",
  todowrite: "todowrite",
  skill: "skill",
};

/** A tool with a designed interior, and `null` for the one that never renders
 *  as a step: a delegation is its own transcript block. A tool an installed
 *  extension draws (TOOL_CARDS) is routed to that card first. Every other alkera
 *  tool takes the spec sheet, which reads its shape off the payload and its field
 *  names off the vendor's own keys -- so a tool the backend adds tomorrow
 *  arrives with a real card and no frontend change. */
const ALKERA_STEPS: Partial<Record<AlkeraToolName, CardKey | null>> = {
  "sql.query": "sql_query",
  "sql.schema": "sql_schema",
  "sql.connections": "sql_connections",
  context_search: "context_search",
  context_get: "context_get",
  context_note: "context_note",
  context_edit: "context_edit",
  manage_tasks: "tasks",
  // The parent-hosted web tools share the vendor tools' wire shapes.
  "web.fetch": "webfetch",
  "web.search": "websearch",
  spawn_agent: null,
  // Unwrapped before routing; reaching this means the wrapper carried no inner call.
  call_tool: "generic",
  use_skill: "skill",
  fetch_result: "fetch_result",
  search_tools: "search_tools",
  list_agent_types: "list_agent_types",
  list_plugins: "list_plugins",
  "blob.create": "blob_create",
  "blob.delete": "blob_delete",
  "blob.info": "blob_info",
  "blob.derive": "blob_derive",
  "blob.query": "blob_query",
  "blob.materialize": "blob_materialize",
  "blob.profile": "blob_profile",
  "looker.list_dashboards": "looker_dashboards",
  "looker.dashboard_tiles": "looker_tiles",
  // The notebook shows every change live, so each call is one quiet line.
  "notebook.cells": "notebook",
  "notebook.create": "notebook",
  "notebook.edit": "notebook",
  "notebook.env": "notebook",
  "notebook.graph": "notebook",
  "notebook.inspect": "notebook",
  "notebook.kernel": "notebook",
  "notebook.output": "notebook",
  "notebook.read": "notebook",
  "notebook.run": "notebook",
  "notebook.settings": "notebook",
  // The one notebook call whose result is the point: the output, drawn.
  "notebook.show_output": "notebook_show",
  "notebook.widget": "notebook",
};

/** The adapter for a call, and the tool's OWN registered name. Adapters read
 *  identity off `part.name` -- the spec sheet takes its vendor, and so its
 *  brand mark, from the head of a dotted name -- so the harness spelling is
 *  resolved away here rather than re-parsed in every card. */
type Route = { kind: "hidden" } | { kind: "step"; adapter: StepAdapter; name: string };

function routeFor(wireName: string): Route {
  const native = nativeToolName(wireName);
  if (native !== null) return { kind: "step", adapter: adapt(OPENCODE_STEPS[native]), name: native };
  const alkera = alkeraToolName(wireName);
  if (alkera !== null) {
    // A card an installed extension draws comes first: the open table never names it.
    const card = registeredCard(alkera);
    if (card) return { kind: "step", adapter: (part, env) => cardStep(card, part, env), name: alkera };
    const key = alkera in ALKERA_STEPS ? ALKERA_STEPS[alkera] : "spec";
    return key ? { kind: "step", adapter: adapt(key), name: alkera } : { kind: "hidden" };
  }
  // An unknown tool still reaches the generic ledger under its own name, not
  // the harness prefix: the manifest lags a tool the backend has already added.
  return { kind: "step", adapter: adapt("generic"), name: canonicalToolName(wireName) };
}

/** A byte count as a reader reads it. Whole units below a megabyte, one decimal
 *  above, so "52.5 MB" tells a reader the scale of what they are not seeing. */
function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** What a step says when the publisher cut the result to fit one transcript
 *  row. The size is the point: a bare "truncated" leaves a reader guessing
 *  whether a line or a gigabyte is missing. When the tool spilled its result to
 *  a blob, the card's own reference chip is the way to the whole thing, so the
 *  note says the output is stored rather than repeating the amount. */
function droppedNote(part: ToolConversationPart, dropped: number): string {
  const stored = (part.references?.length ?? 0) > 0;
  return stored
    ? `${bytes(dropped)} not shown here (open the stored result for all of it)`
    : `${bytes(dropped)} not shown here`;
}

/** The present-tense form of each opening verb a card's voice uses. */
const IN_PROGRESS: Record<string, string> = {
  Called: "Calling",
  Cloned: "Cloning",
  Deleted: "Deleting",
  Described: "Describing",
  Edited: "Editing",
  Explored: "Exploring",
  Fetched: "Fetching",
  Inspected: "Inspecting",
  Listed: "Listing",
  Loaded: "Loading",
  Matched: "Matching",
  Noted: "Noting",
  Opened: "Opening",
  Patched: "Patching",
  Planned: "Planning",
  Profiled: "Profiling",
  Queried: "Querying",
  Ran: "Running",
  Showed: "Showing",
  Shut: "Shutting",
  Restarted: "Restarting",
  Interrupted: "Interrupting",
  Checked: "Checking",
  Used: "Using",
  Changed: "Changing",
  Installed: "Installing",
  Created: "Creating",
  Read: "Reading",
  Refined: "Refining",
  Reshaped: "Reshaping",
  Searched: "Searching",
  Stored: "Storing",
  Traced: "Tracing",
  Updated: "Updating",
  Wrote: "Writing",
};

/** A past-tense phrase ("Ran 1 query", "Listed dashboards on") in the present
 *  ("Running 1 query"). Only the opening verb moves; a phrase that opens with
 *  a word the table does not know stays as it is. */
export function inProgress(phrase: string): string {
  const [first, ...rest] = phrase.split(" ");
  const present = IN_PROGRESS[first];
  return present ? [present, ...rest].join(" ") : phrase;
}

/** The plain form of each opening verb, for a call that failed ("Could not
 *  edit notes.md") or one an ask still holds ("Waiting to run rm -rf …"). */
const PLAIN: Record<string, string> = {
  Called: "call",
  Cloned: "clone",
  Deleted: "delete",
  Described: "describe",
  Edited: "edit",
  Explored: "explore",
  Fetched: "fetch",
  Inspected: "inspect",
  Listed: "list",
  Loaded: "load",
  Matched: "match",
  Noted: "note",
  Opened: "open",
  Patched: "patch",
  Planned: "plan",
  Profiled: "profile",
  Queried: "query",
  Ran: "run",
  Showed: "show",
  Shut: "shut",
  Restarted: "restart",
  Interrupted: "interrupt",
  Checked: "check",
  Used: "use",
  Changed: "change",
  Installed: "install",
  Created: "create",
  Read: "read",
  Refined: "refine",
  Reshaped: "reshape",
  Searched: "search",
  Stored: "store",
  Traced: "trace",
  Updated: "update",
  Wrote: "write",
};

/** A past-tense phrase ("Edited", "Ran 1 query") as a failure ("Could not
 *  edit", "Could not run 1 query"). A phrase that opens with a word the table
 *  does not know says only that the call failed. */
export function failed(phrase: string): string {
  const [first, ...rest] = phrase.split(" ");
  const plain = PLAIN[first];
  return plain ? ["Could not", plain, ...rest].join(" ") : "Could not finish";
}

/** A past-tense phrase as a call held for approval: "Ran 1 terminal command"
 *  as "Waiting to run 1 terminal command". */
export function waiting(phrase: string): string {
  const [first, ...rest] = phrase.split(" ");
  const plain = PLAIN[first];
  return plain ? ["Waiting to", plain, ...rest].join(" ") : "Waiting for approval";
}

const REFUSED = "Refused";

/** "Ran 1 terminal command" as "Refused 1 terminal command". */
function refusedSummary(lone: string): string {
  const [first, ...rest] = lone.split(" ");
  return first in IN_PROGRESS && rest.length ? [REFUSED, ...rest].join(" ") : `${REFUSED} 1 call`;
}

/** Resolve a harness tool call to the step its group renders, or null for a
 *  delegation (rendered as its own transcript block). An unknown tool falls
 *  back to the generic ledger. */
export function resolveStep(rawPart: ToolConversationPart, env?: StepEnvironment): CardStep | null {
  const part = unwrapCallTool(rawPart);
  if (part.toolKind === "task") return null;
  const route = routeFor(part.name);
  if (route.kind === "hidden") return null;
  const { adapter, name } = route;
  // The id and the failure text are stamped here, once, so no adapter repeats them.
  const step = adapter(name === part.name ? part : { ...part, name }, env);
  // Likewise the cut: it happens to any tool's result, so it is said in one
  // place rather than left to each card to remember.
  const dropped = part.truncatedBytes ?? 0;
  // A call the turn's Stop cut off has no result to figure and did not fail: the
  // group states the stop once, from the status, so neither a card's figure nor
  // the harness's own abort words ride along beside it.
  if (part.state === "stopped") {
    return { id: part.callId, ...step, status: "stopped", data: undefined, failure: undefined };
  }
  // A call not yet answered speaks in the present: "Ran" over a command still
  // waiting for approval claims a run that has not happened.
  if (part.state === "pending" || part.state === "running") {
    // An ask still waiting on a person holds the call: nothing runs until it is
    // answered, so the head says it is waiting rather than running.
    if (env?.awaitingApproval) {
      return {
        id: part.callId,
        ...step,
        verb: waiting(step.verb),
        loneSummary: waiting(step.loneSummary),
        waiting: true,
        data: undefined,
        footer: undefined,
      };
    }
    return {
      id: part.callId,
      ...step,
      verb: inProgress(step.verb),
      loneSummary: inProgress(step.loneSummary),
      data: undefined,
      footer: undefined,
    };
  }
  // A refused call never ran: no result figure, nothing to open, only the
  // refusal's reason under a head that says it was refused.
  if (part.state === "error" && isRefusal(part.errorText)) {
    return {
      id: part.callId,
      ...step,
      verb: REFUSED,
      loneSummary: refusedSummary(step.loneSummary),
      refused: true,
      data: undefined,
      body: undefined,
      footer: undefined,
      expanded: false,
      failure: part.errorText || undefined,
    };
  }
  // A call that failed did not do what its verb claims: the head says it could
  // not, and no result figure stands beside the error.
  if (part.state === "error") {
    return {
      id: part.callId,
      ...step,
      verb: failed(step.verb),
      loneSummary: failed(step.loneSummary),
      data: undefined,
      failure: part.errorText || undefined,
    };
  }
  return {
    id: part.callId,
    ...step,
    ...(dropped > 0 ? { footer: droppedNote(part, dropped) } : {}),
    failure: part.errorText || undefined,
  };
}
