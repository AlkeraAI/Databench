"""Write ``facts.json``: statuses exactly as the server's reads carry them.

The web's tests stand in for the server with these facts, so a test there
renders words a server sends. ``test_status_fixture.py`` builds every entry
again and fails on a difference; rerun this after a vocabulary change:

    uv run python packages/api-core/tests/fixtures/status/generate.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alkera_core.status import (
    Generic,
    MachineEvidence,
    StatusFact,
    fleet_status,
    machine_status,
    registered,
)

HERE = Path(__file__).resolve().parent
NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)

#: Named facts of a subject: the arguments the vocabulary builds each from.
NAMED: dict[str, dict[str, dict[str, Any]]] = {
    "chat": {
        "working": {"state": "working"},
        "awake": {"state": "awake"},
        "asleep": {"state": "asleep"},
        "waking": {"state": "waking"},
        "new_chat": {"state": "starting"},
        "queued": {"state": "queued"},
        "stalled_turn_silent": {"state": "stalled", "reason": "turn_silent"},
        "stalled_turn_not_started": {
            "state": "stalled",
            "reason": "turn_not_started",
            "names": {"machine": "lab-b"},
        },
        "starting": {
            "state": "waking",
            "reason": "machine_starting",
            "names": {"machine": "lab-b"},
        },
        "starting_unnamed": {
            "state": "waking",
            "reason": "machine_starting",
            "names": {"machine": "the machine"},
            "generic": ["machine"],
        },
        "draining": {"state": "awake", "reason": "machine_draining", "names": {"machine": "lab-b"}},
        "restarting": {
            "state": "awake",
            "reason": "machine_restarting",
            "names": {"machine": "lab-b"},
        },
        "stopped_credits": {"state": "stopped", "reason": "credits_exhausted"},
        "no_machine": {"state": "unavailable", "reason": "no_machine"},
        "unreachable": {
            "state": "unavailable",
            "reason": "machine_unreachable",
            "names": {"machine": "lab-b"},
        },
        "released": {"state": "unavailable", "reason": "machine_released"},
        "refused": {"state": "unavailable", "reason": "refused"},
        "refused_moving": {"state": "unavailable", "reason": "refused_moving"},
        "waiting": {"state": "waiting", "reason": "not_yet"},
        "waiting_files": {"state": "waiting", "reason": "files_unreachable"},
        "waiting_workspace_elsewhere": {"state": "waiting", "reason": "workspace_elsewhere"},
    },
    "workspace": {
        "working": {"state": "working"},
        "awake": {"state": "awake", "names": {"machine": "lab-b"}},
        "asleep": {"state": "asleep"},
        "waking": {"state": "waking"},
        "stalled": {"state": "stalled", "reason": "turn_silent"},
        "sync_paused": {
            "state": "sync_paused",
            "reason": "worker_silent",
            "names": {"machine": "lab-b"},
        },
        "moving_saving": {
            "state": "moving",
            "reason": "saving_chats",
            "names": {"target": "lab-a"},
        },
        "moving_waking": {
            "state": "moving",
            "reason": "waking_on_target",
            "names": {"target": "lab-a"},
        },
        "unreachable": {
            "state": "unavailable",
            "reason": "machine_unreachable",
            "names": {"machine": "lab-b"},
        },
    },
    "files_live": {
        "live": {"state": "live", "names": {"machine": "box-1"}},
        "live_landing": {"state": "live", "reason": "landing", "names": {"machine": "box-1"}},
        "paused_behind": {
            "state": "sync_paused",
            "reason": "behind",
            "names": {"machine": "box-1"},
        },
        "paused_silent": {
            "state": "sync_paused",
            "reason": "worker_silent",
            "names": {"machine": "box-1"},
        },
        "paused_unreachable": {
            "state": "sync_paused",
            "reason": "machine_unreachable",
            "names": {"machine": "box-1"},
        },
        "saved_copy": {"state": "saved_copy", "names": {"machine": "box-1"}},
    },
}

ALLOCATION_STATES = (
    "pending",
    "provisioning",
    "bootstrapping",
    "ready",
    "draining",
    "asleep",
    "failed",
    "lost",
    "releasing",
    "released",
)
LIVENESS = ("starting", "ready", "draining", "restarting", "unreachable", "asleep", "none")
CARD_STATES = (
    "starting",
    "running",
    "unreachable",
    "stopping",
    "stopped",
    "waiting_for_hardware",
    "failed",
    "deleted",
    "shared",
)
STOP_REASONS = ("", "user", "idle", "credits", "cap", "free_expired", "provider")


def _dump(fact: StatusFact) -> dict[str, Any]:
    dumped: dict[str, Any] = json.loads(fact.model_dump_json())
    return dumped


def named(subject: str, build: dict[str, Any]) -> StatusFact:
    generic = set(build.get("generic", ()))
    names = {
        key: Generic(value) if key in generic else value
        for key, value in build.get("names", {}).items()
    }
    return registered()[subject].fact(build["state"], reason=build.get("reason", ""), **names)


def corpus() -> dict[str, Any]:
    out: dict[str, Any] = {
        subject: {
            name: {"build": build, "fact": _dump(named(subject, build))}
            for name, build in cases.items()
        }
        for subject, cases in NAMED.items()
    }
    #: Every fleet row the console can draw, keyed "state|liveness|wake".
    out["fleet"] = {
        f"{state}|{liveness}|{wake}": _dump(
            fleet_status(
                allocation_state=state,
                liveness=liveness,
                name="box",
                revoked=False,
                wake_pending=wake == "wake",
            )
        )
        for state in ALLOCATION_STATES
        for liveness in LIVENESS
        for wake in ("", "wake")
    }
    out["fleet"]["revoked"] = _dump(
        fleet_status(
            allocation_state=None, liveness="none", name="box", revoked=True, wake_pending=False
        )
    )
    #: Every card state an org machine's card can carry, keyed "state|stop_reason".
    out["machine"] = {
        f"{state}|{reason}": _dump(
            machine_status(MachineEvidence(state=state, name="lab-b", stop_reason=reason), now=NOW)
        )
        for state in CARD_STATES
        for reason in STOP_REASONS
    }
    return out


def render() -> str:
    return json.dumps(corpus(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


if __name__ == "__main__":
    (HERE / "facts.json").write_text(render(), encoding="utf-8")
