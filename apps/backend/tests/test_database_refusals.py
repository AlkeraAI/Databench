"""A database refusal is answered as what it means to the caller, through one handler.

Each case runs a real statement against the suite's Postgres inside a scratch
app wired with the shared exception handlers, so the error that reaches the
handler is exactly the one the driver and SQLAlchemy raise in production: a
value a column cannot hold is the caller's 422, a reference to a missing row a
409, a connection a restart closed under the request a retryable 503, and a
bug (NOT NULL, CHECK) still the opaque 500.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from alkera_core.db.errors import register_unique
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.observability.asgi import install_exception_handlers
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

Body = Callable[[AsyncSession], Awaitable[None]]


async def _answer(body: Body) -> Any:
    app = FastAPI()
    install_exception_handlers(app)

    @app.post("/run")
    async def run() -> dict[str, str]:
        async with AsyncSessionLocal() as session:
            await body(session)
            await session.commit()
        return {"ok": "yes"}

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as raw:
        return await raw.post("/run")


def _statements(*statements: str, **params: object) -> Body:
    async def body(session: AsyncSession) -> None:
        for statement in statements:
            await session.execute(text(statement), params)

    return body


@pytest.mark.parametrize(
    ("body", "code"),
    [
        pytest.param(
            _statements("SELECT CAST(:v AS integer)", v=-(10**30)),
            "invalid_input",
            id="a-number-the-driver-cannot-bind-as-an-integer",
        ),
        pytest.param(
            _statements("SELECT CAST(CAST('1e30' AS numeric) AS integer)"),
            "invalid_input",
            id="a-number-past-the-integer-range",
        ),
        pytest.param(
            _statements("SELECT CAST(:v AS text)::uuid", v="t" * 10_000),
            "invalid_input",
            id="a-uuid-string-that-is-not-one",
        ),
        pytest.param(
            _statements(
                "CREATE TEMP TABLE hostile_probe (k varchar(128))",
                "INSERT INTO hostile_probe VALUES (:v)",
                v="i" * 1_000_000,
            ),
            "invalid_input",
            id="a-string-wider-than-its-column",
        ),
        pytest.param(
            _statements("SELECT CAST(:v AS jsonb)", v='{"name": "a\\u0000b"}'),
            "unstorable_text",
            id="a-nul-inside-a-jsonb-document",
        ),
    ],
)
async def test_hostile_inputs_are_4xx(disposable_database: str, body: Body, code: str) -> None:
    response = await _answer(body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert "hostile_probe" not in response.text


async def test_a_reference_to_a_missing_row_is_a_409(disposable_database: str) -> None:
    response = await _answer(
        _statements(
            "CREATE TEMP TABLE ref_parent (id int PRIMARY KEY)",
            "CREATE TEMP TABLE ref_child (parent int REFERENCES ref_parent (id))",
            "INSERT INTO ref_child VALUES (7)",
        )
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict.reference"
    assert "ref_child" not in response.text


async def test_a_registered_constraint_names_its_field(disposable_database: str) -> None:
    register_unique("uq_named_probe_handle", "handle", "That handle is taken.", code="handle_taken")
    response = await _answer(
        _statements(
            "CREATE TEMP TABLE named_probe (h text CONSTRAINT uq_named_probe_handle UNIQUE)",
            "INSERT INTO named_probe VALUES ('a')",
            "INSERT INTO named_probe VALUES ('a')",
        )
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert (error["code"], error["message"], error["details"]) == (
        "handle_taken",
        "That handle is taken.",
        {"field": "handle"},
    )
    assert "uq_named_probe_handle" not in response.text


async def test_an_unregistered_constraint_is_the_plain_conflict(disposable_database: str) -> None:
    response = await _answer(
        _statements(
            "CREATE TEMP TABLE plain_probe (h text CONSTRAINT uq_plain_probe UNIQUE)",
            "INSERT INTO plain_probe VALUES ('a')",
            "INSERT INTO plain_probe VALUES ('a')",
        )
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert (error["code"], error["message"]) == ("conflict", "That already exists.")
    assert "details" not in error


def test_a_constraint_registered_twice_must_say_the_same_thing() -> None:
    register_unique("uq_twice_probe", "name", "Taken.")
    register_unique("uq_twice_probe", "name", "Taken.")
    with pytest.raises(ValueError, match="uq_twice_probe"):
        register_unique("uq_twice_probe", "email", "Taken.")


async def _closed_under_the_request(session: AsyncSession) -> None:
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    await raw.driver_connection.close()  # type: ignore[union-attr]
    await session.execute(text("SELECT 1"))


async def test_db_restart_inflight_is_503(disposable_database: str) -> None:
    """A restart closes the pooled connection a request is already holding;
    its next statement meets a closed connection, which is the database away,
    not a bug."""
    response = await _answer(_closed_under_the_request)
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "db_unavailable"
    assert error["details"] == {"retryable": True}
    assert response.headers["Retry-After"] == "5"
