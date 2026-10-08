# Databench agent guide

The working guide for everyone who changes this repository, people and coding agents alike. Codex reads `AGENTS.md`; Claude Code reads `CLAUDE.md`, which imports this file. Read it before proposing commands or code changes. Personal rules go in an uncommitted `CLAUDE.local.md` or `AGENTS.local.md`.

## Principles for placing and changing code

Priority when they conflict: **correctness > simplicity > readability > testability > type safety > performance.** Each rule names the gate that enforces it. "(no gate)" means review enforces it until one exists.

1. **One owner per fact.** Every limit, factor, ladder, vocabulary, URL and format lives in one module. Other code imports it; other languages get it generated or are held to it by a shared vector file. A second copy is a bug. (web `apps/web/src/tests/architecture/spelledOnce.test.ts`)
2. **Move, then use.** Before writing logic that exists elsewhere, move it to its owner in its own commit, then call it. Never copy and adjust. (review)
3. **Where a module goes.** One runtime caller means that app. Several runtime callers (backend, worker, CLI, gateway) means an `alkera_core` sub-package whose `__init__` is its public contract. (root `.importlinter`: core never imports an app, apps never import each other)
4. **Layers point down.** Routes parse, call one service, and shape the response. Services hold the logic and import no FastAPI. `alkera_core` never imports an app. CLI libraries import no Typer, Rich, daemon or UI. The daemon and the CLI commands are façades. (no gate yet)
5. **Import the contract, not the internals.** Cross-package imports go through the package `__init__`; never import another module's `_private` name. (`test_files_import_hygiene.py`, `test_backend_architecture.py`; allowlists may only shrink)
6. **The server decides, the UI renders.** Controls are driven by `can_*`, `allowed_actions` and verdict fields from the policy that enforces the write. The UI never compares role strings or ids and never computes a limit. (`test_files_authz_hygiene.py`, `apps/web/src/tests/architecture/roleBranches.test.ts`)
7. **No hidden cycles.** A function-level import that dodges an import cycle is a design bug; fix the layering. A lazy import needs a reason (an optional dependency, startup cost). (function-level import ratchets in `test_backend_architecture.py` and `test_cli_architecture.py`)
8. **Small units.** Aim for modules under 1,500 lines and classes under 800. A file over budget may only shrink. (size ratchets in the same two tests)
9. **Refactor first, then change.** Before adding code, name what should be deleted, moved or restructured. A purely additive change to a mature area is suspect. Moves and fixes never share a commit. When a reviewer says the approach is wrong, stop patching, name the boundaries to delete, move or rebuild, and replace against a stated contract. (review)
10. **Every rule ships with its gate.** A new architectural rule lands here together with the test, lint or contract that enforces it. A rule without one is marked "(no gate)". (review)
11. **Extend by registration.** Optional behaviour (a router, a job family, a compute provider, a CLI plugin, a meter) registers through an `ExtensionPoint` from `alkera_core.extensions`. A point is ordered, refuses duplicates and freezes on first read. Every point has a default, and the app boots with nothing registered. (`packages/api-core/tests/test_extensions.py`)

Layering is enforced by `make lint-imports` (the root `.importlinter` plus `apps/backend/.importlinter` and `apps/cli/.importlinter`) and the architecture tests above. Their allowlists hold today's violations and may only shrink.

## Engineering rules

- **Be an engineer, not a transcriptionist.** Decompose the goal, research unfamiliar domains, challenge weak requirements, and choose the simplest correct design.
- **Flat, explicit code.** Guard clauses, explicit data flow, precise names. Avoid three or more levels of nesting, boolean-flag state machines, vague helpers and single-call wrappers.
- **Typed shapes.** For a dict with known keys in Python, use a `TypedDict`, a dataclass or a Pydantic model.
- **Stop and hand off.** If a reviewer calls the approach wrong, stop editing it and propose the replacement first. If the same visual or behavioural issue is rejected twice, replace the underlying abstraction instead of tuning values. Passing tests never override a reviewer's judgement of how something looks or behaves.
- **Docs move with the code.** If behaviour, interfaces, routing or architecture change, update the owning README or this guide in the same change.

## Writing for people

These rules cover everything a person reads: UI copy, error and CLI messages, emails, docs, commit and PR text.

