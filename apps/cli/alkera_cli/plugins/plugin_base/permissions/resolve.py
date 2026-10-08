"""The SINGLE permission-decision pipeline.

:class:`DecisionEngine` turns a classified action into an allow or a reject:
the effect-aware policy, the measured impact and the intent that may cover it,
the auto-mode safety judge for the recoverable write middle, the destroy floor,
the human broker, the audit, and always-allow persistence. EVERY
permission-generating path runs through one engine, the harness loop (bash, fs,
edit) and every in-tool gate (``sql.query`` via ``gate_sql_action``) alike, so
the floor and auto-mode grounding apply uniformly. There is no second gate.

The engine is CONSTRUCTED with its dependencies instead of taking a bag of
optionals per call. The sink is mandatory. A decision no one can read back is
not a decision this engine grants, so an audit write that fails refuses the
action. The broker and judge stay optional because a session legitimately has
no human and no gateway, and both absences fail closed.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from alkera_core.schemas.chat import (
    STANDING_OPTIONS,
    PermissionOption,
    PermissionOptionId,
    PermissionRequest,
)

from alkera_cli.contracts.tool_types import ActionDescriptor
from alkera_cli.plugins.plugin_base.permissions.audit import (
    AuditUnavailableError,
    DecisionRecord,
    DeclaredIntent,
    IntentLedger,
    covers,
)
from alkera_cli.plugins.plugin_base.permissions.config import add_local_rule
from alkera_cli.plugins.plugin_base.permissions.consent_scope import (
    EXACT_ALWAYS_LABEL,
    needs_exact_consent,
    standing_allow_scope,
)
from alkera_cli.plugins.plugin_base.permissions.impact import ImpactAssessment, refusal_reason
from alkera_cli.plugins.plugin_base.permissions.policy import (
    AutoDecision,
    GateOutcome,
    PolicyResult,
    decide_gate,
    evaluate_action,
    gate_outcome,
    is_floor,
    needs_auto_grounding,
)

if TYPE_CHECKING:
    from alkera_cli.plugins.plugin_base.permissions.config import PermissionsConfig

_PROMPT_OPTIONS = [
    PermissionOption(option_id="allow_once", name="Allow once"),
    PermissionOption(option_id="allow_always", name="Always allow"),
    PermissionOption(option_id="reject_once", name="Reject once"),
    PermissionOption(option_id="reject_always", name="Always reject"),
]

AUDIT_FAILED_REASON = (
    "Refused: this decision could not be written to the audit log, and ungoverned writes do not "
    "run. Check that the project directory is writable, then retry."
)

#: The option ids a broker may answer with. Anything else fails closed to a
#: reject, because a misread vendor value must never authorize a write.
_KNOWN_OPTIONS = frozenset(
    {"allow_once", "allow_always", "reject_once", "reject_always", "cancelled"}
)


class AskReconsideredError(Exception):
    """The person being asked no longer decides this ask: the stance it was
    raised under changed while it waited.

    Raised out of the human seam, and carried through the ladder untouched, so
    the caller decides the ask again from the top under the stance now in
    force. Nothing is recorded for the interrupted attempt; the prompt itself
    stays up, and an ask the new stance still puts to a person is shown the same
    card it already has.
    """


class DecisionRecorder(Protocol):
    """The audit sink. ``record`` raises when the append fails."""

    def record(self, rec: DecisionRecord) -> None: ...


class PermissionAsker(Protocol):
    """The human seam, which resolves a request to a vendor option id."""

    async def resolve(self, request: PermissionRequest) -> Any: ...


class SafetyJudge(Protocol):
    """The auto-mode grounding seam. ``verdict.decision`` is allow or block."""

    async def judge(
        self, descriptor: ActionDescriptor, task_goal: str, *, workspace_root: str | None = None
    ) -> Any: ...


@dataclass(frozen=True)
class ActionResolution:
    """The outcome of :meth:`DecisionEngine.resolve`."""

    allowed: bool
    option: PermissionOptionId
    """The resolved vendor option (``allow_once`` / ``allow_always`` / ``reject_once``
    / ``reject_always``) — the harness forwards it to the adapter; a tool gate just
    reads ``allowed``."""
    decided_by: str
    """``read`` | ``rule`` | ``mode`` | ``floor`` | ``confidence`` | ``bypass`` |
    ``impact`` | ``intent`` | ``unsure`` | ``judge`` | ``human`` | ``fail_closed`` |
    ``audit``."""
    reason: str | None = None
    """The MODEL-visible denial reason on a reject (the impact path, the judge's
    verdict, or the policy's reasons) so the agent course-corrects."""
    judge_unavailable: bool = False
    """The auto-mode judge couldn't run — the caller should stop the turn."""
    weighed: DeclaredIntent | None = None
    """The declaration that covered this call's impact, either the one filed with
    the call (the very object passed in) or a standing ledger commitment from an
    earlier call. Banking only what this call filed is therefore an identity
    check, and a call that weighed nothing reports ``None``."""


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """What the SQL gate brings to a decision: the measured impact and the
    declaration the agent filed with this very call. The declaration is weighed
    for coverage during the decision and committed to the ledger by the caller
    only when the resolution reports it as the one that covered, so a harmless
    write and a refused attempt both leave nothing behind to cover a retry."""

    impact: ImpactAssessment | None = None
    declared: DeclaredIntent | None = None


@dataclass(frozen=True, slots=True)
class _Call:
    """One action as it travels the ladder."""

    descriptor: ActionDescriptor
    mode: str
    task_goal: str = ""
    tool_call_id: str | None = None
    request: PermissionRequest | None = None
    impact: ImpactAssessment | None = None
    intent: DeclaredIntent | None = None
    """The declaration in force, once one is found to cover the impact."""
    standing: bool = True
    """Whether a standing answer may be given on this call at all. ``False`` for
    an ask a bound could not vouch for on a shared host, which a person answers
    every time it comes up."""


_BOUND_TO_ONE_ASK: dict[str, PermissionOptionId] = {
    "allow_always": "allow_once",
    "reject_always": "reject_once",
}


def bind_to_this_ask(option: PermissionOptionId) -> PermissionOptionId:
    """The same answer with its standing scope dropped.

    For a path that records no rule: the person's allow or reject still decides
    the call they were asked about, and nothing is promised past it. A vendor
    told ``always`` learns a coarse prefix rule and then stops raising the ask
    at all, so an answer no rule backs must never reach one as ``always``."""
    return _BOUND_TO_ONE_ASK.get(option, option)


def as_answered_by(option: str, *, answered_by: object, owner: str) -> str:
    """``option`` as a relay from ``answered_by`` may give it in a chat ``owner`` owns.

    A standing answer is recorded as the chat owner's rule and applies in their
    other chats on the same box, so it stands only when the owner gave it. The
    server refuses one from anyone else; one that arrives anyway (an older
    server, a record naming nobody) decides this ask and nothing more."""
    if option in STANDING_OPTIONS and (not owner or answered_by != owner):
        return _BOUND_TO_ONE_ASK[option]
    return option


def scoped_options(
    options: Sequence[PermissionOption], descriptor: ActionDescriptor | None
) -> list[PermissionOption]:
    """``options`` with the shell's standing allow named for what it records.

    A shell command whose "Always allow" remembers its exact text says so on the
    card; one that would record nothing (a destructive command that expands at
    run time) offers no standing allow at all. Every other ask is unchanged."""
    if descriptor is None or descriptor.capability != "shell":
        return list(options)
    scope = standing_allow_scope(descriptor)
    out: list[PermissionOption] = []
    for option in options:
        if option.option_id == "allow_always":
            if scope is None and needs_exact_consent(descriptor):
                continue
            if scope == "exact":
                option = option.model_copy(update={"name": EXACT_ALWAYS_LABEL})
        out.append(option)
    return out


def without_standing_grant(options: Sequence[PermissionOption]) -> list[PermissionOption]:
    """``options`` minus the standing grants, for an ask nothing could record one
    on. A card is built from what the ask offers, so this is what a reader sees."""
    return [option for option in options if option.option_id not in STANDING_OPTIONS]


def standing_answer_recordable(
    request: PermissionRequest,
    owner: str,
    fence_reads: Callable[[PermissionRequest], bool],
) -> bool:
    """Whether a standing answer on a fenced ask is recorded as the chat owner's.

    It is when the box knows the owner, the ask carries the typed subject the
    engine records a rule for, and ``fence_reads`` says the fence proved where
    the ask goes. An ask the fence could not read is approved by a person every
    time it comes up, one it refuses is never put to anyone, and a fence whose
    judge raised proved nothing."""
    if not owner or not request.subject:
        return False
    try:
        return fence_reads(request)
    except Exception:
        return False


def clamp_always(option: PermissionOptionId, descriptor: ActionDescriptor) -> PermissionOptionId:
    """Downgrade a human ``allow_always`` to ``allow_once`` for a floor, an
    unclassifiable action, OR ANY shell command.

    opencode turns ``always`` into a COARSE prefix rule (``psql -c *``) and then
    stops emitting ``permission.asked`` for matching commands — which would skip
    OUR gate (and the floor) on a later ``psql -c "drop table t"`` or
    ``git push -f``. So we never let a shell command persist as opencode ``always``;
    instead OUR own ``(capability, operation)`` rule is persisted (precise — it
    matches ``git_push`` but not ``git_push_force``), keeping the floor re-gated."""
    if option != "allow_always":
        return option
    if (
        is_floor(descriptor)
        or descriptor.confidence == "unknown"
        or descriptor.capability == "shell"
    ):
        return "allow_once"
    return option


@dataclass(frozen=True, slots=True)
class DecisionEngine:
    """One constructed decision maker per session seam."""

    sink: DecisionRecorder
    permissions: PermissionsConfig | None = None
    broker: PermissionAsker | None = None
    judge: SafetyJudge | None = None
    intents: IntentLedger | None = None
    alkera_dir: Path | None = None
    """Enables always-allow/reject persistence to ``permissions.local.yml``."""
    workspace_root: str | None = None
    """Passed to the judge so it can tell an in-workspace write from an
    out-of-scope one. It never affects the floor."""
    source: str = "harness"
    session_id: str = ""
    shared_host: bool = False
    """The session runs on a box that serves more than one person (a cloud box).
    There only the box owner's policy grants an EXEC under ``bypass``, and the
    box's one local policy file holds the answers of every person whose chats
    run on it, so a standing answer is recorded as :attr:`owner`'s and decides
    only that person's chats. A shared-host session with no owner records and
    offers none, and a standing answer a client sends anyway binds the one call.
    The vendor is never told ``always`` on a shared host: it would learn a coarse
    rule of its own and stop raising the ask, which is where the fence judges."""
    owner: str = ""
    """The chat owner's user id: whose standing answers a shared-host session
    consults and records. Empty on a local machine (every rule there is the one
    owner's) and for a chat the box was not told the owner of."""

    @property
    def records_standing_answers(self) -> bool:
        """Whether an "Always allow" or "Always reject" given in this session is
        recorded: always on a local machine, and on a shared host only when the
        session knows whose answer it is."""
        return not self.shared_host or bool(self.owner)

    async def resolve(
        self,
        descriptor: ActionDescriptor,
        *,
        mode: str,
        task_goal: str = "",
        tool_call_id: str | None = None,
        request: PermissionRequest | None = None,
        evidence: GateEvidence | None = None,
    ) -> ActionResolution:
        """Decide one classified action, end to end. ``request`` (an existing
        ``PermissionRequest``) is reused for the human prompt when present;
        ``evidence`` carries the graph's verdict and the declaration filed with
        this call, absent on paths with no warehouse targets."""
        impact = evidence.impact if evidence is not None else None
        declared = evidence.declared if evidence is not None else None
        call = _Call(descriptor, mode, task_goal, tool_call_id, request, impact)
        policy = evaluate_action(
            descriptor,
            mode=mode,
            permissions=self.permissions_now(),
            shared_host=self.shared_host,
            owner=self.owner,
        )
        if policy.decided_by in ("read", "bypass"):
            return await self._settle(call, ActionResolution(True, "allow_once", policy.decided_by))

        # A mode- or rule-pinned reject is beyond any declaration's reach, so none
        # is weighed as covering there: the refusal stays a challenge whose text
        # says intent cannot help, instead of a silent reject labeled "intent".
        if policy.decision != AutoDecision.REJECT:
            call = replace(call, intent=self._covering_intent(impact, declared))
        outcome = gate_outcome(impact, covered=call.intent is not None)
        decision, decided_by = decide_gate(policy, outcome, mode=mode)
        if outcome is GateOutcome.CHALLENGE and decision == AutoDecision.REJECT:
            refusal = (
                refusal_reason(
                    impact, descriptor, offer_intent=policy.decision != AutoDecision.REJECT
                )
                if impact is not None
                else None
            )
            return await self._settle(
                call, ActionResolution(False, "reject_once", decided_by, refusal)
            )

        graded = PolicyResult(decision, decided_by, policy.reasons, policy.effective_effect)
        if self._grounds(call.mode, policy, outcome):
            return await self._judged(call)
        return await self._decided(call, graded)

    async def refuse(
        self,
        descriptor: ActionDescriptor,
        *,
        mode: str,
        decided_by: str,
        reason: str,
        tool_call_id: str | None = None,
    ) -> ActionResolution:
        """A refusal decided OUTSIDE the ladder — a bound like the cloud fence —
        recorded like every other decision, so a refused action is on the same
        record as an allowed one."""
        call = _Call(descriptor, mode, tool_call_id=tool_call_id)
        return await self._settle(call, ActionResolution(False, "reject_once", decided_by, reason))

    async def ask(
        self,
        descriptor: ActionDescriptor,
        *,
        mode: str,
        task_goal: str = "",
        tool_call_id: str | None = None,
    ) -> ActionResolution:
        """The human prompt, whatever the mode's policy would have decided: for an
        action a bound cannot vouch for, where an automatic allow is the one
        answer that is not on the table. No broker fails closed, like any ask.

        On a shared host such an ask offers and records no standing answer: the
        bound's contract is that a person approves it each time it comes up."""
        call = _Call(descriptor, mode, task_goal, tool_call_id, standing=not self.shared_host)
        return await self._asked(call)

    def permissions_now(self, *, standing: bool = True) -> Any:
        """The policy as its files now say it. A session hands the engine the
        policy it loaded at its start; a rule a person records during the
        session must reach the very next decision, so a loaded policy is asked
        for its current self. A policy with no files behind it is used as is.

        On a shared host it is scoped to what may decide this owner's chat
        (:meth:`PermissionsConfig.for_host`); ``standing=False`` leaves out the
        owner's own recorded answers too, for a path that only the box owner's
        committed policy may decide."""
        current = getattr(self.permissions, "current", None)
        policy = current() if callable(current) else self.permissions
        for_host = getattr(policy, "for_host", None)
        if not callable(for_host):
            return policy
        return for_host(shared_host=self.shared_host, owner=self.owner if standing else "")

    async def record_read(self, descriptor: ActionDescriptor, *, mode: str) -> None:
        """Log an ungated READ. A read needs no permission, but it IS warehouse data
        access, so the org audit gets the statement's targets under the same
        discipline as a decision: a failed append raises."""
        await self._record(_Call(descriptor, mode), "allow", "read")

    async def record_standing_allow(self, descriptor: ActionDescriptor, *, mode: str) -> None:
        """Log an allow a standing rule gave with no prompt where the fence could
        not prove the command's reach: a person's earlier answer for this exact
        text, on the record like the ask it stands in for."""
        await self._record(_Call(descriptor, mode), "allow", "rule")

    async def record_confined(self, descriptor: ActionDescriptor, *, mode: str) -> None:
        """Log an allow the fence gave a change it proved confined to the chat's
        own sandbox (a signal to a process in a sandbox whose process table is
        the chat's own): admitted in every stance like a write in the chat's
        folder, with no prompt, and on the record like one."""
        await self._record(_Call(descriptor, mode), "allow", "fence")

    async def record_run_allow(
        self, descriptor: ActionDescriptor, *, mode: str, reasons: list[str]
    ) -> None:
        """Log an allow a notebook run gave: the person ran it, or approved the
        agent's run, so its statements are not asked about one by one. On the
        record like an ask, with ``reasons`` naming the run and who asked for
        it; a failed append raises, so nothing runs unrecorded."""
        await self._record(_Call(descriptor, mode), "allow", "run", extra_reasons=reasons)

    # -- the ladder ---------------------------------------------------------

    def _covering_intent(
        self, impact: ImpactAssessment | None, declared: DeclaredIntent | None
    ) -> DeclaredIntent | None:
        """The declaration in force for this impact: the one filed with this call
        when it covers, else the ledger's most recent covering commitment."""
        if impact is None or not impact.has_impact:
            return None
        urns = impact.broken_urns()
        if declared is not None and covers(declared, urns):
            return declared
        if self.intents is None:
            return None
        return self.intents.covering(session_id=self.session_id, urns=urns)

    def _grounds(self, mode: str, policy: PolicyResult, outcome: GateOutcome) -> bool:
        """Whether the auto-mode judge rules on this. A challenged or escalated
        write never reaches it, since the impact verdict already answered the
        question. A COVERED one still does: an intent speaks to lineage damage, not
        to whether the write belongs in this workspace at all."""
        if outcome in (GateOutcome.CHALLENGE, GateOutcome.ESCALATE):
            return False
        return needs_auto_grounding(
            mode=mode, effect=policy.effective_effect, decided_by=policy.decided_by
        )

    async def _judged(self, call: _Call) -> ActionResolution:
        """The auto-mode write/egress middle. A judge that can't run (out of credit,
        down, broken) fails CLOSED and the caller stops the turn."""
        if self.judge is None:
            # Auto mode with no judge wired (an unauthenticated session) must never
            # allow the write middle ungrounded, so it prompts like default mode.
            graded = PolicyResult(AutoDecision.PROMPT, "mode", [], call.descriptor.effect)
            return await self._decided(call, graded)
        try:
            verdict = await self.judge.judge(
                call.descriptor, call.task_goal, workspace_root=self.workspace_root
            )
        except Exception:
            return await self._settle(
                call, ActionResolution(False, "reject_once", "judge", judge_unavailable=True)
            )
        if getattr(verdict, "decision", "block") == "block":
            reason = getattr(verdict, "reason", "") or None
            return await self._settle(
                call,
                ActionResolution(False, "reject_once", "judge", reason),
                extra_reasons=[reason] if reason else None,
            )
        return await self._settle(call, ActionResolution(True, "allow_once", "judge"))

    async def _decided(self, call: _Call, graded: PolicyResult) -> ActionResolution:
        """Allow, reject, or hand the decision to a human."""
        if graded.decision == AutoDecision.ALLOW:
            return await self._settle(call, ActionResolution(True, "allow_once", graded.decided_by))
        if graded.decision == AutoDecision.REJECT:
            reason = "; ".join(graded.reasons) or None
            return await self._settle(
                call, ActionResolution(False, "reject_once", graded.decided_by, reason)
            )
        return await self._asked(call)

    async def _asked(self, call: _Call) -> ActionResolution:
        """The human prompt. No broker means no approver, which fails closed.
        A supplied request predates the impact measurement, so the card is
        enriched with it here or the human decides blind."""
        if self.broker is None:
            return await self._settle(call, ActionResolution(False, "reject_once", "fail_closed"))
        req = call.request or self.build_request(
            call.descriptor,
            tool_call_id=call.tool_call_id,
            impact=call.impact,
            standing=call.standing,
        )
        if call.request is not None and call.impact is not None:
            req = req.model_copy(update={"impact": call.impact.model_dump(mode="json")})
        # A broker that can say WHO decided (the harness's ``PermissionBroker``)
        # is asked that way: a prompt that ran out, a resolver that failed, or a
        # resolver that refused on its own grounds is then on the record as
        # such, never as the person's choice. A bare asker answers as a person.
        decide = getattr(self.broker, "decide", None)
        try:
            if decide is None:
                raw_option = await self.broker.resolve(req)
                decided_by, reason = "human", None
            else:
                decided = await decide(req)
                raw_option, decided_by, reason = (
                    decided.option,
                    str(decided.decided_by),
                    decided.reason,
                )
        except AskReconsideredError:
            raise
        except Exception:
            return await self._settle(call, ActionResolution(False, "reject_once", "broker"))
        if decided_by != "human":
            option = cast(
                PermissionOptionId, raw_option if raw_option in _KNOWN_OPTIONS else "reject_once"
            )
            allowed = option in ("allow_once", "allow_always")
            return await self._settle(call, ActionResolution(allowed, option, decided_by, reason))
        if call.standing and self.records_standing_answers:
            await self._persist_rule(call.descriptor, raw_option, mode=call.mode)
        if self.shared_host:
            # Recorded (or not) above as this owner's answer; the vendor hears
            # only the decision on this call, so it keeps raising the ask.
            raw_option = bind_to_this_ask(raw_option)
        option = clamp_always(raw_option, call.descriptor)
        if option not in _KNOWN_OPTIONS:
            # An option id this engine does not know fails closed.
            option = "reject_once"
        allowed = option in ("allow_once", "allow_always")
        # A person's words travel with their no: the gate composes the refusal
        # from them, and a tool's error is where the model reads them.
        return await self._settle(
            call, ActionResolution(allowed, option, "human", None if allowed else reason)
        )

    async def _persist_rule(
        self, descriptor: ActionDescriptor, raw_option: Any, *, mode: str
    ) -> None:
        """Persist OUR precise rule on the human's ORIGINAL always-choice, BEFORE
        clamping opencode's coarse learning, so "always allow" is ergonomic yet an
        unknown or a floor action is never persisted as a family allow. How far the
        rule reaches is :func:`standing_allow_scope`'s answer: a destructive
        command records its exact text, never its verb.

        The rule is stamped with the stance it was answered in: an allow is an answer
        to the question THIS stance asked, and a stricter stance asks its own. On a
        shared host it is also stamped with the owner it was answered for, so it
        decides that person's chats and nobody else's."""
        if self.alkera_dir is None:
            return
        owner = self.owner if self.shared_host else None
        scope = standing_allow_scope(descriptor)
        if raw_option == "allow_always" and scope is not None:
            await asyncio.to_thread(
                add_local_rule,
                self.alkera_dir,
                descriptor,
                decision="allow",
                mode=mode,
                exact=scope == "exact",
                owner=owner,
            )
        elif raw_option == "reject_always":
            await asyncio.to_thread(
                add_local_rule,
                self.alkera_dir,
                descriptor,
                decision="deny",
                mode=mode,
                owner=owner,
            )

    def build_request(
        self,
        descriptor: ActionDescriptor,
        *,
        tool_call_id: str | None = None,
        impact: ImpactAssessment | None = None,
        preview: dict[str, Any] | None = None,
        standing: bool = True,
    ) -> PermissionRequest:
        """The permission card. ``impact`` rides beside the edit ``preview`` so a
        card can name the affected columns and the notes filed against them;
        ``preview`` is what the card renders of the content the action sends."""
        return PermissionRequest(
            event_id=secrets.token_hex(10),
            time=datetime.now(UTC),
            session_id=self.session_id,
            request_id=secrets.token_hex(10),
            tool_call_id=tool_call_id,
            permission_kind=descriptor.capability or "sql",
            # A shell command reads as the canonical "shell" kind (parity with the old
            # native bash) so its permission card + the portable policy lane treat it as
            # a shell action; the data tools (sql, ...) stay "other".
            canonical_kind="shell" if descriptor.capability == "shell" else "other",
            patterns=[descriptor.raw] if descriptor.raw else [descriptor.operation],
            subject=descriptor.model_dump(mode="json"),
            impact=impact.model_dump(mode="json") if impact is not None else None,
            preview=preview,
            options=self.offered_options(descriptor, standing=standing),
        )

    def offered_options(
        self, descriptor: ActionDescriptor | None, *, standing: bool = True
    ) -> list[PermissionOption]:
        """The decisions an ask on this session may offer: every one where a
        standing answer is recorded (the shell's standing allow named for what it
        records), none that would outlive the ask where nothing records one: a
        shared host that does not know whose answer it is, or an ask that does
        not admit one (``standing=False``)."""
        options = scoped_options(_PROMPT_OPTIONS, descriptor)
        if standing and self.records_standing_answers:
            return options
        return without_standing_grant(options)

    # -- the audited settle -------------------------------------------------

    async def _settle(
        self,
        call: _Call,
        outcome: ActionResolution,
        *,
        extra_reasons: list[str] | None = None,
    ) -> ActionResolution:
        """Record the decision, then return it carrying the declaration in force.
        An audit failure turns an allow into a refusal, because the write would
        otherwise run unrecorded."""
        try:
            await self._record(
                call,
                "allow" if outcome.allowed else "reject",
                outcome.decided_by,
                extra_reasons=extra_reasons,
            )
        except AuditUnavailableError:
            if outcome.allowed:
                return ActionResolution(False, "reject_once", "audit", reason=AUDIT_FAILED_REASON)
        return replace(outcome, weighed=call.intent)

    async def _record(
        self,
        call: _Call,
        decision: str,
        decided_by: str,
        *,
        extra_reasons: list[str] | None = None,
    ) -> None:
        record = DecisionRecord.from_descriptor(
            call.descriptor,
            decision=decision,
            decided_by=decided_by,
            mode=call.mode,
            source=self.source,
            session_id=self.session_id,
            tool_call_id=call.tool_call_id,
            extra_reasons=extra_reasons,
        ).with_impact(call.impact, intent_id=call.intent.intent_id if call.intent else "")
        await asyncio.to_thread(self.sink.record, record)


__all__ = [
    "AUDIT_FAILED_REASON",
    "STANDING_OPTIONS",
    "ActionResolution",
    "AskReconsideredError",
    "DecisionEngine",
    "DecisionRecorder",
    "GateEvidence",
    "PermissionAsker",
    "SafetyJudge",
    "as_answered_by",
    "bind_to_this_ask",
    "clamp_always",
    "standing_answer_recordable",
    "without_standing_grant",
]
