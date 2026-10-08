"""The supervisor names a host it is not ready for, once, before any worker.

A box whose staged prerequisites predate the org id grant had every org
worker die at ``newuidmap`` with ``uid range ... not allowed``, one crash loop
per org and no word for why. The supervisor now reads the grants at startup;
a box started as the supervisor by its start command becomes the single
daemon instead, and one started by name stops with a status of its own.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from collections.abc import MutableMapping, Sequence
from pathlib import Path

import pytest
from alkera_cli.supervisor import cli, org_events, preflight
from alkera_cli.supervisor.slots import MAX_SLOTS, ORG_UID_BASE, ORG_UID_SPAN
from alkera_core.compute.bootstrap import sandbox_prereqs_script
from alkera_core.compute.box_contract import START_MODE_ENV
from alkera_core.compute.box_logs import EVENT_FIELDS, PREREQUISITES_MISSING

pytestmark = [pytest.mark.spread]

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash"
)

FULL = f"root:{ORG_UID_BASE}:{MAX_SLOTS * ORG_UID_SPAN}\n"
HIGH = ORG_UID_BASE + MAX_SLOTS * ORG_UID_SPAN


def _reader(subuid: str, subgid: str):  # type: ignore[no-untyped-def]
    files = {Path("/etc/subuid"): subuid, Path("/etc/subgid"): subgid}
    return lambda path: files[path]


@pytest.mark.parametrize(
    ("subuid", "subgid", "missing"),
    [
        pytest.param(FULL, FULL, [], id="both-granted"),
        pytest.param(
            "ubuntu:100000:65536\n", FULL, ["/etc/subuid"], id="stale-prereqs-no-root-uid-grant"
        ),
        pytest.param("", "", ["/etc/subuid", "/etc/subgid"], id="no-grants-at-all"),
        pytest.param(f"root:{ORG_UID_BASE}:65536\n", FULL, ["/etc/subuid"], id="one-slot-only"),
        pytest.param(
            f"root:{ORG_UID_BASE}:{(MAX_SLOTS // 2) * ORG_UID_SPAN}\n"
            f"root:{ORG_UID_BASE + (MAX_SLOTS // 2) * ORG_UID_SPAN}:"
            f"{(MAX_SLOTS // 2) * ORG_UID_SPAN}\n",
            FULL,
            [],
            id="two-adjacent-grants-cover-it",
        ),
        pytest.param(
            f"root:{ORG_UID_BASE + 1}:{MAX_SLOTS * ORG_UID_SPAN}\n",
            FULL,
            ["/etc/subuid"],
            id="starts-one-id-late",
        ),
        pytest.param(
            f"alkera:{ORG_UID_BASE}:{MAX_SLOTS * ORG_UID_SPAN}\n",
            FULL,
            ["/etc/subuid"],
            id="granted-to-another-user",
        ),
        pytest.param(f"0:{ORG_UID_BASE}:{MAX_SLOTS * ORG_UID_SPAN}\n", FULL, [], id="root-by-uid"),
    ],
)
def test_the_id_grants_are_checked_for_the_whole_org_block(
    subuid: str, subgid: str, missing: list[str]
) -> None:
    faults = preflight.missing_prerequisites(_reader(subuid, subgid))
    assert [fault.split(" ", 1)[0] for fault in faults] == missing
    for fault in faults:
        assert f"ids {ORG_UID_BASE} to {HIGH - 1}" in fault
        assert "sandbox-prereqs.sh" in fault


@needs_bash
def test_the_prerequisites_grant_step_satisfies_the_check(tmp_path: Path) -> None:
    """The step a stale box lacked is the one the current script carries:
    run it over the grants that box had, and the check passes."""
    script = sandbox_prereqs_script()
    step = re.search(r"for ids in /etc/subuid /etc/subgid; do\n.*?\ndone\n", script, re.S)
    assert step is not None, "the prerequisites script no longer grants the org ids"
    base = re.search(r"^ORG_UID_BASE=(\d+)$", script, re.M)
    total = re.search(r"^ORG_UID_TOTAL=(\d+)$", script, re.M)
    assert base is not None and total is not None
    subuid, subgid = tmp_path / "subuid", tmp_path / "subgid"
    for path in (subuid, subgid):
        path.write_text("ubuntu:100000:65536\n", encoding="utf-8")
    stale = {Path("/etc/subuid"): subuid, Path("/etc/subgid"): subgid}
    assert len(preflight.missing_prerequisites(lambda p: stale[p].read_text())) == 2

    body = step.group(0).replace("/etc/subuid /etc/subgid", f"{subuid} {subgid}")
    subprocess.run(
        ["bash", "-c", f"ORG_UID_BASE={base.group(1)}\nORG_UID_TOTAL={total.group(1)}\n{body}"],
        check=True,
        timeout=30,
    )
    assert preflight.missing_prerequisites(lambda p: stale[p].read_text()) == []


class _FallBack:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def __call__(self, env: MutableMapping[str, str]) -> None:
        self.calls.append(env.get(START_MODE_ENV))


def test_a_host_without_the_grants_starts_no_worker_and_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[object] = []

    async def never(**_kwargs: object) -> int:
        started.append(True)
        return 0

    events = logging.getLogger(org_events.LOGGER)
    monkeypatch.setattr(events, "handlers", [])
    monkeypatch.setattr(events, "propagate", True)
    monkeypatch.setattr(cli, "run_supervisor", never)
    monkeypatch.setenv(START_MODE_ENV, "supervise")
    fall_back = _FallBack()

    code = cli.main([], missing=lambda: ["/etc/subuid grants root nothing"], fall_back=fall_back)

    assert code == cli.EXIT_PREREQUISITES_MISSING
    assert started == []
    assert fall_back.calls == ["supervise"]
    lines = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("{") and PREREQUISITES_MISSING in line
    ]
    assert len(lines) == 1
    shipped = {k: v for k, v in lines[0].items() if k in EVENT_FIELDS[PREREQUISITES_MISSING]}
    assert shipped == {"error": "/etc/subuid grants root nothing"}


def test_a_ready_host_runs_the_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ran(**_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(cli, "run_supervisor", ran)
    fall_back = _FallBack()
    assert cli.main([], missing=list, fall_back=fall_back) == 0
    assert fall_back.calls == []


@pytest.mark.parametrize(
    ("mode", "replaced", "mode_after"),
    [
        pytest.param("supervise", True, "single", id="started-by-the-start-command"),
        pytest.param(None, False, None, id="started-by-name"),
        pytest.param("single", False, "single", id="a-single-node"),
    ],
)
def test_only_a_supervisor_the_start_command_made_becomes_the_single_daemon(
    mode: str | None, replaced: bool, mode_after: str | None
) -> None:
    env: dict[str, str] = {} if mode is None else {START_MODE_ENV: mode}
    argvs: list[Sequence[str]] = []
    cli.fall_back_to_the_single_daemon(env, replace=argvs.append)
    assert [list(argv)[1:] for argv in argvs] == ([["cloud-mirror", "run"]] if replaced else [])
    assert env.get(START_MODE_ENV) == mode_after


def test_the_event_is_one_the_server_takes() -> None:
    assert PREREQUISITES_MISSING in org_events.EVENTS
    assert json.dumps(sorted(EVENT_FIELDS[PREREQUISITES_MISSING])) == '["error"]'