- **No em dashes.** Use a comma, parentheses, or two sentences.
- **Few colons.** Keep one only when it does real work (a label before a value, a time, a list).
- **No filler.** No motivational, reassuring or explanatory padding, no "seamlessly / robust / powerful", no summary that repeats what was just said.
- **Write for where it is read.** Look at the screen, the neighbouring labels and the reader's state first. A toast, a button and a doc page need different words.
- **Plain and specific.** Name the actual thing, limit and next step.
- **Sentence case** for headings, labels, buttons, menu rows, column headers and messages. A sentence built from a variable fragment still starts with a capital.

## Verification

Every behaviour a person would otherwise check by hand should be pinned by an automated test. Design code so a test can reach it, write the unit and integration tests, and extend the end-to-end suites when you touch a path they cover.

### 1. Write tests for everything you create

| If you add or change | Write a test under |
| --- | --- |
| A FastAPI route (`apps/backend/backend/api/routes/<domain>/*.py`) | `apps/backend/tests/test_<resource>.py` with `httpx.AsyncClient` and `ASGITransport` (see `test_health.py`) |
| A service (`apps/backend/backend/services/<domain>/*.py`) | `apps/backend/tests/test_<service>.py`, against a real async session |
| An ORM model (`packages/api-core/alkera_core/models/`) | A round-trip test in `packages/api-core/tests/` and an integration test in `apps/backend/tests/` |
| A Pydantic schema (`packages/api-core/alkera_core/schemas/`) | The mirrored module under `packages/api-core/tests/schemas/`, round trip and validation |
| A worker job (`apps/worker/worker/{tasks,activities,workflows}/<family>.py`) | `apps/worker/tests/test_<family>.py` for the async core, and `test_<family>_workflow.py` through a real Worker (`@pytest.mark.temporal`, the `temporal_worker` fixture) |
| A CLI command (`apps/cli/alkera_cli/commands/*.py`) | `apps/cli/tests/cli/` with `typer.testing.CliRunner` (see `test_cli.py`) |
| A React component or page | The mirrored path under `apps/web/src/tests/` (Vitest and Testing Library, `@/` imports, `QueryClientProvider` and `MemoryRouter`, `fetch` stubbed with `vi.stubGlobal`) |
| A React Query hook in `apps/web/src/api/*.ts` | `apps/web/src/tests/api/`, through a component, with `fetch` mocked |
| An SDK helper (`packages/py-sdk`, `packages/ts-sdk`) | A test beside the source |
| A bug fix | A regression test that fails without the fix and passes with it |
| An authorization policy, or a route moved onto `backend.authz.enforce()` | Every branch in `packages/api-core/tests/authz/test_authorize.py` (each required attribute removed and mistyped), plus a route test asserting the status and the `authz.decision` row (see `apps/backend/tests/test_authz_exemplars.py`) |

No test is required for pure config, documentation, `__init__.py` re-exports or generated artifacts.

#### What counts as a test

- **Behaviour test** (what we write). It drives the code through its public surface and asserts what a caller can observe: return values, persisted state, emitted events, HTTP status and body, raised errors.
- **Resistance to refactoring.** A behaviour test stays green when the implementation changes and the behaviour does not, and goes red when the behaviour breaks.
- **Change-detector test** (forbidden). It pins a private helper's name, a mock's call count or order, or restates the code line by line.
- **Tautological test** (forbidden). It cannot fail: the expected value comes from the code under test, it asserts what a mock was just told to return, or it checks only truthiness. Prove a test is not tautological by showing it fail when the fix is removed or inverted.

Hold every test to this bar:

- Test observable behaviour. Pin an internal invariant only when that invariant is the point (for example "an invalid mutation leaves the bytes on disk unchanged").
- Exhaust the cases: every error path, every boundary, and the negative cases that catch over-eager code.
- Parametrize equivalent cases with `pytest.param(..., id="...")`. Use `pytest.raises`, `tmp_path`, `monkeypatch` and fixture factories.
- Prefer real collaborators. The suite runs against real Postgres, real subprocesses and real TCP. Fake only what is expensive, nondeterministic or costs money: the LLM provider, SMTP, the clock, randomness.
- Build data with the factories (`seed()`, `org_admin` and friends), not hand-rolled rows.
- Assert against values worked out by hand, never values produced by the function under test.

### 2. Design for testability

Every external dependency sits behind a seam a test can substitute. Route through the existing seams.

