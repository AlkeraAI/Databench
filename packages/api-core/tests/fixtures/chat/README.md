# Chat lineage fixtures

Each `v<X_Y_Z>/` directory holds JSON dumps produced by the writer at that version. The lineage tests (`packages/api-core/tests/schemas/chat/test_lineage.py`) load every fixture of every version with the current reader and assert it parses. A fixture that stops loading means a change broke backward compatibility.

## Layout

```
v1_0_0/
  manifest/
    full.json         # ChatManifest dump
    minimal.json
  events/
    session_created.json
    message_completed.json
    ...
  parts/
    text.json
    file.json
    tool_call.json
    ...
```

## Adding fixtures for the current version

Generate them, never write them by hand:

```bash
uv run python packages/api-core/tests/fixtures/chat/generate.py
```

This regenerates every fixture under the current `SCHEMA_VERSION` directory. Commit the result with the schema change.

## Rules

These follow "Versioned persisted models" in the repository's `AGENTS.md`.

1. Bump `SCHEMA_VERSION` on every shape change.
2. Add fixtures under `v<NEW_VERSION>/` capturing the new shape.
3. Never delete old fixtures.
4. Never edit old fixtures. A breaking change gets a migration in the `MIGRATIONS` ladder.
