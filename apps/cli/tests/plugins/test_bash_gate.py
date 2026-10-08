"""The shell permission gate (``gate_shell_action``) — the security boundary for
the ported ``bash`` tool.

Pins the sensitive-path escalation across every permission mode, and that
``bypass`` stays a TRUE accept-everything mode (the escalation is a floor that
``bypass`` waives, never a hard deny).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _decision_sink import MemorySink
from alkera_cli.cloud.fence import FenceVerdict
from alkera_cli.plugins.plugin_base.permissions import (
    CREDENTIAL_PATH_GATE_ENV,
    command_touches_sensitive_path,
    gate_shell_action,
)
from alkera_cli.plugins.plugin_base.permissions.bash import classify_command
from alkera_cli.plugins.plugin_base.permissions.config import add_local_rule, load_permissions
from alkera_cli.plugins.plugin_base.permissions.gate import GateBinding, denied_error
from alkera_cli.plugins.plugin_base.permissions.refusal_words import NOT_GRANTED


@pytest.fixture(autouse=True)
def _credential_path_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is OFF by default (``credential_path_gate_enabled``): the scan
    refused the plan file plan mode asks for, and it has not been tested against
    the paths a real session names. These cases run with it switched on so the
    mechanism stays pinned for the day it is turned back on deliberately; the
    default-off contract lives in ``test_credential_path_gate_switch.py``."""
    monkeypatch.setenv(CREDENTIAL_PATH_GATE_ENV, "1")


async def _shell(command: str, *, mode: str, broker: Any = None) -> Any:
    binding = GateBinding(decision_sink=MemorySink(), broker=broker)
    return await gate_shell_action(command, mode=mode, binding=binding)


class _Broker:
    """Records prompts; answers each with a fixed option."""

    def __init__(self, option: str) -> None:
        self._option = option
        self.prompts = 0

    async def resolve(self, request: Any) -> str:
        self.prompts += 1
        return self._option


# --------------------------------------------------------------------------- #
# A plain read never asks.
# --------------------------------------------------------------------------- #


async def test_plain_read_auto_allows_without_prompting() -> None:
    broker = _Broker("reject_once")  # would reject IF it were ever asked
    res = await _shell("cat README.md", mode="default", broker=broker)
    assert res.allowed is True
    assert broker.prompts == 0


async def test_plain_write_prompts_in_default() -> None:
    broker = _Broker("reject_once")
    res = await _shell("rm -rf build/", mode="default", broker=broker)
    assert res.allowed is False
    assert broker.prompts == 1  # a destroy is gated


# --------------------------------------------------------------------------- #
# The sensitive-path escalation, per mode.
# --------------------------------------------------------------------------- #


async def test_sensitive_read_prompts_in_default_and_honors_approval() -> None:
    # `cat` is in the read corpus (would auto-allow) — but the sensitive path
    # escalates it to a prompt. Approval lets it through.
    broker = _Broker("allow_once")
    res = await _shell("cat ~/.alkera/auth.yml", mode="default", broker=broker)
    assert res.allowed is True
    assert broker.prompts == 1


async def test_sensitive_read_denied_in_default_when_rejected() -> None:
    broker = _Broker("reject_once")
    res = await _shell("cat ~/.ssh/id_rsa", mode="default", broker=broker)
    assert res.allowed is False
    assert broker.prompts == 1


async def test_sensitive_read_fails_closed_without_a_broker() -> None:
    # No human to ask → the escalated read is denied, never silently allowed.
    res = await _shell("cat ~/.aws/credentials", mode="default", broker=None)
    assert res.allowed is False


@pytest.mark.parametrize("mode", ["read_only", "plan"])
@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cat README.md", id="cat-a-plain-file"),
        pytest.param("ls /opt", id="ls-a-dir"),
        pytest.param("printenv", id="dump-the-environment"),
        pytest.param("env", id="env"),
        pytest.param("grep -r token /opt", id="grep-recursive"),
        pytest.param("cat /opt/alkera-home/auth.yml", id="read-the-box-token"),
        pytest.param("cat $ALKERA_HOME/auth.yml", id="env-var-spelling"),
    ],
)
async def test_even_a_plain_read_shell_command_is_refused_in_read_only_and_plan(
    command: str, mode: str
) -> None:
    """The whole point of read-only mode: a read-only (or plan) session runs NO shell
    at all — not even a read-classified ``cat``/``ls``/``env``, which the old READ
    fast path auto-allowed before any mode check, letting the model read the box's
    device token and dump the daemon environment. The refusal happens before
    classification, so no broker is ever asked."""
    from alkera_cli.plugins.plugin_base.permissions.gate import READ_ONLY_SHELL_REASON

    broker = _Broker("allow_once")  # would allow IF it were ever asked
    res = await _shell(command, mode=mode, broker=broker)
    assert res.allowed is False
    assert broker.prompts == 0
    assert res.reason == READ_ONLY_SHELL_REASON


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("cat README.md", id="cat-a-plain-file"),
        pytest.param("ls /opt", id="ls-a-dir"),
        pytest.param("printenv", id="printenv"),
    ],
)
async def test_a_plain_read_shell_command_still_auto_allows_in_default(command: str) -> None:
    """The control: a LOCAL (non-cloud) session in ``default`` mode keeps today's
    behaviour — a plain read runs with no prompt. The read-only refusal is scoped
    to the analyst/plan modes, not a blanket shell ban."""
    broker = _Broker("reject_once")  # would reject IF asked
    res = await _shell(command, mode="default", broker=broker)
    assert res.allowed is True
    assert broker.prompts == 0