| Dependency | Seam |
| --- | --- |
| Outbound HTTP to an LLM provider (gateway) | `transport_factory_override`, swapping in a `MockTransport` that replays scripted SSE. Real-wire variant: the localhost mock servers in `apps/cli/tests/_mocks/` |
| A FastAPI route | `httpx.AsyncClient` with `ASGITransport` (the `client` fixture). For real TCP and disconnects, the `live_gateway` fixture runs uvicorn on an ephemeral port |
| A coding-agent subprocess | `adapter_factory` with `FakeAdapter` (`alkera_cli/harness/_fake.py`), or a scripted mock provider for mock e2e. Inject provider config through `SessionConfig.harness_native`, never global env |
| SMTP | the `monkeypatch_email_send`, `monkeypatch_verification_send` and `monkeypatch_password_reset_send` fixtures |
| Auth, daemon and CLI state on disk | `ALKERA_HOME` points `~/.alkera/` at a temp dir. Never call `Path.home()` directly (use `alkera_cli/host/paths.py`) |
| The opencode or claude binary | `ALKERA_OPENCODE_BIN` and `ALKERA_CLAUDE_BIN` |
| An OAuth provider | `OAUTH_MOCK_ENABLED=true` |
| Retry and backoff timing | inject the sleep so a fixture can zero it |
| Randomness and the clock | take them as parameters. Any time logic gets a `freezegun` test that crosses the boundary it cares about (midnight, week roll, TTL expiry). A frozen block that opens a database connection needs `freeze_time(..., real_asyncio=True)`. Never freeze around a poll-until-deadline loop |

Design rules:

- Split the pure core from the I/O shell, so the logic is callable with plain data in and out (see `apps/cli/alkera_cli/harness/adapters/opencode_translate.py`).
- Make state injectable through settings objects and constructor arguments, not module globals or scattered `os.environ` reads.
- Library first, façade thin. Logic lives in a library that the CLI and the daemon both call. See `apps/cli/alkera_cli/harness/README.md`.
- If you cannot find a seam, add it first.

### 3. Test tiers

The default `pytest` run (`make test`) is free, key-less and deterministic. The opt-in tiers need a binary or a real provider key and run through their own target.

| Tier | Marker | Cost | Runs via | Proves |
| --- | --- | --- | --- | --- |
| Normal | none | free | `make test` | a function, route, service, model or component behaves. Most tests live here |
| Gateway e2e | `billing_e2e` | free | `make test` | the gateway over real TCP with a mocked upstream: streaming, disconnect, metering |
| Mock e2e | `opencode_e2e`, `claude_e2e` | free | `make e2e` | a real opencode or claude subprocess against a scripted mock provider |
| Live e2e | `live_provider` | small real cost | `uv run pytest -m live_provider` | the same harness against real Anthropic, OpenAI or Bedrock. Skips without keys |

- A harness adapter change gets a mock e2e case. If it changes what the provider sees, also a `live_provider` case.
- A gateway, codec or model-catalog change gets a `billing_e2e` case and a `live_provider` case.
- Keep mocked and live tiers in step. When you add a live case, add the matching mock case so the free suites catch the regression on every change.

**Tests run only against a disposable database.** The root `conftest.py` aborts when `DATABASE_URL` names a database outside the allowlist in `alkera_core.db.testing` (`alkera_test*`, `alkera_lane_*`, `alkera_gw[0-9]*`, `alkera_migtest_*`, `*_test`, `test_*`), unless `ALKERA_TEST_ALLOW_DB` names it exactly. Every `make` pytest target provisions its own test database.

**Tests use their own Files bucket.** The root `conftest.py` points `FILES_STORE_BUCKET` at a fresh per-run bucket. Ask for the `files_test_bucket` fixture; never name a bucket.

**Tests never touch your local boxes.** The root `conftest.py` sets a throwaway `COMPOSE_PROJECT_NAME`, so the `localdev` compute provider sees only test containers.

### 4. Windows

The CLI and the daemon ship for Windows, so the suites must pass there too.

