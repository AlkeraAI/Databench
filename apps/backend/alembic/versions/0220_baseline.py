"""The open chain's baseline: every open table with its indexes, constraints, functions, triggers,
row-security policies, grants and seed rows.

Generated from the schema the earlier revisions built, up to 0220. Do not
edit it; a change is a new revision. The statements are in ``0220_baseline_open.sql``
beside this file.
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision: str = "0220"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None
SIDE: str = "open"

_SQL = Path(__file__).with_name("0220_baseline_open.sql")
_DROPS = """
DROP TABLE IF EXISTS public.ws_ticket_uses CASCADE;
DROP TABLE IF EXISTS public.workspace_objects CASCADE;
DROP TABLE IF EXISTS public.workspace_machine_moves CASCADE;
DROP TABLE IF EXISTS public.users CASCADE;
DROP TABLE IF EXISTS public.user_storage_limits CASCADE;
DROP TABLE IF EXISTS public.user_preferences CASCADE;
DROP TABLE IF EXISTS public.user_org_preferences CASCADE;
DROP TABLE IF EXISTS public.user_oauth_tokens CASCADE;
DROP TABLE IF EXISTS public.user_bans CASCADE;
DROP TABLE IF EXISTS public.teams CASCADE;
DROP TABLE IF EXISTS public.team_memberships CASCADE;
DROP TABLE IF EXISTS public.team_connections CASCADE;
DROP TABLE IF EXISTS public.team_allocations CASCADE;
DROP TABLE IF EXISTS public.storage_usage_snapshots CASCADE;
DROP TABLE IF EXISTS public.sso_link_requests CASCADE;
DROP TABLE IF EXISTS public.sso_domain_claims CASCADE;
DROP TABLE IF EXISTS public.sso_connections CASCADE;
DROP TABLE IF EXISTS public.saml_replay_assertions CASCADE;
DROP TABLE IF EXISTS public.role_assignments CASCADE;
DROP TABLE IF EXISTS public.realtime_presence CASCADE;
DROP TABLE IF EXISTS public.realtime_docs CASCADE;
DROP TABLE IF EXISTS public.rate_limit_windows CASCADE;
DROP TABLE IF EXISTS public.proxy_tokens CASCADE;
DROP TABLE IF EXISTS public.personal_access_tokens CASCADE;
DROP TABLE IF EXISTS public.org_sync_settings CASCADE;
DROP TABLE IF EXISTS public.org_storage_limits CASCADE;
DROP TABLE IF EXISTS public.org_settings CASCADE;
DROP TABLE IF EXISTS public.org_memberships CASCADE;
DROP TABLE IF EXISTS public.org_machines CASCADE;
DROP TABLE IF EXISTS public.org_machine_audiences CASCADE;
DROP TABLE IF EXISTS public.org_compute_settings CASCADE;
DROP TABLE IF EXISTS public.org_compute_assignments CASCADE;
DROP TABLE IF EXISTS public.org_audit_events CASCADE;
DROP TABLE IF EXISTS public.object_payload_rows CASCADE;
DROP TABLE IF EXISTS public.oauth_identities CASCADE;
DROP TABLE IF EXISTS public.notebook_runs CASCADE;
DROP TABLE IF EXISTS public.notebook_peers CASCADE;
DROP TABLE IF EXISTS public.notebook_kernels CASCADE;
DROP TABLE IF EXISTS public.notebook_epoch_tails CASCADE;
DROP TABLE IF EXISTS public.notebook_edits CASCADE;
DROP TABLE IF EXISTS public.model_provider_configs CASCADE;
DROP TABLE IF EXISTS public.manifest_terms CASCADE;
DROP TABLE IF EXISTS public.machine_credentials CASCADE;
DROP TABLE IF EXISTS public.login_lockouts CASCADE;
DROP TABLE IF EXISTS public.invitations CASCADE;
DROP TABLE IF EXISTS public.identity_security_events CASCADE;
DROP TABLE IF EXISTS public.identity_org_creations CASCADE;
DROP TABLE IF EXISTS public.file_versions CASCADE;
DROP TABLE IF EXISTS public.file_upload_sessions CASCADE;
DROP TABLE IF EXISTS public.file_upload_parts CASCADE;
DROP TABLE IF EXISTS public.file_trash_ops CASCADE;
DROP TABLE IF EXISTS public.file_sweep_shards CASCADE;
DROP TABLE IF EXISTS public.file_stores CASCADE;
DROP TABLE IF EXISTS public.file_stars CASCADE;
DROP TABLE IF EXISTS public.file_stage_jobs CASCADE;
DROP TABLE IF EXISTS public.file_shares CASCADE;
DROP TABLE IF EXISTS public.file_retention_labels CASCADE;
DROP TABLE IF EXISTS public.file_quarantine CASCADE;
DROP TABLE IF EXISTS public.file_platform CASCADE;
DROP TABLE IF EXISTS public.file_page_grants CASCADE;
DROP TABLE IF EXISTS public.file_packs CASCADE;
DROP TABLE IF EXISTS public.file_ops CASCADE;
DROP TABLE IF EXISTS public.file_nodes CASCADE;
DROP TABLE IF EXISTS public.file_manifests CASCADE;
DROP TABLE IF EXISTS public.file_locks CASCADE;
DROP TABLE IF EXISTS public.file_links CASCADE;
DROP TABLE IF EXISTS public.file_leases CASCADE;
DROP TABLE IF EXISTS public.file_lease_live_entries CASCADE;
DROP TABLE IF EXISTS public.file_lease_epoch_hwm CASCADE;
DROP TABLE IF EXISTS public.file_key_chunks CASCADE;
DROP TABLE IF EXISTS public.file_idempotency_keys CASCADE;
DROP TABLE IF EXISTS public.file_holds CASCADE;
DROP TABLE IF EXISTS public.file_history CASCADE;
DROP TABLE IF EXISTS public.file_erasure_log CASCADE;
DROP TABLE IF EXISTS public.file_drives CASCADE;
DROP TABLE IF EXISTS public.file_dir_stats_deltas CASCADE;
DROP TABLE IF EXISTS public.file_dir_stats CASCADE;
DROP TABLE IF EXISTS public.file_content_grants CASCADE;
DROP TABLE IF EXISTS public.file_conflicts CASCADE;
DROP TABLE IF EXISTS public.file_acls CASCADE;
DROP TABLE IF EXISTS public.file_acl_members CASCADE;
DROP TABLE IF EXISTS public.event_outbox CASCADE;
DROP TABLE IF EXISTS public.entitlement_grants CASCADE;
DROP TABLE IF EXISTS public.email_domain_bans CASCADE;
DROP TABLE IF EXISTS public.device_authorizations CASCADE;
DROP TABLE IF EXISTS public.deployment_health_runs CASCADE;
DROP TABLE IF EXISTS public.deployment_health_checks CASCADE;
DROP TABLE IF EXISTS public.dedup_domains CASCADE;
DROP TABLE IF EXISTS public.crdt_updates CASCADE;
DROP TABLE IF EXISTS public.crdt_peers CASCADE;
DROP TABLE IF EXISTS public.crdt_docs CASCADE;
DROP TABLE IF EXISTS public.crash_reports CASCADE;
DROP TABLE IF EXISTS public.connection_verifications CASCADE;
DROP TABLE IF EXISTS public.connection_inventory CASCADE;
DROP TABLE IF EXISTS public.compute_offerings CASCADE;
DROP TABLE IF EXISTS public.compute_offering_orgs CASCADE;
DROP TABLE IF EXISTS public.compute_machine_types CASCADE;
DROP TABLE IF EXISTS public.compute_grants CASCADE;
DROP TABLE IF EXISTS public.compute_allocations CASCADE;
DROP TABLE IF EXISTS public.compute_allocation_events CASCADE;
DROP TABLE IF EXISTS public.ci_tokens CASCADE;
DROP TABLE IF EXISTS public.chat_workspace_states CASCADE;
DROP TABLE IF EXISTS public.chat_read_marks CASCADE;
DROP TABLE IF EXISTS public.chat_messages CASCADE;
DROP TABLE IF EXISTS public.chat_attachments CASCADE;
DROP TABLE IF EXISTS public.billing_signup_clicks CASCADE;
DROP TABLE IF EXISTS public.billing_models CASCADE;
DROP TABLE IF EXISTS public.billing_model_routes CASCADE;
DROP TABLE IF EXISTS public.auth_tokens CASCADE;
DROP TABLE IF EXISTS public.auth_session_org_grants CASCADE;
DROP TABLE IF EXISTS public.auth_refresh_tokens CASCADE;
DROP TABLE IF EXISTS public.audit_logs CASCADE;
DROP TABLE IF EXISTS public.account_export_requests CASCADE;
DROP TABLE IF EXISTS public.account_deletion_requests CASCADE;
DROP FUNCTION IF EXISTS public.alkera_org_ids() CASCADE;
DROP FUNCTION IF EXISTS public.chat_attachments_adopt(uuid, jsonb, jsonb) CASCADE;
DROP FUNCTION IF EXISTS public.chat_attachments_mirror_legacy_array() CASCADE;
DROP FUNCTION IF EXISTS public.event_outbox_append_only() CASCADE;
DROP FUNCTION IF EXISTS public.event_outbox_notify() CASCADE;
DROP FUNCTION IF EXISTS public.files_share_principal_in_org() CASCADE;
DROP FUNCTION IF EXISTS public.fn_compute_allocations_tenant_org_immutable() CASCADE;
DROP FUNCTION IF EXISTS public.fn_connection_inventory_org() CASCADE;
DROP FUNCTION IF EXISTS public.fn_object_payload_rows_org() CASCADE;
DROP FUNCTION IF EXISTS public.fn_org_machine_audiences_same_org() CASCADE;
DROP FUNCTION IF EXISTS public.fn_team_connections_org() CASCADE;
DROP FUNCTION IF EXISTS public.fn_team_memberships_org() CASCADE;
DROP FUNCTION IF EXISTS public.fn_user_oauth_tokens_org() CASCADE;
DROP FUNCTION IF EXISTS public.fn_users_home_membership() CASCADE;
DROP FUNCTION IF EXISTS public.fn_users_home_org_immutable() CASCADE;
DROP FUNCTION IF EXISTS public.fn_workspace_machine_moves_same_org() CASCADE;
DROP FUNCTION IF EXISTS public.team_root_of(uuid) CASCADE;
"""


def _run(statements: str) -> None:
    # The driver runs the script as it is, in Alembic's transaction: no
    # bind-parameter parsing of the ``%`` and ``:`` a function body holds.
    op.get_bind().connection.driver_connection.execute(statements)


def upgrade() -> None:
    _run(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    # Roles are cluster-wide and other databases may use them; they stay.
    _run(_DROPS)
