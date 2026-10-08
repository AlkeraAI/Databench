"""Whose standing answer decides an ask on a box that serves many people.

A cloud box keeps one ``permissions.local.yml`` for every chat placed on it, of
every person and every org. "Always allow" and "Always reject" are recorded
there naming the chat owner they answer for, and only that owner's chats read
them back. The rules these tests pin:

* on a shared host a recorded answer decides its owner's chats and nobody
  else's, an allow and a reject alike;
* a rule in the overlay that names nobody (written before answers were recorded
  per person) decides nothing on a shared host, and the committed
  ``permissions.yml`` still binds everyone;
* a shared-host chat with no known owner offers and records no standing answer;
* the vendor is never told ``always`` on a shared host, so it keeps raising the
  ask where the fence judges it;
* an ask a bound could not vouch for offers and records no standing answer;
* a local machine is unchanged: one owner, every rule binds, nothing is stamped.

Driven through the real :class:`DecisionEngine` and the real on-disk policy
files, because the seam under test is the round trip between them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from alkera_cli.contracts.tool_types import ActionDescriptor, Effect
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.permissions.config import (
    PERMISSIONS_FILE,
    PERMISSIONS_LOCAL_FILE,
    PermissionsConfig,
    add_local_rule,
    load_permissions,
)
from alkera_cli.plugins.plugin_base.permissions.gate import binding_from_context
from alkera_cli.plugins.plugin_base.permissions.resolve import (
    DecisionEngine,
    standing_answer_recordable,
)
from alkera_core.schemas.chat import PermissionRequest

OWNER_A1 = "00000000-0000-4000-8000-0000000000a1"
OWNER_A2 = "00000000-0000-4000-8000-0000000000a2"

#: A plain, recoverable workspace write: no floor, known confidence, not shell,
#: so a standing allow on it would reach the vendor as ``always`` if let through.
WRITE = ActionDescriptor(
    capability="fs",
    operation="write",
    effect=Effect.WRITE,
    confidence="exact",
    raw="write src/app.py",
)
ALL = ["allow_once", "allow_always", "reject_once", "reject_always"]
ONCE_ONLY = ["allow_once", "reject_once"]


class _Answers:
    """A person at the card, recording every ask it was handed and what it was
    offered, so a test can say "and nobody was asked" and mean it."""

    def __init__(self, option: str) -> None:
        self.option = option
        self.offered: list[list[str]] = []

    @property
    def asked(self) -> int:
        return len(self.offered)

    async def resolve(self, request: Any) -> str:
        self.offered.append([o.option_id for o in request.options])
        return self.option


def _alkera(tmp_path: Path) -> Path:
    alkera = tmp_path / ".alkera"
    (alkera / "chat").mkdir(parents=True)
    return alkera


def _engine(
    alkera: Path, broker: _Answers, *, shared_host: bool = True, owner: str = ""
) -> DecisionEngine:
    return DecisionEngine(
        sink=DecisionSink(alkera / "chat"),
        permissions=load_permissions(alkera),
        broker=broker,
        alkera_dir=alkera,
        session_id="s",
        shared_host=shared_host,
        owner=owner,
    )


def _rules(alkera: Path) -> list[dict[str, Any]]:
    path = alkera / PERMISSIONS_LOCAL_FILE
    data = yaml.safe_load(path.read_text()) if path.exists() else None
    return list((data or {}).get("rules") or [])


def _write(alkera: Path, name: str, rules: list[dict[str, Any]]) -> None:
    (alkera / name).write_text(yaml.safe_dump({"rules": rules}))


# -- the policy a chat may consult ---------------------------------------------------


@pytest.mark.parametrize(
    ("shared_host", "owner", "binding"),
    [
        pytest.param(False, "", {"project", "local", "local-a1", "local-a2"}, id="local-machine"),
        pytest.param(True, OWNER_A1, {"project", "local-a1"}, id="box-owner-a1"),
        pytest.param(True, OWNER_A2, {"project", "local-a2"}, id="box-owner-a2"),
        pytest.param(True, "", {"project"}, id="box-no-owner"),
    ],
)
def test_a_box_consults_only_the_chat_owners_recorded_answers(
    tmp_path: Path, shared_host: bool, owner: str, binding: set[str]
) -> None:
    alkera = _alkera(tmp_path)
    _write(alkera, PERMISSIONS_FILE, [{"capability": "fs", "decision": "ask", "reason": "project"}])
    _write(
        alkera,
        PERMISSIONS_LOCAL_FILE,
        [
            {"capability": "fs", "decision": "deny", "reason": "local"},
            {"capability": "fs", "decision": "deny", "reason": "local-a1", "owner": OWNER_A1},
            {"capability": "fs", "decision": "deny", "reason": "local-a2", "owner": OWNER_A2},
        ],
    )
    policy = load_permissions(alkera).for_host(shared_host=shared_host, owner=owner)
    assert {r.reason for r in policy.rules} == binding


def test_a_committed_rule_naming_an_owner_binds_only_that_owner_on_a_box(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    _write(alkera, PERMISSIONS_FILE, [{"capability": "fs", "decision": "deny", "owner": OWNER_A1}])
    loaded = load_permissions(alkera)
    assert loaded.for_host(shared_host=True, owner=OWNER_A1).rules
    assert not loaded.for_host(shared_host=True, owner=OWNER_A2).rules
    assert loaded.for_host(shared_host=False).rules


# -- recording ------------------------------------------------------------------------


def test_each_owners_answer_is_its_own_rule_and_repeats_are_idempotent(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    assert add_local_rule(alkera, WRITE, mode="default", owner=OWNER_A1)
    assert not add_local_rule(alkera, WRITE, mode="default", owner=OWNER_A1)
    assert add_local_rule(alkera, WRITE, mode="default", owner=OWNER_A2)
    assert add_local_rule(alkera, WRITE, mode="default")
    assert [r.get("owner") for r in _rules(alkera)] == [OWNER_A1, OWNER_A2, None]


def test_an_owner_survives_the_round_trip_and_an_older_document_reads_as_nobodys() -> None:
    stamped = PermissionsConfig.model_validate(
        {"rules": [{"capability": "fs", "decision": "allow", "owner": OWNER_A1}]}
    ).model_dump(mode="json")
    assert stamped["rules"][0]["owner"] == OWNER_A1
    older = PermissionsConfig.model_validate(
        {"schema_version": "2.2.0", "rules": [{"capability": "fs", "decision": "allow"}]}
    ).model_dump(mode="json")
    assert older["schema_version"] == PermissionsConfig.SCHEMA_VERSION == "2.3.0"
    assert older["rules"][0]["owner"] is None


# -- the engine, end to end -------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "allowed", "decision"),
    [
        pytest.param("allow_always", True, "allow", id="always-allow"),
        pytest.param("reject_always", False, "deny", id="always-reject"),
    ],
)
async def test_a_standing_answer_decides_its_owners_next_ask_and_nobody_elses(
    tmp_path: Path, answer: str, allowed: bool, decision: str
) -> None:
    alkera = _alkera(tmp_path)
    first = _Answers(answer)
    res = await _engine(alkera, first, owner=OWNER_A1).resolve(WRITE, mode="default")
    assert first.offered == [ALL], "the owner's card offers what the box records"
    assert res.allowed is allowed
    assert [(r["decision"], r.get("owner")) for r in _rules(alkera)] == [(decision, OWNER_A1)]

    again = _Answers("allow_once")
    replay = await _engine(alkera, again, owner=OWNER_A1).resolve(WRITE, mode="default")
    assert (again.asked, replay.allowed, replay.decided_by) == (0, allowed, "rule")

    other = _Answers("allow_once")
    theirs = await _engine(alkera, other, owner=OWNER_A2).resolve(WRITE, mode="default")
    assert other.asked == 1, "another person's answer decided this person's ask"
    assert (theirs.allowed, theirs.decided_by) == (True, "human")


async def test_the_vendor_is_told_only_this_calls_decision_on_a_box(tmp_path: Path) -> None:
    """On a box the rule is ours; a vendor told ``always`` would stop asking."""
    alkera = _alkera(tmp_path)
    boxed = await _engine(alkera, _Answers("allow_always"), owner=OWNER_A1).resolve(
        WRITE, mode="default"
    )
    assert boxed.option == "allow_once"
    local = _engine(_alkera(tmp_path / "laptop"), _Answers("allow_always"), shared_host=False)
    assert (await local.resolve(WRITE, mode="default")).option == "allow_always"


async def test_a_box_chat_with_no_owner_offers_and_records_no_standing_answer(
    tmp_path: Path,
) -> None:
    alkera = _alkera(tmp_path)
    broker = _Answers("allow_always")
    res = await _engine(alkera, broker, owner="").resolve(WRITE, mode="default")
    assert broker.offered == [ONCE_ONLY]
    assert (res.allowed, res.option) == (True, "allow_once")
    assert _rules(alkera) == []


async def test_an_overlay_rule_naming_nobody_decides_nothing_on_a_box(tmp_path: Path) -> None:
    """An "Always allow" a box recorded before answers named their owner was some
    unknown person's: it must not answer for anyone now."""
    alkera = _alkera(tmp_path)
    _write(
        alkera,
        PERMISSIONS_LOCAL_FILE,
        [{"capability": "fs", "decision": "allow", "mode": "default"}],
    )
    broker = _Answers("allow_once")
    await _engine(alkera, broker, owner=OWNER_A1).resolve(WRITE, mode="default")
    assert broker.asked == 1
    laptop = _Answers("allow_once")
    res = await _engine(alkera, laptop, shared_host=False).resolve(WRITE, mode="default")
    assert (laptop.asked, res.decided_by) == (0, "rule"), "a laptop's own file still binds"


