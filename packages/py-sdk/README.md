# packages/py-sdk (`alkera-sdk`)

A typed Python client for the API, generated from [`packages/shared-openapi/openapi.json`](../shared-openapi/openapi.json) and wrapped in a small hand-written surface.

## What it provides

- **`AlkeraClient`**, the hand-written wrapper, with namespaced methods: `health`, `auth` (`login`, `signup`, `me`, `logout`), `errors` and `files`. It holds one `httpx.Client`, so the cookie `login()` receives is sent on every later call.
- **`alkera_sdk.models`**, the generated attrs models (`LoginRequest`, `UserRead`, `InvitationRead`, ...), re-exported unchanged.
- **`alkera_sdk._generated`**, the full output of [`openapi-python-client`](https://github.com/openapi-generators/openapi-python-client): one module per endpoint, each with `sync`, `sync_detailed`, `asyncio` and `asyncio_detailed` variants. Call it through `AlkeraClient.raw_client` for routes the wrapper does not cover.

## Layout

```
alkera_sdk/
  __init__.py          # public exports
  client.py            # the hand-written AlkeraClient
  py.typed             # PEP 561 marker
  _generated/          # generated and committed; never edit by hand
```

## Regenerating

After any change to a backend route or a Pydantic schema, run from the repository root:

```bash
make gen-sdk           # regenerates the OpenAPI document, then both SDKs
```

The Python half on its own:

```bash
uv run openapi-python-client generate \
    --path packages/shared-openapi/openapi.json \
    --output-path packages/py-sdk/alkera_sdk/_generated \
    --config packages/py-sdk/openapi-python-client.yaml \
    --meta none --overwrite
```

`_generated/` is committed, so a fresh checkout type-checks without running the generator, and CI fails when `make gen-sdk` leaves a diff. ruff and mypy skip `_generated/` (configured in the root `pyproject.toml`).

## Usage

```python
from alkera_sdk import AlkeraClient

with AlkeraClient(base_url="http://localhost:8000") as api:
    # No sign-in needed
    health = api.health.live()
    print(int(health.status_code), health.parsed.status)

    # Cookie session: login() keeps the cookie on the wrapped httpx.Client
    api.auth.login(email="admin@example.com", password="admin")
    me = api.auth.me()
    print(me.email, me.platform_role)
    api.auth.logout()
```

For a route the wrapper does not expose:

```python
from alkera_sdk import AlkeraClient
from alkera_sdk._generated.api.teams import list_teams_api_v1_teams_get

with AlkeraClient(base_url="http://localhost:8000") as api:
    api.auth.login(email="admin@example.com", password="admin")
    teams = list_teams_api_v1_teams_get.sync(client=api.raw_client)
```

When a raw call is needed in a second place, add it to `client.py` as a namespace.

## Tests

`packages/py-sdk/tests/` drives the wrapper against `httpx.MockTransport`, with no infrastructure. The backend's suite covers each route against a real Postgres.
