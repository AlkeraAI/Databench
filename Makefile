# ============================================================
# Developer entrypoint
#
# Usage: `make` or `make help` to see targets.
# All targets are idempotent and safe to re-run.
# ============================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Per-worktree dev isolation. Load order (later wins for overlapping keys):
#   1. .env            committed defaults (classic ports)
#   2. .env.workspace  generated per-worktree infra (ports, project name, DB/Temporal URLs)
#   3. .env.local      optional, the developer's own overrides, wins
# .env.workspace is generated on demand (rule below, via ops/scripts/
# workspace-env.sh): `main` keeps the classic ports, every other worktree gets
# an isolated high-range block. It carries only infra keys, so it never shadows
# a secret, and .env.local is included last regardless.
#
# A checkout that carries this tree inside a larger one runs these targets
# with `make -C Databench` and passes its own root as ALKERA_ENV_DIR, so both
# read one set of files.
# alkera_core.env_files reads the same variable.
ALKERA_ENV_DIR ?= $(CURDIR)
export ALKERA_ENV_DIR
ifneq (,$(wildcard $(ALKERA_ENV_DIR)/.env))
  include $(ALKERA_ENV_DIR)/.env
  export
endif
-include $(ALKERA_ENV_DIR)/.env.workspace
export
ifneq (,$(wildcard $(ALKERA_ENV_DIR)/.env.local))
  include $(ALKERA_ENV_DIR)/.env.local
  export
endif

# How Python tools run. A composing checkout points this at its own workspace
# (`$(UV_RUN) --project <its root>`), so one venv serves both trees.
UV_RUN ?= uv run
# The apps these targets start. A composing checkout names its own composition
# roots here; the open ones install no extension.
BACKEND_APP ?= --factory backend.app_factory:create_app
GATEWAY_APP ?= --factory model_gateway.app_factory:create_app
WORKER_MODULE ?= worker

# OrbStack project group: `alkera` on main, `alkera-<branch-slug>` elsewhere.
# Drives container/volume namespacing in deploy/docker/compose.local.yml.
COMPOSE_PROJECT_NAME := $(shell bash ops/scripts/workspace-env.sh project-name)
export COMPOSE_PROJECT_NAME

# Generate .env.workspace on first use, and refresh it whenever the generator that
# produced it has changed. GNU make auto-remakes an included file that has a rule,
# then re-reads it, so a fresh worktree self-provisions ports, and a worktree whose
# file predates a generator upgrade picks up the new keys on the very next `make`.
# The generator reuses the port block already recorded in the file, so a refresh
# never moves Postgres/the API out from under a running stack; .env.local is still
# included last, so a developer's overrides keep winning.
$(ALKERA_ENV_DIR)/.env.workspace: ops/scripts/workspace-env.sh
	@cd $(ALKERA_ENV_DIR) && bash $(CURDIR)/ops/scripts/workspace-env.sh generate

