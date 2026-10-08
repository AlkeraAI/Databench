# packages/api-core (`alkera-core`)

The shared Python libraries every process depends on: the backend, the worker, the model gateway and the CLI. A module lives here when more than one of them calls it; a module with one caller lives in that app.

## What is in here

| Module | What it holds |
| --- | --- |
| `alkera_core.config` | Settings, read with `pydantic-settings` from the environment and the env files |
| `alkera_core.logging` | structlog setup |
| `alkera_core.db` | The engine, the async session and `get_db` |
| `alkera_core.models` | SQLAlchemy ORM models, read by Alembic autogenerate |
| `alkera_core.schemas` | Pydantic request and response models, one package or module per domain |
| `alkera_core.authz` | The authorization policies and `authorize()` |
| `alkera_core.files` | The Files domain: tree, content store, sharing |
| `alkera_core.compute` | Compute providers and machine placement |
| `alkera_core.email` | Email templates and the one send helper |
| `alkera_core.temporal` | Job names, task queues and their assignment |
| `alkera_core.extensions` | `ExtensionPoint`, the registry optional behaviour plugs into |
| `alkera_core.versioning` | `VersionedModel`, the base for every model persisted to disk. See [`versioning/README.md`](alkera_core/versioning/README.md) |
| `alkera_core.project` | The `.alkera/` directory: `ProjectDirectory`, `ChatStore`, `BlobStore`, `FileLock`. See [`project/README.md`](alkera_core/project/README.md) |

## What is not in here

- HTTP routes, in `apps/backend/backend/api/routes/`.
- Services bound to a request, in `apps/backend/backend/services/`.
- The generated API clients, in `packages/py-sdk` and `packages/ts-sdk`.

## Adding a model or schema

1. Add the class under `alkera_core/models/<name>.py` or `alkera_core/schemas/<domain>/`.
2. Re-export it from the matching `__init__.py`. Alembic sees only models exported from `alkera_core/models/__init__.py`.
3. A schema persisted to disk (not only sent over HTTP) inherits `alkera_core.versioning.VersionedModel`, not `pydantic.BaseModel`. See "Versioned persisted models" in the repository's `AGENTS.md`.
4. For an ORM change, run `make migrate-create MSG="add <name> table"` from the repository root, review the generated file, then run `make migrate`.

## Tests

```bash
uv run pytest packages/api-core/tests
```
