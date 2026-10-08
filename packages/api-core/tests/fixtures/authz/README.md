# Actor-document lineage fixtures

Each `v<X_Y_Z>/` directory holds `ActorChainRecord` JSON dumps produced by the writer at that version, one per credential shape the backend resolves (`user`, `agent`, `service_ci`, `service_proxy`, `pat`). The lineage test (`packages/api-core/tests/authz/test_actor_chain_lineage.py`) loads every fixture of every version with the current reader and asserts it parses.

`ActorChainRecord` is the `actor` document stored on every event outbox row and org audit event, so the reader must load every shape a past writer emitted.

## Adding fixtures for the current version

Generate them, never write them by hand:

```bash
uv run python packages/api-core/tests/fixtures/authz/generate.py
```

This regenerates every fixture under the current `SCHEMA_VERSION` directory. Commit the result with the schema change.

## Rules

These follow "Versioned persisted models" in the repository's `AGENTS.md`.

1. Bump `SCHEMA_VERSION` on every shape change.
2. Add fixtures under `v<NEW_VERSION>/` capturing the new shape.
3. Never delete old fixtures.
4. Never edit old fixtures. A breaking change gets a migration in the `MIGRATIONS` ladder.
