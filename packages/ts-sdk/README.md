# packages/ts-sdk (`@alkera/sdk`)

A typed TypeScript client for the API, generated from [`packages/shared-openapi/openapi.json`](../shared-openapi/openapi.json).

## What it provides

- **`paths`, `components`, `operations`**, the types of the OpenAPI document, generated with [`openapi-typescript`](https://github.com/openapi-ts/openapi-typescript).
- **`createClient(options)`**, a typed `fetch` wrapper built on [`openapi-fetch`](https://github.com/openapi-ts/openapi-typescript/tree/main/packages/openapi-fetch). Every call, for example `client.GET("/api/v1/teams")`, is type-checked against the document.
- **The server's field limits** (`schemaLimits.ts`), read from the same document, so a client never spells a limit itself.

## Regenerating

After any change to a backend route or a Pydantic schema, run from the repository root:

```bash
make gen-sdk            # regenerates the OpenAPI document, then both SDKs
```

The two steps on their own:

```bash
make gen-openapi                       # writes packages/shared-openapi/openapi.json
pnpm --filter @alkera/sdk gen          # writes src/schema.d.ts and the limits
```

`schema.d.ts` is committed, so a fresh checkout type-checks without running the generator, and CI fails when `make gen-sdk` leaves a diff.

## Usage

```ts
import { createClient } from "@alkera/sdk";

const api = createClient();            // same origin; Vite proxies /api and /health in development
const { data, error } = await api.GET("/api/v1/teams");
```

In the web app, components do not call the client directly. React Query hooks in `apps/web/src/api/` (`useTeams`, `useTeamMembers`, ...) wrap it.
