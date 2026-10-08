// Translates DataSource shapes into the shared conversation-model shapes the chat
// surfaces consume. Pure functions — no React.

import { PERMISSION_MODE_VALUES, PERMISSION_MODES } from "@alkera/chat-model";

import type { ModelInfo, PermissionMode, SendOptions } from "./data";

const KNOWN_MODES: ReadonlySet<string> = new Set(PERMISSION_MODE_VALUES);

/** Narrow a composer/daemon mode string to the permission-mode literal, or
 *  `undefined` when it isn't one (a newer daemon's mode we can't type yet). */
export function toPermissionMode(value: string | undefined): PermissionMode | undefined {
  return value && KNOWN_MODES.has(value)
    ? (value as PermissionMode)
    : undefined;
}

/** Permission-mode options for the composer's mode picker under the OpenCode
 *  engine, from the one mode registry every surface renders (the Slack card's
 *  menu and `/alkera mode` read the same list server-side). Each `description`
 *  is a promise about when the reader will be asked — so the sentences are the
 *  ones in the machine's own stance table
 *  (`alkera_cli/harness/permission_mode.py:MODE_RULES`), which is where the
 *  outcomes they describe are decided. `apps/cli/tests/harness/test_stance_contract.py`
 *  pins the registry to that table; a wording change belongs there first.
 *  Every mode admits a write inside the chat's own folder — that is the agent's
 *  working directory and where plan.md is drafted — which is what "outside
 *  this chat" names.
 *
 *  Each description shows on one line in the picker, so a sentence longer than
 *  the row is cut mid-word: keep every one of them inside the width the shortest
 *  entries already prove fits. */
export const PERMISSION_MODE_OPTIONS: {
  value: PermissionMode;
  label: string;
  description: string;
}[] = PERMISSION_MODES.flatMap((mode) => {
  const value = toPermissionMode(mode.value);
  return value ? [{ value, label: mode.label, description: mode.description }] : [];
});

/** Map the composer's send meta (selected model id + effort + mode) to engine
 *  SendOptions, resolving the model id against the fetched gateway catalog.
 *  An unrecognized model id or a non-permission mode is dropped so the daemon
 *  falls back to its defaults.
 *
 *  Effort options are model-scoped in the composer (each model's `efforts`
 *  from the gateway catalog / the chat's pinned manifest model); the chosen
 *  effort is forwarded verbatim and the host driver sends it as a `variant`
 *  only when the pinned model offers it. */
export function composerMetaToSendOptions(
  meta: { model?: string; effort?: string; mode?: string },
  models: ModelInfo[],
): SendOptions {
  const opts: SendOptions = {};
  const model = models.find((m) => m.id === meta.model);
  if (model) opts.model = model;
  if (meta.effort) opts.effort = meta.effort;
  const mode = toPermissionMode(meta.mode);
  if (mode) opts.mode = mode;
  return opts;
}
