# `alkera_cli.harness`

The in-process library that drives external coding agents behind one async contract, `HarnessAdapter`. There are two implementations: opencode (`adapters/opencode_http.py`, the default, vendored under `vendor/opencode/` from [sst/opencode](https://github.com/sst/opencode), MIT) and Claude Code (`adapters/claude_agent.py`). Another agent plugs in the same way. The CLI and the daemon (`alkera serve`) both call this library directly; the daemon's JSON-RPC methods are a thin façade with no logic of their own.

## Architecture

```
HarnessRuntime ─► ChatSession ─► HarnessAdapter (one per chat)
                                  ├── start() / stop()
                                  ├── send_prompt()
                                  ├── cancel()
                                  ├── resolve_permission()
                                  ├── answer_question() / reject_question()
                                  ├── subscribe() → AsyncIterator[Event]
                                  └── native_state()
```

`HarnessRuntime` is scoped to a workspace. It owns the chat store (`.alkera/chats/`), the per-chat locks and the adapter factory. Each active chat has one `ChatSession`, and each `ChatSession` one `HarnessAdapter`.

## Adapter contract

Every adapter must hold these invariants.

1. **Streaming closure.** Every `PartStarted` is followed by exactly one `PartCreated` (the final form), or the turn ends with `TurnFinished{stop_reason: "cancelled" | "error"}` after the adapter has synthesized a closing `PartCreated` for every part still open. The UI never sees an orphan `PartStarted`. The reference is `OpencodeHttpAdapter._synthesize_close_events()`, and `apps/cli/tests/harness/test_harness_crash_invariant.py` tests it.

2. **Turn closure.** Every `TurnStarted` is followed by exactly one `TurnFinished`. Synthesize it on error if the agent does not.

3. **Every permission goes to the broker.** No agent-side heuristic decides anything. Every tool that needs a permission, read-only ones included, is routed to the Alkera broker: Claude's `_PERMISSION_SETTINGS` sets every tool to "ask", and opencode's `_OPENCODE_PERMISSION_ASK` is `"*": "ask"`. Each adapter attaches a typed `ActionDescriptor` to `PermissionRequest.subject` (bash through the tree-sitter classifier in `plugins/plugin_base/permissions/bash.py`, file and network tools through a static map).

   The runtime's permission loop is the single place policy is applied. `request_auto_decision` runs the effect-aware engine (the mode, the rules, and the floor for destructive and egress actions), and only a real prompt reaches a person. A read is allowed without asking, `rm -rf` prompts under `default` and `auto`, and an editor connected to the daemon sees only real prompts. The modes are `read_only`, `default`, `auto`, `plan` and `bypass`. The floor applies in every mode except `bypass`, which runs everything without asking, even past the floor and an explicit deny rule. In `auto` mode, recoverable writes are checked by an LLM judge (`safety_judge.py`), and a judge that cannot run stops the turn. Every decision is appended to `.alkera/decisions.jsonl`.

   A knowledge note that stays on this machine (`context_note`, `context_edit`) has `Effect.MEMORY`. It is the agent's memory, not a change to the workspace or a data system, so every mode admits it, `read_only` and `plan` included, and only a deny rule refuses it. Sharing a note with the team is an `EGRESS` decision, and the modes that allow no changes keep the note private instead. Every mode's steering quotes `KNOWLEDGE_IS_MEMORY` so the model does not read "read-only" as "don't save what you learned". A subagent shares its parent's broker and is limited by its agent definition's `tool_scope`.

4. **Native state pinning.** Override `native_state()` to return the durable handles the runtime should persist in `ChatManifest.harness`, so the next `open_chat` resumes the same agent session (for example, opencode's session id). The default returns `{}`. On `start()`, read `SessionConfig.harness_native` first and attach to the pinned handle. If it is gone, raise `HarnessUnavailableError` rather than silently starting a new one.

5. **One prompt at a time.** The runtime serializes `send_prompt` per chat with the chat lock, so an adapter may assume one outstanding prompt.

6. **Cancellation safety.** `stop()` cancels and awaits every asyncio task the adapter owns (SSE pumps, heartbeats, process watchers) before it returns.

## Steering with synthetic parts

Per-turn directives, such as the plan-mode rules, are sent as a leading text part with `synthetic=true`. opencode uses the same mechanism for its own `<system-reminder>` blocks. A synthetic part:

- sits at the start of the user message, where the model weighs it, unlike `body["system"]`, which is appended to the end of the system block;
- is skipped by the renderer, so steering never shows in the chat;
- does not collide with opencode's own reminder injection.

Do not also set `body["system"]`; it repeats the directive and adds prompt cost. See `OpencodeHttpAdapter.send_prompt`.

## Event order

Without a cancellation, an adapter publishes each turn's events in this order:

```
SessionStatusChanged(status="running") → TurnStarted → MessageCreated
  → PartStarted (text) → AgentMessageChunk × N → PartCreated
  → [tool call, permission, question, …]
  → MessageCompleted → TurnFinished → SessionStatusChanged(status="idle")
```

On a cancellation or crash, the adapter closes any open parts, then emits `TurnFinished{stop_reason}` and the status change, in that order.

The adapter enforces that tool events arrive before idle. opencode itself does not guarantee it: its session status can report idle before a tool's final `message.part.updated`. The opencode adapter holds such an idle until the tool's closing update arrives, and a watchdog (`IDLE_TOOL_CLOSE_GRACE_SECONDS`) synthesizes the closure if that update was lost to an SSE reconnect. A consumer that stops reading at idle therefore never misses a tool's completion.

The hold is bounded both ways. A closure the translator has taken but not yet published still counts as in flight, and the watchdog waits for it. Once nothing is open or in flight, the watchdog releases the idle itself, because a turn that never reports idle is worse than an idle out of order. See `OpencodeHttpAdapter._publish_ordered` and `apps/cli/tests/harness/test_harness_idle_tool_ordering.py`.

## Writing a new adapter

1. Subclass `HarnessAdapter`.
2. Pick a short opaque `name` (for example `"oc"` or `"cc"`).
3. Declare the capabilities UIs care about, such as `frozenset({"fork", "resume", "share", "summarize"})`.
4. Implement the abstract methods.
5. Override `native_state()` if the agent has durable handles.
6. Register it in the adapter factory, and cover it in the style of `_fake.py` (`FakeAdapter`) so the runtime tests exercise it.
7. Keep the translation from native events to the shared event model in its own module, as `adapters/opencode_translate.py` does, so it can be tested with plain data and no process, HTTP or SSE.

## Tests a new or changed adapter needs

An adapter is a streaming pipeline across processes, and a unit test alone will not catch a spawn, streaming or permission regression. Ship all four tiers:

1. **Translator unit tests.** Every native event mapped, including the `permission_kind` table, with parametrized cases. Pattern: `apps/cli/tests/harness/test_harness_opencode_translate.py`.
2. **Crash and cancel invariant.** Follow `apps/cli/tests/harness/test_harness_crash_invariant.py`: build the adapter with open parts, run its close path, and assert no orphan `PartStarted` and exactly one `TurnFinished`.
3. **Mock end-to-end (free, required).** A real agent subprocess against a scripted mock provider, marked `@pytest.mark.opencode_e2e` or `claude_e2e` (or a new marker registered in the root `pyproject.toml`). Use the mock servers in `apps/cli/tests/_mocks/` and the runners in `apps/cli/tests/_helpers/`, and inject provider config through `SessionConfig.harness_native`, never the global environment. CI runs this tier on every pull request (`make e2e`).
4. **Live provider (`@pytest.mark.live_provider`).** The same flow against the real provider, skipped when its credentials are unset. Add the matching mock case in the same change so the wire shape is checked on every pull request.

## Spawning the agent

Every child process starts through `alkera_core.process` (`spawn`, `spawn_async`, `run`, `run_async`) from a `SpawnSpec` whose defaults are the safe ones: stdin is `/dev/null`, every descriptor not handed over is closed, the child gets its own session (out of reach of the terminal's Ctrl-C), and it is bound to this process's lifetime (`setpriv --pdeathsig` or `PR_SET_PDEATHSIG` on Linux, a kill-on-close Job Object on Windows, the startup orphan sweep on macOS). `kill_tree` and `kill_tree_async` end a child and everything it started. `harness/spawn.py` adds one thing: a sandbox wrapper runs inside the death-signal launcher (`sandbox_spec`, `sandbox_command`), so the whole chain ends with the daemon. `packages/api-core/tests/test_process_seam.py` fails on any process started another way.

## Finding the opencode binary

`opencode_binary.py:resolve_opencode_binary()` picks the executable the opencode adapter runs. The first of these that succeeds wins:

1. **Override.** `ALKERA_OPENCODE_BIN=<path>`, for tests and custom builds. Never set automatically.
2. **Bundled.** A packaged CLI that embeds the agent at `runtime/alkera-agent` (with a `runtime/.alkera-sha` fingerprint) extracts it once to `~/.alkera/cache/runtime-<sha>/alkera-agent`, atomically, and reuses that copy.
3. **Staged.** `apps/cli/dist/opencode/alkera-agent`, built by `make opencode-binary` for the host platform. The same script stages a pinned `rg` beside it, so opencode never downloads ripgrep at run time.
4. **Source.** `bun run vendor/opencode/packages/opencode/src/index.ts`. No build step, only `bun` on PATH. `make e2e` uses this.

The resolver never falls back to an `opencode` on PATH, so a separately installed version cannot take over a chat.

## Models come only from the gateway

A chat runs only on a model the Alkera model gateway serves, named by a provider the runtime's gateway config declares (`alkera-anthropic` or `alkera-openai`, pointed at the gateway with the user's token, or a test's mock provider declared the same way). There is no default model anywhere in the stack, so a chat never runs on a model nobody chose, outside the organisation's model policy. Three checks enforce it, and each refuses on its own:

1. **The server resolves the model when the chat is created** (`chat_catalog.default_pin_for`). A create that names no model gets the one the composer would preselect: the saved preference, else the catalog's first selectable model. If none can be resolved, the create is refused with the reason (`model_catalog_unavailable` or `no_model_offered`). A chat with no pinned model has every turn refused, with the reason shown in the chat, until someone picks a model.
2. **The adapter refuses the turn** (`OpencodeHttpAdapter._admit_turn_model`). The injected config (`_OPENCODE_DEFAULT_CONFIG`) names no model and no provider. The model a turn would run on must name a provider in the injected config's `provider` map, or the turn is refused before any request reaches the agent: a `status="error"` with the reason is published, and `HarnessModelError` is raised.
3. **The vendored provider registry admits only declared providers** (`vendor/opencode/.../provider/provider.ts`, an `ALKERA EDIT`). When opencode runs under the harness, every built-in provider is disabled whatever credentials the machine holds. A model on an undeclared provider is refused, and a config with no `model` is refused instead of picking one.

Tests: `apps/backend/tests/test_chat_model_and_mode.py` (the server), `apps/cli/tests/cloud/test_cloud_mirror_model.py` (the box), `apps/cli/tests/harness/test_harness_opencode_model_gate.py` (the adapter) and `apps/cli/tests/e2e/test_opencode_gateway_only_e2e.py` (the registry, through a real opencode). A new way to open a chat must go through the first check, a new harness must implement the second, and none of them may gain a default model.

## Every turn names its model and effort

The model and effort can change while the agent runs (a pick in an open chat, an effort change, a change the box picks up from the chat row). The agent never re-reads the pin, so each turn carries it.

1. **`ChatSession` fills in the turn** (`harness/turn_model.py`). A caller that names a model is obeyed. Otherwise the turn carries the manifest's pin and effort as they are when it is sent.
2. **The adapter sends what the turn names.** opencode takes it nested (`model: {providerID, modelID}`, with the effort in the model key as `<id>::<effort>`), and its schema silently drops top-level ids. An effort is applied to the turn's own model, never the one the agent was spawned with. The Claude agent changes model by starting the client again on the new model and resuming the conversation, because the SDK's `set_model` validates with a non-streaming request the gateway does not serve (see `adapters/claude_model.py`).
3. **A host restarts an agent that cannot serve the model.** `HarnessAdapter.serves_model` says whether the running agent can answer on a model. The box spawns opencode knowing only the pinned model, so before a turn it checks `ChatSession.serves_pinned_model()` and reopens the agent when needed (`cloud/model_follow.py`).
4. **What ran is recorded.** opencode's assistant `MessageCreated.model` and the Claude agent's `TurnStarted.model` carry `{provider_id, model_id, effort?}` from what the turn ran. The server stores it with the message, and a change of model or effort becomes a `model.changed` transcript row naming who made it.

An effort change is not a model change. The vendored `message-v2.ts` compares model keys without the `::effort` suffix, so earlier signed thinking is replayed unchanged after an effort change. A different base model still converts it to text, as upstream does.

Tests: `apps/cli/tests/e2e/test_claude_model_switch_e2e.py` asserts what the mock provider received.

## Where to look

| For | File |
| --- | --- |
| The abstract adapter | `adapter.py` |
| Permission modes | `permission_mode.py` |
| Permission routing | `permission_broker.py` |
| Question routing | `question_broker.py` |
| Per-adapter event bus | `event_bus.py` |
| `FakeAdapter` for tests | `_fake.py` |
| `HarnessRuntime` and `ChatSession` | `runtime.py` |
| The opencode adapter | `adapters/opencode_http.py` |
| The opencode event translator | `adapters/opencode_translate.py` |
