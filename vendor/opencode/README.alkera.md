# vendor/opencode — alkera patch-management

> This directory is a **git subtree** of [sst/opencode](https://github.com/sst/opencode),
> imported via squash merges. It is NOT a submodule and NOT a separate fork.

opencode is one implementation of Alkera's agent harness. The harness
contract, `HarnessAdapter` in `apps/cli/alkera_cli/harness/`, has two
implementations: this one (`adapters/opencode_http.py`, the default,
manifest slug `agent`) and Claude Code (`adapters/claude_agent.py`, slug
`claude-agent`). opencode is Copyright (c) 2025 opencode and MIT licensed
(see `LICENSE` in this directory and the repository's `NOTICE`). Thank you to
its authors.

Our builds name the runtime `alkera-agent` and answer as the product's
assistant, and the patches below say what each change does. None of them is
there to deny that this is opencode.

The alkera patches to opencode live as **normal commits in the parent
repo's history**, alongside the squashed import of upstream. Every
alkera-local **source** edit is fenced inline with
`// == ALKERA EDIT START` … `// == ALKERA EDIT END` comments — so each change
is greppable and stands out instantly in a merge conflict after an upstream
bump — and inventoried below.

Prompt / tool-description `.txt` files **cannot** carry `//` markers (their
contents are fed verbatim into the model's prompt, where `//` is just text).
Their alkera edits are documented in a local `README.alkera.md` next to them:

- [`packages/opencode/src/session/prompt/README.alkera.md`](packages/opencode/src/session/prompt/README.alkera.md) — `anthropic.txt`, `gpt.txt`, `plan.txt`
- [`packages/opencode/src/tool/README.alkera.md`](packages/opencode/src/tool/README.alkera.md) — `question.txt`, `plan-present.txt`

## Alkera-local source patches (inventory)

Keep this list in sync when you add/remove a patch. On an upstream bump,
re-apply each by searching the new tree for the old string and restoring
the fenced (`== ALKERA EDIT`) change. All are surgical (a few lines each).

| File | What | Why |
| --- | --- | --- |
| `packages/opencode/src/tool/plan.ts`, `session/prompt/plan.txt` | `plan_present` tool | Plan-mode approval used by the harness (pre-existing). |
| `packages/core/src/global.ts` | `app = "opencode"` → `"agent"` | **Product names.** The agent runtime's data/config/state/cache/tmp/log/repos/bin dirs (and `os.tmpdir()/<app>`) use the product name `agent`. Single source for all of those paths. Existing chats keep their session database under it, so it does not change back. |
| `packages/opencode/src/storage/db.ts` | `opencode.db` / `opencode-<channel>.db` → `agent.db` / `agent-<channel>.db` | **Product names.** The session database is `agent.db`, matching `global.ts`. Must match the migration marker in `index.ts`. |
| `packages/opencode/src/index.ts` | migration marker `opencode.db` → `agent.db` | **Product names.** Must match `db.ts`. With `ALKERA_DISABLE_CHANNEL_DB=1` the names line up so the one-time-migration banner stops printing every boot. |
| `packages/opencode/src/cli/cmd/serve.ts` | writes the listen URL to the file named by `ALKERA_LISTEN_FILE` before printing upstream's unchanged banner `opencode server listening on …` | **Correctness.** A `bun build --compile` binary block-buffers stdout to a pipe on Windows, so the banner can sit unflushed and the adapter would time out. The file is the adapter's primary readiness signal; the banner is the fallback, which the adapter (`opencode_http.py`) finds by the substring `"server listening on "`. |
| `packages/opencode/src/server/server.ts` | `--port 0` under `ALKERA_LISTEN_FILE` binds a true ephemeral port (no prefer-4096 probe) | **Correctness.** Many managed instances start concurrently (one per chat; parallel e2e workers); the legacy prefer-4096 fallback made them contend for one port, and a loser could read a FOREIGN server through it and fail its session probe. |
| `packages/opencode/src/file/ripgrep.ts` | `ALKERA_DISABLE_RIPGREP_DOWNLOAD` gate before the github `rg` download | **No phone-home.** opencode normally fetches `rg` from `github.com/BurntSushi` into a per-chat sandbox on first glob/grep. We ship a bundled per-platform `rg` instead (staged + Nuitka-bundled), put it first on `PATH`, and set this flag so the runtime never downloads. The gate is conditional (the adapter only sets it when a bundled rg is present) so a bare `bun-dev` checkout keeps the download fallback — that conditionality is what makes re-adding the gate safe this time (see below). |
| `packages/core/src/npm.ts` | `ALKERA_DISABLE_NPM_INSTALL` master gate at the `Npm` chokepoint (`reify` + `add` + `install` + `which`) | **No phone-home.** opencode's runtime npm installer (`@npmcli/arborist` → `registry.npmjs.org`) is the last remaining per-sandbox web call. It funnels EVERY runtime install — the `@opencode-ai/plugin` config-dep (installed into every config dir on load), external plugin packages, non-bundled provider SDKs, and the edit/write-tool formatters (prettier/oxfmt/biome). We use none of those, so the harness sets this flag and the gate short-circuits at `reify()` (the sole arborist call) plus every entrypoint — a complete guarantee, proven by the airgap e2e (`test_opencode_airgap_e2e.py`). Callers degrade gracefully (providers → InitError, formatters/LSP → "not found", config dep → silent no-op). |
| **env-var rename** `OPENCODE_* → ALKERA_*` — `packages/core/src/flag/flag.ts`, `packages/opencode/src/effect/runtime-flags.ts`, `packages/opencode/src/config/config.ts`, `packages/core/src/global.ts`, `packages/opencode/src/share/share-next.ts`, `packages/core/src/npm.ts`, `packages/opencode/src/file/ripgrep.ts`, `packages/opencode/src/index.ts`, `packages/core/src/util/opencode-process.ts`, `packages/core/src/util/log.ts`, `packages/opencode/src/server/auth.ts` | Every env var the **adapter sets** (`CONFIG_CONTENT`, `SERVER_PASSWORD`, `PERMISSION`, `TEST_HOME`, `DISABLE_PROJECT_CONFIG`, the lockdown flags, `DISABLE_RIPGREP_DOWNLOAD`, `BASH_DEFAULT_TIMEOUT_MS`) plus the **self-set** ones opencode writes onto its own process (`OPENCODE`→`ALKERA`, `OPENCODE_PID`→`ALKERA_PID`, `RUN_ID`, `PROCESS_ROLE`, `LOG_INITIALIZED_RUN_ID`) now read/write `ALKERA_*` names. JS keys / exported identifiers are unchanged (pure code, never leak); only the env-name **string literals** change. | **Product settings.** The adapter passes every setting under its `ALKERA_` prefix, so none collides with an `OPENCODE_*` value the user's own shell exports for their own opencode (the adapter scrubs those). Must stay in lockstep with `_build_env` in `opencode_http.py` (the adapter sets the `ALKERA_*` names). Scrub-only readers (`OPENCODE_CONFIG/CONFIG_DIR/AUTH_CONTENT/DB`) are deliberately left — the adapter never sets them, so they never reach the child env; the scrub still pops the native names. |
| **model-facing persona + capability** — all `packages/opencode/src/session/prompt/*.txt` (see that dir's `README.alkera.md`), `session/system.ts` (`environment()` capability line, every model), `session/session.ts` (plan dir `.opencode`→`.agent` in the user's worktree), `tool/lsp.txt`, `tool/repo_clone.txt`, `tool/websearch.ts` (outbound `User-Agent`), `command/template/initialize.txt` | **Product persona.** The model answers as the product's assistant: "You are Alkera" plus a shared "good at coding, data engineering and data science" capability line, instead of "You are OpenCode" and an instruction to WebFetch opencode.ai docs. No prompt tells the model to deny or hide what runs it. Plan-mode writes plans under `.agent/` instead of `.opencode/` in the user's repo. |
| **product name on disk and on the wire** — `packages/core/src/project.ts` (`.git/opencode`→`.git/agent` project-id cache), `packages/opencode/src/index.ts` (startup `Log.Default.info("opencode")`→`"agent"`), `packages/core/src/effect/observability.ts` (OTEL `serviceName`/attr keys), `packages/core/src/plugin/provider/{llmgateway,nvidia,cerebras,openrouter,kilo,zenmux,vercel}.ts` + `packages/opencode/src/provider/provider.ts` (outbound `X-Title`/`X-Source`/`X-Cerebras`/`HTTP-Referer`/`User-Agent` attribution → `alkera`) | **Product names.** On-disk names and outbound attribution name the product. `project.ts` writes its project-id cache into the user's **real `.git/`** (outside the sandbox) as `agent`; the id derives from the git remote/root so a missing cache just recomputes. The provider attribution headers name the product as the calling app; they are value-only swaps (greppable via `rg '"alkera"' packages/opencode/src/provider/provider.ts`); `provider.ts` is **not** inline-fenced (≈13 identical attribution lines). **Deferred (gateway-coupled):** the `"opencode"` provider id + `opencode/big-pickle` default model + the Zen-path headers in `session/llm/request.ts` (`x-opencode-*`, `User-Agent`) — these route to opencode's hosted gateway and move with the "point opencode at our own gateway" milestone. |
| **process User-Agent** — `packages/opencode/script/build.ts` (`Bun.build` `compile.execArgv`) | The Bun process's default fetch `--user-agent` `opencode/<ver>` → `alkera-agent/<ver>`. | **Product name.** The compiled binary's process-wide default UA (any fetch that doesn't set its own) is `alkera-agent/<ver>`. Distinct from the per-request provider/websearch UAs above. |
| **instruction-file support** — `packages/core/src/flag/flag.ts` (new `OPENCODE_DISABLE_PROJECT_INSTRUCTIONS` getter reading `ALKERA_DISABLE_PROJECT_INSTRUCTIONS`), `packages/opencode/src/session/instruction.ts` (`ALKERA_FILE`, precedence block in `systemPaths()`, additive `find()`→`string[]` + `resolve()`, size caps in `read()`/`system()`) | A NEW flag **decouples repo instruction-file discovery from the `opencode.json` config lockdown**: the adapter still sets `ALKERA_DISABLE_PROJECT_CONFIG=true` (config stays off via `config.ts`/`paths.ts`) but does NOT set `ALKERA_DISABLE_PROJECT_INSTRUCTIONS`, so AGENTS.md/CLAUDE.md/CONTEXT.md project + subdir discovery turns ON. Adds **`ALKERA.md`** — additive (never suppresses the others) with **precedence** (emitted first, never the file dropped at the budget). Adds conservative **size caps** (32KB/file head-truncate, 64KB total) at the single `read()` chokepoint + `system()`. | Honor repo + alkera-branded agent-instruction files inside the sandboxed harness **without** re-enabling the user's `opencode.json`, and bound the context/cost they consume. The coarse `ALKERA_DISABLE_PROJECT_CONFIG` flag conflated config + instructions; this splits them. |
| **parent-hosted shell under the native name** — `packages/opencode/src/effect/runtime-flags.ts` (new `parentShell` flag reading `ALKERA_PARENT_SHELL`), `packages/opencode/src/tool/registry.ts` (`tools()` filter drops `ShellTool`), `packages/opencode/src/mcp/index.ts` (`ALKERA_BARE_TOOL_NAMES` remap at the `<server>_<tool>` composition) | **A COUPLED PAIR — re-apply both or neither.** Under one flag: (1) the native `ShellTool` is dropped from the ADVERTISED tool set, and (2) our loopback-MCP `alkera_bash` is advertised under the bare name `bash`. Edit 1 is what frees the `bash` id for edit 2, so applying edit 2 alone collides with the native and applying edit 1 alone leaves the agent with no shell at all. `mcp/index.ts` reads `process.env` directly rather than `RuntimeFlags` — that Effect has no flags dependency today and adding one would change its requirement graph. | Models are pretrained on a bare `bash`; `alkera_bash` was the one odd name beside the unprefixed natives (read/write/edit/glob/grep), making the shell needlessly unfamiliar to reach for. Also fixes a real bug: the previous approach denied native bash via `ALKERA_PERMISSION`, and the model saw BOTH `bash` and `alkera_bash` — every attempt at the trained-on `bash` burned a turn on a `DeniedError` whose text is a raw ruleset dump that never names the replacement. **A permission rule is not how a tool is withheld here.** What a bare `pattern:"*"` deny does today is subtract the tool before the request (`Permission.disabled` → `session/llm/request.ts` `resolveTools`) — but only while our ruleset is the last word on it, and a deny scoped to patterns (which is what the shell needs, since some commands must run) is never subtracted at all. The `tools()` filter is the gate; a deny is the call-time floor under it. Adapter side: `_OPENCODE_PARENT_SHELL_FLAG` + the `"bash": "allow"` permission key in `opencode_http.py`, both POSIX-guarded (Windows keeps the native shell until our port is cross-platform). |
| **no-delegation deployments** — `packages/opencode/src/effect/runtime-flags.ts` (new `subagents` flag reading `ALKERA_SUBAGENTS_ENABLED`, default **true**), `packages/opencode/src/tool/registry.ts` (`SUBAGENT_TOOL_IDS` + the `tools()` filter that drops them) | With the flag false, every subagent-spawning built-in (`task`) is removed from the ADVERTISED tool set. A new spawner is withheld by being added to `SUBAGENT_TOOL_IDS`, not by a second filter. | Alkera can serve chats with delegation off (`ALKERA_SUBAGENTS_ENABLED=false` — the provisioned demo box, whose web-portal chats have no subagent surface at all): the parent withholds its own `spawn_agent`/`list_agent_types`, and this withholds opencode's private one, so the model is not offered a door out of the chat. The adapter's `"task": "deny"` stays as the call-time floor: a deny does subtract the tool today (`Permission.disabled` collects every `pattern:"*"` deny, `session/llm/request.ts` `resolveTools` subtracts it), but it is one merged ruleset away from being outranked, and a box still running an older staged binary has no filter at all. Adapter side: `_OPENCODE_SUBAGENTS_FLAG` in `opencode_http.py`, set from `SessionConfig.subagents_enabled` (never read from the spawning process's env). |
| **the summariser summarises** — `packages/opencode/src/session/compaction.ts` (`pendingTurn`, `pendingExcerpt`, `pendingPlaceholder`, `summaryBudgetWords`, `summaryMaxChars`, `cleanSummaryText`, `normalizeSummary`; the pending/replay decision and the `textMaxChars` cap in `process`; the rules block of `SUMMARY_TEMPLATE`; the post-compaction nudge), `packages/opencode/src/session/message-v2.ts` (`truncateText` + the `textMaxChars` option on `toModelMessages[Effect]`), `packages/opencode/src/agent/prompt/compaction.txt` (the compaction agent's system prompt — a `.txt`, so unfenced; re-apply by diff) | Upstream ran the summariser as one more assistant step on the same message list: the whole conversation, then "Create a new anchored summary…", so the model read the history as a to-do list and the instruction as one more item. Four things change. (1) Every text part the summariser sees is head+tail capped at `SUMMARY_TEXT_MAX_CHARS` (3 000) with the middle named, so it cannot copy a dump it never saw. (2) The instruction gains a rules block (describe data, ≤ 10 lines of any dump, keep identifiers/paths/numbers, never carry out a request, no preamble or closing question) and a word budget of 5 % of the usable window (400–2 500), and the agent's own system prompt is rewritten to say it is not the assistant in that conversation. (3) The newest user message whose reply has not finished (`pendingTurn` — a mid-turn overflow, a tool-call step, an errored reply) is never a turn in the summariser's input: it stays as the retained tail when the tail holds it, otherwise it is replaced in place by `pendingPlaceholder` (keeping its user role, since dropping it would open the call with its own replies) and quoted inside the instruction as `<in-progress-request>`; when nothing of its turn survives the boundary it is re-attached after the summary through the existing replay path, and the nudge names it and forbids asking the user. (4) What is STORED is normalised to the template (`cleanSummaryText`: cut before `## Goal`, collapse a run of > 10 tabular rows, drop a closing question/offer, then bound the whole thing to `summaryMaxChars` on a line boundary with a marker saying how much went) by rewriting the first text part and emptying the rest — the harness translator keys its buffer by part id for exactly this. A summary the rules would reduce to nothing is kept whole. Upstream's own provider-overflow replay path and its nudge wording for a chat with nothing in progress are untouched. | **Product bug** (live run on the pod, Sep 2026): one summary was 36 KB and held 300 verbatim CSV rows the compaction was meant to discard (context fell to 49 k instead of 21 k, the pass took 85 s); another recorded a user turn that never happened and opened with the model's scratchpad; both turns then asked "how would you like to proceed?". Proven by `test/session/compaction.test.ts` ("session.compaction.summariser") + `test/session/message-v2.test.ts` (the `textMaxChars` cap), and end-to-end by the mock-e2e `apps/cli/tests/e2e/test_opencode_e2e.py::test_auto_compaction_summarises_instead_of_answering` (the wire-level system prompt is only observable there — the unit fake replaces `LLM.Service`). |
| **gateway-only provider registry** — `packages/opencode/src/provider/provider.ts` (`alkeraAdmittedProviders()`, `NotAdmittedError`, `NoModelConfiguredError`; the `disabled` seeding + `isProviderAllowed` clause in the state build; the guards in `getModel`, `getSmallModel`, `defaultModel`), `packages/opencode/src/session/prompt.ts` (`getModel` publishes the guard's refusal as the session error) | When the process runs under the harness (`ALKERA_CONFIG_CONTENT` is set) the ONLY providers that exist are the ones that injected config's `provider` map declares. Every catalog built-in (`opencode`'s hosted provider, `anthropic`, `openai`, `zenmux`, …) is disabled before any auth file, env key or custom loader can connect it; resolving a model on an undeclared provider (a prompt's `providerID`, `cfg.small_model`) dies with a clear refusal, and a config with no `model` dies with one too instead of picking a recent or first-connected model. Outside the harness nothing changes. Coupled with the adapter's own gate (`opencode_http.py::_admit_turn_model`) and the server-side pin resolver — see `apps/cli/alkera_cli/harness/README.md`, "Models only ever come from the gateway". | **Security / money path.** opencode's `opencode` provider is "connected" with no credential at all, so a chat that reached the harness without a gateway model ran on `opencode/big-pickle` — no credential, no metering, no billing, no audit, outside the org's model policy — while the reader was shown another model. The registry guard makes that unreachable inside the subprocess even if a config slipped past the adapter. Proven by `apps/cli/tests/e2e/test_opencode_gateway_only_e2e.py` (mock-e2e, bun-dev). |

| **a host-shortened tool result is not shortened again** — `packages/opencode/src/tool/truncate.ts` (`HOST_SPILL_META_KEY`, `hostSpill()`, `toolOutput()`), `packages/opencode/src/session/tools.ts` (the MCP branch calls `Truncate.toolOutput` instead of `truncate.output`), NEW `packages/opencode/test/tool/host-spill.test.ts` | An MCP result whose `_meta` carries `ai.alkera/host-spill` is passed through as it stands, with the host's `outputPath` kept. Everything else still truncates exactly as before. | **Correctness.** The alkera tools (`bash`, `graph_python`, the integration SDK) spill the whole output to a file of their own and return a tail plus that path. Truncating that tail AGAIN wrote an opencode file from it and stamped THAT path as `metadata.outputPath` — the path the transcript row, the tool card and the model are given — so a 50 MB command left the reader pointed at ~50 KB of preview while the output sat in a file nothing named. Mirrors what `tool.ts` already does for a native tool that sets `metadata.truncated` itself. Host side: `host_spill_pointer` + the `_meta` stamp in `apps/cli/alkera_cli/harness/mcp_server.py`. Proven by `apps/cli/tests/e2e/test_opencode_extreme_output_e2e.py` (mock-e2e, bun-dev). |
| **an effort change is not a model change, nor is a switch to a model that reads the reasoning**: `packages/opencode/src/session/message-v2.ts` (`alkeraBaseModelID()`, `alkeraCanReplayReasoning()`; the `differentModel` line in `toModelMessagesEffect`), `packages/opencode/src/session/llm/request.ts` (`options.alkera` removed before the provider options are built), `packages/opencode/test/session/message-v2.test.ts` ("alkera effort suffix") | Reasoning is replayed as written when the source and target share `providerID/<model id up to the first ::>`, or when the target's config entry lists the source's format in `options.alkera.readsReasoningFormats`, the source's format being looked up by base model id in `options.alkera.reasoningFormatsByModel` (both written by `build_alkera_opencode_config`). `options.alkera` is opencode's own note and never reaches the provider. | **Correctness.** The Alkera gateway carries the reasoning effort in the model key (`<id>::<effort>`), so an effort change made opencode treat every earlier assistant message as another model's: its signed thinking was replayed as plain text and its provider metadata dropped, losing the reasoning and editing the conversation prefix (a hard 400 where Anthropic enforces the prefix). A change to a model that does not read the source's format keeps upstream's conversion; the server never allows such a switch once the chat holds reasoning. Proven end to end by the product suite's `test_gateway_model_switch_e2e.py` (`test_an_effort_change_replays_earlier_thinking_unchanged`, `test_a_switch_to_a_model_that_reads_the_reasoning_replays_it_unchanged`, `test_a_switch_the_target_cannot_read_replays_thinking_as_text`) and `test_box_model_switch_e2e.py::test_a_switch_to_a_reader_keeps_the_reasoning_across_the_reopen_and_a_wake` (mock-e2e, bun-dev, through the real gateway). |
| **secrets by file, not by environment** — `packages/core/src/alkera-secrets.ts` (new, whole file; `alkeraSecret()`), `packages/core/src/flag/flag.ts` (`OPENCODE_CONFIG_CONTENT` / `OPENCODE_SERVER_PASSWORD`), `packages/opencode/src/server/auth.ts` (the `password` config), `packages/opencode/src/config/config.ts` (the injected-config read), `packages/opencode/src/provider/provider.ts` (`alkeraAdmittedProviders()`); test `packages/core/test/alkera-secrets.test.ts` | The loopback password and the injected config are read from the JSON file named by `ALKERA_SECRETS_FILE`: once, on first use, after which the file is removed and the variable deleted from this process's environment so no child learns where it was. Only `ALKERA_SERVER_PASSWORD` and `ALKERA_CONFIG_CONTENT` are taken from it, and its values win over the environment's. A named file that cannot be read or parsed fails every later read too, never falling back to the environment. With no `ALKERA_SECRETS_FILE` the names are read from the environment as before. | **Security.** An environment stays readable in `/proc/<pid>/environ` for the life of the process, by anything running as the same uid, and passes to every child; the injected config carries the chat's gateway token and the password guards an API that can run shell commands. Adapter side: `write_agent_secrets` / `agent_secrets` and the `ALKERA_SECRETS_FILE` entry in `build_agent_env` (`opencode_http.py`); the adapter no longer sets either name in the environment, so a binary without this patch would serve with no password; the adapter's anonymous probe (`require_agent_auth`, which demands a 401) refuses it at start. |

> **Bundled ripgrep (replaces the runtime download).** The `rg` download in
> `packages/opencode/src/file/ripgrep.ts` is now gated behind
> `ALKERA_DISABLE_RIPGREP_DOWNLOAD`. This was reverted once before because
> `glob`/`grep` hard-die (`glob.ts` wraps the lookup in `Effect.orDie`)
> wherever no `rg` exists — so the gate is **only** safe paired with a bundled
> rg, which we now have:
>
> - **Build:** `scripts/build-opencode-binary.sh::stage_ripgrep` downloads the
>   host-platform `rg` (pinned to the same `VERSION` + PLATFORM map as
>   `ripgrep.ts`) once at build time and stages it at
>   `apps/cli/dist/opencode/rg`; the release build bundles it into the
>   Nuitka onefile at `opencode/rg`.
> - **Resolve:** `alkera_cli.harness.opencode_binary` extracts/locates `rg`
>   next to the harness binary (`ResolvedOpencodeBinary.ripgrep_path`),
>   honouring `ALKERA_RIPGREP_BIN` as an override.
> - **Run:** the adapter's `_build_env` (`_apply_ripgrep_env`) prepends the rg
>   dir to `PATH` (opencode's `which("rg")` resolves OUR rg first) and sets the
>   gate — but only when `ripgrep_path` is present, preserving the download
>   fallback for a bare source checkout.
>
> If you ever bump `VERSION` in `ripgrep.ts`, bump it in `stage_ripgrep` too —
> the staged archive's inner dir name (`ripgrep-<VERSION>-<triple>/`) must match.

### Coupled env flags (set by the Alkera adapter)

The harness spawns opencode with these to suppress all non-gateway network
calls and pin deterministic on-disk names (see `_build_env` in
`apps/cli/alkera_cli/harness/adapters/opencode_http.py`). Their names were
renamed `OPENCODE_* → ALKERA_*` (see the env-var rename row above); unlike the
ripgrep/npm gates they need no separate behavioural edit, just the renamed read:
`ALKERA_DISABLE_MODELS_FETCH`, `ALKERA_DISABLE_SHARE`,
`ALKERA_DISABLE_AUTOUPDATE`, `ALKERA_DISABLE_LSP_DOWNLOAD`,
`ALKERA_DISABLE_CHANNEL_DB`. The model catalog falls back to the build-time
`OPENCODE_MODELS_DEV` snapshot compiled into the binary (a build define, not a
runtime env var — left as-is), so no models.dev fetch is needed.
`ALKERA_DISABLE_RIPGREP_DOWNLOAD` is set too, but **conditionally**
— only when a bundled `rg` is on `PATH` (see the bundled-ripgrep note above);
that's the one flag the adapter must NOT set blindly, or glob/grep break where
no rg exists.

`ALKERA_DISABLE_NPM_INSTALL` + `ALKERA_PURE` close the runtime npm installer
(the `@npmcli/arborist` → `registry.npmjs.org` path). The first is the master
kill-switch gated at the `Npm` chokepoint in `packages/core/src/npm.ts`; the
second skips config-declared external plugins so opencode never tries to resolve
them. Together they neutralise the `@opencode-ai/plugin` config-dep install,
external plugins, non-bundled provider SDKs, and the edit/write formatters — none
of which the Alkera harness uses. The **airgap e2e** (`apps/cli/tests/
test_opencode_airgap_e2e.py`) routes all non-loopback egress through a deny-all
proxy and asserts ZERO external calls during a real turn, which is the durable
guard against any of these gates regressing.

> The original single patch (`plan_present`) is the canonical example of the
> surgical style; the product-name and no-phone-home patches above follow it.

## Editing files here

Any change to a file under `vendor/opencode/` is a normal git change.
Just edit + `git add` + `git commit` as usual; no submodule dance, no
detached pointers.

## Pulling upstream

```bash
make opencode-fetch-upstream   # show what's new (no merge yet)
make opencode-bump              # subtree-pull, squashed
```

`opencode-bump` runs `git subtree pull --prefix=vendor/opencode
opencode-upstream main --squash`. Conflicts with alkera-local patches
resolve the same way any merge conflict does. After bumping:

```bash
cd vendor/opencode && bun install
make lint && make typecheck && make test
make e2e
```

## Listing alkera-local patches

```bash
make opencode-show-patches
```

Surfaces every commit on `vendor/opencode/` from this repo's history —
the inverse of `git log --grep` against upstream-only commits.

## Don't reintroduce the submodule

Pre-2026-06 this directory was a `git submodule`. The migration to a
subtree was a one-time operation (sweep G1-G4); DO NOT add it back to
`.gitmodules` or detach into a separate fork. The point of subtree is
that alkera's history holds everything, including upstream + patches.

## Build targets

- `make opencode-binary` — Nuitka-compiles the opencode source here
  into the bundled native binary the daemon ships.
- `make e2e` — spawns opencode from source via `bun run src/index.ts
  serve` against a scripted mock OpenAI provider (no Nuitka build
  needed for this).