- Write cross-platform by default: `pathlib`, argument lists instead of shell strings, `resolve()` before comparing paths, explicit `encoding="utf-8"`, and the existing seams (`alkera_core.process.process_alive`, `harness/spawn.py`) instead of `os.kill`, `signal` or `Path.home()`.
- A test that needs POSIX (bash, signals, mode bits, symlinks, Unix sockets) skips on Windows with a named marker and a reason. Mark the test, not the module. Split a single POSIX-only assertion into its own test.
- A test of Windows behaviour runs only on Windows. When the mechanism differs per platform, write one marked test per platform.
- `make` and the `ops/scripts/*.sh` helpers are macOS and Linux tools; their tests skip on Windows.

### 5. When to verify, and what to run

Run checks after a logical unit of work and before reporting it done, not after every edit. Run long suites in the background and do not edit code a running suite exercises.

```bash
# Python
uv run ruff format <files you touched>
uv run ruff check . --fix
uv run mypy apps packages
uv run pytest

# TypeScript
pnpm -r --if-present lint
pnpm -r --if-present typecheck
pnpm --filter @alkera/web test -- --run

# API surface, daemon methods, agent tools or email templates changed
make gen-sdk gen-open-subset gen-email-snapshots

# ORM model or migration changed
make migrate && make migrate-check
```

Run these checks locally before you push:

| Check | Command |
| --- | --- |
| `lint` | `make lint` |
| `typecheck` | `make typecheck` |
| `test` | `make migrate && make migrate-check && make test` (Postgres up with `make infra-up`) |
| `drift` | `make gen-sdk gen-open-subset gen-email-snapshots`, then `git diff --exit-code` and an empty `git status --porcelain` |
| `e2e` | `make e2e` (needs bun on PATH and `cd vendor/opencode && bun install`) |
| `dco` | every commit carries `Signed-off-by` (see CONTRIBUTING.md); `uv run python scripts/check_dco.py` checks a range |

The drift check covers every generated artifact: `packages/shared-openapi/openapi.json`, `packages/ts-sdk/src/schema.d.ts`, `packages/py-sdk/alkera_sdk/_generated/`, the tool manifest (`packages/shared-openapi/tool-manifest.json` and `packages/chat-model/src/generated/`), the open daemon schema and tool manifest (`packages/shared-openapi/open/`) and the email snapshots (`packages/api-core/tests/fixtures/email/*.html`). Regenerate and commit them in the same change as their source. Never hand-edit them.

New dependencies must pass `uv run python scripts/licence_audit.py`.

If a check fails, fix the cause. Do not silence it with `# type: ignore`, `# noqa` or an ESLint disable unless you can say why the tool is wrong in that case.

When you finish, state which checks you ran and their results, and say why you skipped any.

## Environment

- **Python.** One uv workspace with `.venv/` at the root. Always `uv run <cmd>`. Never the system Python, `python3` or `pip`. Add a dependency by editing the right `pyproject.toml` and running `uv sync`, never `uv pip install`.
- **Node.** One pnpm workspace. Never `npm` or `yarn`. The Node version is in `.nvmrc`.
- **Docker.** Used for local infrastructure (Postgres, Temporal, Mailpit, SeaweedFS) and the local compute box. App code runs on the host.

## Make targets

The Makefile is the source of truth. Use it even for one-off runs.

```
make bootstrap              # uv sync + pnpm install + .env from .env.example
make infra-up / infra-down  # Postgres, Temporal, Mailpit, SeaweedFS, test databases
make migrate                # alembic upgrade head
make seed                   # dev seeds (admin@example.com / admin)
make dev-all                # backend + worker + web + gateway, one terminal
make dev-box                # a local compute box (a Docker container, chats under gVisor)
make urls                   # this checkout's dev URLs
make test / lint / typecheck
make gen-sdk                # OpenAPI, both SDKs and the tool manifest
make docker-images          # backend, worker, gateway and web images
```

Each git worktree gets its own Docker project, containers, volumes and ports, generated into a gitignored `.env.workspace`. Do not assume fixed ports; run `make urls`.

## Repository layout