async def test_sensitive_read_refused_in_read_only_without_prompting() -> None:
    broker = _Broker("allow_once")
    res = await _shell("cat ~/.aws/credentials", mode="read_only", broker=broker)
    assert res.allowed is False
    assert broker.prompts == 0  # read_only refuses by mode — it never asks


async def test_sensitive_read_refused_in_plan_without_prompting() -> None:
    broker = _Broker("allow_once")
    res = await _shell("grep secret ~/.netrc", mode="plan", broker=broker)
    assert res.allowed is False
    assert broker.prompts == 0


async def test_bypass_waives_the_sensitive_escalation_and_never_prompts() -> None:
    # THE invariant: bypass is a true accept-everything mode. A sensitive read runs
    # with no prompt and no denial — the escalation floor is waived in bypass.
    broker = _Broker("reject_once")
    res = await _shell("cat ~/.alkera/auth.yml", mode="bypass", broker=broker)
    assert res.allowed is True
    assert broker.prompts == 0


async def test_bypass_waives_a_destroy_too() -> None:
    broker = _Broker("reject_once")
    res = await _shell("rm -rf /", mode="bypass", broker=broker)
    assert res.allowed is True
    assert broker.prompts == 0


async def test_pem_file_reference_escalates() -> None:
    broker = _Broker("reject_once")
    res = await _shell("cat server.pem", mode="default", broker=broker)
    assert res.allowed is False  # escalated → prompted → rejected
    assert broker.prompts == 1


async def test_quote_split_evasion_is_caught() -> None:
    # The trivial evasion: shell-quote a sensitive path so the substring scan misses
    # `.ssh`. De-quoting re-collapses `~/.s''sh` → `~/.ssh`, so it still escalates.
    broker = _Broker("reject_once")
    for command in ("cat ~/.s''sh/id_rsa", 'cat ~/."ssh"/config', r"cat ~/.s\sh/id_rsa"):
        broker.prompts = 0
        res = await _shell(command, mode="default", broker=broker)
        assert res.allowed is False, command
        assert broker.prompts == 1, command


async def test_dotenv_secret_read_escalates_in_default() -> None:
    # `.env` files hold the project's DB passwords / API keys / SMTP creds, so the
    # sensitive-path floor escalates a plain `cat .env` READ to a prompt, the same
    # treatment as ~/.alkera/auth.yml.
    broker = _Broker("reject_once")
    for command in ("cat .env", "cat .env.local", "grep SECRET .env", "cat config/.env.production"):
        broker.prompts = 0
        res = await _shell(command, mode="default", broker=broker)
        assert res.allowed is False, command  # escalated → prompted → rejected
        assert broker.prompts == 1, command


async def test_dotenv_secret_read_fails_closed_without_a_broker() -> None:
    # No human to ask → the escalated `.env` read is denied, never silently allowed.
    res = await _shell("cat .env", mode="default", broker=None)
    assert res.allowed is False


@pytest.mark.parametrize(
    ("command", "touches"),
    [
        # The genuine credential-exfil paths still escalate.
        pytest.param(
            "gcloud auth print-access-token", True, id="gcloud-auth-token-prints-live-creds"
        ),
        pytest.param("gcloud auth list", True, id="gcloud-auth-list"),
        pytest.param("cat ~/.config/gcloud/credentials.db", True, id="gcloud-cred-file"),
        # ...but harmless gcloud READS stay off the floor: a bare "gcloud" marker
        # would push every invocation to the exfiltration floor.
        pytest.param("gcloud config list", False, id="gcloud-config-list-harmless"),
        pytest.param("gcloud projects list", False, id="gcloud-projects-list-harmless"),
        pytest.param("gcloud compute instances list", False, id="gcloud-compute-list-harmless"),
        # `.env` secrets are covered; a Python `.venv` (touched constantly) must NOT
        # trip the scan — its dot is followed by 'v', so the ".env" substring can't match.
        pytest.param("cat .env", True, id="dotenv-covered"),
        pytest.param("source .venv/bin/activate", False, id="dotvenv-not-a-false-positive"),
        pytest.param("cat .venv/pyvenv.cfg", False, id="dotvenv-cfg-not-a-false-positive"),
    ],
)
def test_sensitive_path_markers_are_precise(command: str, touches: bool) -> None:
    assert command_touches_sensitive_path(command) is touches