async def test_an_ask_the_bound_cannot_vouch_for_records_no_standing_answer_on_a_box(
    tmp_path: Path,
) -> None:
    alkera = _alkera(tmp_path)
    broker = _Answers("allow_always")
    res = await _engine(alkera, broker, owner=OWNER_A1).ask(WRITE, mode="default")
    assert broker.offered == [ONCE_ONLY]
    assert (res.allowed, res.option) == (True, "allow_once")
    assert _rules(alkera) == []


async def test_the_owners_answers_do_not_reach_the_box_owners_policy_alone(
    tmp_path: Path,
) -> None:
    """``standing=False`` is the policy a path only the committed file decides."""
    alkera = _alkera(tmp_path)
    add_local_rule(alkera, WRITE, mode="default", owner=OWNER_A1)
    engine = _engine(alkera, _Answers("allow_once"), owner=OWNER_A1)
    assert engine.permissions_now().rules
    assert not engine.permissions_now(standing=False).rules


# -- the in-tool gate picks up the chat's owner ----------------------------------------


class _Registry:
    knowledge_owner = OWNER_A1
    lineage_store = None
    context_store = None


class _Ctx:
    registry = _Registry()
    decision_sink: Any = None
    permissions: Any = None
    broker: Any = None
    judge: Any = None
    task_goal = ""
    session_id = "s"
    alkera_dir: Any = None
    fence: Any = object()


