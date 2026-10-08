"""What a standing "Always allow" is allowed to decide once the stance moves.

"Always allow" writes Alkera's own ``(capability, operation)`` rule into
``permissions.local.yml``, and the rule outlives the ask, the turn and the
chat. The stance the person granted it under does NOT outlive it today: the
rule is read back as a bare allow, so a stance that asks reads an answer given
where nobody was asked. A reader who moves a chat from Auto to Default has said
"ask me"; the rule minted twenty minutes earlier under Auto must not answer for
them.

The rules these tests pin:

* a standing allow binds the stance it was granted under and every LOOSER one;
* a STRICTER stance re-asks — the rule decides nothing there;
* tightening is never stance-scoped: a standing REJECT binds everywhere;
* a hand-authored policy rule carries no stance and keeps binding everywhere,
  which is the whole point of writing one into ``permissions.yml``;
* Bypass records nothing at all, so nothing it waived can be replayed later.

Driven through the real :class:`DecisionEngine` and the real on-disk policy
file, because the seam under test is exactly the round trip between them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from alkera_cli.contracts.tool_types import ActionDescriptor, Effect
from alkera_cli.plugins.plugin_base.permissions.audit import DecisionSink
from alkera_cli.plugins.plugin_base.permissions.config import (
    PERMISSIONS_FILE,
    PERMISSIONS_LOCAL_FILE,
    load_permissions,
)
from alkera_cli.plugins.plugin_base.permissions.resolve import ActionResolution, DecisionEngine

#: A plain, recoverable workspace write: no floor, known confidence, not shell.
#: Every stance treats it differently, which is what makes it the descriptor
#: that separates the stances from one another.
WRITE = ActionDescriptor(
    capability="fs",
    operation="write",
    effect=Effect.WRITE,
    confidence="exact",
    raw="write src/app.py",
)


class _Answers:
    """A person at the card. Records every ask it was handed, so a test can say
    'and nobody was asked' and mean it."""

    def __init__(self, option: str) -> None:
        self.option = option
        self.asked: list[str] = []

    async def resolve(self, request: Any) -> str:
        self.asked.append(request.request_id)
        return self.option


def _engine(alkera: Path, broker: _Answers) -> DecisionEngine:
    return DecisionEngine(
        sink=DecisionSink(alkera / "chat"),
        permissions=load_permissions(alkera),
        broker=broker,
        alkera_dir=alkera,
        session_id="s",
    )


def _alkera(tmp_path: Path) -> Path:
    alkera = tmp_path / ".alkera"
    (alkera / "chat").mkdir(parents=True)
    return alkera


async def _grant_always(alkera: Path, *, mode: str) -> _Answers:
    """A person answers "Always allow" to one ask in ``mode``. Returns the
    broker so the caller can assert they WERE asked — a grant nobody was asked
    for would make every replay assertion below vacuous."""
    broker = _Answers("allow_always")
    res = await _engine(alkera, broker).resolve(WRITE, mode=mode)
    assert res.allowed, f"the {mode} ask was not allowed, so no grant was made"
    return broker


async def _replay(alkera: Path, *, mode: str) -> tuple[ActionResolution, _Answers]:
    """The same action again, in ``mode``, on a freshly loaded policy — the next
    turn, the next chat, or the same chat after the stance moved."""
    broker = _Answers("allow_once")
    return await _engine(alkera, broker).resolve(WRITE, mode=mode), broker


def _rules(alkera: Path) -> list[dict[str, Any]]:
    text = (alkera / PERMISSIONS_LOCAL_FILE).read_text() if _has_local(alkera) else ""
    data = yaml.safe_load(text) or {}
    return list(data.get("rules") or [])


def _has_local(alkera: Path) -> bool:
    return (alkera / PERMISSIONS_LOCAL_FILE).exists()


async def test_the_grant_is_really_recorded(tmp_path: Path) -> None:
    """The floor under every case below: answering "Always allow" in a stance
    that asks does write a rule. If this stops being true the replay tests pass
    for the wrong reason."""
    alkera = _alkera(tmp_path)
    broker = await _grant_always(alkera, mode="default")
    assert broker.asked, "nobody was asked, so nothing was granted"
    assert [r["capability"] for r in _rules(alkera)] == ["fs"]


@pytest.mark.parametrize(
    ("granted_in", "in_force"),
    [
        pytest.param("default", "default", id="default-stays-default"),
        pytest.param("default", "auto", id="default-then-auto-is-looser"),
        pytest.param("auto", "auto", id="auto-stays-auto"),
    ],
)
async def test_a_standing_allow_binds_its_own_stance_and_looser_ones(
    tmp_path: Path, granted_in: str, in_force: str
) -> None:
    """What "Always allow" is FOR. The person answered where they were asked,
    and the same stance — or one that asks even less — honours it without
    asking again."""
    alkera = _alkera(tmp_path)
    await _grant_always(alkera, mode=granted_in)
    res, broker = await _replay(alkera, mode=in_force)
    assert res.allowed
    assert res.decided_by == "rule"
    assert broker.asked == [], "the standing grant should have answered without a person"


@pytest.mark.parametrize(
    ("granted_in", "in_force"),
    [
        pytest.param("auto", "default", id="auto-then-default-asks"),
        pytest.param("auto", "read_only", id="auto-then-read-only-refuses"),
        pytest.param("auto", "plan", id="auto-then-plan-refuses"),
        pytest.param("default", "read_only", id="default-then-read-only-refuses"),
        pytest.param("default", "plan", id="default-then-plan-refuses"),
    ],
)
async def test_a_stricter_stance_never_replays_a_looser_stance_grant(
    tmp_path: Path, granted_in: str, in_force: str
) -> None:
    """The finding. A rule minted where the stance asked little must not decide
    under a stance that asks more: Default asks a person, and the analyst modes
    refuse outright. Either way the rule is not what answered."""
    alkera = _alkera(tmp_path)
    await _grant_always(alkera, mode=granted_in)
    res, broker = await _replay(alkera, mode=in_force)
    assert res.decided_by != "rule", "a looser stance's grant decided under a stricter one"
    if in_force == "default":
        assert broker.asked, "Default promises to ask, and nobody was asked"
        assert res.allowed, "the person allowed it once, so it runs once"
    else:
        assert not res.allowed
        assert broker.asked == [], "an analyst mode refuses without asking anyone"


async def test_granting_again_under_the_stricter_stance_widens_the_rule(
    tmp_path: Path,
) -> None:
    """The ergonomics the clamp must not cost. A person who answers "Always
    allow" a second time — now in Default — has granted it where Default asks,
    so the THIRD identical action runs without a card. One rule, widened, not
    two."""
    alkera = _alkera(tmp_path)
    await _grant_always(alkera, mode="auto")
    await _grant_always(alkera, mode="default")
    assert len(_rules(alkera)) == 1, "the second grant should widen the rule, not duplicate it"
    res, broker = await _replay(alkera, mode="default")
    assert res.allowed
    assert res.decided_by == "rule"
    assert broker.asked == []


async def test_a_standing_reject_binds_every_stance(tmp_path: Path) -> None:
    """Tightening is never stance-scoped. "Always reject" answered in Bypass —
    the stance that asks nobody anything — still refuses under it, and under
    every looser reading of every other stance."""
    alkera = _alkera(tmp_path)
    broker = _Answers("reject_always")
    res = await _engine(alkera, broker).resolve(WRITE, mode="default")
    assert not res.allowed
    for mode in ("default", "auto", "read_only", "plan"):
        replayed, asked = await _replay(alkera, mode=mode)
        assert not replayed.allowed, f"the standing reject stopped binding in {mode}"
        assert asked.asked == [], f"{mode} asked a person about a settled reject"


async def test_a_hand_authored_policy_rule_carries_no_stance(tmp_path: Path) -> None:
    """Backward compatibility, and the design line: a rule a person WROTE into
    ``permissions.yml`` is a policy statement about the project, not the record
    of one card answered in one stance. It binds wherever the mode admits a
    rule at all."""
    alkera = _alkera(tmp_path)
    (alkera / PERMISSIONS_FILE).write_text(
        yaml.safe_dump({"rules": [{"capability": "fs", "operation": "write", "decision": "allow"}]})
    )
    res, broker = await _replay(alkera, mode="default")
    assert res.allowed
    assert res.decided_by == "rule"
    assert broker.asked == []


async def test_bypass_records_no_standing_grant_to_replay(tmp_path: Path) -> None:
    """Bypass never asks, so there is no answer to bank. Nothing it waived can
    come back as a rule under a stance that would have asked — the strongest
    form of the fix, and the one that needs no stance bookkeeping at all."""
    alkera = _alkera(tmp_path)
    broker = _Answers("allow_always")
    res = await _engine(alkera, broker).resolve(WRITE, mode="bypass")
    assert res.allowed
    assert broker.asked == [], "bypass asked a person"
    assert not _has_local(alkera), "bypass wrote a standing rule nobody was asked for"
    replayed, asked = await _replay(alkera, mode="default")
    assert replayed.decided_by != "rule"
    assert asked.asked, "Default must ask; bypass left something behind that answered"


async def test_a_legacy_local_grant_is_read_as_granted_under_auto(tmp_path: Path) -> None:
    """The installs that already exist. ``permissions.local.yml`` is where the
    engine records a person's "Always allow"; a rule written before the stance
    was recorded names none, and reading it as unscoped would hand every
    existing install exactly the hole the clamp closes.

    So a stance-less LOCAL allow reads as granted under the loosest stance that
    records one: it still binds in Auto, and Default asks once."""
    alkera = _alkera(tmp_path)
    (alkera / PERMISSIONS_LOCAL_FILE).write_text(
        yaml.safe_dump({"rules": [{"capability": "fs", "operation": "write", "decision": "allow"}]})
    )
    under_auto, auto_broker = await _replay(alkera, mode="auto")
    assert under_auto.allowed
    assert under_auto.decided_by == "rule"
    assert auto_broker.asked == [], "the legacy grant stopped binding where it was plausibly given"

    under_default, default_broker = await _replay(alkera, mode="default")
    assert under_default.decided_by != "rule", "a legacy local grant decided under Default"
    assert default_broker.asked, "Default promises to ask, and nobody was asked"


async def test_a_legacy_local_grant_is_widened_by_re_granting_it(tmp_path: Path) -> None:
    """The ask Default raises is answerable, and answering it settles the
    matter: the existing rule is widened in place, so the reader is asked once,
    not on every identical action forever."""
    alkera = _alkera(tmp_path)
    (alkera / PERMISSIONS_LOCAL_FILE).write_text(
        yaml.safe_dump({"rules": [{"capability": "fs", "operation": "write", "decision": "allow"}]})
    )
    await _grant_always(alkera, mode="default")
    assert len(_rules(alkera)) == 1, "the re-grant should widen the legacy rule, not add a second"
    res, broker = await _replay(alkera, mode="default")
    assert res.allowed
    assert res.decided_by == "rule"
    assert broker.asked == []


async def test_a_hand_authored_project_allow_is_not_read_as_a_recorded_grant(
    tmp_path: Path,
) -> None:
    """The line the file draws. The SAME rule written into ``permissions.yml``
    is a person's policy for the project, not the record of one card answered in
    one stance, so it binds under Default as it always has. Only the local
    overlay — which nothing but the engine writes grants into — is read as a
    recorded grant."""
    alkera = _alkera(tmp_path)
    (alkera / PERMISSIONS_FILE).write_text(
        yaml.safe_dump({"rules": [{"capability": "fs", "operation": "write", "decision": "allow"}]})
    )
    res, broker = await _replay(alkera, mode="default")
    assert res.allowed
    assert res.decided_by == "rule"
    assert broker.asked == []
