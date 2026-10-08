"""The settings fence: finding it, reading its TOML, and placing it."""

from __future__ import annotations

from typing import Any

import pytest
from alkera_notebook.format.header import (
    FENCE_CLOSE,
    FENCE_OPEN,
    assemble_header,
    default_settings,
    fence_position,
    parse_settings,
    render_fence,
    split_header,
)


def fence(*lines: str) -> str:
    return "\n".join([FENCE_OPEN, *lines, FENCE_CLOSE])


# ---- split_header ------------------------------------------------------------------


def test_the_fence_is_removed_and_its_body_returned() -> None:
    region = (
        "#!/usr/bin/env python\n" + fence('# format = "1.0"', "#", "# a = 1") + '\n"""Doc."""\n\n'
    )
    parts = split_header(region)
    assert parts.header_text == '#!/usr/bin/env python\n"""Doc."""\n'
    assert parts.fence_body == 'format = "1.0"\n\na = 1'
    assert parts.fence_line == 2
    assert parts.violations == []


def test_no_fence() -> None:
    parts = split_header("# just a comment\n")
    assert (parts.header_text, parts.fence_body) == ("# just a comment\n", None)


def test_empty_region() -> None:
    parts = split_header("")
    assert (parts.header_text, parts.fence_body, parts.violations) == ("", None, [])


@pytest.mark.parametrize(
    ("region", "code"),
    [
        pytest.param(FENCE_OPEN + '\n# format = "1.0"\n', "settings_unclosed", id="unclosed"),
        pytest.param(
            FENCE_OPEN + "\nx = 1\n" + FENCE_CLOSE + "\n", "settings_line", id="code-line"
        ),
    ],
)
def test_broken_fences_are_ignored_and_kept_as_text(region: str, code: str) -> None:
    parts = split_header(region)
    assert parts.fence_body is None
    assert [v.code for v in parts.violations] == [code]
    assert parts.header_text == region


def test_a_second_fence_is_kept_as_comments() -> None:
    second = fence('# format = "9.9"')
    parts = split_header(fence('# format = "1.0"') + "\n" + second + "\n")
    assert parts.fence_body == 'format = "1.0"'
    assert parts.header_text == second + "\n"
    assert [v.code for v in parts.violations] == ["settings_duplicate"]


def test_a_line_without_the_space_is_read_and_reported() -> None:
    parts = split_header(fence("#format = '1.0'") + "\n")
    assert parts.fence_body == "format = '1.0'"
    assert [v.code for v in parts.violations] == ["settings_line"]


def test_fence_lines_must_match_exactly() -> None:
    region = "#  >>> alkera\n# format = 1\n# <<< alkera \n"
    assert split_header(region).fence_body is None


# ---- parse_settings -------------------------------------------------------------------


def test_absent_fence_gives_defaults() -> None:
    settings = parse_settings(None)
    assert (settings.format, settings.values, settings.unknown) == ("1.0", default_settings(), "")


def test_every_known_key() -> None:
    body = (
        'format = "1.3"\nreactivity = "lazy"\ndataframe = "pandas"\nenv = "./env"\n'
        'outputs_in_git = true\nautoreload = "on"'
    )
    settings = parse_settings(body)
    assert settings.format == "1.3"
    assert settings.values == {
        "reactivity": "lazy",
        "dataframe": "pandas",
        "env": "./env",
        "outputs_in_git": True,
        "autoreload": "on",
    }
    assert settings.violations == []


@pytest.mark.parametrize(
    ("line", "key"),
    [
        pytest.param("reactivity = 1", "reactivity", id="reactivity-int"),
        pytest.param('reactivity = "eager"', "reactivity", id="reactivity-unknown"),
        pytest.param('dataframe = "arrow"', "dataframe", id="dataframe"),
        pytest.param('env = "/abs"', "env", id="env-absolute"),
        pytest.param('env = "env"', "env", id="env-bare"),
        pytest.param("env = 1", "env", id="env-int"),
        pytest.param('outputs_in_git = "true"', "outputs_in_git", id="outputs-string"),
        pytest.param("autoreload = true", "autoreload", id="autoreload-bool"),
    ],
)
def test_wrong_typed_values_fall_back_to_the_default(line: str, key: str) -> None:
    settings = parse_settings(line)
    assert settings.values == default_settings()
    assert [v.code for v in settings.violations] == ["settings_value"]
    assert key in settings.violations[0].message
    # A dropped value is not kept as unknown text (it would duplicate the key).
    assert settings.unknown == ""


@pytest.mark.parametrize(
    "value",
    ["1", '"1"', '"1.0.0"', '"v1.0"', '"01.0"', '""'],
    ids=["int", "no-minor", "patch", "prefix", "leading-zero", "empty"],
)
def test_malformed_format_versions(value: str) -> None:
    settings = parse_settings(f"format = {value}")
    assert settings.format == "1.0"
    assert [v.code for v in settings.violations] == ["format_version"]


@pytest.mark.parametrize("env", ["default", "script", "./venv", "../shared/env"])
def test_valid_env_values(env: str) -> None:
    assert parse_settings(f'env = "{env}"').values["env"] == env