async def test_the_tool_gate_records_a_standing_answer_as_the_chat_owners(tmp_path: Path) -> None:
    alkera = _alkera(tmp_path)
    ctx = _Ctx()
    ctx.decision_sink = DecisionSink(alkera / "chat")
    ctx.permissions = load_permissions(alkera)
    ctx.broker = _Answers("allow_always")
    ctx.alkera_dir = alkera
    engine = binding_from_context(ctx).engine(source="sql_gate")
    assert engine is not None
    await engine.resolve(WRITE, mode="default")
    assert [r.get("owner") for r in _rules(alkera)] == [OWNER_A1]


# -- what a fenced card may offer -------------------------------------------------------


def _ask(subject: dict[str, Any] | None) -> PermissionRequest:
    return PermissionRequest(
        event_id="ev",
        time=datetime(2026, 10, 5, tzinfo=UTC),
        session_id="s",
        request_id="r",
        permission_kind="edit",
        subject=subject,
    )


def _fence_raises(_request: PermissionRequest) -> bool:
    raise RuntimeError("the judge could not run")


@pytest.mark.parametrize(
    ("owner", "subject", "fence_reads", "recordable"),
    [
        pytest.param(OWNER_A1, WRITE.model_dump(mode="json"), lambda _r: True, True, id="owner"),
        pytest.param("", WRITE.model_dump(mode="json"), lambda _r: True, False, id="no-owner"),
        pytest.param(OWNER_A1, None, lambda _r: True, False, id="no-subject"),
        pytest.param(
            OWNER_A1, WRITE.model_dump(mode="json"), lambda _r: False, False, id="fence-unread"
        ),
        pytest.param(
            OWNER_A1, WRITE.model_dump(mode="json"), _fence_raises, False, id="fence-raised"
        ),
    ],
)
def test_a_fenced_ask_offers_a_standing_answer_only_where_one_is_recorded(
    owner: str,
    subject: dict[str, Any] | None,
    fence_reads: Callable[[PermissionRequest], bool],
    recordable: bool,
) -> None:
    assert standing_answer_recordable(_ask(subject), owner, fence_reads) is recordable
