"""Every backend advisory-lock key stays byte-identical across releases.

A rolling deploy runs old and new backend tasks against one database. If a
release derives a different key for the same resource, an old task and a new
one no longer exclude each other, and the invariant the lock protects (one
audit chain tail, an acyclic team tree, the CI-token
ceiling, the org-creation cap, one realtime document per kind) breaks for the
length of the deploy. The expected integers were computed by the code that
shipped before the derivation moved into ``alkera_core.db.locking``
(the KB keys are pinned beside the KB concurrency tests).
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

import pytest
from alkera_core.db.locking import advisory_key

A = UUID("00000000-0000-4000-8000-000000000001")
B = UUID("12345678-1234-5678-9abc-def012345678")


@pytest.mark.parametrize(
    ("derive", "expected_a", "expected_b"),
    [
        pytest.param(
            lambda org: advisory_key("audit-chain", org).value,
            -8744821190931909926,
            -1923577696494932869,
            id="audit-chain",
        ),
        pytest.param(
            lambda org: advisory_key("org-creation", org).value,
            -7307171052510043595,
            3776633096433816157,
            id="org-creation",
        ),
        pytest.param(
            lambda org: advisory_key("team-tree", org).value,
            1124628425256096784,
            -802889815494900973,
            id="team-tree",
        ),
        pytest.param(
            lambda org: advisory_key("ci-token-mint", org).value,
            -1432522322440073882,
            4448873619435926890,
            id="ci-token-mint",
        ),
        pytest.param(
            lambda org: (
                advisory_key("realtime-doc-creation", org, "notebook" if org == A else "text").value
            ),
            6429634549172864741,
            -6254785168294177744,
            id="realtime-doc-creation",
        ),
    ],
)
def test_lock_key_matches_the_shipped_derivation(
    derive: Callable[[UUID], int], expected_a: int, expected_b: int
) -> None:
    assert derive(A) == expected_a
    assert derive(B) == expected_b
