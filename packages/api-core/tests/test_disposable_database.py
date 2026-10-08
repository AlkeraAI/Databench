"""A test session may only touch a database it is allowed to destroy.

The suite has no per-test rollback: fixtures commit real rows, and several of them
clear whole tables to get a clean count. None of them knows which database it is
pointed at, and the workspace's generated ``.env.workspace`` names the DEV database
— so the decision has to be made once, up front, from the database NAME.

These cases pin that decision: which names are disposable, which are refused, and
the one explicit way to name a database the patterns do not cover.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alkera_core.db.testing import (
    ALLOW_ENV_VAR,
    DISPOSABLE_DATABASE_PATTERNS,
    NonDisposableDatabaseError,
    assert_disposable_database,
    database_name,
    is_disposable_database,
    main,
    server_reachable,
    worker_database_name,
)

DSN = "postgresql+psycopg://alkera:hunter2@127.0.0.1:5432/{name}"

#: The same, on a port nothing is listening on — for the cases that must not reach a
#: real server (`ensure` creates the database it is handed).
DEAD_DSN = "postgresql+psycopg://alkera:hunter2@127.0.0.1:1/{name}"

#: What `ensure` may spend before it gives up on a server that is not answering.
#: It runs at the head of every `make` pytest target, so this is generous enough
#: that a loaded machine cannot fail it and still far short of what an unbounded
#: driver connect costs: one refused connection held a Windows runner for 130 s,
#: and against a host that accepts TCP without ever speaking the protocol an
#: unbounded connect does not return at all.
UNANSWERED_BUDGET_SECONDS = 30.0


@contextmanager
def a_listener_that_never_answers() -> Iterator[int]:
    """A port that completes the TCP handshake and then says nothing.

    Nothing ever calls ``accept``, so the kernel finishes the handshake into the
    backlog on its own and the client is left waiting for a startup reply that is
    never coming. A refused port would not stand for this: a refusal comes back
    at once here, while this is the shape that hangs.
    """
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        yield int(listener.getsockname()[1])


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("alkera_test", id="the-make-target-database"),
        pytest.param("alkera_test_ci", id="alkera_test-prefixed"),
        pytest.param("alkera_lane_guard", id="a-fleet-lane"),
        pytest.param("alkera_lane_seed_scale", id="another-fleet-lane"),
        pytest.param("alkera_gw0", id="an-xdist-worker"),
        pytest.param("alkera_gw13", id="a-two-digit-xdist-worker"),
        pytest.param("alkera_migtest_9f2c1a", id="a-scratch-migration-database"),
        pytest.param("something_test", id="suffixed-_test"),
        pytest.param("test_something", id="prefixed-test_"),
        pytest.param("ALKERA_TEST", id="case-folded"),
    ],
)
def test_an_allowlisted_name_is_disposable(name: str) -> None:
    assert is_disposable_database(name) is True
    assert assert_disposable_database(DSN.format(name=name)) == name


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("alkera", id="the-dev-database-itself"),
        pytest.param("alkera_jdoe_foo", id="a-worktree-dev-database"),
        pytest.param("alkera_prod", id="prod-looking"),
        pytest.param("postgres", id="the-maintenance-database"),
        pytest.param("testing", id="test-without-the-separator"),
        pytest.param("alkera_testament", id="alkera_test-without-the-separator"),
        pytest.param("contest", id="contains-test-but-is-not-a-test-database"),
        pytest.param("alkera_gateway", id="alkera_gw-is-the-prefix-of-a-real-word"),
    ],
)
def test_a_name_outside_the_allowlist_is_refused(name: str) -> None:
    assert is_disposable_database(name) is False
    with pytest.raises(NonDisposableDatabaseError) as excinfo:
        assert_disposable_database(DSN.format(name=name))
    assert name in str(excinfo.value)


def test_the_separator_is_what_makes_a_name_a_test_database() -> None:
    """The allowlist must not degrade into "anything that starts with alkera_test"
    or "anything that starts with alkera_gw" — a dev database is one typo away."""
    assert is_disposable_database("alkera_test_x") is True
    assert is_disposable_database("alkera_testament") is False
    assert is_disposable_database("alkera_gw2") is True
    assert is_disposable_database("alkera_gateway") is False


def test_the_refusal_names_the_database_and_both_ways_forward() -> None:
    with pytest.raises(NonDisposableDatabaseError) as excinfo:
        assert_disposable_database(DSN.format(name="alkera"))
    message = str(excinfo.value)
    assert "alkera" in message
    assert ALLOW_ENV_VAR in message
    assert all(pattern in message for pattern in DISPOSABLE_DATABASE_PATTERNS)


def test_the_refusal_never_prints_the_password() -> None:
    with pytest.raises(NonDisposableDatabaseError) as excinfo:
        assert_disposable_database(DSN.format(name="alkera"))
    assert "hunter2" not in str(excinfo.value)


def test_the_allow_variable_admits_exactly_the_database_it_names() -> None:
    env = {ALLOW_ENV_VAR: "alkera"}
    assert is_disposable_database("alkera", env=env) is True
    assert assert_disposable_database(DSN.format(name="alkera"), env=env) == "alkera"


@pytest.mark.parametrize(
    "allowed",
    [
        pytest.param("alkera_other", id="a-different-database"),
        pytest.param("alker", id="a-prefix-of-the-name"),
        pytest.param("alkera*", id="a-pattern-is-not-a-name"),
        pytest.param("*", id="a-wildcard-opens-nothing"),
        pytest.param(" alkera", id="whitespace-is-not-stripped-into-a-match"),
        pytest.param("", id="empty"),
    ],
)
def test_the_allow_variable_is_exact_not_a_pattern(allowed: str) -> None:
    env = {ALLOW_ENV_VAR: allowed}
    assert is_disposable_database("alkera", env=env) is False
    with pytest.raises(NonDisposableDatabaseError):
        assert_disposable_database(DSN.format(name="alkera"), env=env)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(None, id="unset"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("postgresql+psycopg://alkera:alkera@127.0.0.1:5432/", id="no-database"),
        pytest.param("postgresql+psycopg://alkera:alkera@127.0.0.1:5432", id="host-only"),
        pytest.param("not a url at all", id="unparseable"),
        pytest.param("://", id="garbage"),
    ],
)
def test_an_empty_or_invalid_url_is_refused(url: str | None) -> None:
    """Fail closed: a URL we cannot read a database name out of is not a database we
    can prove is disposable."""
    assert database_name(url) is None
    with pytest.raises(NonDisposableDatabaseError):
        assert_disposable_database(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "postgresql+asyncpg://a:b@h:5432/alkera_test", "alkera_test", id="async-driver"
        ),
        pytest.param("postgresql+psycopg://a:b@h:5432/alkera_gw0", "alkera_gw0", id="sync-driver"),
        pytest.param("postgresql://a:b@h/alkera_lane_x", "alkera_lane_x", id="bare-scheme"),
        pytest.param(
            "postgresql+psycopg://a:b@h:5432/alkera_test?sslmode=require",
            "alkera_test",
            id="a-query-string-is-not-part-of-the-name",
        ),
    ],
)
def test_the_database_name_is_read_out_of_the_url(url: str, expected: str) -> None:
    assert database_name(url) == expected


def test_the_allow_variable_still_requires_a_readable_url() -> None:
    """The escape hatch names a database; it does not excuse an unusable URL."""
    with pytest.raises(NonDisposableDatabaseError):
        assert_disposable_database("", env={ALLOW_ENV_VAR: "alkera"})


def test_a_server_that_accepts_connections_is_reachable() -> None:
    """``server_reachable`` is only ever consulted to WAIVE a refusal, so what it must
    never do is call a live server dead."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert server_reachable(f"postgresql+psycopg://a:b@127.0.0.1:{port}/alkera") is True


