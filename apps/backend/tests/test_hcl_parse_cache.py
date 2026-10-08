"""The memoized HCL parse the terraform shape tests share.

It is only safe to hand every test the same parse if no test can see what
another one did to its tree, and if a changed file is read as changed.
"""

from __future__ import annotations

import pytest
from lark.exceptions import LarkError
from tests import _hcl as hcl

pytestmark = [pytest.mark.spread]

_TEXT = 'locals {\n  retention = 365\n  hosts = ["app", "api"]\n}\n'


def test_a_tree_one_caller_changes_is_not_the_tree_the_next_caller_gets() -> None:
    first = hcl.loads(_TEXT)
    first["locals"][0]["retention"] = 1
    first["locals"][0]["hosts"].append("gateway")
    first["resource"] = []

    second = hcl.loads(_TEXT)

    assert second["locals"][0]["retention"] == 365
    assert second["locals"][0]["hosts"] == ['"app"', '"api"']
    assert "resource" not in second


def test_new_text_is_parsed_again_rather_than_served_from_the_old_parse() -> None:
    assert hcl.loads(_TEXT)["locals"][0]["retention"] == 365
    assert hcl.loads(_TEXT.replace("365", "90"))["locals"][0]["retention"] == 90


def test_crlf_text_reads_the_same_as_lf() -> None:
    assert hcl.loads(_TEXT.replace("\n", "\r\n")) == hcl.loads(_TEXT)


def test_text_the_parser_rejects_still_raises_every_time() -> None:
    broken = 'locals {\n  x = "${\n  var.a}"\n'
    for _ in range(2):
        with pytest.raises(LarkError):
            hcl.loads(broken)
