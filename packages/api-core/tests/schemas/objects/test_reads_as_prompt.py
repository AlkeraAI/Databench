"""Which transcript rows read back as a person's message.

The server refuses a peer that publishes one, and the box's catch-up runs one
as the member it names, so both must read the same rows the same way.
"""

from __future__ import annotations

import pytest
from alkera_core.schemas.objects import reads_as_prompt


@pytest.mark.parametrize(
    ("role", "kind", "expected"),
    [
        pytest.param("user", "prompt", True, id="a-user-prompt"),
        pytest.param("user", "", True, id="a-user-row-of-no-kind"),
        pytest.param("user", None, True, id="a-user-row-naming-no-kind"),
        pytest.param("user", "message.created", False, id="the-harness-echo-of-a-message"),
        pytest.param("user", "answer", False, id="a-recorded-answer"),
        pytest.param("assistant", "prompt", False, id="not-a-person-speaking"),
        pytest.param("assistant", "", False, id="an-assistant-row-of-no-kind"),
        pytest.param("tool", None, False, id="a-tool-row"),
    ],
)
def test_reads_as_prompt(role: str, kind: str | None, expected: bool) -> None:
    assert reads_as_prompt(role=role, kind=kind) is expected
