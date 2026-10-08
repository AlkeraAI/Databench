"""``set_config`` takes marimo's cell options only, and its refusal says so:
it names the keys it takes and sends a SQL or Markdown setting to
``set_meta``. Every applier raises this one refusal."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_notebook.format.op_rules import (
    CONFIG_TYPES,
    META_KEYS,
    OpRuleError,
    check_config_keys,
)


@pytest.mark.parametrize("config", [{}, {"disabled": True}, {k: None for k in CONFIG_TYPES}])
def test_marimos_options_pass(config: dict[str, Any]) -> None:
    check_config_keys(config)


@pytest.mark.parametrize(
    ("config", "says"),
    [
        *(
            pytest.param(
                {key: True}, f"{key} is a SQL cell setting: change it with set_meta", id=key
            )
            for key in sorted(META_KEYS["sql"])
        ),
        pytest.param({"quote": "rf"}, "quote is a Markdown cell setting", id="quote"),
        pytest.param({"colour": "red"}, "set_config has no key colour", id="unknown"),
        pytest.param({"disabled": True, "show_output": False}, "show_output is a SQL", id="mixed"),
    ],
)
def test_a_key_set_config_does_not_take_is_refused_with_what_it_does_take(
    config: dict[str, Any], says: str
) -> None:
    with pytest.raises(OpRuleError) as refused:
        check_config_keys(config)
    assert refused.value.code == "invalid_config"
    assert says in refused.value.message
    assert "takes column, disabled, hide_code, expand_output" in refused.value.message