async def test_cloud_and_k8s_secret_stores_escalate() -> None:
    broker = _Broker("reject_once")
    for command in (
        "cat ~/.kube/config",
        "cat ~/.config/gcloud/credentials.db",
        "cat ~/.vault-token",
        "cat /var/run/secrets/kubernetes.io/serviceaccount/token",
    ):
        broker.prompts = 0
        res = await _shell(command, mode="default", broker=broker)
        assert res.allowed is False, command


# --------------------------------------------------------------------------- #
# A gated shell command reads as the canonical "shell" kind (not "other").
# --------------------------------------------------------------------------- #


async def test_shell_permission_request_reads_as_canonical_shell() -> None:
    """A gated shell command's PermissionRequest carries ``canonical_kind="shell"``
    (parity with the old native bash) so its permission card + the portable policy
    lane treat it as a shell action — the data tools (sql, …) stay ``"other"``."""
    captured: list[Any] = []

    class _CaptureBroker:
        async def resolve(self, request: Any) -> str:
            captured.append(request)
            return "reject_once"

    res = await _shell("rm -rf build/", mode="default", broker=_CaptureBroker())
    assert res.allowed is False
    assert len(captured) == 1
    assert captured[0].canonical_kind == "shell"
    assert captured[0].permission_kind == "shell"


# --------------------------------------------------------------------------- #
# Under the fence, a command whose reach cannot be proved.
# --------------------------------------------------------------------------- #

_UNKNOWN_EXPLANATION = (
    "The workspace policy could not tell where this command reads or writes, "
    "so it runs only with a person's approval."
)


class _UnknownFence:
    """A fence that cannot tell where the command reaches."""

    working_dir = Path("/work")

    def judge_shell(self, command: str, *, cwd: Any, env: Any, writing: bool) -> FenceVerdict:
        return FenceVerdict("unknown", target=command, writing=writing)

    def explain(self, verdict: FenceVerdict) -> str:
        return _UNKNOWN_EXPLANATION


class _DecidingBroker:
    """The harness's broker shape: says who decided and with what words."""

    def __init__(self, option: str, *, decided_by: str, reason: str | None) -> None:
        self._answer = SimpleNamespace(option=option, decided_by=decided_by, reason=reason)
        self.prompts = 0

    async def decide(self, request: Any) -> Any:
        self.prompts += 1
        return self._answer


async def _fenced(command: str, *, mode: str, broker: Any = None, permissions: Any = None) -> Any:
    binding = GateBinding(
        decision_sink=MemorySink(), broker=broker, permissions=permissions, fence=_UnknownFence()
    )
    return await gate_shell_action(command, mode=mode, binding=binding)


async def test_a_persons_wordless_no_under_the_fence_reads_as_not_granted() -> None:
    # The ask's explanation is why a person was asked, not what they answered:
    # handed to the model as the refusal it read as a policy to work around.
    broker = _Broker("reject_once")
    res = await _fenced("rm -rf ./scratch", mode="default", broker=broker)
    assert broker.prompts == 1
    assert not res.allowed
    assert res.reason == NOT_GRANTED


async def test_a_persons_no_with_feedback_carries_the_feedback_after_the_sentence() -> None:
    broker = _DecidingBroker("reject_once", decided_by="human", reason="use the trash instead")
    res = await _fenced("rm -rf ./scratch", mode="default", broker=broker)
    assert not res.allowed
    assert res.reason == f"{NOT_GRANTED} use the trash instead"


async def test_a_prompt_that_ran_out_is_a_persons_no_too() -> None:
    broker = _DecidingBroker("reject_once", decided_by="timeout", reason=None)
    res = await _fenced("rm -rf ./scratch", mode="default", broker=broker)
    assert res.reason == NOT_GRANTED


async def test_a_policys_wordless_no_under_the_fence_keeps_the_asks_explanation() -> None:
    # Nobody to ask: the refusal is the policy's, and the explanation is the
    # most useful thing it can say.
    res = await _fenced("rm -rf ./scratch", mode="default", broker=None)
    assert not res.allowed
    assert res.reason == _UNKNOWN_EXPLANATION


