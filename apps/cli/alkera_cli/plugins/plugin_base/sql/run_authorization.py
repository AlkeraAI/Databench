"""The authorization a notebook run carries for the statements it executes.

A notebook cell runs only when a person runs it, or approves the agent's run
(or the agent's session runs without asking, which a person chose). That run
IS the approval for every statement its cells execute, so a write on its way
to a connection is not asked about a second time. It is still recorded: the
decision names the run, the notebook and who asked for it.

Only code builds one, from the run the notebook engine says it is executing.
No tool input carries it, so a model cannot claim a run it is not in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: How the run was approved. ``person_ran``: a person ran the cells.
#: ``agent_run_approved``: an agent ran them, and its run cleared the agent
#: session's own permission gate (a person approved it, or the session's mode
#: runs without asking).
RunApproval = Literal["person_ran", "agent_run_approved"]

_APPROVAL_OF: dict[str, RunApproval] = {"person": "person_ran", "agent": "agent_run_approved"}


@dataclass(frozen=True, slots=True)
class RunAuthorization:
    """One notebook run's standing to execute its statements."""

    run_id: str
    requester_kind: Literal["person", "agent"]
    requester_id: str
    notebook: str
    approval: RunApproval
    acting_for: str = ""
    """The person an agent's run acts for, when it acts for one."""

    def __post_init__(self) -> None:
        if not self.run_id or not self.requester_id:
            raise ValueError("a run authorization names its run and who asked for it")
        if _APPROVAL_OF.get(self.requester_kind) != self.approval:
            raise ValueError(f"a {self.requester_kind}'s run is not approved as {self.approval}")

    @classmethod
    def for_run(
        cls,
        *,
        run_id: str,
        requester_kind: str,
        requester_id: str,
        notebook: str,
        acting_for: str = "",
    ) -> RunAuthorization | None:
        """The authorization of the run the engine is executing, or ``None``
        when there is no run or no requester, or the requester is not a
        person or an agent (the system runs nothing of its own). Without
        one, statements keep the ordinary gate."""
        if not run_id or not requester_id:
            return None
        if requester_kind == "person":
            return cls(run_id, "person", requester_id, notebook, "person_ran")
        if requester_kind == "agent":
            return cls(run_id, "agent", requester_id, notebook, "agent_run_approved", acting_for)
        return None

    def audit_reasons(self) -> list[str]:
        """What the decision record says about the run."""
        reasons = [
            f"notebook run {self.run_id}",
            f"notebook {self.notebook or '(unsaved)'}",
            f"requested by {self.requester_kind} {self.requester_id}",
            f"approval {self.approval}",
        ]
        if self.acting_for:
            reasons.append(f"acting for person {self.acting_for}")
        return reasons


__all__ = ["RunApproval", "RunAuthorization"]
