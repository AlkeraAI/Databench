# Realtime lineage fixtures

Each `v<X_Y_Z>/` directory holds JSON dumps produced by the writer at that version: one `DocEnvelope` per kind (`envelope/`), one dump per kind-specific payload model (`payloads/`), and one frame per client and server tag (`frames/client/`, `frames/server/`). The lineage test (`packages/api-core/tests/schemas/realtime/test_realtime_lineage.py`) loads every fixture of every version with the current reader and asserts it parses.

The envelope is persisted (a durable operation is an event outbox `doc.op` row, and a document's state lives in `realtime_docs`), so the reader must keep loading whatever a past writer emitted. The frames are the socket contract the web app and the daemon pin themselves against.

## Adding fixtures for the current version

Generate them, never write them by hand:

```bash
uv run python packages/api-core/tests/fixtures/realtime/generate.py
```

This regenerates every fixture under the current `SCHEMA_VERSION` directory. Commit the result with the schema change.

## Rules

These follow "Versioned persisted models" in the repository's `AGENTS.md`.

1. Bump `SCHEMA_VERSION` on every shape change.
2. Add fixtures under `v<NEW_VERSION>/` capturing the new shape.
3. Never delete old fixtures.
4. Never edit old fixtures. A breaking change gets a migration in the `MIGRATIONS` ladder.
