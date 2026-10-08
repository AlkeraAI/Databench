# apps/web

The web app: Vite, React 19 and strict TypeScript, with shared UI from [`@alkera/ui`](../../packages/ui), React Query for data, React Router for navigation, and the typed API client [`@alkera/sdk`](../../packages/ts-sdk).

The main areas are sign-in and sign-up (`/login`, `/signup`, which accepts `?invite=<token>`), chats and workspaces (`/chat`), Files (`/files`), teams and settings (`/teams`, `/settings/...`), and the platform staff console (`/admin`). The route table is `src/App.tsx`.

`make seed` creates the development admin `admin@example.com` with password `admin` when `APP_ENV=local`.

## Run

From the repository root:

```bash
make dev-web
```

`make urls` prints the port. The Vite dev server proxies `/api`, `/admin/v1` and `/health` to this checkout's backend (`src/dev/proxyTable.ts`), so the typed client uses same-origin URLs in development.

## Layout

```
src/
  main.tsx            entry point: startPortal(PORTAL_EXTENSIONS); the open app installs none
  App.tsx             the route table
  test-setup.ts       Vitest setup (jest-dom and browser polyfills)
  styles/             global CSS: portal.css and concept-tokens.css
  app/                the shell: AppLayout, Topbar, nav, icons
    guards/           RequireAuth, RequireOrgAdmin, RequirePlatformStaff, RequirePlatformAdmin
    boot/             startPortal, error boundary, session bridges, error reporting
    extensions/       portal.ts, the portal's extension points
  pages/              routed pages, one folder per area: auth, workspace, organization, platform
  api/                React Query hooks, one file per resource, plus the client and query keys
    admin/            platform admin hooks
  lib/                shared helpers
  components/         page components shared across areas
  tests               the tests, mirroring src/ and importing subjects through @/
```

The browser app (`src/{pages,app,api,lib,components}`) must not import an editor webview (`@/webview/*`); an ESLint `no-restricted-imports` rule enforces it. The one path alias is `@/*` for `src/*` (tsconfig and Vite).

Tests live under `src/tests/`, mirroring `src/`: `src/tests/pages/auth/LoginPage.test.tsx` tests `src/pages/auth/LoginPage.tsx`.

## Typed API client

The app calls the backend through `@alkera/sdk`, generated from the backend's OpenAPI document.

1. Change a route or a Pydantic schema under `apps/backend/backend/api/routes/` or `packages/api-core/alkera_core/schemas/`.
2. From the repository root, run `make gen-sdk`. It regenerates `packages/shared-openapi/openapi.json` and `packages/ts-sdk/src/schema.d.ts`; commit both.
3. Write a React Query hook in `src/api/<resource>.ts` that calls the module-level client. Paths, parameters and responses are checked against the schema. Add the query key to `src/api/keys.ts` rather than writing it inline.

   ```ts
   import { useQuery } from "@tanstack/react-query";
   import { api } from "./client";
   import { keys } from "./keys";

   export function useThing() {
     return useQuery({
       queryKey: keys.thing,
       queryFn: async () => {
         const { data, error, response } = await api.GET("/api/v1/thing");
         if (error || !data) throw new Error(`HTTP ${response.status}`);
         return data; // typed from schema.d.ts
       },
     });
   }
   ```

CI fails when `make gen-sdk` produces a diff.

## Extension points

A page, nav entry, tab or chat card that is not part of this app reaches it by registering into an extension point (`@alkera/ui/extensions`), never by being imported:

| Point | Declared in |
| --- | --- |
| `PORTAL_ROUTES`, `PORTAL_NAV`, `ORG_SETTINGS_TABS`, `ORG_SETTINGS_SECTIONS`, `ADMIN_ORG_TABS`, `ADMIN_ORG_CARDS`, `ADMIN_USER_CARDS`, `TEAM_SECTIONS`, `TEAM_PLATES`, `CHAT_MENU_ACTIONS` | `src/app/extensions/portal.ts` |
| `TOOL_CARDS` | `@alkera/ui` (chat) |

With nothing registered the app is exactly what ships here; `src/tests/app/openPortal.test.tsx` checks it.

## Adding a page

Add the page under `src/pages/<area>/`, then a `<Route>` in `src/App.tsx` inside the right guard and layout:

```tsx
<Route path="/users" element={<UsersPage />} />
```

Add its sidebar entry to the `NAV` table in `src/app/nav.ts`. A page from an extension registers its route and nav entry through the extension points instead.

## Environment

Vite reads env files from `apps/web/` only, not the repository root. Copy `apps/web/.env.example` to `apps/web/.env.local` to point the app at an API other than the local one.

## Tests, lint and typecheck

```bash
pnpm --filter @alkera/web test -- --run
pnpm --filter @alkera/web lint
pnpm --filter @alkera/web typecheck
```
