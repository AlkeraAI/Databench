"""A leased folder reads live only while its machine is writing and answering."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from itertools import product

import pytest
from alkera_core.status import FILES_LIVE_STATUS, FilesLiveEvidence, files_live_status

TAKEN = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
SYNCED = datetime(2026, 10, 6, 12, 5, 0, tzinfo=UTC)

#: A box streaming a chat's folder, beating and caught up.
LIVE = FilesLiveEvidence(
    live=True,
    beating=True,
    behind=False,
    since=TAKEN,
    last_sync_at=SYNCED,
    machine_name="lab-b",
)


def read(evidence: FilesLiveEvidence) -> tuple[str, str, str, str]:
    fact = files_live_status(evidence)
    return (fact.state, fact.reason_code, fact.tone, fact.sentence)


@pytest.mark.parametrize(
    ("evidence", "expected", "since"),
    [
        pytest.param(
            LIVE,
            ("live", "", "success", "lab-b is writing these files as it works."),
            TAKEN,
            id="live",
        ),
        pytest.param(
            replace(LIVE, landing=3),
            (
                "live",
                "landing",
                "success",
                "lab-b is writing these files as it works. Some are still on their way to Files.",
            ),
            TAKEN,
            id="live-with-files-landing",
        ),
        pytest.param(
            replace(LIVE, behind=True),
            (
                "sync_paused",
                "behind",
                "warning",
                "lab-b is behind on syncing. These are the files it last saved.",
            ),
            SYNCED,
            id="beating-but-behind",
        ),
        pytest.param(
            replace(LIVE, beating=False),
            (
                "sync_paused",
                "worker_silent",
                "warning",
                "lab-b has stopped syncing. These are the files it last saved.",
            ),
            SYNCED,
            id="holder-gone-quiet",
        ),
        pytest.param(
            replace(LIVE, machine_unreachable=True, landing=2),
            (
                "sync_paused",
                "machine_unreachable",
                "warning",
                "lab-b isn't responding. These are the files it last saved.",
            ),
            SYNCED,
            id="machine-unreachable-outranks-a-beat-it-last-sent",
        ),
        pytest.param(
            replace(LIVE, beating=False, last_sync_at=None),
            (
                "sync_paused",
                "worker_silent",
                "warning",
                "lab-b has stopped syncing. These are the files it last saved.",
            ),
            TAKEN,
            id="never-synced-is-paused-since-the-lease",
        ),
        pytest.param(
            replace(LIVE, live=False, beating=False, behind=True),
            (
                "saved_copy",
                "",
                "neutral",
                "lab-b holds this folder and saves it from time to time. "
                "These are the files it last saved.",
            ),
            SYNCED,
            id="checkpoint-lease-is-a-saved-copy-whatever-its-beat",
        ),
        pytest.param(
            replace(LIVE, machine_name=""),
            ("live", "", "success", "The machine is writing these files as it works."),
            TAKEN,
            id="unnamed-machine",
        ),
    ],
)
def test_the_folder_reads_what_its_lease_proves(
    evidence: FilesLiveEvidence, expected: tuple[str, str, str, str], since: datetime
) -> None:
    fact = files_live_status(evidence)
    assert read(evidence) == expected
    assert fact.since == since
    assert fact.subject == "files_live"


def test_every_combination_reads_a_registered_state() -> None:
    for live, beating, behind, unreachable, landing in product(
        (True, False), (True, False), (True, False), (True, False), (0, 1)
    ):
        fact = files_live_status(
            FilesLiveEvidence(
                live=live,
                beating=beating,
                behind=behind,
                machine_unreachable=unreachable,
                landing=landing,
            )
        )
        assert fact.state in FILES_LIVE_STATUS.states
        if fact.state == "live":
            assert live and beating and not behind and not unreachable
