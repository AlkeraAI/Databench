# packages/shared-openapi

The generated OpenAPI document for the API, and the other contracts shared between the Python and TypeScript sides. Both SDKs are generated from `openapi.json`.

## Regenerating

```bash
make gen-openapi
```

This writes `openapi.json` here. It is committed so a consumer that does not run the generator still sees the API contract. Never edit it by hand: it is generated from `apps/backend`. If CI reports drift, run `make gen-openapi` and commit the result.

`tool-manifest.json` (every agent tool's name and its input and output JSON Schema) and `notebook-settings.json` are generated too, by `make gen-sdk`.

## `name-rules.cases.json` is written by hand

The file and folder naming rules exist twice: in `alkera_core.files.names` (the server, which is the authority) and in `names.ts` in `@alkera/chat-model` (the client). This corpus keeps them from drifting. `packages/api-core/tests/files/test_files_names_contract.py` and `packages/chat-model/src/names.contract.test.ts` each drive every case through their own implementation.

A rule change edits this file and both implementations in the same commit; nothing regenerates it. Names are text, so only cases that are valid UTF-8 belong here. The server's byte-level cases are in `packages/api-core/tests/fixtures/files/`.