# Nice banners
CYAN   := \033[1;36m
GREEN  := \033[1;32m
YELLOW := \033[1;33m
RED    := \033[1;31m
RESET  := \033[0m

define banner
	@printf "\n$(CYAN)==>$(RESET) $(GREEN)%s$(RESET)\n" $(1)
endef

COMPOSE := docker compose -f deploy/docker/compose.local.yml -p $(COMPOSE_PROJECT_NAME)

# ------------------------------------------------------------
# Help
# ------------------------------------------------------------
.PHONY: help
help: ## Show this help
	@printf "\n$(CYAN)Databench dev commands$(RESET)\n\n"
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ { printf "  $(GREEN)%-18s$(RESET) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf "\n"

# ------------------------------------------------------------
# Bootstrap
# ------------------------------------------------------------
# The first-day path (bootstrap, migrate, seed, the dev servers) runs from the lock
# as committed, for the reason the bootstrap recipe gives below. A dependency you
# add to a pyproject still needs an explicit `uv sync`.
bootstrap migrate seed dev-all dev-backend dev-worker dev-gateway dev-cli: export UV_FROZEN := 1

.PHONY: bootstrap
bootstrap: ## Install Python + Node deps from the lockfiles, create .env from .env.example
	$(call banner,"Bootstrapping Python workspace (uv sync --frozen)")
	@# --frozen installs exactly what uv.lock pins and never rewrites it. A lock
	@# re-resolved on a Mac drops a Linux-only line and fails the drift check, so a
	@# plain `uv sync` would leave a fresh clone dirty before its first edit.
	uv sync --frozen
	$(call banner,"Bootstrapping JS workspace (pnpm install)")
	pnpm install
	$(call banner,"Ensuring .env exists")
	@if [ ! -f .env ]; then cp .env.example .env && echo "  created .env from .env.example"; else echo "  .env already present"; fi
	@printf "\n$(GREEN)✓ bootstrap complete$(RESET)\n"

# Self-provisioning JS deps. `dev-web` / `dev-all` / `test` need the pnpm
# workspace installed (vite, vitest), a fresh worktree otherwise dies with
# "vite: command not found". This installs on first use and re-installs only
# when `pnpm-lock.yaml` changes or `node_modules` is missing (an up-to-date tree
# is a no-op), mirroring how `dev-all` self-provisions `.env.workspace`. Not a
# substitute for `make bootstrap` (which also does `uv sync` + seeds `.env`).
node_modules: pnpm-lock.yaml
	$(call banner,"Installing JS workspace deps (pnpm install)")
	pnpm install
	@touch node_modules

.PHONY: clean
clean: ## Remove caches, venvs, node_modules, build artifacts
	$(call banner,"Cleaning caches and artifacts")
	rm -rf .venv .mypy_cache .ruff_cache .pytest_cache
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type d -name "node_modules" -prune -exec rm -rf {} +
	rm -rf apps/web/dist
	rm -rf vendor/opencode/packages/opencode/dist

# ------------------------------------------------------------
# OpenCode (vendored subtree, driven by the harness layer)
# ------------------------------------------------------------
OPENCODE_VENDOR := vendor/opencode
OPENCODE_PKG    := $(OPENCODE_VENDOR)/packages/opencode

.PHONY: opencode-binary
opencode-binary: ## Build the vendored opencode standalone binary (requires bun)
	bash scripts/build-opencode-binary.sh

.PHONY: node-bundle
NODE_BUNDLE_OUT ?= dist/node-bundle
node-bundle: ## Build the node bundle from source for this Linux machine (requires uv and bun); serve it with NODE_BUNDLE_DIR
	bash scripts/build-node-bundle.sh --out $(NODE_BUNDLE_OUT)

.PHONY: opencode-ripgrep
opencode-ripgrep: ## Stage pinned ripgrep for opencode e2e without building opencode
	bash scripts/build-opencode-binary.sh --ripgrep-only


.PHONY: opencode-fetch-upstream
opencode-fetch-upstream: ## Fetch upstream/main + list new commits since current vendor/opencode HEAD
	@if ! git remote get-url opencode-upstream >/dev/null 2>&1; then \
	  git remote add opencode-upstream https://github.com/sst/opencode.git; \
	fi
	git fetch opencode-upstream
	@printf "\n$(YELLOW)New upstream commits touching vendor/opencode/:$(RESET)\n"
	@git log --oneline HEAD..opencode-upstream/main -- vendor/opencode | head -50 || true

.PHONY: opencode-bump
opencode-bump: ## Subtree-pull latest upstream/main into vendor/opencode (squashed)
	$(call banner,"Subtree-pulling vendor/opencode from upstream")
	@if ! git remote get-url opencode-upstream >/dev/null 2>&1; then \
	  git remote add opencode-upstream https://github.com/sst/opencode.git; \
	fi
	git fetch opencode-upstream
	git subtree pull --prefix=vendor/opencode opencode-upstream main --squash
	@printf "\n$(GREEN)✓$(RESET) Subtree pull complete. Resolve any conflicts (our patches live in normal commits), then run:\n"
	@printf "  cd vendor/opencode && bun install\n"
	@printf "  make lint && make typecheck && make test\n"
	@printf "If our local patches still apply cleanly, just commit. Otherwise fix + commit.\n"

.PHONY: opencode-show-patches
opencode-show-patches: ## List our local commits touching vendor/opencode/
	@printf "$(YELLOW)Alkera-local commits on vendor/opencode/:$(RESET)\n"
	@git log --oneline -- vendor/opencode | head -30

# ---- Notebooks: the agent notebook tools and the simulator ---------------------
.PHONY: nbagt-sim-soak nbagt-eval
nbagt-sim-soak: ## Long random simulator runs over the notebook tools (ALKERA_SIM_SOAK_EXAMPLES, default 2000)
	ALKERA_SIM_SOAK=1 $(UV_RUN) pytest packages/alkera-notebook/tests/test_nbagt_sim.py -k sim_soak -p no:randomly

nbagt-eval: ## Evaluate a real model on the notebook scenarios (needs ALKERA_EVAL_TOKEN or ANTHROPIC_API_KEY; writes ALKERA_EVAL_REPORT)
	$(UV_RUN) pytest packages/alkera-notebook/tests/sim_eval -m live_provider -p no:randomly

# ---- Notebooks: the marimo fork and the notebook format ------------------------
# vendor/marimo is a squashed subtree of marimo at a release tag, without
# upstream's docs, frontend and examples trees (scripts/marimo-subtree.sh). Alkera's edits are
# fenced and listed in vendor/marimo/README.alkera.md.

.PHONY: marimo-fetch-upstream
marimo-fetch-upstream: ## Fetch marimo release tags and list the ones newer than vendor/marimo's base
	bash scripts/marimo-subtree.sh fetch

.PHONY: marimo-bump
marimo-bump: ## Move vendor/marimo to release TAG=x.y.z (squashed subtree merge), keeping Alkera edits
	@test -n "$(TAG)" || { echo "usage: make marimo-bump TAG=x.y.z" >&2; exit 2; }
	bash scripts/marimo-subtree.sh bump $(TAG)

.PHONY: marimo-show-patches
marimo-show-patches: ## List Alkera's fenced edits in vendor/marimo and the commits that touched it
	@printf "$(YELLOW)Fenced Alkera edits in vendor/marimo:$(RESET)\n"
	@grep -rn --include='*.py' '# == ALKERA EDIT START' vendor/marimo || true
	@printf "\n$(YELLOW)Commits touching vendor/marimo:$(RESET)\n"
	@git log --oneline -- vendor/marimo | head -30

.PHONY: gen-alkera-marimo
gen-alkera-marimo: ## Regenerate alkera_notebook._marimo (the private fork package) from vendor/marimo
	$(UV_RUN) --frozen python scripts/gen_alkera_marimo.py

.PHONY: gen-format-corpus
gen-format-corpus: ## Regenerate the writer-produced cases of the notebook format corpus
	$(UV_RUN) --frozen python packages/alkera-notebook/tests/format_corpus/generate.py

.PHONY: opencode-clean
opencode-clean: ## Remove the built opencode dist + staged binary
	rm -rf $(OPENCODE_PKG)/dist apps/cli/dist/opencode apps/cli/dist/opencode-linux-x64


.PHONY: local
local: opencode-binary ## Stage everything a source checkout needs to run the agent locally (opencode harness binary + embedding model)


# ------------------------------------------------------------
# Local infra (docker)
# ------------------------------------------------------------
# Services that run once and exit (the Files bucket creation). `up --wait` counts
# any exited container as failed, even at status 0, so the long-running services
# are awaited healthy first, then each one-shot is run and its own exit status
# decides (`compose wait` returns it).
INFRA_ONE_SHOT := seaweedfs-init

.PHONY: infra-up
infra-up: $(ALKERA_ENV_DIR)/.env.workspace ## Start Postgres, Temporal (+ its UI), Mailpit, SeaweedFS (+ its bucket) and the test databases via docker compose
	$(call banner,"Starting local infra ($(COMPOSE_PROJECT_NAME): pg, temporal, mailpit, seaweedfs, test dbs)")
	services="$$($(COMPOSE) config --services | grep -vxF $(foreach s,$(INFRA_ONE_SHOT),-e $(s)))" && \
		test -n "$$services" && \
		$(COMPOSE) up --wait --wait-timeout 120 $$services
	$(COMPOSE) up --detach $(INFRA_ONE_SHOT)
	$(COMPOSE) wait $(INFRA_ONE_SHOT)
	@printf "\n$(GREEN)✓ infra up$(RESET), mailpit UI: http://localhost:$${MAILPIT_UI_PORT:-8025}  temporal UI: http://localhost:$${TEMPORAL_UI_PORT:-8233}\n"

.PHONY: infra-down
infra-down: ## Stop this workspace's local infra (keeps volumes)
	$(call banner,"Stopping local infra ($(COMPOSE_PROJECT_NAME))")
	$(COMPOSE) down

.PHONY: infra-nuke
infra-nuke: ## Stop this workspace's infra AND delete its volumes (destroys local DB data)
	$(call banner,"Destroying local infra volumes ($(COMPOSE_PROJECT_NAME))")
	$(COMPOSE) down -v

.PHONY: infra-logs
infra-logs: ## Tail this workspace's infra logs
	$(COMPOSE) logs -f


.PHONY: urls
urls: $(ALKERA_ENV_DIR)/.env.workspace ## Print this workspace's dev URLs (web, api, gateway, mailpit, temporal, db)
	@bash ops/scripts/print-urls.sh

.PHONY: dev-box
dev-box: $(ALKERA_ENV_DIR)/.env.workspace ## Start a local developer box: a pool box in Docker on this machine, every chat under gVisor (needs `make dev-all`; the backend keeps it running)
	$(call banner,"Starting a local developer box for $(COMPOSE_PROJECT_NAME)")
	@bash ops/scripts/dev/dev-box.sh

.PHONY: dev-chat-local
dev-chat-local: $(ALKERA_ENV_DIR)/.env.workspace ## Serve this stack's cloud chats from THIS machine (mints the device credential, runs the mirror; needs `make dev-all`)
	$(call banner,"Registering this machine as a chat box for $(COMPOSE_PROJECT_NAME)")
	@bash ops/scripts/dev/chat-local-machine.sh


.PHONY: workspace-env-refresh
workspace-env-refresh: ## Rewrite .env.workspace from the generator, keeping this workspace's ports
	@bash ops/scripts/workspace-env.sh generate
	@bash ops/scripts/print-urls.sh

# Drops the file and re-allocates from scratch. It does NOT force new ports: the
# allocator starts from the same slug-derived block and only moves on if that block
# is now busy or claimed by a sibling worktree, so this usually hands back the very
# same ports. Reach for it when the recorded block IS the problem (a collision, a
# hand-mangled file); to pick up new keys with the ports untouched, plain `make`
# does it, or `make workspace-env-refresh` to do it explicitly.
.PHONY: workspace-env-reset
workspace-env-reset: ## Rebuild .env.workspace from scratch (re-allocates the port block)
	@rm -f .env.workspace
	@bash ops/scripts/workspace-env.sh generate
	@bash ops/scripts/print-urls.sh


# ------------------------------------------------------------
# Dev servers
# ------------------------------------------------------------
.PHONY: dev-backend
dev-backend: ## Run FastAPI backend with hot reload (http://localhost:8000)
	$(call banner,"Starting backend on http://$(API_HOST):$(API_PORT)")
	cd apps/backend && $(UV_RUN) uvicorn $(BACKEND_APP) \
		--reload \
		--host $${API_HOST:-127.0.0.1} \
		--port $${API_PORT:-8000} \
		--ws-max-size 2162688 \
		--timeout-graceful-shutdown 5

.PHONY: dev-worker
dev-worker: ## Run the Temporal worker (all four task queues; needs `make infra-up`)
	$(call banner,"Starting worker (python -m worker run) against $${TEMPORAL_ADDRESS:-localhost:7233}")
	cd apps/worker && $(UV_RUN) python -m $(WORKER_MODULE) run \
		--queues $${ALKERA_TEMPORAL_TASK_QUEUES:-money,email,sync,default} \
		--health-port $${ALKERA_WORKER_HEALTH_PORT:-9000}

.PHONY: dev-web
dev-web: node_modules ## Run apps/web (the deployed web portal) Vite dev server (port from .env.workspace; `make urls`)
	$(call banner,"Starting apps/web Vite dev server")
	pnpm --filter @alkera/web dev

.PHONY: dev-gateway
dev-gateway: ## Run the model gateway with hot reload (http://localhost:8081)
	$(call banner,"Starting model gateway on http://$${API_HOST:-127.0.0.1}:$${GATEWAY_PORT:-8081}")
	cd apps/model-gateway && $(UV_RUN) uvicorn $(GATEWAY_APP) \
		--reload \
		--host $${API_HOST:-127.0.0.1} \
		--port $${GATEWAY_PORT:-8081}

.PHONY: dev-cli
dev-cli: ## Run the CLI (pass args via ARGS="...", default: health)
	cd apps/cli && $(UV_RUN) alkera $${ARGS:-health}

.PHONY: dev-all
dev-all: $(ALKERA_ENV_DIR)/.env.workspace node_modules ## Run backend + worker + web + model-gateway concurrently (Ctrl-C stops all)
	$(call banner,"Running backend + worker + web + gateway in parallel")
	@bash ops/scripts/dev-all.sh


.PHONY: kill-dev
kill-dev: ## Force-kill THIS workspace's dev servers (scoped by its ports, incl. the worker's health port)
	$(call banner,"Killing this workspace's dev servers ($(COMPOSE_PROJECT_NAME))")
	@bash ops/scripts/kill-dev.sh

.PHONY: kill-all-dev
kill-all-dev: ## NUCLEAR, force-kill EVERY workspace's dev servers across all branches
	$(call banner,"Killing ALL dev servers across all worktrees")
	@bash ops/scripts/kill-all-dev.sh

# ------------------------------------------------------------
# Database
# ------------------------------------------------------------
# A checkout that mounts this tree names more models for this chain with
# ALEMBIC_MODELS (`-x models=pkg.models`) and its own installer with
# BACKEND_INSTALL (`module:function`); alone, neither is set.
ALEMBIC_MODELS ?=
BACKEND_INSTALL ?= backend.open_product:install
# CLI_INSTALL names the CLI composition the tool manifest and daemon schema
# exports install; unset, they install the open CLI.
CLI_INSTALL ?=

.PHONY: migrate
migrate: ## Apply all Alembic migrations to local DB
	$(call banner,"Running alembic upgrade head")
	cd apps/backend && $(UV_RUN) alembic upgrade head

.PHONY: migrate-create
migrate-create: ## Create a new autogenerated migration, usage: make migrate-create MSG="message"
	@if [ -z "$(MSG)" ]; then echo "$(RED)MSG is required. Usage: make migrate-create MSG=\"add users table\"$(RESET)"; exit 1; fi
	cd apps/backend && $(UV_RUN) alembic $(ALEMBIC_MODELS) revision --autogenerate -m "$(MSG)"

.PHONY: migrate-check
migrate-check: ## Fail if ORM models drift from migrations (autogenerate would emit ops)
	$(call banner,"Running alembic check")
	cd apps/backend && $(UV_RUN) alembic $(ALEMBIC_MODELS) check

.PHONY: migrate-down
migrate-down: ## Roll back one migration
	cd apps/backend && $(UV_RUN) alembic downgrade -1

# --- Backup / restore (Postgres is the only durable store; Temporal's databases are disposable) ---
# scripts/db_backup.py does the work: the
# dump keeps privileges and carries the roles they name beside it, a restore
# runs in one transaction and refuses a database that is not empty. It needs
# the Postgres client tools of the SERVER's major version (PG_BIN_DIR points at
# another directory of them). PG_URL is DATABASE_URL_SYNC; the script strips
# SQLAlchemy's driver suffix itself.
PG_URL ?= $(DATABASE_URL_SYNC)
BACKUP_FILE ?= alkera-$(shell date +%Y%m%d-%H%M%S).dump

backup restore db-regrant: export UV_FROZEN := 1

.PHONY: backup
backup: ## Dump Postgres to BACKUP_FILE (pg custom format, privileges kept) plus BACKUP_FILE.roles.sql
	$(call banner,"pg_dump -> $(BACKUP_FILE)")
	@$(UV_RUN) python scripts/db_backup.py backup --url "$(PG_URL)" --file "$(BACKUP_FILE)"
	@echo "  store both files off-box"

.PHONY: restore
restore: ## Restore BACKUP_FILE=<path> into an EMPTY database; RECREATE=1 CONFIRM=<database> drops and recreates it first
	@[ -n "$(BACKUP_FILE)" ] && [ -f "$(BACKUP_FILE)" ] || { echo "usage: make restore BACKUP_FILE=<path> [RECREATE=1 CONFIRM=<database>]"; exit 1; }
	$(call banner,"pg_restore <- $(BACKUP_FILE)")
	@$(UV_RUN) python scripts/db_backup.py restore --url "$(PG_URL)" --file "$(BACKUP_FILE)" \
		$(if $(RECREATE),--recreate --confirm "$(CONFIRM)",)

.PHONY: db-regrant
db-regrant: ## Re-apply the migrations' grants to a database restored from a dump that carried none
	@$(UV_RUN) python scripts/db_backup.py regrant --url "$(PG_URL)"

.PHONY: seed
seed: ## Run idempotent seeds (re-runnable; each seed is responsible for its own idempotency)
	$(call banner,"Running seeds")
	cd apps/backend && BACKEND_INSTALL=$(BACKEND_INSTALL) $(UV_RUN) python -m scripts.seed

.PHONY: load-model-catalog
load-model-catalog: ## Load the default model catalog + list prices (idempotent; writes only the catalog tables; safe in prod)
	$(call banner,"Loading the default model catalog")
	cd apps/backend && $(UV_RUN) python -m scripts.load_model_catalog

.PHONY: bootstrap-admin
bootstrap-admin: ## Create the first prod org + admin (idempotent; reads ADMIN_BOOTSTRAP_EMAIL/ORG_NAME or pass --email/--org-name via ARGS=)
	$(call banner,"Bootstrapping first admin")
	cd apps/backend && BACKEND_INSTALL=$(BACKEND_INSTALL) $(UV_RUN) python -m scripts.bootstrap_admin $(ARGS)

# Saved queries and reports are retired: a chat template is the reusable unit
# now. This converts every live one into a template, the prose into its brief,
# the files into its working directory, and trashes what it converted. Safe to
# re-run: a converted row is no longer live, so a second run finds nothing.
#
# The database is named on the command line, and only there. The dotenv chain at
# the top of this file is `include`d as MAKE variables, and an included
# assignment overrides an inherited environment variable (absent `make -e`), so
# an exported DATABASE_URL cannot point any target in this Makefile at another
# database, and a door with no undo must not be aimed by something that quietly
# does not work. A variable given on the command line is the one kind an include
# never overrides, which is why it is the one that is read. The guard is spelled
# inside each recipe rather than hoisted into a variable: the bare `export` above
# expands every variable in this file, so a hoisted $(error) would fire on every
# target instead of on these two.
.PHONY: retire-contexts
retire-contexts: ## Convert every saved query and report into a chat template (idempotent), needs RETIRE_DATABASE_URL=postgresql+asyncpg://…
	$(call banner,"Retiring saved queries and reports")
	cd apps/backend && $(UV_RUN) --frozen python -m scripts.retire_replication_contexts --database-url "$(if $(strip $(RETIRE_DATABASE_URL)),$(RETIRE_DATABASE_URL),$(error Name the database on the command line: make $@ RETIRE_DATABASE_URL=postgresql+asyncpg://user:pass@host:port/db; an exported DATABASE_URL cannot reach this target, the dotenv include above overrides it))"

.PHONY: retire-contexts-dry-run
retire-contexts-dry-run: ## Rehearse retire-contexts: list what would convert, change nothing, needs RETIRE_DATABASE_URL=postgresql+asyncpg://…
	$(call banner,"Rehearsing the retirement of saved queries and reports")
	cd apps/backend && $(UV_RUN) --frozen python -m scripts.retire_replication_contexts --dry-run --database-url "$(if $(strip $(RETIRE_DATABASE_URL)),$(RETIRE_DATABASE_URL),$(error Name the database on the command line: make $@ RETIRE_DATABASE_URL=postgresql+asyncpg://user:pass@host:port/db; an exported DATABASE_URL cannot reach this target, the dotenv include above overrides it))"

# ------------------------------------------------------------
# Quality
# ------------------------------------------------------------
.PHONY: lint
lint: check-settings-surfaces lint-imports ## Lint Python (ruff) + JS/TS (pnpm) + settings reachability + import contracts
	$(call banner,"ruff check")
	$(UV_RUN) ruff check .
	$(call banner,"pnpm lint")
	pnpm -r --if-present lint

.PHONY: lint-imports
lint-imports: ## Check the import contracts: the root .importlinter plus every apps/*/.importlinter
	$(call banner,"import contracts")
	@set -e; for config in .importlinter apps/*/.importlinter; do \
		if [ -f "$$config" ]; then \
			echo "lint-imports --config $$config"; \
			$(UV_RUN) --frozen lint-imports --no-cache --config "$$config"; \
		fi; \
	done

.PHONY: check-settings-surfaces
check-settings-surfaces: ## Fail when a setting reaches none of the surfaces an operator edits
	$(call banner,"settings surfaces")
	$(UV_RUN) --frozen python scripts/check_settings_surfaces.py

.PHONY: format
format: ## Auto-format Python + JS/TS
	$(call banner,"ruff format + fix")
	$(UV_RUN) ruff format .
	$(UV_RUN) ruff check --fix .
	$(call banner,"pnpm format")
	pnpm -r --if-present format

.PHONY: typecheck
typecheck: ## mypy (Python) + tsc (TS)
	$(call banner,"mypy")
	$(UV_RUN) mypy apps packages
	$(call banner,"tsc")
	pnpm -r --if-present typecheck


.PHONY: test
test: node_modules test-py test-web ## Run Python + JS tests

# Tests run only against a DISPOSABLE database, never the one `make dev-all`
# serves. The suite has no per-test rollback and several fixtures clear whole
# tables, so the repo-root conftest.py refuses any database whose name is not on
# the allowlist in alkera_core.db.testing (a bare `$(UV_RUN) pytest`, which inherits
# .env.workspace's dev DSN, is refused with that message and this remedy).
#
# Every pytest target below therefore provisions its own database on the Postgres
# this worktree is already configured for, per-worktree by construction, since
# the port comes from .env.workspace, and exports its DSNs for the run.
# Override with `make test-py TEST_DB=alkera_test_other`.
TEST_DB ?= alkera_test_suite
PYTEST_DB = dsn="$$($(UV_RUN) python -m alkera_core.db.testing ensure $(TEST_DB))" && eval "$$dsn" && export DATABASE_URL DATABASE_URL_SYNC
# The same, with the schema brought to head: an xdist worker migrates its own
# database, but a serial run (`make live`, `make e2e-live`, `make test-py ARGS=-p
# no:xdist`) uses this one directly and needs the tables.
PYTEST_DB_MIGRATED = $(PYTEST_DB) && (cd apps/backend && $(UV_RUN) alembic upgrade head)

# How many xdist workers every parallel pytest target asks for. `auto` is one per
# core, which is the right default on a laptop and the wrong one on a 96-core CI
# runner: each worker DROPs, CREATEs and migrates its own Postgres database and
# then holds a pool of up to database_pool_size + database_pool_max_overflow
# connections, so the worker count is really a demand on the DATABASE, which on
# CI shares the same box. Every CI job therefore pins a number and sizes its
# Postgres for exactly that number; nothing depends on `nproc` moving.
PYTEST_WORKERS ?= auto

# Vitest's default is one fork per core, and a jsdom fork costs a couple of
# hundred megabytes. Empty means "vitest decides" (the laptop default); CI sets a
# number so a 96-core runner does not start ninety-five jsdom forks for a suite
# whose whole runtime is under a minute.
VITEST_WORKERS ?=
VITEST_FLAGS = $(if $(VITEST_WORKERS),--maxWorkers=$(VITEST_WORKERS),)
# For a `pnpm run` whose script takes no other pass-through argument: the `--`
# separator only belongs there when something follows it.
VITEST_ARGS = $(if $(VITEST_WORKERS),-- $(VITEST_FLAGS),)

.PHONY: test-py
test-py: ## Python suite only (pytest)
	$(call banner,"pytest")
	# `-n $(PYTEST_WORKERS)` (default `auto`) parallelizes across CPUs; each xdist worker gets its own
	# Postgres DB cloned from the migrated base (see repo-root conftest.py).
	#
	# `--dist loadgroup` hands out work one TEST at a time and keeps together only
	# the tests that share an `xdist_group` mark. Every test gets one: the repo-root
	# conftest's `pytest_collection_modifyitems` names each ungrouped item's own
	# module as its group, so a module stays WHOLE on one worker. That is the safe
	# default, the suite has no per-test rollback, fixtures commit real rows, and a
	# module whose tests read what an earlier test in the same module wrote breaks as
	# soon as another module's test is interleaved between them.
	#
	# A module that shares NOTHING across its tests opts out and is handed out per
	# test, which is where the parallelism comes from:
	#
	#     pytestmark = [pytest.mark.spread]
	#
	# Opt out only when all of it is true: no module- or class-scoped fixture (its
	# own or a conftest's above it), no test reading rows or files another test in
	# the module wrote, no dependence on the order they run in. Module-level Python
	# state is per worker PROCESS, so two tests that never have to see each other's
	# leftovers are free to land on different workers.
	#
	# A module may also name its own group, `pytestmark =
	# pytest.mark.xdist_group("some_stable_name")`, which the conftest leaves alone;
	# that is how several modules are pinned to ONE worker together.
	# `ops/scripts/tests/test_xdist_groups.py` enforces both halves: a module with a
	# shared-scope fixture must carry an explicit group, and a module that says
	# `spread` must have no shared-scope fixture and no `setUpClass`.
	#
	# Under `-n`, xdist appends `@<group>` to every node id it prints; that suffix is
	# how the scheduler groups, not part of the test's name. Drop it when you paste a
	# failing id back into a serial `$(UV_RUN) pytest <path>::<test>`.
	#
	# No reruns: a test that fails under load is a test that reads the clock or
	# the box, and the fix belongs in the test, a retry only hides it and pays
	# its timeout twice. Bare `$(UV_RUN) pytest` stays serial (so --pdb / -s /
	# single-test debugging are unaffected), export the DSNs this target prints
	# if you want the same disposable database there.
	# The temporal-marked tests need the `temporal` CLI as their local dev server;
	# resolve (or fetch) the pinned binary and export its path for this run.
	$(PYTEST_DB_MIGRATED) && \
	eval "$$(bash ops/scripts/ensure-temporal-cli.sh)" && export ALKERA_TEMPORAL_BIN && \
	$(UV_RUN) pytest -n $(PYTEST_WORKERS) --dist loadgroup $(ARGS)

.PHONY: test-rls-login
# The tenant-isolation suite (Files and the content tables), run as a NON-superuser login. The default suite
# connects as the superuser that provisions it, and a superuser is a place
# where an isolation bug can hide: here the app engine connects as a login
# made exactly the way a deployment makes its runtime login (not superuser,
# BYPASSRLS, a member of alkera_files_app, DML on every table, see
# alkera_core.db.row_security.app_login_statements), so the policies bind
# through the same role assumption production uses. The guard module fails
# the tier outright if the engine is not that login.
RLS_LOGIN_TEST_PATHS := apps/backend/tests/files/test_rls_login_tier.py apps/backend/tests/files/test_files_two_org_fuzz.py apps/backend/tests/files/test_row_security_canary.py packages/api-core/tests/files/test_files_repo_rls.py packages/api-core/tests/files/test_files_cross_tenant_read.py apps/backend/tests/test_content_row_security_plans.py
test-rls-login: ## Tenant-isolation suite (two-org fuzz, Files RLS, boot canary) as a non-superuser login
	$(call banner,"pytest (non-superuser login)")
	ALKERA_TEST_APP_LOGIN=alkera_test_app_login $(MAKE) test-py ARGS="$(RLS_LOGIN_TEST_PATHS) $(ARGS)"

# The gate an environment must pass before MULTI_ORG_ENABLED is turned on there.
# Every piece also runs in the default suite;
# this runs them together and alone, so a red one cannot hide in a long run:
# the zero-tolerance tenancy scans (no allowlist: one finding fails), the
# generated cross-tenant route matrix where every operation must hold, every
# two-org adversarial suite (backend, gateway, worker, billing, CLI profiles,
# pins and per-chat binding), the settings that default the switch off and
# accept it on, the switch's delivery to every deployed service, and the
# tenant-isolation tier as a non-superuser login.
MULTI_ORG_GATE_PATHS := apps/backend/tests/test_tenancy_architecture.py apps/backend/tests/cross_tenant $(wildcard apps/backend/tests/test_cross_tenant_*.py) packages/api-core/tests/test_settings.py apps/cli/tests/auth/test_auth_profiles.py apps/cli/tests/auth/test_org_commands.py apps/cli/tests/harness/test_chat_profile_binding.py apps/cli/tests/daemon/test_daemon_auth_orgs.py

.PHONY: test-web
test-web: node_modules ## Web (browser portal + @alkera/ui + @alkera/chat-model + @alkera/chart-guard) vitest only
	$(call banner,"vitest")
	# jsdom-only and platform-agnostic: it asserts identical results on Linux and
	# Windows, so CI runs it on Linux alone rather than paying for it twice. The
	# shared UI library and the conversation model carry their own vitest suites;
	# they run here too so a primitive or fold regression fails the same gate as
	# the portal that consumes them.
	pnpm --filter @alkera/web --filter @alkera/ui --filter @alkera/chat-model --filter @alkera/chart-guard test -- --run $(VITEST_FLAGS)

.PHONY: files-coverage
files-coverage: ## Files coverage gate: >=95% line+branch, every gap reviewed in UNCOVERED.md
	$(call banner,"files coverage gate")
	# Its own target on purpose: `make test` never pays for coverage, and
	# coverage only engages when --cov is passed. The backend/worker Files test
	# directories are listed as they land, a path that does not exist yet is
	# dropped rather than failing collection. The database is MIGRATED here as in
	# `test-py`: every repo/RLS test talks to real tables, and an unmigrated
	# database errors them all while the percentage still prints.
	$(PYTEST_DB_MIGRATED) && \
	dirs="packages/api-core/tests/files"; \
	for candidate in apps/backend/tests/files apps/worker/tests/test_files*.py; do \
		if [ -e "$$candidate" ]; then dirs="$$dirs $$candidate"; fi; \
	done; \
	echo "coverage over: $$dirs"; \
	$(UV_RUN) pytest $$dirs \
		--cov --cov-branch --cov-report=term-missing \
		--cov-report=json:.coverage-files.json \
		--cov-fail-under=95 -p no:cacheprovider
	$(UV_RUN) python scripts/check_files_uncovered.py \
		.coverage-files.json packages/api-core/tests/files/UNCOVERED.md


.PHONY: e2e
# Where the opt-in suites live. `-m <marker>` alone still imports every test
# module in the tree (38k items) to deselect all but a couple of hundred, and on a
# Windows runner that collection is most of an e2e job's pytest time. Naming the
# homes collects only those. A marked test outside its home is refused by
# apps/backend/tests/test_opt_in_suite_homes.py, so the list cannot go stale
# silently.
E2E_TEST_PATHS := apps/cli/tests/e2e apps/cli/tests/gateway apps/cli/tests/mcp apps/cli/tests/cloud/test_cloud_template_brief_e2e.py apps/cli/tests/cloud/test_cloud_live_folder_e2e.py apps/cli/tests/cloud/test_cloud_report_page_e2e.py apps/backend/tests/test_oauth_live.py
LIVE_TEST_PATHS := apps/cli/tests/e2e apps/backend/tests/files/test_files_store_factory_layout.py packages/api-core/tests/test_deployment_health.py packages/api-core/tests/files/test_files_seaweedfs_smoke.py packages/api-core/tests/files/store/test_files_store_admin.py packages/api-core/tests/files/store/test_files_store_conformance_live.py apps/cli/tests/harness/test_sandbox_live.py apps/cli/tests/harness/test_sandbox_none_live.py apps/cli/tests/harness/test_sandbox_env_escape_live.py apps/cli/tests/harness/test_sandbox_member_isolation_live.py apps/cli/tests/cloud/test_cloud_folder_node_live.py packages/alkera-notebook/tests/cases/test_nbeng_default_template.py
SUITE_PATHS = $(if $(filter e2e,$(SUITE) ),$(E2E_TEST_PATHS),$(if $(filter live,$(SUITE) ),$(LIVE_TEST_PATHS),))

.PHONY: print-suite-paths
print-suite-paths: ## Print the paths an opt-in suite is collected from (SUITE=e2e|live) for a CI job that calls pytest itself
	@test -n "$(SUITE_PATHS)" || { echo "SUITE must be e2e or live (got '$(SUITE)')" >&2; exit 2; }
	@echo "$(SUITE_PATHS)"

e2e: ## Run the end-to-end suite: opencode (bun + mock OpenAI) + claude-agent (real claude binary + mock Anthropic)
	$(call banner,"e2e (opencode: bun+mock OpenAI; claude-agent: real claude+mock Anthropic)")
	@command -v bun >/dev/null 2>&1 || { echo "bun missing; install from https://bun.sh"; exit 1; }
	@test -f vendor/opencode/packages/opencode/src/index.ts || { echo "vendor/opencode missing; it's a git subtree committed in-tree; re-checkout the repo (or run 'make opencode-bump')"; exit 1; }
	# When a prebuilt compiled binary is supplied via ALKERA_OPENCODE_BIN, the
	# opencode_e2e suite runs THAT (self-contained) instead of bun-dev source, so
	# vendor/opencode/node_modules isn't needed, skip the install. On Windows CI
	# that install means tar-untarring ~150k tiny files onto NTFS (~168s); the
	# compiled binary dodges it. A bare `make e2e` (no env) still installs to run
	# bun-dev opencode locally.
	@if [ -n "$$ALKERA_OPENCODE_BIN" ]; then \
		echo "ALKERA_OPENCODE_BIN set ($$ALKERA_OPENCODE_BIN); skipping bun install, using the compiled binary"; \
	else \
		( cd vendor/opencode && bun install ); \
	fi
	# Parallel like the default suite: each xdist worker gets its own migrated
	# Postgres DB (repo-root conftest.py); the mock provider servers bind
	# ephemeral ports and each test spawns its own opencode/claude subprocess in
	# a tmp ALKERA_HOME, so workers don't collide. No reruns, for the reason
	# test-py gives: a subprocess test that fails under load names its own bug.
	#
	# `--dist load` (per TEST), not loadscope (per MODULE) as the default suite
	# uses. There are only ~80 e2e tests in 17 modules, and two of those modules
	# hold 23 tests each, under loadscope they pin to one worker apiece and the
	# last 8 tests took 104s of a 200s run while 14 workers sat idle. Distributing
	# per test is safe here precisely because of the isolation described above:
	# none of the 17 modules declares a module/class-scoped fixture or module-level
	# mutable state, so no test depends on sharing a worker with its neighbours.
	$(PYTEST_DB_MIGRATED) && \
	$(UV_RUN) pytest -m "opencode_e2e or claude_e2e" -n $(PYTEST_WORKERS) --dist load --durations=15 $(E2E_TEST_PATHS)


.PHONY: files-specs
files-specs: ## Model-check the Files TLA+ specs with TLC (uses a local java, else eclipse-temurin in docker)
	$(call banner,"TLA+ model checking (packages/api-core/specs/files)")
	@eval "$$(ops/scripts/ensure-tla-tools.sh)"; \
	  test -n "$$TLA_TOOLS_JAR" || { echo "could not resolve tla2tools.jar"; exit 1; }; \
	  found=0; \
	  for spec in packages/api-core/specs/files/*.tla; do \
	    cfg="$${spec%.tla}.cfg"; \
	    [ -f "$$cfg" ] || { echo "skipping $$spec (no sibling .cfg)"; continue; }; \
	    found=1; \
	    echo "==> TLC $$spec"; \
	    ops/scripts/tlc.sh "$$spec" "$$cfg" || exit 1; \
	  done; \
	  [ "$$found" = 1 ] || { echo "no .tla/.cfg pair under packages/api-core/specs/files"; exit 1; }

FILES_MUTATION_PATHS ?= packages/api-core/alkera_core/authz/policies/files.py \
                        packages/api-core/alkera_core/files/authz/decider.py

.PHONY: files-mutation
files-mutation: ## Mutate the Files authorization policy; a surviving mutant fails (nightly)
	$(call banner,"mutation testing the Files policy")
	@mkdir -p reports
	@for module in $(FILES_MUTATION_PATHS); do \
	  test -f "$$module" || { echo "missing mutation target: $$module"; exit 1; }; \
	done
	@set -o pipefail; \
	  uvx --from mutmut==3.3.1 mutmut run 2>&1 | tee reports/files-mutation.txt; \
	  uvx --from mutmut==3.3.1 mutmut results 2>&1 | tee -a reports/files-mutation.txt; \
	  if grep -qE '^(survived|suspicious|timeout)$$' reports/files-mutation.txt; then \
	    echo "a mutant of the Files policy survived the branch table"; exit 1; \
	  fi


# ------------------------------------------------------------
# OpenAPI
# ------------------------------------------------------------
# The gen-* targets must NOT mutate uv.lock. A path dependency with
# platform-conditional metadata (an extra that pulls a package only on
# non-macOS) makes a `uv run` re-resolve on a different OS rewrite the lock
# and trip the CI drift check (`git diff --exit-code`). Freezing the lock for these targets keeps it stable across
# platforms; `uv sync --frozen` still validates it against pyproject elsewhere.
gen-openapi gen-sdk gen-tool-manifest gen-open-subset drift-open-subset: export UV_FROZEN := 1


.PHONY: gen-openapi
gen-openapi: ## Dump FastAPI OpenAPI schema to packages/shared-openapi/openapi.json
	$(call banner,"Generating OpenAPI schema")
	@bash ops/scripts/gen-openapi.sh --out packages/shared-openapi/openapi.json
	@printf "$(GREEN)✓ wrote packages/shared-openapi/openapi.json$(RESET)\n"

.PHONY: gen-chart-profile
gen-chart-profile: ## Copy the chart profile validator into the stdlib-only `alkera` client (byte-identical; the client cannot import alkera_core)
	cp packages/api-core/alkera_core/charts/profile.py packages/alkera-py/alkera/chart/_profile.py
	@printf "$(GREEN)✓ wrote packages/alkera-py/alkera/chart/_profile.py$(RESET)\n"

.PHONY: gen-sdk
gen-sdk: gen-openapi gen-tool-manifest gen-notebook-settings gen-chart-profile ## Regenerate OpenAPI schema, typed clients, the daemon JSON-RPC protocol, the chat tool manifest, and the client's copy of the chart profile
	$(call banner,"Generating @alkera/sdk types")
	pnpm --filter @alkera/sdk gen
	@printf "$(GREEN)✓ wrote packages/ts-sdk/src/schema.d.ts$(RESET)\n"
	$(call banner,"Generating alkera-sdk Python client")
	$(UV_RUN) openapi-python-client generate \
		--path packages/shared-openapi/openapi.json \
		--output-path packages/py-sdk/alkera_sdk/_generated \
		--config packages/py-sdk/openapi-python-client.yaml \
		--meta none \
		--overwrite
	@printf "$(GREEN)✓ wrote packages/py-sdk/alkera_sdk/_generated/$(RESET)\n"


.PHONY: gen-tool-manifest
.PHONY: gen-notebook-settings
gen-notebook-settings: ## Export the notebook settings schema (shared-openapi JSON + the notebook-ui TS module)
	$(call banner,"Exporting the notebook settings schema")
	$(UV_RUN) python scripts/export_notebook_settings.py

gen-tool-manifest: ## Export the agent tool surface (names + I/O schemas), the permission presentation registry, and codegen the chat tool TS types
	$(call banner,"Exporting agent tool manifest")
	CLI_INSTALL=$(CLI_INSTALL) $(UV_RUN) python scripts/export_tool_manifest.py
	$(call banner,"Exporting the permission presentation registry + conformance vectors")
	$(UV_RUN) python scripts/export_permission_presentation.py
	$(call banner,"Generating chat tool TS types")
	pnpm --filter @alkera/chat-model gen-tool-schemas
	@printf "$(GREEN)✓ wrote packages/shared-openapi/tool-manifest.json + packages/chat-model/src/generated/{toolSchemas,toolManifest}.ts$(RESET)\n"


# The open subset of the generated artifacts: the same generators, each run in a
# fresh process against the open composition (nothing private installed), so
# the open repository's SDK and schemas name no private route, method or tool.
# drift-open-subset is their drift gate.
OPEN_SUBSET_ARTIFACTS := packages/shared-openapi/open

.PHONY: gen-open-subset
gen-open-subset: ## Regenerate the open subset of the OpenAPI document, TS types, tool manifest and daemon schema
	$(call banner,"Exporting the open tool manifest")
	$(UV_RUN) python scripts/export_tool_manifest.py --open
	$(call banner,"Exporting the open daemon schema")
	$(UV_RUN) python scripts/export_daemon_schema.py --open
	@printf "$(GREEN)✓ wrote packages/shared-openapi/open/{tool-manifest,daemon-schema}.json$(RESET)\n"

.PHONY: drift-open-subset
drift-open-subset: gen-open-subset ## Fail when the committed open subset differs from a fresh generation
	@git diff --exit-code -- $(OPEN_SUBSET_ARTIFACTS) || { \
		printf "$(RED)✗ the open subset is stale; run 'make gen-open-subset' and commit the result$(RESET)\n"; exit 1; }
	@dirty="$$(git status --porcelain -- $(OPEN_SUBSET_ARTIFACTS))"; \
	if [ -n "$$dirty" ]; then \
		printf "$(RED)✗ untracked open subset files; run 'make gen-open-subset' and commit them:$(RESET)\n%s\n" "$$dirty"; \
		exit 1; \
	fi
	@printf "$(GREEN)✓ the open subset matches a fresh generation$(RESET)\n"


.PHONY: gen-free-email-domains
gen-free-email-domains: ## Refresh the vendored free/personal email-provider domain list (deliberate, reviewable)
	$(call banner,"Refreshing free email-provider domains")
	@bash ops/scripts/gen-free-email-domains.sh
	@printf "$(GREEN)✓ wrote apps/backend/backend/auth/data/free_email_domains.txt$(RESET)\n"

.PHONY: gen-email-snapshots
gen-email-snapshots: ## Re-render the golden HTML email snapshots (packages/api-core/tests/fixtures/email/)
	$(call banner,"Regenerating email HTML snapshots")
	$(PYTEST_DB) && \
	UPDATE_EMAIL_SNAPSHOTS=1 $(UV_RUN) pytest $(wildcard packages/api-core/tests/test_email_render*.py) -q -k golden_snapshot
	@printf "$(GREEN)✓ wrote packages/api-core/tests/fixtures/email/*.html$(RESET)\n"


# ------------------------------------------------------------
# Production Docker images
#
# Build context is the REPO ROOT for all images so workspace packages
# (api-core, py-sdk, ui, ts-sdk) are reachable.
#
# Override version on the command line:
#   `make docker-images IMAGE_VERSION=1.2.3`
#
# `IMAGE_VERSION` and `IMAGE_BUILD_ID` are deliberately Make-only names so
# `make` doesn't export them as `VERSION` / `BUILD_ID` env vars into every
# subprocess (they'd leak into the FastAPI settings, see
# packages/api-core/alkera_core/config.py).
# ------------------------------------------------------------
IMAGE_VERSION ?= 0.0.0
IMAGE_BUILD_ID ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
# Operator override: push into a private registry. Empty registry → local
# `alkera/*` tags (the unchanged default). e.g. IMAGE_REGISTRY=registry.acme.internal
IMAGE_REGISTRY ?=
IMAGE_REPO_PREFIX ?= alkera
DOCKER_SERVICES := backend worker gateway web
# image_ref,<svc> → [registry/]prefix/svc:version ; local_ref is always alkera/<svc>:ver
image_ref = $(if $(IMAGE_REGISTRY),$(IMAGE_REGISTRY)/,)$(IMAGE_REPO_PREFIX)/$(1):$(IMAGE_VERSION)
local_ref = alkera/$(1):$(IMAGE_VERSION)
# Supply-chain tooling (override if installed elsewhere / a different scanner).
SYFT ?= syft
GRYPE ?= grype
COSIGN ?= cosign
OSV_SCANNER ?= osv-scanner
GRYPE_FAIL_ON ?= high
SBOM_DIR ?= dist/sbom
# The lockfiles `scan-deps` reads. Every third-party version that reaches an image
# is resolved in one of these, so an advisory is catchable without a docker build.
DEP_LOCKFILES := uv.lock pnpm-lock.yaml
# Infra images bundled into the air-gap tarball, pinned by digest to match
# deploy/docker/compose.prod.example.yml exactly (an operator can load these offline);
# apps/backend/tests/test_self_hosted_manifests.py fails on any drift between the two.
# The Temporal console is bundled too so an air-gapped operator can enable its profile.
AIRGAP_INFRA_IMAGES ?= postgres:16-alpine@sha256:e013e867e712fec275706a6c51c966f0bb0c93cfa8f51000f85a15f9865a28cb temporalio/auto-setup:1.29.7@sha256:f14912b699cf73015ad5c4fc18d522d4b014db90e794039214dfb7c022c2644f temporalio/ui:2.53.3@sha256:eef301146e60fad34b47adaecfae4149016e34b2d44ba94fca5fd8e5441f182a chrislusf/seaweedfs:3.99@sha256:8d5b323911a996d5ea152115306bed468d9a849d72bcbb3800ea8d91b7728563

.PHONY: docker-images
docker-images: docker-backend docker-worker docker-gateway docker-web ## Build all production images (override IMAGE_VERSION=...)
	@printf "\n$(GREEN)✓ built alkera/{backend,worker,gateway,web}:$(IMAGE_VERSION)$(RESET)\n"

.PHONY: docker-retag
docker-retag: ## Retag local images for IMAGE_REGISTRY (no-op if registry unset)
	@$(if $(IMAGE_REGISTRY),,echo "IMAGE_REGISTRY is empty; nothing to retag"; exit 0)
	@$(foreach s,$(DOCKER_SERVICES),docker tag $(call local_ref,$(s)) $(call image_ref,$(s));)
	@printf "$(GREEN)✓ retagged → $(IMAGE_REGISTRY)/$(IMAGE_REPO_PREFIX)/*:$(IMAGE_VERSION)$(RESET)\n"

.PHONY: docker-push
docker-push: docker-retag ## Push images to IMAGE_REGISTRY (your private registry)
	@test -n "$(IMAGE_REGISTRY)" || { echo "set IMAGE_REGISTRY=registry.acme.internal"; exit 1; }
	@$(foreach s,$(DOCKER_SERVICES),docker push $(call image_ref,$(s));)

.PHONY: docker-publish
docker-publish: ## Multi-arch (amd64+arm64) build + push straight to IMAGE_REGISTRY via buildx
	@test -n "$(IMAGE_REGISTRY)" || { echo "set IMAGE_REGISTRY=…"; exit 1; }
	@$(foreach s,$(DOCKER_SERVICES),docker buildx build --platform linux/amd64,linux/arm64 \
		-f apps/$(if $(filter gateway,$(s)),model-gateway,$(s))/Dockerfile \
		-t $(call image_ref,$(s)) \
		--build-arg APP_VERSION=$(IMAGE_VERSION) --build-arg BUILD_ID=$(IMAGE_BUILD_ID) \
		--provenance=mode=max --sbom=true --push . ;)

.PHONY: sbom
sbom: ## Generate a CycloneDX SBOM per image (syft) into $(SBOM_DIR)
	@command -v $(SYFT) >/dev/null || { echo "install syft: https://github.com/anchore/syft"; exit 1; }
	@mkdir -p $(SBOM_DIR)
	@$(foreach s,$(DOCKER_SERVICES),$(SYFT) scan docker:$(call local_ref,$(s)) -o cyclonedx-json=$(SBOM_DIR)/$(s)-$(IMAGE_VERSION).cdx.json -q && echo "✓ SBOM $(s)";)

.PHONY: scan
scan: ## Vuln-scan each image (grype), failing on >= $(GRYPE_FAIL_ON) (.grype.yaml ignores accepted CVEs)
	@command -v $(GRYPE) >/dev/null || { echo "install grype: https://github.com/anchore/grype"; exit 1; }
	@$(foreach s,$(DOCKER_SERVICES),echo "== scan $(s) ==" && $(GRYPE) docker:$(call local_ref,$(s)) --fail-on $(GRYPE_FAIL_ON) -q;)

.PHONY: scan-deps
scan-deps: ## Vuln-scan the dependency lockfiles (osv-scanner), no image build, safe in CI
	@command -v $(OSV_SCANNER) >/dev/null || { echo "install osv-scanner: https://github.com/google/osv-scanner"; exit 1; }
	$(OSV_SCANNER) scan source $(foreach l,$(DEP_LOCKFILES),--lockfile $(l))

.PHONY: scan-all
scan-all: scan-deps docker-images scan ## The full vulnerability gate: lockfile advisories, then image CVEs
	@printf "$(GREEN)✓ dependency + image scan clean$(RESET)\n"


.PHONY: airgap-bundle
airgap-bundle: docker-images sbom ## Offline tarball: app + infra images + SBOMs + a load script
	@mkdir -p dist/airgap
	docker save $(foreach s,$(DOCKER_SERVICES),$(call local_ref,$(s))) $(AIRGAP_INFRA_IMAGES) \
		| gzip > dist/airgap/alkera-images-$(IMAGE_VERSION).tar.gz
	cp -r $(SBOM_DIR) dist/airgap/sbom
	@printf '#!/usr/bin/env sh\nset -e\ngunzip -c "$$(dirname "$$0")"/alkera-images-*.tar.gz | docker load\n' > dist/airgap/load-images.sh
	@chmod +x dist/airgap/load-images.sh
	@cd dist/airgap && sha256sum * > SHA256SUMS 2>/dev/null || true
	@printf "$(GREEN)✓ air-gap bundle → dist/airgap/$(RESET)\n"

.PHONY: docker-backend
docker-backend: ## Build the backend image (alkera/backend:$(IMAGE_VERSION))
	$(call banner,"Building alkera/backend:$(IMAGE_VERSION)")
	docker build \
		-f apps/backend/Dockerfile \
		-t alkera/backend:$(IMAGE_VERSION) \
		--build-arg APP_VERSION=$(IMAGE_VERSION) \
		--build-arg BUILD_ID=$(IMAGE_BUILD_ID) \
		.

.PHONY: docker-worker
docker-worker: ## Build the worker image (alkera/worker:$(IMAGE_VERSION))
	$(call banner,"Building alkera/worker:$(IMAGE_VERSION)")
	docker build \
		-f apps/worker/Dockerfile \
		-t alkera/worker:$(IMAGE_VERSION) \
		--build-arg APP_VERSION=$(IMAGE_VERSION) \
		--build-arg BUILD_ID=$(IMAGE_BUILD_ID) \
		.

.PHONY: docker-gateway
docker-gateway: ## Build the model-gateway image (alkera/gateway:$(IMAGE_VERSION))
	$(call banner,"Building alkera/gateway:$(IMAGE_VERSION)")
	docker build \
		-f apps/model-gateway/Dockerfile \
		-t alkera/gateway:$(IMAGE_VERSION) \
		--build-arg APP_VERSION=$(IMAGE_VERSION) \
		--build-arg BUILD_ID=$(IMAGE_BUILD_ID) \
		.

.PHONY: docker-web
docker-web: ## Build the web image (alkera/web:$(IMAGE_VERSION))
	$(call banner,"Building alkera/web:$(IMAGE_VERSION)")
	docker build \
		-f apps/web/Dockerfile \
		-t alkera/web:$(IMAGE_VERSION) \
		--build-arg APP_VERSION=$(IMAGE_VERSION) \
		--build-arg BUILD_ID=$(IMAGE_BUILD_ID) \
		.

# ---------------------------------------------------------------------------
# Notebooks: the CRDT sandbox workers' own interpreter
# ---------------------------------------------------------------------------
# The notebook format code runs in the CRDT sandbox workers, on a Python at
# least as new as the newest a notebook's environment may use. This builds
# that interpreter's environment (uv-managed Python 3.14, independent of the
# backend's) holding only what a worker imports: loro, alkera-notebook with
# its dependencies, and the backend's sandbox package (without the backend's
# own dependencies). Point REALTIME_CRDT_WORKER_PYTHON at its python.
NB_SANDBOX_VENV ?= $(CURDIR)/.venv-crdt-sandbox
NB_SANDBOX_PYTHON ?= 3.14

.PHONY: nb-doc-sandbox-python
nb-doc-sandbox-python: ## Build the CRDT sandbox workers' Python 3.14 environment
	uv venv --python $(NB_SANDBOX_PYTHON) --allow-existing "$(NB_SANDBOX_VENV)"
	uv pip install --python "$(NB_SANDBOX_VENV)/bin/python" "loro==1.16.2" -e packages/alkera-notebook
	uv pip install --python "$(NB_SANDBOX_VENV)/bin/python" --no-deps -e apps/backend
	@echo "REALTIME_CRDT_WORKER_PYTHON=$(NB_SANDBOX_VENV)/bin/python"

# ------------------------------------------------------------
# Notebooks: kernel runtime, RPC framing, local launcher
# ------------------------------------------------------------

.PHONY: gen-kernel-rpc
gen-kernel-rpc: ## Copy the RPC framing module into the kernel runtime (_alkera_kernel/_frames.py)
	$(UV_RUN) python packages/alkera-kernel/scripts/gen_kernel_rpc.py

.PHONY: test-nbkrn
test-nbkrn: ## Kernel, RPC and launcher tests
	$(UV_RUN) pytest packages/alkera-py packages/alkera-kernel packages/alkera-notebook/tests -k "nbkrn or rpc"
