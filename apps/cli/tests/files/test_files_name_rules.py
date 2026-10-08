"""The naming rule a box applies before it pushes is the server's own."""

from __future__ import annotations

import pytest
from alkera_cli.files.name_rules import files_own_refusal_code, name_refusal
from alkera_core.files import names


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        pytest.param(b"report.txt", None, id="plain"),
        pytest.param("café.txt".encode(), None, id="non-ascii-utf8"),
        pytest.param(b" leading", "surrounding_space", id="leading-space"),
        pytest.param(b"trailing ", "surrounding_space", id="trailing-space"),
        pytest.param(b"a\tb", "control", id="control-character"),
        pytest.param(b"..", "dot", id="dotdot"),
        pytest.param(b"bad\xffutf8", "not UTF-8", id="not-utf8"),
        pytest.param(b"a" * (names.NAME_MAX_BYTES + 1), "too_long", id="too-long"),
    ],
)
def test_a_name_is_refused_exactly_when_the_server_would_refuse_it(
    name: bytes, reason: str | None
) -> None:
    assert name_refusal(name) == reason


@pytest.mark.parametrize(
    ("code", "own"),
    [
        pytest.param("files.invalid_name.surrounding_space", True, id="a-name-rule"),
        pytest.param("files.parts_mismatch", True, id="the-commits-parts"),
        pytest.param("files.too_large", True, id="a-file-past-the-size-ceiling"),
        pytest.param("files.lease_fenced", False, id="the-fence-is-the-folders"),
        pytest.param("files.quota_exceeded", False, id="the-quota-is-every-files"),
        pytest.param(None, False, id="no-code"),
    ],
)
def test_which_refusals_belong_to_one_file(code: str | None, own: bool) -> None:
    assert files_own_refusal_code(code) is own