async def test_the_box_owners_exact_text_allow_is_not_asked_again(tmp_path: Path) -> None:
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    command = "echo one && touch walkthrough.txt"
    (alkera / "permissions.yml").write_text(
        f"rules:\n  - capability: shell\n    decision: allow\n    match: {command!r}\n"
    )
    cfg = load_permissions(alkera)
    broker = _Broker("reject_once")  # would refuse IF it were asked

    res = await _fenced(command, mode="default", broker=broker, permissions=cfg)
    assert res.allowed
    assert broker.prompts == 0

    # A different compound that leads with the same program is not that answer.
    other = await _fenced(
        "echo one && touch other.txt", mode="default", broker=broker, permissions=cfg
    )
    assert not other.allowed
    assert broker.prompts == 1


async def test_an_exact_text_answer_a_chat_recorded_is_asked_again_on_a_box(
    tmp_path: Path,
) -> None:
    """A box's ``permissions.local.yml`` serves every chat placed on it, of every
    org, and nothing in it says whose answer a rule was. An answer recorded there
    decides nothing on a fenced session: the person in front of this chat is
    asked."""
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    command = "echo one && touch walkthrough.txt"
    assert add_local_rule(alkera, classify_command(command), mode="default") is True
    cfg = load_permissions(alkera)
    broker = _Broker("reject_once")

    res = await _fenced(command, mode="default", broker=broker, permissions=cfg)
    assert not res.allowed
    assert broker.prompts == 1


async def test_a_familys_standing_allow_does_not_cover_an_unproved_command(tmp_path: Path) -> None:
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    assert add_local_rule(alkera, classify_command("touch a.txt"), mode="default") is True
    cfg = load_permissions(alkera)
    broker = _Broker("allow_once")

    res = await _fenced("echo one && touch b.txt", mode="default", broker=broker, permissions=cfg)
    assert res.allowed
    assert broker.prompts == 1  # asked, then allowed by the person


async def test_the_exact_text_answer_is_held_to_its_stance(tmp_path: Path) -> None:
    # Granted under `default`, the answer does not replay under a stricter
    # stance that asks more.
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    command = "echo one && touch walkthrough.txt"
    add_local_rule(alkera, classify_command(command), mode="auto")
    cfg = load_permissions(alkera)
    broker = _Broker("reject_once")

    res = await _fenced(command, mode="default", broker=broker, permissions=cfg)
    assert not res.allowed
    assert broker.prompts == 1


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        pytest.param(NOT_GRANTED, NOT_GRANTED, id="persons-wordless-no"),
        pytest.param(
            f"{NOT_GRANTED} use the trash", f"{NOT_GRANTED} use the trash", id="persons-feedback"
        ),
        pytest.param(
            "The workspace policy refused this call.",
            "permission denied: The workspace policy refused this call.",
            id="policys-sentence",
        ),
        pytest.param(
            "connection 'pg' is read-only",
            "permission denied: connection 'pg' is read-only",
            id="tools-detail",
        ),
    ],
)
def test_the_tool_error_lets_a_persons_sentence_stand_alone(reason: str, expected: str) -> None:
    assert denied_error(reason) == expected


async def test_an_always_allow_recorded_mid_session_is_honoured_on_the_next_call(
    tmp_path: Path,
) -> None:
    # The tool server loads its policy when the chat starts. The person's
    # "Always allow" during the chat writes the local file; the next call must
    # read it, or the same command asks again until the chat is restarted.
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    cfg = load_permissions(alkera)  # the session's snapshot, taken before the answer
    assert add_local_rule(alkera, classify_command("touch a.txt"), mode="default") is True
    broker = _Broker("reject_once")  # would refuse IF it were asked
    binding = GateBinding(decision_sink=MemorySink(), broker=broker, permissions=cfg)
    plain = await gate_shell_action("touch b.txt", mode="default", binding=binding)
    assert plain.allowed
    assert broker.prompts == 0


async def test_a_standing_answer_given_on_a_box_records_nothing(tmp_path: Path) -> None:
    """The in-tool gate on a fenced session offers no standing grant, and an
    answer that names one anyway decides the one call and writes no rule."""
    alkera = tmp_path / ".alkera"
    alkera.mkdir()
    sink = MemorySink()
    offered: list[list[str]] = []

    class _Always:
        prompts = 0

        async def resolve(self, request: Any) -> str:
            self.prompts += 1
            offered.append([option.option_id for option in request.options])
            return "allow_always"

    broker = _Always()
    binding = GateBinding(
        decision_sink=sink,
        broker=broker,
        permissions=load_permissions(alkera),
        alkera_dir=alkera,
        fence=_UnknownFence(),
    )
    command = "echo one && touch walkthrough.txt"
    first = await gate_shell_action(command, mode="default", binding=binding)
    second = await gate_shell_action(command, mode="default", binding=binding)
    assert first.allowed and second.allowed
    assert broker.prompts == 2
    assert offered == [["allow_once", "reject_once"], ["allow_once", "reject_once"]]
    assert not (alkera / "permissions.local.yml").exists()
