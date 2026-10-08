from enum import StrEnum


class AgentAuditEventInAction(StrEnum):
    AGENT_AUDIT_GAP = "agent.audit_gap"
    AGENT_COST = "agent.cost"
    AGENT_DATA_ACCESS = "agent.data_access"
    AGENT_DECISION_DENIED = "agent.decision_denied"
    AGENT_DECISION_ESCALATED = "agent.decision_escalated"
    AGENT_SESSION_FINISHED = "agent.session_finished"
    AGENT_SESSION_STARTED = "agent.session_started"

    def __str__(self) -> str:
        return str(self.value)
