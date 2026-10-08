"""The in-tool write gate.

The gate is where a warehouse write meets what it would break. It resolves the
statement's targets against the lineage graph, collects the knowledge filed
against the affected assets, and hands that assessment to the one decision
engine every other path uses. Delegating means an in-tool action gets the same
treatment as a vendor tool ask: the effect-aware policy, the floor, the
auto-mode judge, the human prompt, the audit, and always-allow persistence.

Returns a :class:`SqlGateResult` carrying the ``CapToken`` the connector
chokepoint validates, the impact the card renders, and the model-visible
refusal that tells the agent what it would break and how to proceed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alkera_cli.contracts.tool_types import ActionDescriptor, CapToken, Effect
from alkera_cli.plugins.plugin_base.permissions.actor import (
    ActingPrincipal,
    current_acting_principal,
)
from alkera_cli.plugins.plugin_base.permissions.audit import draft_intent, ledger_for_sink
from alkera_cli.plugins.plugin_base.permissions.bash import (
    classify_command,
    command_touches_sensitive_path,
    credential_path_gate_enabled,
    raise_effect,
)
from alkera_cli.plugins.plugin_base.permissions.impact import ImpactAssessment
from alkera_cli.plugins.plugin_base.permissions.policy import AutoDecision
from alkera_cli.plugins.plugin_base.permissions.refusal_words import (
    NOT_GRANTED,
    person_decided,
    refusal_feedback,
)
from alkera_cli.plugins.plugin_base.permissions.resolve import (
    ActionResolution,
    DecisionEngine,
    GateEvidence,
)
from alkera_cli.plugins.plugin_base.permissions.wiring import sql_gate_impact

NO_SINK_REASON = (
    "Refused: this session has no decisions log, so the write cannot be recorded. "
    "Reopen the chat, or run from a project directory the agent can write to."
)

#: The MODEL-visible reason when a shell command is refused because the session is
#: read-only or planning. These modes are the analyst / explore-only contract: no
#: shell runs at all, so even a read-classified command (``cat``/``ls``/``env``) is
#: refused here. This is agent-facing guidance (it rides a tool error back so the
#: turn course-corrects); the READER-facing transcript sentence is a separate string
#: owned by ``cloud.refusal`` (``read_only_shell``), reached via ``note_for_tool``.
READ_ONLY_SHELL_REASON = (
    "read-only mode does not run shell commands; use the read-only data tools instead"
)


@dataclass(frozen=True, slots=True)
class GateBinding:
    """The session handles a gate decides with. Built once per tool call from the
    ``ToolContext``, so a tool body names what it gates with instead of forwarding
    nine keywords."""

    decision_sink: Any
    """The audit sink. ``None`` refuses every gated action."""
    permissions: Any = None
    broker: Any = None
    judge: Any = None
    lineage_store: Any = None
    """The project's ``LineageFactStore``, reached through ``ctx.registry``."""
    context_store: Any = None
    """The KB, for the knowledge bound to the affected set."""
    task_goal: str = ""
    session_id: str = ""
    alkera_dir: Any = None
    actor: ActingPrincipal | None = None
    """Who is acting, with their teams and the org's escalation opt-in, resolved
    once per session inside the trusted boundary. ``None`` (signed out, or a path
    that never resolved one) keeps cross-team escalation silent."""
    fence: Any = None
    """The session's bound (a cloud chat's ``SessionFence``), or ``None`` for a
    local session. With one, EVERY shell command is judged by where it reads and
    writes before any mode, rule or judge sees it — a read included — and the
    verdict is on the decision record whatever it is."""
    owner: str = ""
    """The chat owner's user id: whose standing answers a fenced session consults
    and records. Empty on a person's own machine and for a chat the box was not
    told the owner of."""

    def engine(self, *, source: str) -> DecisionEngine | None:
        """The decision engine for this binding, or ``None`` with no sink."""
        if self.decision_sink is None:
            return None
        return DecisionEngine(
            sink=self.decision_sink,
            permissions=self.permissions,
            broker=self.broker,
            judge=self.judge,
            intents=ledger_for_sink(self.decision_sink),
            alkera_dir=Path(self.alkera_dir) if self.alkera_dir is not None else None,
            # The workspace root (parent of ``.alkera/``) lets the auto-mode judge
            # reason about scope, passed for parity with the harness gate even
            # though SQL table targets aren't paths.
            workspace_root=(
                str(Path(self.alkera_dir).parent) if self.alkera_dir is not None else None
            ),
            source=source,
            session_id=self.session_id,
            # A fenced session is a cloud box's, and a cloud box serves more than
            # one person: only its owner's policy grants an EXEC there, and a
            # standing answer is the chat owner's alone.
            shared_host=self.fence is not None,
            owner=self.owner,
        )


