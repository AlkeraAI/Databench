"""The idempotency claim under a real two-session race.

Lives beside the concurrency kit because it needs `sessions(n)`: two coroutines
on one `AsyncSession` would be serialized by SQLAlchemy and the interleaving
under test — B's INSERT arriving while A still holds its claim uncommitted —
could never happen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from alkera_core.authz.principal import ActingContext
from alkera_core.files.checkpoints import PausingCheckpoints
from alkera_core.files.idempotency import AFTER_INSERT, StoredResponse, idempotent
from alkera_core.files.repo import FilesRepo
from sqlalchemy.ext.asyncio import AsyncSession
from tests.files.test_files_idempotency import ROUTE, Runner, _effects

pytest_plugins = ["tests.files.test_files_idempotency"]


async def test_two_identical_requests_in_flight_produce_one_effect(
    repo_factory: Callable[[AsyncSession], FilesRepo],
    ctx: ActingContext,
    files_session: AsyncSession,
    effects_table: None,
    sessions: Callable[..., Awaitable[list[AsyncSession]]],
    checkpoints: PausingCheckpoints,
) -> None:
    """The forced interleaving: A holds the claim while B tries to make it."""
    session_a, session_b = await sessions(2)
    repo_a, repo_b = repo_factory(session_a), repo_factory(session_b)
    runner_a, runner_b = Runner(repo_a, "race"), Runner(repo_b, "race")
    checkpoints.pause(AFTER_INSERT)

    async def call(repo: FilesRepo, runner: Runner) -> StoredResponse:
        return await idempotent(
            repo,
            ctx,
            route=ROUTE,
            key="k-race",
            request_hash=b"\x55",
            run=runner,
            checkpoints=checkpoints,
        )

    task_a = asyncio.create_task(call(repo_a, runner_a))
    await checkpoints.wait_paused(AFTER_INSERT)
    task_b = asyncio.create_task(call(repo_b, runner_b))
    checkpoints.release(AFTER_INSERT)
    answer_a, answer_b = await asyncio.gather(task_a, task_b)

    assert answer_a == answer_b
    assert answer_a.body == answer_b.body
    assert answer_a.headers == answer_b.headers
    assert runner_a.calls + runner_b.calls == 1
    assert await _effects(files_session, "race") == 1


async def test_fifty_replays_of_twenty_keys_across_four_sessions_run_once_each(
    repo_factory: Callable[[AsyncSession], FilesRepo],
    ctx: ActingContext,
    files_session: AsyncSession,
    effects_table: None,
    sessions: Callable[..., Awaitable[list[AsyncSession]]],
) -> None:
    """The stress: effects == distinct keys, however the 1,000 calls interleave."""
    pool = await sessions(4)
    repos = [repo_factory(session) for session in pool]
    runners = [Runner(repo, "stress") for repo in repos]
    keys = [f"k-stress-{n:02d}" for n in range(20)]

    async def worker(index: int) -> list[StoredResponse]:
        answers = []
        for replay in range(50):
            key = keys[(index + replay) % len(keys)]
            answers.append(
                await idempotent(
                    repos[index],
                    ctx,
                    route=ROUTE,
                    key=key,
                    request_hash=b"\x99",
                    run=runners[index],
                )
            )
        return answers

    results = await asyncio.gather(*(worker(index) for index in range(4)))

    assert sum(runner.calls for runner in runners) == len(keys)
    assert await _effects(files_session, "stress") == len(keys)
    bodies = {answer.body for batch in results for answer in batch}
    assert bodies == {b'{"id":"fixed"}'}