```
apps/
  backend/            FastAPI HTTP layer: routes, services, alembic        (alkera-backend, `backend`)
  worker/             Temporal workflows, activities, schedules            (alkera-worker, `worker`)
  model-gateway/      LLM gateway: provider codecs, routing, streaming     (alkera-model-gateway, `model_gateway`)
  web/                Vite + React 19 web app                              (@alkera/web)
  cli/                Typer CLI, daemon, harness, box supervisor           (alkera-cli, `alkera_cli`, command `alkera`)
packages/
  api-core/           Shared libraries: settings, db, models, schemas, authz, Files, compute, email (alkera-core, `alkera_core`)
  alkera-py/          The `alkera` Python API used inside notebooks (standard library only)
  alkera-notebook/    The notebook format, reader and writer
  alkera-kernel/      The notebook kernel
  ui/                 Shared React UI                                      (@alkera/ui)
  chat-model/         Conversation model and generated tool schemas        (@alkera/chat-model)
  notebook-ui/, widgets/, chart-guard/   notebook rendering
  py-sdk/, ts-sdk/    Generated API clients                                (alkera-sdk, @alkera/sdk)
  shared-openapi/     Generated OpenAPI document and tool manifest
  test-support/       In-memory fakes for the test suites, never shipped
vendor/
  marimo/             marimo, Apache-2.0, with fenced changes (see NOTICE)
  opencode/           opencode, MIT, with fenced changes (see NOTICE)
deploy/docker/        Compose files: local infrastructure and the reference self-hosted deployment
scripts/              Repository tooling (licence audit, DCO check, generators)
```

## Python

- The root `pyproject.toml` depends on every workspace member, so plain `uv sync` installs them all. Do not pass `--all-packages`.
- Shared shapes (settings, logging, db session, ORM models, schemas) live in `alkera_core` and are imported as `from alkera_core.config import settings`. Backend-only code lives in `apps/backend/backend/`.
- `mypy --strict` covers `apps/` and `packages/`. Type annotations are mandatory. FastAPI `Depends(...)` and `Query(...)` defaults are allow-listed in ruff's bugbear config.
- `apps/*/tests/` have no `__init__.py`; adding one breaks collection. Test file basenames must be unique across the repo.
- `from __future__ import annotations` at the top of every module except `__init__.py` and Alembic versions.

## env files

- Read in this order, a later file winning per key: `.env` (copied from `.env.example`, the committed defaults), `.env.workspace` (generated per checkout), `.env.local` (your own keys and overrides). They are for Python apps and scripts only.
- `apps/web/.env.example` is for Vite only.
- Never put a comment on the same line as a value; pydantic-settings keeps the trailing text.
- Every runtime setting is assigned in `.env.example` unless `scripts/check_settings_surfaces.py` names it dev-only (`make lint` checks it).
- Never commit `.env`, `.env.workspace` or `.env.local`.

## Frontend (`apps/web`)

- Vite, React 19 strict, React Query, React Router 7, and `@alkera/ui`. No third-party component framework without an architecture decision. No Tailwind and no CSS-in-JS in the product UI.
- Route guards (`RequireAuth`, `RequireOrgAdmin`, `RequirePlatformStaff`, `RequirePlatformAdmin`) live in `apps/web/src/app/guards/`. A new page goes in `apps/web/src/pages/<domain>/`, is registered in `src/App.tsx` and linked from `src/app/nav.ts`.
- **Mutations never hand-wire cache invalidation.** `createQueryClient` (`apps/web/src/api/queryClient.ts`) installs a policy. A mutation that declares nothing invalidates every query on success. Narrow it with `meta: { invalidates: [keys] }`, or opt out with `meta: { invalidates: "none" }`. A 409 refreshes the declared slots. Tests that exercise a mutation build their client with `createQueryClient(...)`. Contract pinned in `apps/web/src/tests/api/queryClient.test.tsx`.
- **Every query key is spelled once**, in `apps/web/src/api/keys.ts`. `apps/web/src/tests/api/keys.test.ts` scans for inline keys.
- Response types come from `components["schemas"][...]` in `@alkera/sdk`, never hand-typed. Hooks live in `apps/web/src/api/<resource>.ts`; components call hooks, not `fetch`.
- UI implementation: align icons and text with a shared flex row (`align-items: center`, fixed icon cells, `gap`), never with transforms or pixel nudges. The parent owns vertical rhythm with `gap`. Styles are component-owned; split a stylesheet before growing it.
- Fonts: Newsreader (display), IBM Plex Sans (UI), IBM Plex Mono (code), self-hosted through `@alkera/ui/fonts`.

## Shared UI (`packages/ui`)

- Organised by concern (`theme`, `hooks`, `primitives`, `brand`, `auth`, `viz`, `chat`, ...). Import from `@alkera/ui` or a public concern barrel, never deep paths.
- A component moves here when a second surface needs it. Components are pure presentation: no `fetch`, React Query or router imports.