def test_unknown_keys_keep_their_order_spelling_and_comments() -> None:
    body = 'zeta = 1\nformat = "1.0"\n# a note\nalpha  =  "x"\n\n[table]\nk = [1, 2]'
    settings = parse_settings(body)
    assert settings.unknown == 'zeta = 1\n# a note\nalpha  =  "x"\n\n[table]\nk = [1, 2]'


def test_invalid_toml_keeps_known_lines_and_the_rest_verbatim() -> None:
    settings = parse_settings('format = "1.0"\nreactivity = "lazy"\nthis = = broken\nx = 1')
    assert settings.values["reactivity"] == "lazy"
    assert settings.unknown == "this = = broken\nx = 1"
    assert [v.code for v in settings.violations] == ["settings_toml"]


# ---- render and place -----------------------------------------------------------------


def test_render_writes_known_keys_in_order_without_defaults_then_unknown() -> None:
    values: dict[str, Any] = {
        "autoreload": "on",
        "reactivity": "autorun",
        "dataframe": "polars",
        "outputs_in_git": False,
    }
    assert render_fence("1.0", values, "x = 1\n\n[t]\ny = 2\n") == (
        "# >>> alkera\n"
        '# format = "1.0"\n'
        '# dataframe = "polars"\n'
        '# autoreload = "on"\n'
        "# x = 1\n"
        "#\n"
        "# [t]\n"
        "# y = 2\n"
        "# <<< alkera\n"
    )


def test_render_skips_invalid_values() -> None:
    assert render_fence("1.0", {"reactivity": "eager", "env": None}, "") == (
        '# >>> alkera\n# format = "1.0"\n# <<< alkera\n'
    )


def test_render_then_parse_round_trips() -> None:
    values = {"reactivity": "lazy", "env": "../e", "outputs_in_git": True}
    unknown = 'k = "v"\n[t]\nn = 1'
    rendered = render_fence("1.2", values, unknown)
    parts = split_header(rendered)
    settings = parse_settings(parts.fence_body)
    assert settings.format == "1.2"
    assert {k: settings.values[k] for k in values} == values
    assert settings.unknown == unknown


@pytest.mark.parametrize(
    ("lines", "position"),
    [
        pytest.param([], 0, id="empty"),
        pytest.param(['"""doc"""'], 0, id="docstring"),
        pytest.param(["#!/usr/bin/env python"], 1, id="shebang"),
        pytest.param(["#!/usr/bin/env python", "# -*- coding: utf-8 -*-"], 2, id="shebang-coding"),
        pytest.param(["# vim: set fileencoding=utf-8 :"], 1, id="coding-first"),
        pytest.param(["#!/x", "# a", "# coding: utf-8"], 1, id="coding-too-late"),
        pytest.param(["# /// script", "# dependencies = []", "# ///", "# note"], 3, id="pep723"),
        pytest.param(["#!/x", "# /// script", "# ///"], 3, id="shebang-pep723"),
        pytest.param(["# /// script", "x = 1", "# ///"], 0, id="broken-pep723"),
    ],
)
def test_fence_position(lines: list[str], position: int) -> None:
    assert fence_position(lines) == position


def test_assemble_places_the_fence_and_one_blank_line() -> None:
    text = assemble_header("#!/x\n# licence\n\n\n", fence('# format = "1.0"') + "\n")
    assert text == '#!/x\n# >>> alkera\n# format = "1.0"\n# <<< alkera\n# licence\n\n'


# -- what the file itself sets -------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "set_keys"),
    [
        pytest.param('format = "1.0"\ndataframe = "pandas"', {"dataframe"}, id="one-key"),
        pytest.param('format = "1.0"\nreactivity = "autorun"', {"reactivity"}, id="a-default"),
        pytest.param('format = "1.0"\nreactivity = 3', set(), id="an-invalid-value"),
        pytest.param('format = "1.0"', set(), id="nothing"),
        pytest.param(None, set(), id="no-fence"),
    ],
)
def test_the_reader_says_which_settings_the_file_sets(body: str | None, set_keys: set[str]) -> None:
    """Values hold every setting, defaults filled in; ``set_keys`` names only
    what the fence itself sets, a value equal to the default included."""
    read = parse_settings(body)
    assert read.set_keys == set_keys
    assert set(read.values) >= {"reactivity", "dataframe"}


def test_a_document_of_a_file_holds_only_what_the_file_sets() -> None:
    """A document built from a read never takes the defaults the reader filled
    in as settings of the file: they would read back as set by it."""
    from alkera_notebook import format as notebook_format
    from alkera_notebook.document.convert import document_from_ir
    from alkera_notebook.document.fmt import default_format

    text = (
        '# >>> alkera\n# format = "1.0"\n# dataframe = "pandas"\n# <<< alkera\n'
        "import marimo\n\napp = marimo.App()\n"
    )
    ir = notebook_format.read(text)
    assert ir.settings["reactivity"] == "autorun"
    assert notebook_format.file_settings(ir) == {"dataframe": "pandas"}
    doc = document_from_ir(ir, default_format())
    assert doc.settings == {"dataframe": "pandas", "format": "1.0"}
