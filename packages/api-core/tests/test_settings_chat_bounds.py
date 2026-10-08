"""The chat and machine bounds a deployment may tune.

Each of these was a module constant nobody could move without a release. The
cases below pin three things per field: the default is what the product shipped,
the environment variable moves it, and a nonsense value is refused loudly rather
than silently substituted.
"""

from __future__ import annotations

import pytest
from _settings_env import seal_settings_env
from alkera_core.config import Settings
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _sealed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported dev config in the shell must not decide what a default is."""
    seal_settings_env(monkeypatch)


#: field name -> (env var, shipped default)
DEFAULTS: dict[str, tuple[str, object]] = {
    "chat_message_max_attachments": ("CHAT_MESSAGE_MAX_ATTACHMENTS", 20),
    "chat_max_attachments": ("CHAT_MAX_ATTACHMENTS", None),
    "chat_answer_max_choices": ("CHAT_ANSWER_MAX_CHOICES", 32),
    "chat_refusal_reason_max_chars": ("CHAT_REFUSAL_REASON_MAX_CHARS", 500),
    # A tenancy boundary controlled by a boolean: whether an org admin may open
    # a chat of their org that nobody shared with them. The shipped answer is
    # "no", and a default nothing holds down is a default a merge can flip.
    "chat_org_admin_reads_private": ("CHAT_ORG_ADMIN_READS_PRIVATE", False),
    "event_outbox_payload_max_bytes": ("EVENT_OUTBOX_PAYLOAD_MAX_BYTES", 2 * 1024 * 1024),
    "chat_model_catalog_timeout_seconds": ("CHAT_MODEL_CATALOG_TIMEOUT_SECONDS", 10.0),
}


@pytest.mark.parametrize(
    ("field", "expected"),
    [pytest.param(name, default, id=name) for name, (_, default) in DEFAULTS.items()],
)
def test_default_is_what_the_product_shipped(field: str, expected: object) -> None:
    """Folding a constant into settings must not move the value under anyone."""
    assert getattr(Settings(_env_file=None), field) == expected


@pytest.mark.parametrize(
    ("field", "env", "raw", "expected"),
    [
        pytest.param("chat_message_max_attachments", *pair, id=pair[0])
        for pair in [("CHAT_MESSAGE_MAX_ATTACHMENTS", "100", 100)]
    ]
    + [
        pytest.param(
            "chat_max_attachments", "CHAT_MAX_ATTACHMENTS", "500", 500, id="CHAT_MAX_ATTACHMENTS"
        ),
        pytest.param("chat_answer_max_choices", "CHAT_ANSWER_MAX_CHOICES", "8", 8, id="choices"),
        pytest.param(
            "chat_refusal_reason_max_chars",
            "CHAT_REFUSAL_REASON_MAX_CHARS",
            "120",
            120,
            id="refusal_reason",
        ),
        pytest.param(
            "chat_org_admin_reads_private",
            "CHAT_ORG_ADMIN_READS_PRIVATE",
            "true",
            True,
            id="admin_reads_private",
        ),
        pytest.param(
            "event_outbox_payload_max_bytes",
            "EVENT_OUTBOX_PAYLOAD_MAX_BYTES",
            "65536",
            65536,
            id="outbox_payload",
        ),
        pytest.param(
            "chat_model_catalog_timeout_seconds",
            "CHAT_MODEL_CATALOG_TIMEOUT_SECONDS",
            "45.5",
            45.5,
            id="catalog_timeout",
        ),
    ],
)
def test_env_var_moves_the_bound(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    env: str,
    raw: str,
    expected: object,
) -> None:
    monkeypatch.setenv(env, raw)
    assert getattr(Settings(_env_file=None), field) == expected


@pytest.mark.parametrize(
    ("env", "raw"),
    [
        pytest.param("CHAT_MESSAGE_MAX_ATTACHMENTS", "0", id="attachments_zero"),
        pytest.param("CHAT_MESSAGE_MAX_ATTACHMENTS", "-1", id="attachments_negative"),
        pytest.param("CHAT_MAX_ATTACHMENTS", "0", id="chat_attachments_zero"),
        pytest.param("CHAT_MAX_ATTACHMENTS", "-20", id="chat_attachments_negative"),
        pytest.param("CHAT_MAX_ATTACHMENTS", "many", id="chat_attachments_not_a_number"),
        pytest.param("CHAT_ANSWER_MAX_CHOICES", "0", id="choices_zero"),
        pytest.param("CHAT_ANSWER_MAX_CHOICES", "-4", id="choices_negative"),
        pytest.param("CHAT_REFUSAL_REASON_MAX_CHARS", "0", id="refusal_zero"),
        pytest.param("EVENT_OUTBOX_PAYLOAD_MAX_BYTES", "0", id="outbox_zero"),
        pytest.param("EVENT_OUTBOX_PAYLOAD_MAX_BYTES", "-2048", id="outbox_negative"),
        pytest.param("CHAT_MODEL_CATALOG_TIMEOUT_SECONDS", "0", id="catalog_zero"),
        pytest.param("CHAT_MODEL_CATALOG_TIMEOUT_SECONDS", "-1.5", id="catalog_negative"),
    ],
)
def test_nonsense_is_refused(monkeypatch: pytest.MonkeyPatch, env: str, raw: str) -> None:
    """Zero is never "no cap" here: a zero page sweeps nothing for ever and a
    zero payload ceiling refuses every event. Unbounded, where it is allowed at
    all, is spelled as an unset optional."""
    monkeypatch.setenv(env, raw)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("env", "field"),
    [
        pytest.param("CHAT_MESSAGE_MAX_ATTACHMENTS", "chat_message_max_attachments", id="message"),
        pytest.param("CHAT_MAX_ATTACHMENTS", "chat_max_attachments", id="chat"),
    ],
)
def test_an_attachment_ceiling_may_be_emptied_for_no_cap(
    monkeypatch: pytest.MonkeyPatch, env: str, field: str
) -> None:
    """The two fields where absence means something: for a message, the
    schema's published ceiling becomes the only one; for a chat, there is no
    ceiling at all. An emptied variable clears the cap rather than failing to
    parse, so an operator can undo a cap without editing the file it is in."""
    monkeypatch.setenv(env, "")
    assert getattr(Settings(_env_file=None), field) is None