## Auth and organisations

- Cookie session (`alkera_session`, HTTP-only JWT) for the browser. The CLI signs in with the device grant (RFC 8628): it prints a code and a `/device` URL, and polls until someone approves. There is no loopback server. The token is saved to `~/.alkera/auth.yml` (mode 0600).
- `current_user` accepts the cookie or `Authorization: Bearer <jwt>`.
- Signup needs only email and password. `org_name` creates an organisation, `invite_token` joins one; sending both is a 400.
- Admin of a team is admin of every team below it. Membership rows are written for the leaf and every ancestor; always go through `services/org/memberships`, never write `TeamMembership` rows directly.
- Invitations to an email already in another organisation return 409. Tests patch email sending with `monkeypatch_email_send`.

## Authorization (`alkera_core.authz` and `backend.authz`)

- **One choke point.** A route resolves its facts and calls `backend.authz.enforce(request, db, ctx, action, resource, attrs)`, which returns the allow or raises the denial (403, a coded error, or an opaque 404). Policies are pure, one per `ResourceType`, in `packages/api-core/alkera_core/authz/policies/`. `authorize()` denies by default.
- **Every decision is recorded** as an `authz.decision` outbox row. Allows roll back with the request; denials commit in their own session. Only a policy's `audited_attrs` are recorded, and secrets can never be declared.
- **Who is acting.** `current_principal(request, db)` resolves every credential (cookie, bearer JWT, CI token, personal access token) into one `ActingContext`. Roles come from `role_resolver(...).for_team(team_id)`.
- The agent assertion headers are built only by `alkera_core.authz.headers.agent_headers(session_id)` and parsed only by `parse_agent_assertion(headers)`.

## Versioned persisted models

Anything written to disk or storage that outlives a process inherits `alkera_core.versioning.VersionedModel`, not bare `BaseModel`. HTTP request and response shapes stay on `BaseModel`.

- `VersionedModel` keeps unknown fields (`extra="allow"`), stamps `schema_version` from `SCHEMA_VERSION`, and runs `MIGRATIONS` keyed by the old version.
- When you change one: bump `SCHEMA_VERSION` (minor for an added optional field, major for a rename, removal or new required field). A major bump registers a pure dict-in, dict-out migration. Add a fixture of the new writer's output. Never delete old fixtures or migrations; the lineage test loads every historical fixture.
- Discriminated unions get a raw fallback variant (`make_unknown_tag_discriminator`). Experimental fields go in `metadata` first. Removal waits for a major version.
- A new persisted model starts at `1.0.0`, defaults every field it sensibly can, and gets a fixture and a lineage test.

## The `.alkera/` project directory

The per-workspace `.alkera/` directory is owned by `alkera_core.project.ProjectDirectory`. Never build its paths by hand.

```python
from alkera_cli.host.paths import project_directory

project = project_directory(workspace_root)
with project.chats().open(session_id) as chat:
    chat.append_event(...)
```

Opening a chat takes a PID-stamped lock; a second writer gets `LockHeldError`, and a dead holder's lock is reclaimed. Liveness goes through `alkera_core.process.process_alive`, never `os.kill(pid, 0)` (on Windows that kills the process). Writes use the atomic helpers in `alkera_core.atomic_io`; JSONL reads use `alkera_core.project.jsonl.iter_jsonl`, which skips a truncated last line.

## Database and migrations

- SQLAlchemy 2.0 style only (`Mapped[...]`, `mapped_column`, async sessions). The app uses the asyncpg URL, Alembic the psycopg URL, both from the env files.
- Add a model under `packages/api-core/alkera_core/models/`, re-export it from `models/__init__.py`, run `make migrate-create MSG="..."`, review the file, then `make migrate`.
- **Never ship the raw autogenerated file.** Autogenerate misses renames (it drops and recreates, losing data), server defaults, CHECK and primary key constraints, and data backfills; write those by hand. Name every constraint and index in the model. Renumber the file to the next `NNNN_slug.py` with sequential revision ids, write a descriptive docstring, and check `downgrade()`.
- `make migrate-check` must report "No new upgrade operations detected". Run it before you push.

## Harness (`alkera_cli.harness`)

