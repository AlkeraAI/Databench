from enum import StrEnum


class RealtimeEventType(StrEnum):
    ARTIFACT_UPDATED = "artifact.updated"
    BILLING_SUMMARY_CHANGED = "billing.summary_changed"
    CHAT_UPDATED = "chat.updated"
    COMPUTE_MACHINE_CHANGED = "compute_machine.changed"
    CONNECTION_CREDENTIAL_CHANGED = "connection.credential_changed"
    CONNECTION_VERIFICATION_CHANGED = "connection.verification_changed"
    FILE_LEASE_CHANGED = "file_lease.changed"
    FILE_NODE_CHANGED = "file_node.changed"
    FILE_OPERATION_CHANGED = "file_operation.changed"
    GATE_LEASE_CHANGED = "gate_lease.changed"
    GATE_RUN_INGESTED = "gate_run.ingested"
    INVITATION_CHANGED = "invitation.changed"
    KB_ITEM_CHANGED = "kb_item.changed"
    MEMBERSHIP_CHANGED = "membership.changed"
    ORG_BILLING_CHANGED = "org_billing.changed"
    ORG_MACHINE_CHANGED = "org_machine.changed"
    TEAM_CONNECTION_PROBED = "team_connection.probed"
    TEAM_CONNECTION_UPDATED = "team_connection.updated"
    USER_EMAIL_VERIFIED = "user.email_verified"
    WORKSPACE_MACHINE_MOVE = "workspace.machine_move"
    WORKSPACE_OBJECT_CHANGED = "workspace_object.changed"

    def __str__(self) -> str:
        return str(self.value)
