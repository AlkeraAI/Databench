"""The copy a person reads while adding a connection, swept for house-style defects.

Every caption on the add-a-connection surfaces comes from one of two places: the
driver-free ``ConnectorDescriptor`` catalog the portal renders, and each bundled
plugin's ``PluginManifest.description`` + ``connection_form_schema()``, which the
TUI's connector picker and form render. Both are swept here, so a connector added
tomorrow is held to the same bar with no edit to this file.

Three rules, all from CLAUDE.md's copy section:

* No em dash. It is the tell of machine-written copy; a full stop, a comma or a
  colon says the same thing.
* No reassurance filler. A caption states the fact the reader needs; a clause
  whose only job is to say the thing is fine ("works", "just", "simply") is the
  sentence to delete, not to soften.
* No clause that was already cut. A phrase deleted once comes back the next time
  somebody rewrites the caption around it, so each one is named here.

Below the sweeps sit the captions rewritten one at a time, each pinned on the
words that replaced them rather than on the shape of the sentence.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from alkera_cli.plugins.plugin_base.plugin import CLI_PLUGINS
from alkera_core.connectors.catalog import all_descriptors
from alkera_core.connectors.connection_form import ConnectionFormSchema

#: Words that only ever reassure. ``works`` is here because it is the shape the
#: Tinybird token caption took ("Any token that can run queries works.") — a
#: sentence that told the reader nothing they could act on.
FILLER = re.compile(
    r"\b(just|simply|easily|no need to|don't worry|you can always|feel free|"
    r"seamless(ly)?|powerful|robust|effortless(ly)?)\b",
    re.IGNORECASE,
)

#: Whole clauses already cut from these surfaces once, each for its own reason:
#: a reassurance about who can see a connection, a reassurance about whose fault
#: a failed check is, and an implementation note about where a secret is written.
#: Naming them keeps a rewrite from walking any of them back in.
CUT_PHRASES = (
    "Only you can see or use",
    "not a problem with",
    "before persistence",
)


def _schema_copy(schema: ConnectionFormSchema) -> Iterator[tuple[str, str]]:
    """Every string the generic form renders out of one schema."""
    if schema.note:
        yield "note", schema.note
    fields = [*schema.shared_fields, *schema.trailing_fields]
    for method in schema.auth_methods:
        yield f"auth_method[{method.name}].label", method.label
        fields.extend(method.fields)
    for field in fields:
        for attr in ("label", "help", "placeholder"):
            value = getattr(field, attr)
            if value:
                yield f"{field.name}.{attr}", value
        for key, word in field.enum_labels.items():
            if word:
                yield f"{field.name}.enum_labels[{key}]", word


def _catalog_copy() -> Iterator[tuple[str, str, str]]:
    """``(connector, where, text)`` for the portal's connector catalog."""
    for descriptor in all_descriptors():
        yield descriptor.name, "title", descriptor.title
        for where, text in _schema_copy(descriptor.form_schema()):
            yield descriptor.name, where, text


def _plugin_copy() -> Iterator[tuple[str, str, str]]:
    """``(plugin, where, text)`` for the TUI's picker row and connection form."""
    for plugin_cls in CLI_PLUGINS.items():
        manifest = plugin_cls.manifest
        yield manifest.name, "manifest.description", manifest.description
        schema = plugin_cls().connection_form_schema()
        if schema is None:
            continue
        for where, text in _schema_copy(schema):
            yield manifest.name, where, text


CAPTIONS: list[tuple[str, str, str]] = [*_catalog_copy(), *_plugin_copy()]


def _ids(rows: list[tuple[str, str, str]]) -> list[str]:
    return [f"{name}-{where}" for name, where, _ in rows]


def test_the_sweep_actually_reaches_every_surface() -> None:
    """A guard on the sweep itself: an empty or collapsed inventory would make
    every rule below pass vacuously."""
    connectors = {name for name, _, _ in CAPTIONS}
    assert len(connectors) >= 20, connectors
    wheres = {where for _, where, _ in CAPTIONS}
    assert {"title", "manifest.description", "note"} <= wheres, wheres
    # The two surfaces the sweep exists for: a form field's caption and a picker row.
    assert ("tinybird", "manifest.description") in {(n, w) for n, w, _ in CAPTIONS}
    assert any(where.endswith(".help") for _, where, _ in CAPTIONS)


@pytest.mark.parametrize(("connector", "where", "text"), CAPTIONS, ids=_ids(CAPTIONS))
def test_no_em_dash_in_connection_copy(connector: str, where: str, text: str) -> None:
    assert "—" not in text, (
        f"{connector} {where} uses an em dash: {text!r}. Rewrite it as a full stop and a "
        "new sentence, a comma, or a colon."
    )


@pytest.mark.parametrize(("connector", "where", "text"), CAPTIONS, ids=_ids(CAPTIONS))
def test_no_reassurance_filler_in_connection_copy(connector: str, where: str, text: str) -> None:
    found = FILLER.search(text)
    assert found is None, (
        f"{connector} {where} reassures rather than informs ({found.group(0)!r} in {text!r}). "
        "State the fact the reader acts on, or delete the sentence."
    )