def test_a_server_nothing_is_listening_on_is_not_reachable() -> None:
    assert server_reachable("postgresql+psycopg://a:b@127.0.0.1:1/alkera", timeout=1.0) is False


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(None, id="unset"),
        pytest.param("not a url at all", id="unparseable"),
        pytest.param("postgresql+psycopg:///alkera", id="no-host"),
    ],
)
def test_an_unreadable_url_counts_as_reachable(url: str | None) -> None:
    """Fail closed: the waiver needs proof that nothing is there, and a URL we cannot
    read a host out of is not proof."""
    assert server_reachable(url) is True


def test_ensure_prints_the_dsns_every_make_target_runs_on(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Makefile evals this output, so its shape is a contract: two assignments,
    the async one first, both naming the requested database on the given server.
    Provisioning is best effort — the drift job has no Postgres at all — so a dead
    server warns and still prints."""
    monkeypatch.delenv(ALLOW_ENV_VAR, raising=False)
    code = main(["ensure", "alkera_test_suite", "--from-url", DEAD_DSN.format(name="alkera")])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.splitlines() == [
        "DATABASE_URL=postgresql+asyncpg://alkera:hunter2@127.0.0.1:1/alkera_test_suite",
        "DATABASE_URL_SYNC=postgresql+psycopg://alkera:hunter2@127.0.0.1:1/alkera_test_suite",
    ]


def test_ensure_stops_at_a_socket_when_nothing_is_listening(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is asked of the driver until a plain socket has opened.

    "Is anything there" is one dial with a timeout we choose; handed to the
    driver instead it is whatever the platform does about a connection that never
    completes, which on one Windows runner was 130 s at the head of every make
    target. So the warning names the socket, not a driver error — and the DSNs
    are still printed, because provisioning is best effort and the drift job has
    no Postgres at all.
    """
    monkeypatch.delenv(ALLOW_ENV_VAR, raising=False)
    started = time.monotonic()
    code = main(["ensure", "alkera_test_suite", "--from-url", DEAD_DSN.format(name="alkera")])
    elapsed = time.monotonic() - started
    captured = capsys.readouterr()

    assert code == 0
    assert elapsed < UNANSWERED_BUDGET_SECONDS, (
        f"ensure spent {elapsed:.1f}s on a server nothing is listening on"
    )
    assert "nothing is listening" in captured.err
    assert "hunter2" not in captured.err
    assert captured.out.splitlines() == [
        "DATABASE_URL=postgresql+asyncpg://alkera:hunter2@127.0.0.1:1/alkera_test_suite",
        "DATABASE_URL_SYNC=postgresql+psycopg://alkera:hunter2@127.0.0.1:1/alkera_test_suite",
    ]


def test_ensure_gives_up_on_a_server_that_accepts_and_never_answers(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket that opens is not proof a server is there.

    This one accepts the connection and then never sends the startup reply, which
    the socket probe above cannot tell from a live Postgres — so the connection
    itself carries a timeout. Without one libpq waits for that reply forever and
    the make target never starts its tests.
    """
    monkeypatch.delenv(ALLOW_ENV_VAR, raising=False)
    with a_listener_that_never_answers() as port:
        silent = f"postgresql+psycopg://alkera:hunter2@127.0.0.1:{port}/alkera"
        started = time.monotonic()
        code = main(["ensure", "alkera_test_suite", "--from-url", silent])
        elapsed = time.monotonic() - started
    captured = capsys.readouterr()

    assert code == 0
    assert elapsed < UNANSWERED_BUDGET_SECONDS, (
        f"ensure spent {elapsed:.1f}s on a server that accepts and never answers"
    )
    assert captured.out.splitlines() == [
        f"DATABASE_URL=postgresql+asyncpg://alkera:hunter2@127.0.0.1:{port}/alkera_test_suite",
        f"DATABASE_URL_SYNC=postgresql+psycopg://alkera:hunter2@127.0.0.1:{port}/alkera_test_suite",
    ]


def test_ensure_refuses_to_hand_out_a_non_disposable_database(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A make target that asked for the dev database gets nothing to eval and a
    non-zero exit, rather than DSNs that would sail past the guard."""
    monkeypatch.delenv(ALLOW_ENV_VAR, raising=False)
    code = main(["ensure", "alkera", "--from-url", DEAD_DSN.format(name="alkera")])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.out == ""
    assert "alkera" in captured.err
    assert ALLOW_ENV_VAR in captured.err


def test_the_patterns_are_data_a_caller_can_read_and_replace() -> None:
    """The allowlist is data, not a chain of ifs — the refusal quotes it, and a
    caller with its own throwaway naming can pass its own."""
    assert "alkera_lane_*" in DISPOSABLE_DATABASE_PATTERNS
    assert is_disposable_database("wharrgarbl", patterns=("wharrgarbl",)) is True
    assert is_disposable_database("alkera_test", patterns=("nothing_matches",)) is False


@pytest.mark.parametrize(
    ("base", "worker", "expected"),
    [
        pytest.param(
            "alkera_test_suite", "gw0", "alkera_test_suite_gw0", id="the-make-target-base"
        ),
        pytest.param("alkera_test_lane_b", "gw12", "alkera_test_lane_b_gw12", id="a-lane-base"),
        pytest.param("alkera_lane_guard", "gw3", "alkera_lane_guard_gw3", id="a-fleet-lane-base"),
        pytest.param(None, "gw0", "alkera_gw0", id="an-unreadable-base-keeps-the-old-spelling"),
        pytest.param("", "gw1", "alkera_gw1", id="an-empty-base-keeps-the-old-spelling"),
    ],
)
def test_a_worker_database_is_scoped_under_its_session(
    base: str | None, worker: str, expected: str
) -> None:
    # Two sessions on one Postgres (two worktrees, two lanes) each get their own
    # worker databases; the old shared `alkera_gw0` let one run drop the other's.
    assert worker_database_name(base, worker) == expected


@pytest.mark.parametrize(
    "base",
    [
        pytest.param("alkera_test_suite", id="the-make-target-base"),
        pytest.param("alkera_test_lane_b", id="a-lane-base"),
        pytest.param("alkera_lane_guard", id="a-fleet-lane-base"),
        pytest.param("mydb_test", id="the-general-convention"),
        pytest.param("test_mine", id="the-other-general-convention"),
        pytest.param(None, id="the-fallback-spelling"),
    ],
)
def test_every_worker_database_a_session_can_provision_is_itself_disposable(
    base: str | None,
) -> None:
    # The worker DROPs its database at import, so the name it derives must pass
    # the same gate as the session's own — for every base the gate admits.
    if base is not None:
        assert is_disposable_database(base)
    assert is_disposable_database(worker_database_name(base, "gw0"))
    assert is_disposable_database(worker_database_name(base, "gw17"))


def test_two_sessions_never_share_a_worker_database() -> None:
    assert worker_database_name("alkera_test_lane_a", "gw0") != worker_database_name(
        "alkera_test_lane_b", "gw0"
    )
