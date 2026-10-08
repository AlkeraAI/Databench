"""A derived member is rendered from the row, and the same bytes every time.

A chat template's ``README.md`` is never written anywhere: it is produced from
the template's own row on every read. So the interesting properties are the
ones a stored file would not have — the bytes are a pure function of the
document, the author's brief survives verbatim, and a member nobody registered
is refused rather than rendered blank.

The rendering the retired replication contexts still get is exercised here too:
the conversion that turns a live saved query or report into a chat template
writes that README into the new template's brief, so it has to keep producing
what its author wrote down.
"""

from __future__ import annotations

from typing import Any

import pytest
from alkera_core.files.providers.context_folder import (
    CONTEXT_FOLDER_TYPES,
    README_FILE,
    SPEC_FILE,
    context_folder_files,
    render_readme,
)
from alkera_core.files.providers.derived_members import DERIVED_MEMBERS, render_member


def _document(object_type: str, spec: dict[str, Any], *, title: str = "") -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "type": object_type,
        "object": {"title": title, "id": "obj-1"},
        "spec": spec,
    }


def test_a_template_folder_declares_exactly_one_derived_member() -> None:
    """The registry is what the bridge creates and what the provider serves."""
    assert DERIVED_MEMBERS == {"chat_template": {"README.md": "text/markdown"}}


def test_the_readme_carries_the_authors_brief_verbatim() -> None:
    """The brief is the author's own words, reproduced exactly.

    Not summarized, not re-wrapped, not escaped: a new chat is handed this
    text, and a template whose instructions were reworded on the way out would
    run the wrong analysis.
    """
    brief = "Pull the weekly numbers.\n\n- region: EU\n- **do not** guess the range"
    rendered = render_member(
        "chat_template",
        "README.md",
        _document("chat_template", {"title": "Weekly", "brief": brief}),
    ).decode("utf-8")

    assert rendered.startswith("# Weekly\n")
    assert brief in rendered
    assert "## Brief" in rendered
    assert "`scratch/`" in rendered, "a reader is told where the files a new chat starts with are"


def test_the_readme_is_a_pure_function_of_the_document() -> None:
    """Two renderings of one document are the same bytes.

    Nothing is stored for a derived member, so a rendering that varied between
    reads would show up as a file that changes when nobody edited it.
    """
    document = _document("chat_template", {"title": "Weekly", "brief": "Ask first."})
    assert render_member("chat_template", "README.md", document) == render_member(
        "chat_template", "README.md", document
    )


def test_a_title_falls_back_to_the_objects_own() -> None:
    """A template whose spec carries no title is still named by its row."""
    rendered = render_member(
        "chat_template", "README.md", _document("chat_template", {}, title="Untitled draft")
    ).decode("utf-8")
    assert rendered.startswith("# Untitled draft\n")


@pytest.mark.parametrize(
    ("object_type", "member"),
    [
        pytest.param("chat_template", "spec.json", id="unregistered-member"),
        pytest.param("chat", "README.md", id="type-with-no-derived-members"),
        pytest.param("report", "README.md", id="retired-type"),
    ],
)
def test_a_member_nobody_registered_is_refused(object_type: str, member: str) -> None:
    """An unregistered member is an error, never empty bytes.

    A blank file would look like a member whose content went missing; the
    refusal says the registry never named it.
    """
    with pytest.raises(KeyError):
        render_member(object_type, member, _document(object_type, {}))


def test_the_replication_context_types_are_retired() -> None:
    """Nothing materializes as one any more, and the name still answers."""
    assert CONTEXT_FOLDER_TYPES == frozenset()


def test_the_conversion_can_still_read_what_a_retired_context_said() -> None:
    """The README a retired query rendered is what its brief is built from.

    The type is gone; what its author wrote about running it is not, so the
    rendering the conversion reads has to keep producing the questions, the
    connection and the SQL.
    """
    spec = {
        "title": "Weekly revenue",
        "params": [{"name": "region", "prompt": "Which region?", "type": "string"}],
        "engine": "snowflake",
        "sql_template": "SELECT 1",
    }
    readme = render_readme(_document("query", spec))
    assert "# Weekly revenue" in readme
    assert "Which region?" in readme
    assert "`snowflake`" in readme
    assert "SELECT 1" in readme

    files = context_folder_files(_document("query", spec))
    assert files[README_FILE].decode("utf-8") == readme
    assert b'"title": "Weekly revenue"' in files[SPEC_FILE]