@pytest.mark.parametrize(("connector", "where", "text"), CAPTIONS, ids=_ids(CAPTIONS))
def test_no_cut_clause_returns_to_connection_copy(connector: str, where: str, text: str) -> None:
    for phrase in CUT_PHRASES:
        assert phrase.lower() not in text.lower(), (
            f"{connector} {where} brings back a clause that was cut ({phrase!r} in {text!r})."
        )


def test_the_mongodb_note_says_what_to_paste_and_stops() -> None:
    """The note used to carry where a password ends up in storage. A reader pastes
    a connection string either way, so the sentence changed nothing they do."""
    schema = next(d for d in all_descriptors() if d.name == "mongodb").form_schema()

    assert schema.note == "Paste the driver connection string."


def test_a_certificate_field_asks_for_contents_without_naming_an_internal_lane() -> None:
    """ "Team probes" and "Local workspace connections" are Alkera's own words for
    where a check runs; an admin pasting a CA has met neither."""
    for connector, field_name in (("druid", "ca_bundle"), ("elasticsearch", "ca_cert")):
        schema = next(d for d in all_descriptors() if d.name == connector).form_schema()
        field = next(f for f in schema.shared_fields if f.name == field_name)
        assert field.help == "Paste the certificate contents, with \\n for line breaks."


def test_the_sigma_picker_row_names_the_capability_and_leaves_the_caveat_to_the_form() -> None:
    """A picker row is read while choosing between connectors; whose credential
    sees what belongs where the credential is entered."""
    manifest = next(p.manifest for p in CLI_PLUGINS.items() if p.manifest.name == "sigma")
    schema = next(
        p for p in CLI_PLUGINS.items() if p.manifest.name == "sigma"
    )().connection_form_schema()

    assert manifest.description == "Sigma BI lineage + workbook context from the REST API."
    assert "falls back" not in manifest.description
    assert schema is not None
    assert "falls back to what the credential's owner can see" in (schema.note or "")


def test_the_looker_client_secret_carries_no_bare_navigation_path() -> None:
    """The form note two lines above already says where API3 credentials are made,
    so the field repeated a path with no verb attached to it."""
    schema = next(
        p for p in CLI_PLUGINS.items() if p.manifest.name == "looker"
    )().connection_form_schema()
    assert schema is not None
    secret = next(
        f for method in schema.auth_methods for f in method.fields if f.name == "client_secret"
    )

    assert secret.help == ""
    assert "API keys" in (schema.note or "")


@pytest.mark.parametrize(
    ("connector", "field_name", "caption"),
    [
        pytest.param(
            "bigquery",
            "dataset",
            "The dataset used for unqualified table names.",
            id="bigquery-dataset-names-the-effect-not-the-control",
        ),
        pytest.param(
            "fivetran",
            "api_key",
            "The key half of the pair (a non-secret identifier).",
            id="fivetran-key",
        ),
        pytest.param(
            "fivetran",
            "api_secret",
            "The secret half (shown only when the key is generated).",
            id="fivetran-secret",
        ),
        pytest.param(
            "sigma",
            "client_id",
            "The API client ID (a non-secret identifier).",
            id="sigma-client-id",
        ),
        pytest.param(
            "sigma",
            "client_secret",
            "The client secret (shown only when the credential is created).",
            id="sigma-client-secret",
        ),
        pytest.param(
            "tableau",
            "token_name",
            "The Personal Access Token's name.",
            id="tableau-token-name",
        ),
        pytest.param(
            "tableau",
            "token_secret",
            "The token's secret value (shown only when the PAT is created).",
            id="tableau-token-secret",
        ),
    ],
)
def test_a_caption_that_is_a_sentence_ends_like_one(
    connector: str, field_name: str, caption: str
) -> None:
    """These sat in files where every other caption already had its stop, so one
    form read as two hands."""
    schema = next(
        p for p in CLI_PLUGINS.items() if p.manifest.name == connector
    )().connection_form_schema()
    if schema is None:  # pragma: no cover - the connector is in the parametrize list
        schema = next(d for d in all_descriptors() if d.name == connector).form_schema()
    fields = [
        *schema.shared_fields,
        *schema.trailing_fields,
        *(f for method in schema.auth_methods for f in method.fields),
    ]

    assert next(f for f in fields if f.name == field_name).help == caption


def test_the_tinybird_token_field_carries_no_caption() -> None:
    """The one caption this sweep was written for. The admin never picks read-only
    per connection, so a line about which tokens are accepted described a setting
    that does not exist."""
    schema = next(d for d in all_descriptors() if d.name == "tinybird").form_schema()
    token = next(f for f in schema.shared_fields if f.name == "token")
    assert token.label == "Workspace token"
    assert token.help == ""
    # And the picker row for the same connector no longer ends on the same claim.
    manifest = next(p.manifest for p in CLI_PLUGINS.items() if p.manifest.name == "tinybird")
    assert "token that can run a query" not in manifest.description