@dataclass(frozen=True)
class SqlGateResult:
    """The outcome of :func:`gate_sql_action`."""

    cap_token: CapToken | None
    reason: str | None = None
    """The model-visible refusal: the impact path, the judge's verdict, or the
    policy's reasons."""
    judge_unavailable: bool = False
    """The auto-mode judge couldn't run — the tool refuses; the caller may also
    want to surface a turn-level error."""
    impact: ImpactAssessment | None = None
    """What the graph said this action reaches."""


@dataclass(frozen=True)
class ShellGateResult:
    """The outcome of :func:`gate_shell_action`."""

    allowed: bool
    reason: str | None = None
    judge_unavailable: bool = False


def _refused_reason(res: ActionResolution, *, fallback: str | None) -> str | None:
    """The sentence a refused action carries to the model and the card.

    The decider's own words come first: a person's feedback after the
    not-granted sentence, a policy's reason as itself. A person who said no
    without words is still a person's no — never the ask's explanation dressed
    as the policy's refusal, which reads to the model as something to work
    around. A policy with no words of its own falls back to what the ask
    explained."""
    if res.allowed:
        return None
    if res.reason:
        return refusal_feedback(res.decided_by, res.reason) or res.reason
    return NOT_GRANTED if person_decided(res.decided_by) else fallback


def denied_error(reason: str) -> str:
    """The tool error for a refused action. A person's refusal is a whole
    sentence and stands alone; a policy's reason rides after the gate's
    ``permission denied:`` prefix, which every surface reads as a refusal."""
    return reason if reason.startswith(NOT_GRANTED) else f"permission denied: {reason}"


async def gate_sql_action(
    descriptor: ActionDescriptor,
    *,
    mode: str,
    binding: GateBinding,
    tool_call_id: str | None = None,
    intent: tuple[Sequence[str], str] | None = None,
    preview: dict[str, Any] | None = None,
) -> SqlGateResult:
    """Gate a non-READ action: measure the impact, then decide. ``cap_token`` is
    set iff authorized (a READ needs no token, since the connector runs it in a
    read path).

    ``intent`` is the agent's declaration, ``(assets, reason)``. It reaches the
    ledger only when it covered what THIS call breaks and that call was then
    authorized. From then on it covers every later write inside the set. When
    an earlier banked declaration does the covering instead, the new
    declaration was never weighed and stays out of the ledger. A harmless
    write the declaration never had to answer for, and a refused attempt,
    both leave nothing behind to cover a retry in a laxer mode.

    ``preview`` is what the permission card renders beside the statement (a
    ``{kind, title, content, truncated}`` dict), for an action whose raw text is not
    the best view of what it sends."""
    engine = binding.engine(source="sql_gate")
    if engine is None:
        return SqlGateResult(None, NO_SINK_REASON)
    impact = await sql_gate_impact(
        descriptor,
        lineage_store=binding.lineage_store,
        context_store=binding.context_store,
        actor=binding.actor,
    )
    declared = (
        draft_intent(session_id=binding.session_id, assets=intent[0], reason=intent[1])
        if intent is not None
        else None
    )
    res = await engine.resolve(
        descriptor,
        mode=mode,
        task_goal=binding.task_goal,
        tool_call_id=tool_call_id,
        request=(
            engine.build_request(descriptor, tool_call_id=tool_call_id, preview=preview)
            if preview is not None
            else None
        ),
        evidence=GateEvidence(impact=impact, declared=declared),
    )
    # Only this call's own declaration is banked. ``res.weighed`` is a ledger
    # commitment when an earlier call did the covering, and that one is already in.
    # Matched by id, so normalizing a declaration in flight cannot quietly stop the
    # ledger from recording it.
    if (
        declared is not None
        and res.allowed
        and res.weighed is not None
        and res.weighed.intent_id == declared.intent_id
        and engine.intents is not None
    ):
        engine.intents.commit(declared)
    token = CapToken.mint(descriptor) if res.allowed else None
    return SqlGateResult(token, _refused_reason(res, fallback=None), res.judge_unavailable, impact)