- The same `HarnessRuntime`, `ChatSession` and `HarnessAdapter` back `alkera run` and the daemon's `harness.*` methods. The daemon adds no logic.
- The adapter contract and its invariants are in `apps/cli/alkera_cli/harness/README.md`. Read it before touching a harness.
- The agent subprocess is detached from the terminal's Ctrl-C and must never be orphaned: a Job Object on Windows, `PR_SET_PDEATHSIG` on Linux, and a startup orphan sweep on macOS that reaps only processes whose identity it confirms.
- `make e2e` runs a real opencode subprocess from `vendor/opencode/` against a scripted mock provider.

## Worker (Temporal)

- Every background job is a workflow (`apps/worker/worker/workflows/<family>.py`) running one activity (`activities/<family>.py`) that calls an async core (`tasks/<family>.py`). Job names, the four task queues (`money`, `email`, `sync`, `default`) and queue assignment live in `alkera_core.temporal.contract`. Families are discovered by module walk.
- Every activity has an explicit retry policy in `worker/temporal/retry.py`; a non-idempotent one gets `NO_RETRY`. Periodic jobs are entries in `worker/schedules.py`. The backend starts workflows only through `backend/services/infra/task_queue.py`.
- Run it with `python -m worker run [--queues ...]`. See `apps/worker/README.md`.

## FastAPI

- Route, service, model. Keep routes thin; logic goes in `apps/backend/backend/services/`.
- A new router goes in `apps/backend/backend/api/routes/<domain>/<resource>.py` and is mounted by the app factory.
- `/health/live` is always 200; `/health/ready` pings the database and returns 503 on failure; `/health/info` reports app, version, build and environment.

## Deployment

- Never hard-code a URL. Everything an operator may change goes through settings (`frontend_base_url`, `api_cors_origins`, `database_url`, `smtp_host`, ...). The web image reverse-proxies `/api` and `/health`, so the app stays same-origin.
- Do not assume Mailpit. All mail goes through the one send helper in `alkera_core.email`.
- `alkera_core.config._validate_production` refuses to boot `APP_ENV=production` with insecure defaults. A new production-required setting extends it, with a case in `packages/api-core/tests/test_settings.py`.
- Images are built with `make docker-images`. The reference deployment is `deploy/docker/compose.prod.example.yml`.
- The CLI resolves `ALKERA_API_URL` from the environment, then the system config file, then its default.

## Daemon protocol (`alkera serve`)

A client talks to `alkera serve` over stdio JSON-RPC with LSP-style framing.

- Add a method in `apps/cli/alkera_cli/daemon/` with Pydantic request and response models in `methods/<area>.py` and the `@method("area.name")` decorator. Daemon-to-client requests use `client_request("name")`; notifications use `@notification("name")`.
- When the daemon needs a sign-in it answers JSON-RPC error `-32001`.
- A slow answer is never a reason for a client to kill the daemon (a chat turn may run for hours); only a long silence or an exit is.
- Runtime state lives under `~/.alkera/` (`daemon/<pid>/`, `logs/daemon.log`, `auth.yml`).

## Git safety

- Never run a discarding command with a broad target: `git checkout -- .`, `git restore .`, `git reset --hard`, `git clean -f`, a bare `git stash`. Name the files.
- When `ruff format .` reformats files you did not touch, format only your own files instead of reverting.
- Commit before any merge, rebase or broad revert. Never force-push a shared branch or rewrite a pushed commit.

## Committing

- Sign off every commit (`git commit -s`). Unsigned commits are not merged.
- Stage specific files, never `git add -A` or `git add .`.
- Subject is `<scope>: <Sentence-case subject>` with no trailing period, for example `cli: The daemon reconnects after a sleep`. Scopes: `cli`, `backend`, `worker`, `gateway`, `web`, `api-core`, `notebook`, `py-sdk`, `docs`, `test`, `ci`, `build`. Join at most three with commas.
- The body explains why. Describe the change itself; never cite private plans or ticket shorthand that a reader of `git log` cannot look up.

## Code style

- Python: full type hints and `from __future__ import annotations`. No speculative abstractions. Follow the existing patterns.
- Minimal comments. Docstrings on public modules and classes; inline comments only where behaviour would surprise a reader. A comment states the reason itself, not a reference to a plan.
- UI copy states what the reader needs and nothing else. One sentence for an empty state, a dialog or a toast. Sentence case everywhere.
