"""The words a machine's timeline speaks for each allocation edge."""

from __future__ import annotations

import pytest
from backend.services.compute.org_machine_reads import timeline_words


@pytest.mark.parametrize(
    ("from_state", "to_state", "reason", "idle", "words"),
    [
        pytest.param("pending", "provisioning", "", None, "Starting", id="starting"),
        pytest.param("bootstrapping", "ready", "claim", None, "Started", id="first-start"),
        pytest.param("asleep", "ready", "wake", None, "Started", id="woken"),
        pytest.param("draining", "asleep", "idle", 30, "Stopped: idle for 30 minutes", id="idle"),
        pytest.param("draining", "asleep", "idle", None, "Stopped: idle", id="idle-no-setting"),
        pytest.param("draining", "asleep", "credits", None, "Stopped: out of credits", id="credit"),
        pytest.param("draining", "asleep", "cap", None, "Stopped: monthly cap reached", id="cap"),
        pytest.param(
            "draining", "asleep", "free_expired", None, "Stopped: free period ended", id="free"
        ),
        pytest.param("draining", "asleep", "move", None, "Stopped: workspace moved", id="moved"),
        pytest.param("draining", "asleep", "user", None, "Stopped", id="user-stop"),
        pytest.param(
            "provisioning",
            "failed",
            "provider_capacity",
            None,
            "Couldn't start: no hardware available",
            id="capacity",
        ),
        pytest.param(
            "bootstrapping",
            "failed",
            "boot_failed",
            None,
            "Couldn't start: it didn't finish starting",
            id="boot",
        ),
        pytest.param(
            "provisioning",
            "failed",
            "no node bundle for linux-x64 in /opt/alkera-node-bundle",
            None,
            "Couldn't start: no node bundle for linux-x64 in /opt/alkera-node-bundle",
            id="the-providers-words",
        ),
        pytest.param("provisioning", "failed", "", None, "Couldn't start", id="no-reason"),
        pytest.param(
            "releasing", "released", "replaced", None, "Replaced with new hardware", id="replaced"
        ),
        pytest.param("releasing", "released", "delete", None, "Released", id="released"),
        pytest.param("ready", "lost", "gone", None, "Lost by the provider", id="lost"),
        pytest.param("ready", "draining", "user", None, None, id="drain-is-not-news"),
        pytest.param("provisioning", "bootstrapping", "", None, None, id="boot-step-is-not-news"),
    ],
)
def test_timeline_words(
    from_state: str, to_state: str, reason: str, idle: int | None, words: str | None
) -> None:
    assert timeline_words(from_state, to_state, reason, idle_stop_minutes=idle) == words