async def gate_shell_action(
    command: str,
    *,
    mode: str,
    binding: GateBinding,
    tool_call_id: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ShellGateResult:
    """Gate a shell command through the SAME engine the SQL gate and harness loop
    use, so a backgrounded or foreground ``bash`` command is judged identically.

    Classifies the command (read/write/destroy/egress), then applies the
    SENSITIVE-PATH escalation: a command that references a secret path (``~/.ssh``,
    ``~/.alkera/auth.yml``, ``*.pem`` …) is raised to EGRESS so it can't be silently
    auto-allowed as a plain read. It PROMPTS in ``default``, is JUDGED in ``auto``,
    and is REFUSED in ``read_only``/``plan``. The escalation is a FLOOR that only
    ``bypass`` waives.

    ``read_only`` and ``plan`` refuse the shell OUTRIGHT — before the read fast path
    — because their contract is no side effects at all, and a shell ``read`` is still
    an uncontained escape from the workspace bound (a ``cat``/``env``/``grep`` can
    disclose the box's own credentials, which the SQL read path never touches). This
    is what makes the cloud analyst session's "no shell" guarantee true at the one
    chokepoint every parent-hosted ``bash`` call flows through, rather than trusting
    the classifier to have marked every escape non-read."""
    if mode in ("read_only", "plan"):
        return ShellGateResult(allowed=False, reason=READ_ONLY_SHELL_REASON)
    descriptor = classify_command(command)
    # The credential-path escalation is off unless its switch is set — see
    # `credential_path_gate_enabled`; a path then decides nothing on its own.
    if credential_path_gate_enabled() and command_touches_sensitive_path(command):
        # Tighten-only: a sensitive read/write rises to EGRESS (the exfiltration
        # floor); a DESTROY or an EXEC is already above it and keeps its tier.
        descriptor = raise_effect(descriptor, Effect.EGRESS)
    if binding.fence is not None:
        # A bounded (cloud) session: where the command reads and writes is judged
        # BEFORE its effect class, its mode, any rule and the judge — a READ
        # included, because on a shared box ``cat`` reaches the box's own token
        # and the other chats exactly as well as ``cp`` does. The verdict is on
        # the record whatever it is; nothing below runs unrecorded.
        engine = binding.engine(source="shell_gate")
        if engine is None:
            return ShellGateResult(allowed=False, reason=NO_SINK_REASON)
        # The judge resolves names and expands globs on disk: off the loop.
        verdict = await asyncio.to_thread(
            binding.fence.judge_shell,
            command,
            cwd=cwd if cwd is not None else binding.fence.working_dir,
            env=env,
            writing=descriptor.effect != Effect.READ,
        )
        if verdict.escaped:
            # Out of bounds is refused in every mode, bypass included: what bypass
            # hands over is the asking, never the boundary.
            reason = binding.fence.explain(verdict)
            res = await engine.refuse(
                descriptor, mode=mode, decided_by="fence", reason=reason, tool_call_id=tool_call_id
            )
            return ShellGateResult(allowed=False, reason=res.reason or reason)
        sandboxed = bool(getattr(binding.fence, "sandboxed", False))
        if verdict.unknown and mode != "bypass" and not sandboxed:
            # A command whose reach cannot be proved is a person's to answer where
            # the stance asks one. In bypass it is not a boundary the fence caught,
            # so it takes the ordinary ladder below, as a classified write does;
            # so does every command in a sandbox of its own, whose mounts and uid
            # bound what the judge could not read (a read runs, a write is judged
            # in auto and asked in default, a destroy is asked in both).
            # A person who already answered for this exact text — what "Always
            # allow" on a compound command records, and nothing wider — is not
            # asked again; a family's standing allow is not that answer. On a box
            # that is the box owner's committed policy alone, as on the harness
            # path: no chat records an answer for a command the fence cannot read.
            exact = getattr(engine.permissions_now(standing=False), "exact_text_decision", None)
            standing = exact(descriptor, mode=mode) if exact is not None else None
            if standing is AutoDecision.ALLOW:
                await engine.record_standing_allow(descriptor, mode=mode)
                return ShellGateResult(allowed=True)
            res = await engine.ask(
                descriptor, mode=mode, task_goal=binding.task_goal, tool_call_id=tool_call_id
            )
            return ShellGateResult(
                allowed=res.allowed,
                reason=_refused_reason(res, fallback=binding.fence.explain(verdict)),
            )
        if verdict.bound == "process" and descriptor.effect == Effect.WRITE:
            # A signal in a sandbox with a process table of its own reaches only
            # what the chat itself runs: confined like a write in its folder,
            # admitted in every stance. A destroy still takes the ladder below.
            await engine.record_confined(descriptor, mode=mode)
            return ShellGateResult(allowed=True)
        if descriptor.effect == Effect.READ:
            await engine.record_read(descriptor, mode=mode)
            return ShellGateResult(allowed=True)
    elif descriptor.effect == Effect.READ:
        # A plain read auto-allows — no gate (matches the harness READ path).
        return ShellGateResult(allowed=True)
    engine = binding.engine(source="shell_gate")
    if engine is None:
        return ShellGateResult(allowed=False, reason=NO_SINK_REASON)
    impact = await sql_gate_impact(
        descriptor,
        lineage_store=binding.lineage_store,
        context_store=binding.context_store,
        actor=binding.actor,
    )
    res = await engine.resolve(
        descriptor,
        mode=mode,
        task_goal=binding.task_goal,
        tool_call_id=tool_call_id,
        evidence=GateEvidence(impact=impact),
    )
    return ShellGateResult(
        allowed=res.allowed,
        reason=_refused_reason(res, fallback=None),
        judge_unavailable=res.judge_unavailable,
    )


def binding_from_context(ctx: Any) -> GateBinding:
    """The gate binding a tool call carries. The lineage handle lives on the
    registry, which is how the store reaches the gate."""
    registry = getattr(ctx, "registry", None)
    return GateBinding(
        decision_sink=ctx.decision_sink,
        permissions=ctx.permissions,
        broker=ctx.broker,
        judge=ctx.judge,
        lineage_store=getattr(registry, "lineage_store", None),
        context_store=getattr(registry, "context_store", None),
        task_goal=ctx.task_goal,
        session_id=ctx.session_id,
        alkera_dir=ctx.alkera_dir,
        actor=current_acting_principal(
            alkera_dir=ctx.alkera_dir, credential=getattr(ctx, "credential", None)
        ),
        fence=getattr(ctx, "fence", None),
        owner=str(getattr(registry, "knowledge_owner", "") or ""),
    )


__all__ = [
    "NO_SINK_REASON",
    "READ_ONLY_SHELL_REASON",
    "GateBinding",
    "ShellGateResult",
    "SqlGateResult",
    "binding_from_context",
    "denied_error",
    "gate_shell_action",
    "gate_sql_action",
]
