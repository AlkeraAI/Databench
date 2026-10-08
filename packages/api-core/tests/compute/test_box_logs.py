"""The rule for what a box may ship of its own logs, and where an EC2 node is
told to ship them.

The rule is applied on the box and again on the server, so it is pinned here
once: an event outside the table leaves nothing, a field the event does not
list leaves nothing, a listed field of the wrong kind leaves nothing, and free
text leaves only with every secret-shaped run replaced.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from alkera_core.compute.bootstrap import BootstrapError, BootstrapSpec, render_bootstrap
from alkera_core.compute.box_contract import BootstrapPlan, StartMode
from alkera_core.compute.box_logs import (
    EVENT_FIELDS,
    FIELD_KINDS,
    REDACTED,
    SHIPPER_DROPPED,
    TEXT_MAX_CHARS,
    sanitize,
    sanitize_fields,
    sanitize_level,
    sanitize_timestamp,
    scrub_text,
)

#: What every render here installs and starts, unless a test says otherwise.
PLAN = BootstrapPlan(version="1.4.2", start_mode=StartMode.SUPERVISE)

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
CHAT = "be0a3393-082f-4acf-918d-6eac00904c9b"
STAMP = "2026-10-05T12:00:00+00:00"

#: Values a secret takes, each of which must never survive a text field.
SECRETS = {
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlc2lnbmF0dXJl",
    "machine-credential": "alkm_3f9a1c2b7d8e4f60a1b2c3d4e5f6a7b8",
    "task-token": "alk_task_9b8c7d6e5f4a3b2c1d0e",
    "openai-key": "sk-proj-AbCdEfGhIjKlMnOpQrStUv",
    "aws-key-id": "AKIAIOSFODNN7EXAMPLE",
    "aws-secret": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "hex-blob": "0123456789abcdef0123456789abcdef",
}


# -- the event table -----------------------------------------------------------


def test_every_allowed_field_has_a_kind() -> None:
    named = {field for fields in EVENT_FIELDS.values() for field in fields}
    assert named <= set(FIELD_KINDS)


@pytest.mark.parametrize(
    "event",
    [
        pytest.param("chat.message.sent", id="chat-content"),
        pytest.param("harness.tool.output", id="tool-output"),
        pytest.param("supervisor.worker.started.extra", id="suffix-of-an-allowed-name"),
        pytest.param("SUPERVISOR.WORKER.STARTED", id="case-changed"),
        pytest.param("", id="empty"),
    ],
)
def test_an_event_outside_the_table_leaves_nothing(event: str) -> None:
    assert sanitize(event, "error", STAMP, {"org_id": ORG}) is None
    assert sanitize_fields(event, {"org_id": ORG}) is None


def test_a_name_that_is_not_text_leaves_nothing() -> None:
    assert sanitize(None, "info", STAMP, {}) is None
    assert sanitize(["supervisor.worker.killed"], "info", STAMP, {}) is None


def test_an_allowed_event_keeps_exactly_its_listed_fields() -> None:
    record = sanitize(
        "supervisor.worker.crash_loop",
        "ERROR",
        STAMP,
        {
            "slot": 3,
            "org_id": ORG.upper(),
            "restarts": 6,
            "window": 900.0,
            "backoff": 8,
            # Not this event's: dropped even though another event lists them.
            "chat_id": CHAT,
            "error": "boom",
            # Never anyone's.
            "prompt": "summarise the board deck",
            "content": "hello",
            "token": SECRETS["machine-credential"],
            "level": "debug",
            "event": "supervisor.org_admission.refused",
        },
    )
    assert record == {
        "timestamp": STAMP,
        "level": "error",
        "event": "supervisor.worker.crash_loop",
        "slot": 3,
        "org_id": ORG,
        "restarts": 6,
        "window": 900.0,
        "backoff": 8,
    }


@pytest.mark.parametrize(
    ("field", "value", "kept"),
    [
        pytest.param("slot", 2, True, id="int"),
        pytest.param("slot", None, True, id="int-absent"),
        pytest.param("slot", True, False, id="bool-is-not-an-int"),
        pytest.param("slot", "2", False, id="string-is-not-an-int"),
        pytest.param("slot", 2.5, False, id="float-is-not-an-int"),
        pytest.param("backoff", 2.5, True, id="number"),
        pytest.param("backoff", float("inf"), False, id="infinite-number"),
        pytest.param("backoff", float("nan"), False, id="nan"),
        pytest.param("backoff", False, False, id="bool-is-not-a-number"),
        pytest.param("final", False, True, id="bool"),
        pytest.param("final", 0, False, id="int-is-not-a-bool"),
        pytest.param("final", None, False, id="bool-absent"),
        pytest.param("org_id", ORG, True, id="uuid"),
        pytest.param("org_id", "not-a-uuid", False, id="not-a-uuid"),
        pytest.param("org_id", ORG.replace("-", ""), False, id="uuid-without-dashes"),
        pytest.param("org_id", None, False, id="id-absent"),
        pytest.param("reason", "another_org", True, id="known-reason"),
        pytest.param("reason", "the chat's row names another org", False, id="reason-prose"),
        pytest.param("command", "systemctl stop alkera-org@3.service", True, id="command"),
        pytest.param("command", "systemctl stop $(curl evil)", False, id="command-shell"),
        pytest.param("command", "x" * 121, False, id="command-too-long"),
        pytest.param("command", "unit " + SECRETS["hex-blob"], False, id="command-with-a-blob"),
    ],
)
def test_a_listed_field_leaves_only_with_a_value_of_its_kind(
    field: str, value: object, kept: bool
) -> None:
    event = next(e for e, fields in EVENT_FIELDS.items() if field in fields)
    out = sanitize_fields(event, {field: value})
    assert out is not None
    assert (field in out) is kept


def test_an_id_is_lowercased() -> None:
    assert sanitize_fields("supervisor.worker.ready", {"org_id": ORG.upper()}) == {"org_id": ORG}


def test_the_shippers_own_report_is_in_the_table() -> None:
    assert sanitize_fields(SHIPPER_DROPPED, {"count": 7, "error": "x"}) == {"count": 7}


# -- free text -------------------------------------------------------------------


@pytest.mark.parametrize("secret", [pytest.param(v, id=k) for k, v in SECRETS.items()])
@pytest.mark.parametrize(
    "template",
    [
        pytest.param("{}", id="bare"),
        pytest.param("refused: {} (retrying)", id="in-prose"),
        pytest.param("Authorization: Bearer {}", id="bearer-header"),
        pytest.param("password={}", id="key-value"),
        pytest.param("token: {}", id="token-colon"),
    ],
)
def test_free_text_never_carries_a_secret(template: str, secret: str) -> None:
    out = scrub_text(template.format(secret))
    assert secret not in out
    assert REDACTED in out
    record = sanitize(
        "supervisor.org.reap_failed", "error", STAMP, {"error": template.format(secret)}
    )
    assert record is not None
    assert secret not in json.dumps(record)


def test_free_text_keeps_ids_and_plain_words() -> None:
    text = f"could not remove /opt/alkera-work/orgs/{ORG}/home: Permission denied"
    assert scrub_text(text) == text


def test_free_text_is_cut_and_flattened() -> None:
    out = scrub_text("line one\nline two\x00" + "word " * 200)
    assert len(out) == TEXT_MAX_CHARS
    assert "\n" not in out and "\x00" not in out


# -- level and timestamp ---------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        pytest.param("WARNING", "warning", id="upper"),
        pytest.param("error", "error", id="lower"),
        pytest.param("exception", "info", id="unknown"),
        pytest.param(40, "info", id="number"),
    ],
)
def test_a_level_is_one_of_the_known_few(level: object, expected: str) -> None:
    assert sanitize_level(level) == expected


@pytest.mark.parametrize(
    ("stamp", "kept"),
    [
        pytest.param(STAMP, True, id="iso"),
        pytest.param("yesterday", False, id="prose"),
        pytest.param(STAMP + "x" * 40, False, id="too-long"),
        pytest.param(1730000000, False, id="epoch-number"),
    ],
)
def test_a_timestamp_is_kept_only_as_iso(stamp: object, kept: bool) -> None:
    assert (sanitize_timestamp(stamp) is not None) is kept


# -- where an EC2 node is told to ship -------------------------------------------


def _spec(provider: str, **overrides: Any) -> BootstrapSpec:
    base: dict[str, Any] = {
        "provider": provider,
        "allocation_id": "12345678-1234-5678-1234-567812345678",
        "machine_name": "pool-1",
        "type_code": "m6i.large",
        "tenancy": "pool",
        "api_url": "https://api.example.test",
        "release_base_url": "https://releases.example.test/",
        "credential_secret": "alkera/test/node/x" if provider == "ec2" else "",
        "region": "us-east-1",
    }
    base.update(overrides)
    return BootstrapSpec(**base)


def _node_env(script: str) -> list[str]:
    body = script.split("cat >\"$BOX_ROOT/node.env\" <<'ENV'\n", 1)[1]
    return body.split("\nENV\n", 1)[0].splitlines()


def test_an_ec2_node_is_told_its_log_group_and_region() -> None:
    env = _node_env(render_bootstrap(_spec("ec2", log_group="/ec2/example-chat-box"), PLAN))
    assert "ALKERA_BOX_LOG_GROUP=/ec2/example-chat-box" in env
    assert "ALKERA_BOX_LOG_REGION=us-east-1" in env


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(_spec("ec2"), id="ec2-without-a-group"),
        pytest.param(_spec("runpod", log_group="/ec2/example-chat-box"), id="runpod"),
    ],
)
def test_a_node_with_no_group_or_no_instance_role_names_none(spec: BootstrapSpec) -> None:
    env = _node_env(render_bootstrap(spec, PLAN))
    assert not [line for line in env if line.startswith("ALKERA_BOX_LOG_")]


@pytest.mark.parametrize(
    "group",
    [
        pytest.param("/ec2/x'; rm -rf /", id="shell"),
        pytest.param("/ec2/x\nALKERA_API_URL=evil", id="newline"),
        pytest.param("x" * 513, id="too-long"),
    ],
)
def test_a_log_group_cloudwatch_would_not_take_is_refused(group: str) -> None:
    with pytest.raises(BootstrapError, match="log group"):
        render_bootstrap(_spec("ec2", log_group=group), PLAN)
