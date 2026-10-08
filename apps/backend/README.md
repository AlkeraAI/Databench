# apps/backend

The FastAPI backend: HTTP routes, the service layer and the Alembic migrations. Shared shapes (settings, logging, the database session, ORM models, Pydantic schemas) live in [`packages/api-core`](../../packages/api-core) as `alkera_core`.

## Layout

```
backend/
  app_factory.py       create_app(), the app uvicorn serves
  composition.py       installs the deployment's extensions before the app is built
  auth/                password hashing, session tokens, the permission dependencies
  authz/               enforce(), the one authorization choke point for routes
  services/            business logic, one package per domain; routes stay thin
  api/
    deps/              dependencies and HTTP helpers shared by routes
    routes/            tenant routes under /api/v1, one package per domain
    admin/             platform staff routes under /admin/v1
  seeds/               idempotent development seeds (make seed)
  utils/               cookie and id helpers
alembic/
  env.py
  versions/            the migration chain, starting from a baseline revision
scripts/
  seed.py              entry point for make seed
  bootstrap_admin.py   creates the first organisation and admin in production
tests                 the suite (apps/backend/tests)
```

## Auth

- `POST /api/v1/auth/login` with `{email, password}` sets the `alkera_session` HTTP-only cookie (a signed JWT). `POST /api/v1/auth/logout` clears it, and `GET /api/v1/auth/me` returns the signed-in user.
- `current_user` also accepts `Authorization: Bearer <jwt>`, which the CLI uses.
- The permission dependencies in [`backend/auth/dependencies.py`](backend/auth/dependencies.py) are used as parameter types:
  - `CurrentUser`: any signed-in user.
  - `OrgAdmin`: admin of the caller's organisation (its root team).
  - `require_team_admin("team_id")`: admin of that team or of any team above it.
  - `PlatformStaff`: any platform role (`alkera_support` or `alkera_admin`).
  - `PlatformAdmin`: the `alkera_admin` platform role.

  ```python
  @router.get("/users")
  async def list_users(db: DbSession, admin: OrgAdmin, org_id: CurrentOrg) -> list[OrgUserRead]: ...
  ```

- A route that decides who may act on a resource calls `backend.authz.enforce(...)`, which records every decision. See the authorization section of [AGENTS.md](../../AGENTS.md).

## Admin routes

`backend/api/admin/__init__.py` builds the `/admin/v1` router with `require_platform_staff` as a router-level dependency, so every endpoint mounted on it requires a platform role. Endpoints that grant privileges or change other accounts add `require_platform_admin` per route. Tenant endpoints never go here.

## Seeds

`make seed` installs the deployment's extensions, then runs every function in `backend/seeds/__init__.py:SEEDS` followed by any an extension registered in `EXTENSION_SEEDS`. Each seed is async, takes an `AsyncSession`, returns a one-line summary, and is idempotent (it looks rows up by natural key and creates or updates them).

The `dev_admin` seed runs only when `APP_ENV` is `local` or `staging`. It creates an organisation and the admin `admin@example.com` with password `admin` and the `alkera_admin` platform role (`AUTH_DEV_ADMIN_EMAIL` and `AUTH_DEV_ADMIN_PASSWORD` change them).

## Run

From the repository root:

```bash
make migrate && make seed
make dev-backend
```

The API listens on `API_PORT` (8000 unless `.env.workspace` names another; `make urls` prints it). Interactive API docs are at `/docs`. To try the session cookie:

```bash
curl -fsS -c cookies.txt -X POST http://localhost:8000/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"email":"admin@example.com","password":"admin"}'
curl -fsS -b cookies.txt http://localhost:8000/api/v1/auth/me
curl -fsS -b cookies.txt http://localhost:8000/admin/v1/orgs
```

## Add a migration

1. Add or edit a model under `packages/api-core/alkera_core/models/` and re-export it from `models/__init__.py`.
2. Generate a candidate with `make migrate-create MSG="add users table"`.
3. Review and edit the generated file (below). Never ship it unedited.
4. Apply it with `make migrate`, then run `make migrate-check`, which must report "No new upgrade operations detected". CI runs the same check.

Autogenerate compares column types but not server defaults, and the metadata has no naming convention. Write these by hand:

- **Renames.** Autogenerate emits a drop and a create, which loses data. Use `op.rename_table(...)` or `op.alter_column("t", "old", new_column_name="new")`.
- **Server defaults.** Use `op.alter_column(..., server_default=...)`.
- **CHECK, primary key and EXCLUDE constraints.** Use `op.create_check_constraint(...)` and `op.drop_constraint(...)`.
- **Data backfills.** Add the column nullable or with a server default, backfill it with `op.execute(...)`, then tighten it, and write the real inverse in `downgrade()`.

And always:

- Name every constraint and index in the model. Declare partial and composite indexes in the model's `__table_args__`; an index that exists only in a migration shows up as drift and is proposed for dropping.
- Rename the file to the next `NNNN_slug.py` and set `revision` and `down_revision` to sequential numbers (autogenerate writes a random hex id).
- Write a docstring in the style of the existing revisions and check that `downgrade()` reverses the change.

`make migrate-check` catches a model change with no migration. It cannot catch the hand-written cases above, so the review still matters.

## Conventions

- SQLAlchemy 2.0 style only: `Mapped[...]`, `mapped_column`, async sessions.
- Routes parse the request, call one service and shape the response. Services hold the logic and import no FastAPI.
- Type hints are mandatory (ruff `ANN` and `mypy --strict`).
- Read configuration through `alkera_core.config.settings`, never `os.environ`.
- Import shared shapes as `from alkera_core.X import Y` and backend code as `from backend.X import Y`.

## Tests

```bash
make test                         # the whole workspace
uv run pytest apps/backend/tests  # this app only
```

Route tests drive the app in process with `httpx.AsyncClient` and `ASGITransport` (the `client` fixture in `apps/backend/tests/conftest.py`) against a real Postgres. `test_health.py` is the smallest example.
