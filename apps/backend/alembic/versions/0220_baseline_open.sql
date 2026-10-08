-- The open baseline at 0220: generated from the schema the earlier
-- revisions built. Do not edit; a change is a new revision.

SET LOCAL check_function_bodies = false;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'alkera_files_app') THEN
        BEGIN
            CREATE ROLE alkera_files_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END;
    END IF;
END
$$;

GRANT alkera_files_app TO CURRENT_USER;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'alkera_tenant_app') THEN
        BEGIN
            CREATE ROLE alkera_tenant_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END;
    END IF;
END
$$;

GRANT alkera_tenant_app TO CURRENT_USER;

CREATE EXTENSION IF NOT EXISTS ltree WITH SCHEMA public;

COMMENT ON EXTENSION ltree IS 'data type for hierarchical tree-like structures';

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';

CREATE FUNCTION public.alkera_org_ids() RETURNS uuid[]
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$
    SELECT coalesce(
        string_to_array(nullif(current_setting('alkera.org_ids', true), ''), ',')::uuid[],
        '{}'::uuid[]
    )
$$;

CREATE FUNCTION public.chat_attachments_adopt(chat uuid, before jsonb, after jsonb) RETURNS void
    LANGUAGE plpgsql
    AS $_$
BEGIN
    DELETE FROM chat_attachments
     WHERE chat_id = chat
       AND node_id IN (
           SELECT gone.value::uuid
             FROM jsonb_array_elements_text(before) AS gone(value)
            WHERE gone.value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND NOT after ? gone.value
       );
    INSERT INTO chat_attachments (chat_id, node_id, position)
    SELECT chat,
           added.value::uuid,
           (SELECT COALESCE(MAX(position), 0) FROM chat_attachments WHERE chat_id = chat)
               + row_number() OVER (ORDER BY added.first_seen)
      FROM (
           SELECT link.value, MIN(link.ordinality) AS first_seen
             FROM jsonb_array_elements_text(after) WITH ORDINALITY AS link(value, ordinality)
            WHERE link.value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AND NOT before ? link.value
              AND NOT EXISTS (SELECT 1 FROM chat_attachments
                               WHERE chat_id = chat AND node_id = link.value::uuid)
            GROUP BY link.value
      ) AS added
    ON CONFLICT DO NOTHING;
END
$_$;

CREATE FUNCTION public.chat_attachments_mirror_legacy_array() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    before jsonb := '[]'::jsonb;
    after jsonb := '[]'::jsonb;
BEGIN
    -- A write that carries no array says nothing about the links: a spec that
    -- dropped the deprecated key, or one that holds something else under it,
    -- is not "every link removed". Only an array is a statement.
    IF jsonb_typeof(NEW.spec -> 'attachments') IS DISTINCT FROM 'array' THEN
        RETURN NULL;
    END IF;
    after := NEW.spec -> 'attachments';
    IF TG_OP = 'UPDATE' AND jsonb_typeof(OLD.spec -> 'attachments') = 'array' THEN
        before := OLD.spec -> 'attachments';
    END IF;
    IF before IS DISTINCT FROM after THEN
        PERFORM chat_attachments_adopt(NEW.id, before, after);
    END IF;
    RETURN NULL;
END
$$;

CREATE FUNCTION public.event_outbox_append_only() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND coalesce(current_setting('alkera.outbox_erasure', true), '') = 'on'
     AND (to_jsonb(NEW) - 'actor' - 'payload') = (to_jsonb(OLD) - 'actor' - 'payload') THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION USING
    MESSAGE = 'event_outbox is append-only: ' || TG_OP || ' refused',
    ERRCODE = 'restrict_violation';
END
$$;

CREATE FUNCTION public.event_outbox_notify() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  PERFORM pg_notify('alkera_events', NEW.id::text);
  RETURN NEW;
END
$$;

CREATE FUNCTION public.files_share_principal_in_org() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    walker uuid;
    guard int := 0;
BEGIN
    IF NEW.principal_kind = 'org' THEN
        IF NEW.principal_id = NEW.org_team_id THEN
            RETURN NEW;
        END IF;
    ELSIF NEW.principal_kind = 'team' THEN
        walker := NEW.principal_id;
        WHILE walker IS NOT NULL AND guard < 64 LOOP
            IF walker = NEW.org_team_id THEN
                RETURN NEW;
            END IF;
            SELECT parent_team_id INTO walker FROM teams WHERE id = walker;
            guard := guard + 1;
        END LOOP;
    ELSIF NEW.principal_kind = 'user' THEN
        IF EXISTS (
            SELECT 1 FROM team_memberships
            WHERE user_id = NEW.principal_id AND team_id = NEW.org_team_id
        ) THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'files.share_principal_not_in_org'
        USING ERRCODE = 'check_violation';
END;
$$;

CREATE FUNCTION public.fn_compute_allocations_tenant_org_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF OLD.tenant_org_id IS NOT NULL
       AND NEW.tenant_org_id IS DISTINCT FROM OLD.tenant_org_id THEN
        RAISE EXCEPTION 'compute_allocations.tenant_org_id is immutable (allocation %)', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION public.fn_connection_inventory_org() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.org_team_id IS NULL THEN
        SELECT org_team_id INTO NEW.org_team_id FROM users WHERE id = NEW.user_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION public.fn_object_payload_rows_org() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    owner_org uuid;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.org_team_id IS NOT NULL
       AND NEW.org_team_id IS DISTINCT FROM OLD.org_team_id THEN
        RAISE EXCEPTION 'object_payload_rows.org_team_id is immutable (object %)', OLD.object_id
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT org_team_id INTO owner_org FROM workspace_objects WHERE id = NEW.object_id;
    IF owner_org IS NULL THEN
        RAISE EXCEPTION 'object % has no org', NEW.object_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF NEW.org_team_id IS NULL THEN
        NEW.org_team_id := owner_org;
    ELSIF NEW.org_team_id <> owner_org THEN
        RAISE EXCEPTION 'payload org % is not the org % of object %',
            NEW.org_team_id, owner_org, NEW.object_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION public.fn_org_machine_audiences_same_org() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.team_id IS NOT NULL
       AND team_root_of(NEW.team_id) IS DISTINCT FROM NEW.org_team_id THEN
        RAISE EXCEPTION 'team % is not in org %', NEW.team_id, NEW.org_team_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION public.fn_team_connections_org() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    root uuid := team_root_of(NEW.team_id);
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.org_team_id IS NOT NULL
       AND NEW.org_team_id IS DISTINCT FROM OLD.org_team_id THEN
        RAISE EXCEPTION 'team_connections.org_team_id is immutable (connection %)', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    IF root IS NULL THEN
        RAISE EXCEPTION 'team % has no root org', NEW.team_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF NEW.org_team_id IS NULL THEN
        NEW.org_team_id := root;
    ELSIF NEW.org_team_id <> root THEN
        RAISE EXCEPTION 'connection org % is not the root % of team %',
            NEW.org_team_id, root, NEW.team_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION public.fn_team_memberships_org() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    root uuid := team_root_of(NEW.team_id);
BEGIN
    IF root IS NULL THEN
        RAISE EXCEPTION 'team % has no root org', NEW.team_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF NEW.org_team_id IS NULL THEN
        NEW.org_team_id := root;
    ELSIF NEW.org_team_id <> root THEN
        RAISE EXCEPTION 'team membership org % is not the root % of team %',
            NEW.org_team_id, root, NEW.team_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION public.fn_user_oauth_tokens_org() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    owner_org uuid;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.org_team_id IS NOT NULL
       AND NEW.org_team_id IS DISTINCT FROM OLD.org_team_id THEN
        RAISE EXCEPTION 'user_oauth_tokens.org_team_id is immutable (grant %)', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    -- A connection the back-fill has not reached yet is named by its team's
    -- root, the value the back-fill is about to give it.
    SELECT coalesce(org_team_id, team_root_of(team_id)) INTO owner_org
    FROM team_connections WHERE id = NEW.team_connection_id;
    IF owner_org IS NULL THEN
        RAISE EXCEPTION 'team connection % has no org', NEW.team_connection_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF NEW.org_team_id IS NULL THEN
        NEW.org_team_id := owner_org;
    ELSIF NEW.org_team_id <> owner_org THEN
        RAISE EXCEPTION 'grant org % is not the org % of connection %',
            NEW.org_team_id, owner_org, NEW.team_connection_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION public.fn_users_home_membership() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    INSERT INTO org_memberships
        (id, user_id, org_team_id, status, sso_exempt, scim_external_id, deactivated_at)
    VALUES (
        gen_random_uuid(),
        NEW.id,
        NEW.org_team_id,
        CASE WHEN NEW.is_active THEN 'active' ELSE 'deactivated' END,
        NEW.sso_exempt,
        NEW.scim_external_id,
        CASE WHEN NEW.is_active THEN NULL ELSE now() END
    )
    ON CONFLICT (user_id, org_team_id) DO NOTHING;
    RETURN NULL;
END
$$;

CREATE FUNCTION public.fn_users_home_org_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.org_team_id IS DISTINCT FROM OLD.org_team_id
       AND coalesce(current_setting('alkera.allow_home_repoint', true), '') <> 'on' THEN
        RAISE EXCEPTION 'users.org_team_id is immutable (user %)', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION public.fn_workspace_machine_moves_same_org() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    owner_org uuid;
BEGIN
    SELECT org_team_id INTO owner_org FROM workspace_objects WHERE id = NEW.workspace_id;
    IF owner_org IS NULL THEN
        RAISE EXCEPTION 'workspace % not found', NEW.workspace_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF owner_org <> NEW.org_team_id THEN
        RAISE EXCEPTION 'workspace % is not in org %', NEW.workspace_id, NEW.org_team_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION public.team_root_of(team uuid) RETURNS uuid
    LANGUAGE sql STABLE
    AS $$
    WITH RECURSIVE chain(id, parent_team_id, depth) AS (
        SELECT id, parent_team_id, 0 FROM teams WHERE id = team
        UNION ALL
        SELECT t.id, t.parent_team_id, c.depth + 1
        FROM teams t JOIN chain c ON t.id = c.parent_team_id
        WHERE c.depth < 64
    )
    SELECT id FROM chain WHERE parent_team_id IS NULL LIMIT 1
$$;

CREATE TABLE public.account_deletion_requests (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    requested_by_id uuid,
    source character varying(16) DEFAULT 'self'::character varying NOT NULL,
    status character varying(16) DEFAULT 'scheduled'::character varying NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    purge_after timestamp with time zone NOT NULL,
    cancelled_at timestamp with time zone,
    completed_at timestamp with time zone,
    blocked_reason character varying(64),
    blocked_at timestamp with time zone,
    blocked_notified_at timestamp with time zone,
    plan jsonb NOT NULL,
    certificate jsonb,
    CONSTRAINT ck_account_deletion_requests_source CHECK (((source)::text = ANY ((ARRAY['self'::character varying, 'support'::character varying, 'restore'::character varying])::text[]))),
    CONSTRAINT ck_account_deletion_requests_status CHECK (((status)::text = ANY ((ARRAY['scheduled'::character varying, 'cancelled'::character varying, 'completed'::character varying])::text[])))
);

CREATE TABLE public.account_export_requests (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    requested_by_id uuid,
    source character varying(16) DEFAULT 'self'::character varying NOT NULL,
    status character varying(16) DEFAULT 'queued'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    expires_at timestamp with time zone,
    attempts integer DEFAULT 0 NOT NULL,
    archive_key character varying(512),
    archive_bytes bigint,
    download_token_hash character varying(96),
    error text,
    CONSTRAINT ck_account_export_requests_source CHECK (((source)::text = ANY ((ARRAY['self'::character varying, 'support'::character varying, 'restore'::character varying])::text[]))),
    CONSTRAINT ck_account_export_requests_status CHECK (((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'ready'::character varying, 'failed'::character varying, 'expired'::character varying])::text[])))
);

CREATE TABLE public.audit_logs (
    id uuid NOT NULL,
    actor_id uuid,
    actor_email character varying(320) DEFAULT ''::character varying NOT NULL,
    actor_platform_role character varying(32),
    action character varying(128) NOT NULL,
    method character varying(8) NOT NULL,
    path character varying(512) NOT NULL,
    status_code integer NOT NULL,
    detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    target character varying(512)
);

CREATE TABLE public.auth_refresh_tokens (
    id uuid NOT NULL,
    family_id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(96) NOT NULL,
    access_jti character varying(32),
    family_started_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone,
    idle_expires_at timestamp with time zone NOT NULL,
    absolute_expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    revoked_reason character varying(32),
    user_agent character varying(255),
    ip_prefix character varying(64),
    inserted_at timestamp with time zone DEFAULT now() NOT NULL,
    active_org_team_id uuid,
    successor_id uuid,
    successor_sealed character varying(160)
);

CREATE TABLE public.auth_session_org_grants (
    id uuid NOT NULL,
    family_id uuid NOT NULL,
    org_team_id uuid,
    method character varying(16) NOT NULL,
    authenticated_at timestamp with time zone NOT NULL
);

CREATE TABLE public.auth_tokens (
    id uuid NOT NULL,
    jti character varying(32) NOT NULL,
    user_id uuid NOT NULL,
    token_type character varying(16) NOT NULL,
    issued_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    last_used_at timestamp with time zone,
    label character varying(255),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    org_team_id uuid,
    membership_id uuid,
    refresh_family_id uuid
);

CREATE TABLE public.billing_model_routes (
    id uuid NOT NULL,
    model_id character varying(128) NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    provider character varying(32) NOT NULL,
    region character varying(64),
    upstream_model_id character varying(255) NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.billing_models (
    id character varying(128) NOT NULL,
    display_name character varying(255) NOT NULL,
    family character varying(64) NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    context_window integer DEFAULT 0 NOT NULL,
    supports_thinking boolean DEFAULT false NOT NULL,
    supports_caching boolean DEFAULT false NOT NULL,
    default_credit_class character varying(32) DEFAULT 'free_monthly'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    reasoning_efforts character varying(16)[] DEFAULT '{}'::character varying[] NOT NULL,
    default_effort character varying(16),
    thinking_mode character varying(16),
    tier character varying(32) DEFAULT 'standard'::character varying NOT NULL,
    flags character varying(32)[] DEFAULT '{}'::character varying[] NOT NULL,
    max_output_tokens integer DEFAULT 0 NOT NULL,
    cache_min_tokens integer,
    reasoning_format character varying(128),
    reads_reasoning_formats character varying(128)[] DEFAULT '{}'::character varying[] NOT NULL
);

CREATE TABLE public.billing_signup_clicks (
    id uuid NOT NULL,
    visit_token character varying(64) NOT NULL,
    tier character varying(32),
    "interval" character varying(32),
    ref character varying(128),
    utm_source character varying(128),
    utm_medium character varying(128),
    utm_campaign character varying(128),
    utm_term character varying(128),
    utm_content character varying(128),
    referer_host character varying(255),
    user_id uuid,
    stripe_customer_id character varying(255),
    stripe_subscription_id character varying(255),
    converted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.chat_attachments (
    chat_id uuid NOT NULL,
    node_id uuid NOT NULL,
    "position" bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.chat_messages (
    id uuid NOT NULL,
    chat_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    seq integer NOT NULL,
    role character varying(16) NOT NULL,
    kind character varying(64) DEFAULT ''::character varying NOT NULL,
    event_id character varying(255) NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_chat_messages_payload_size CHECK ((octet_length((payload)::text) <= 2097152)),
    CONSTRAINT ck_chat_messages_role CHECK (((role)::text = ANY ((ARRAY['user'::character varying, 'assistant'::character varying, 'tool'::character varying, 'system'::character varying])::text[]))),
    CONSTRAINT ck_chat_messages_seq_positive CHECK ((seq >= 1))
);

ALTER TABLE ONLY public.chat_messages FORCE ROW LEVEL SECURITY;

CREATE TABLE public.chat_read_marks (
    chat_id uuid NOT NULL,
    user_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    last_read_seq integer DEFAULT 0 NOT NULL,
    marked_unread boolean DEFAULT false NOT NULL,
    read_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_chat_read_marks_seq_nonnegative CHECK ((last_read_seq >= 0))
);

CREATE TABLE public.chat_workspace_states (
    chat_id uuid NOT NULL,
    user_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.ci_tokens (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    label character varying(255),
    repo character varying(512),
    created_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone
);

CREATE TABLE public.compute_allocation_events (
    id uuid NOT NULL,
    allocation_id uuid NOT NULL,
    at timestamp with time zone NOT NULL,
    from_state character varying(16) NOT NULL,
    to_state character varying(16) NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    actor jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE TABLE public.compute_allocations (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    machine_type_id uuid NOT NULL,
    lifecycle character varying(16) DEFAULT 'session'::character varying NOT NULL,
    name character varying(128) DEFAULT ''::character varying NOT NULL,
    state character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    provider_machine_id character varying(128) DEFAULT ''::character varying NOT NULL,
    public_ip character varying(64) DEFAULT ''::character varying NOT NULL,
    ssh_port integer DEFAULT 0 NOT NULL,
    ssh_user character varying(32) DEFAULT 'root'::character varying NOT NULL,
    ssh_public_key text DEFAULT ''::text NOT NULL,
    ssh_private_key_enc text DEFAULT ''::text NOT NULL,
    project_path character varying(1024) DEFAULT ''::character varying NOT NULL,
    session_id character varying(128) DEFAULT ''::character varying NOT NULL,
    error character varying(1024) DEFAULT ''::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    ready_at timestamp with time zone,
    released_at timestamp with time zone,
    grant_id uuid,
    price_per_minute_nanos bigint DEFAULT '0'::bigint NOT NULL,
    true_cost_per_minute_nanos bigint DEFAULT '0'::bigint NOT NULL,
    last_metered_at timestamp with time zone,
    minutes_billed integer DEFAULT 0 NOT NULL,
    billed_nanos bigint DEFAULT '0'::bigint NOT NULL,
    true_cost_nanos bigint DEFAULT '0'::bigint NOT NULL,
    terminated_reason character varying(32) DEFAULT ''::character varying NOT NULL,
    low_credit boolean DEFAULT false NOT NULL,
    max_lease_minutes integer,
    last_heartbeat_at timestamp with time zone,
    last_reported_status character varying(16) DEFAULT ''::character varying NOT NULL,
    origin character varying(16) DEFAULT 'provisioned'::character varying NOT NULL,
    registered_jti character varying(64) DEFAULT ''::character varying NOT NULL,
    tenancy character varying(16) DEFAULT 'org'::character varying NOT NULL,
    capacity integer DEFAULT 6 NOT NULL,
    chats_served integer DEFAULT 0 NOT NULL,
    daemon_version character varying(32) DEFAULT ''::character varying NOT NULL,
    daemon_instance_id character varying(64),
    drain_kind character varying(16),
    storage_gb integer DEFAULT 0 NOT NULL,
    state_changed_at timestamp with time zone,
    drain_requested_at timestamp with time zone,
    drain_reason character varying(512) DEFAULT ''::character varying NOT NULL,
    auto_terminate boolean DEFAULT false NOT NULL,
    resources_json jsonb,
    sandbox character varying(16) DEFAULT 'none'::character varying NOT NULL,
    node_token_jti character varying(32),
    tenant_org_id uuid,
    wake_requested_at timestamp with time zone,
    wake_request_json jsonb,
    capabilities_json jsonb,
    ran_org_workers_at timestamp with time zone,
    org_machine_id uuid,
    storage_price_per_minute_nanos bigint DEFAULT '0'::bigint NOT NULL,
    true_storage_cost_per_minute_nanos bigint DEFAULT '0'::bigint NOT NULL,
    storage_metered_at timestamp with time zone,
    drain_deadline_at timestamp with time zone,
    provision_attempts integer DEFAULT 0 NOT NULL,
    last_activity_at timestamp with time zone,
    failure_kind character varying(16) DEFAULT ''::character varying NOT NULL,
    capacity_gave_up_at timestamp with time zone,
    isolation_json jsonb,
    fault_code character varying(32),
    fault_summary character varying(300) DEFAULT ''::character varying NOT NULL,
    fault_since timestamp with time zone,
    fault_until timestamp with time zone,
    billed_through_at timestamp with time zone,
    CONSTRAINT ck_compute_allocations_capacity_positive CHECK ((capacity >= 1)),
    CONSTRAINT ck_compute_allocations_chats_nonnegative CHECK ((chats_served >= 0)),
    CONSTRAINT ck_compute_allocations_lifecycle CHECK (((lifecycle)::text = ANY ((ARRAY['session'::character varying, 'workspace'::character varying])::text[]))),
    CONSTRAINT ck_compute_allocations_org_machine_tenant CHECK (((org_machine_id IS NULL) OR (tenant_org_id IS NOT NULL))),
    CONSTRAINT ck_compute_allocations_origin CHECK (((origin)::text = ANY ((ARRAY['provisioned'::character varying, 'registered'::character varying])::text[]))),
    CONSTRAINT ck_compute_allocations_provision_attempts_nonnegative CHECK ((provision_attempts >= 0)),
    CONSTRAINT ck_compute_allocations_sandbox CHECK (((sandbox)::text = ANY ((ARRAY['gvisor'::character varying, 'none'::character varying])::text[]))),
    CONSTRAINT ck_compute_allocations_state CHECK (((state)::text = ANY ((ARRAY['pending'::character varying, 'provisioning'::character varying, 'bootstrapping'::character varying, 'ready'::character varying, 'draining'::character varying, 'asleep'::character varying, 'releasing'::character varying, 'released'::character varying, 'failed'::character varying, 'lost'::character varying])::text[]))),
    CONSTRAINT ck_compute_allocations_storage_nonnegative CHECK ((storage_gb >= 0)),
    CONSTRAINT ck_compute_allocations_storage_price_nonnegative CHECK ((storage_price_per_minute_nanos >= 0)),
    CONSTRAINT ck_compute_allocations_tenancy CHECK (((tenancy)::text = ANY ((ARRAY['org'::character varying, 'pool'::character varying, 'dedicated'::character varying, 'personal'::character varying])::text[])))
);

CREATE TABLE public.compute_grants (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    machine_type_id uuid,
    ceiling integer NOT NULL,
    per_user_max integer,
    rate_per_minute_nanos bigint NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_by uuid,
    note character varying(512) DEFAULT ''::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_compute_grants_ceiling_nonnegative CHECK ((ceiling >= 0)),
    CONSTRAINT ck_compute_grants_per_user_max_positive CHECK (((per_user_max IS NULL) OR (per_user_max >= 1))),
    CONSTRAINT ck_compute_grants_rate_nonnegative CHECK ((rate_per_minute_nanos >= 0))
);

CREATE TABLE public.compute_machine_types (
    id uuid NOT NULL,
    provider character varying(32) DEFAULT 'runpod'::character varying NOT NULL,
    provider_type_id character varying(128) NOT NULL,
    display_name character varying(128) NOT NULL,
    compute_class character varying(16) DEFAULT 'cpu'::character varying NOT NULL,
    gpu_count integer DEFAULT 0 NOT NULL,
    vcpu integer DEFAULT 0 NOT NULL,
    memory_gb integer DEFAULT 0 NOT NULL,
    provider_price_per_minute_nanos bigint DEFAULT '0'::bigint NOT NULL,
    active boolean DEFAULT true NOT NULL,
    availability character varying(16) DEFAULT 'unknown'::character varying NOT NULL,
    available_for_new boolean DEFAULT true NOT NULL,
    synced_at timestamp with time zone,
    provider_config jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    gpu_name character varying(64) DEFAULT ''::character varying NOT NULL,
    gpu_memory_gb integer DEFAULT 0 NOT NULL,
    disk_gb integer DEFAULT 0 NOT NULL,
    capacity_refused_at timestamp with time zone,
    CONSTRAINT ck_compute_machine_types_provider CHECK (((provider)::text = ANY ((ARRAY['container'::character varying, 'ec2'::character varying, 'localdev'::character varying, 'personal'::character varying, 'runpod'::character varying])::text[])))
);

CREATE TABLE public.compute_offering_orgs (
    offering_id uuid NOT NULL,
    org_team_id uuid NOT NULL
);

CREATE TABLE public.compute_offerings (
    id uuid NOT NULL,
    machine_type_id uuid NOT NULL,
    name character varying(128) NOT NULL,
    description character varying(512) DEFAULT ''::character varying NOT NULL,
    pricing_mode character varying(16) NOT NULL,
    markup_bps integer DEFAULT 0 NOT NULL,
    fixed_rate_per_minute_nanos bigint,
    storage_gb_default integer NOT NULL,
    storage_gb_max integer NOT NULL,
    storage_rate_per_gb_month_nanos bigint DEFAULT '0'::bigint NOT NULL,
    region character varying(32) DEFAULT ''::character varying NOT NULL,
    audience character varying(16) NOT NULL,
    purchasable boolean DEFAULT true NOT NULL,
    idle_stop_minutes_default integer,
    sort_order integer DEFAULT 0 NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    retired_at timestamp with time zone,
    CONSTRAINT ck_compute_offerings_audience CHECK (((audience)::text = ANY ((ARRAY['all'::character varying, 'enterprise'::character varying, 'listed'::character varying])::text[]))),
    CONSTRAINT ck_compute_offerings_fixed_rate CHECK ((((pricing_mode)::text <> 'fixed'::text) OR (fixed_rate_per_minute_nanos IS NOT NULL))),
    CONSTRAINT ck_compute_offerings_fixed_rate_nonnegative CHECK (((fixed_rate_per_minute_nanos IS NULL) OR (fixed_rate_per_minute_nanos >= 0))),
    CONSTRAINT ck_compute_offerings_idle_stop_default CHECK (((idle_stop_minutes_default IS NULL) OR (idle_stop_minutes_default >= 5))),
    CONSTRAINT ck_compute_offerings_markup_nonnegative CHECK ((markup_bps >= 0)),
    CONSTRAINT ck_compute_offerings_pricing_mode CHECK (((pricing_mode)::text = ANY ((ARRAY['pass_through'::character varying, 'fixed'::character varying])::text[]))),
    CONSTRAINT ck_compute_offerings_storage_default CHECK ((storage_gb_default > 0)),
    CONSTRAINT ck_compute_offerings_storage_max CHECK ((storage_gb_max >= storage_gb_default)),
    CONSTRAINT ck_compute_offerings_storage_rate_nonnegative CHECK ((storage_rate_per_gb_month_nanos >= 0))
);

CREATE TABLE public.connection_inventory (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    workspace_key character varying(64) NOT NULL,
    plugin character varying(64) NOT NULL,
    status character varying(32) DEFAULT 'unverifiable'::character varying NOT NULL,
    last_verified_at timestamp with time zone,
    reported_at timestamp with time zone DEFAULT now() NOT NULL,
    org_team_id uuid NOT NULL
);

CREATE TABLE public.connection_verifications (
    id uuid NOT NULL,
    team_id uuid NOT NULL,
    plugin character varying(64) NOT NULL,
    attributes jsonb DEFAULT '{}'::jsonb NOT NULL,
    secret_encrypted character varying(8192),
    detail text DEFAULT ''::character varying NOT NULL,
    requested_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    named_secrets_encrypted jsonb DEFAULT '{}'::jsonb NOT NULL,
    endpoint_results jsonb DEFAULT '{}'::jsonb NOT NULL,
    connection_id uuid,
    vantage_kind character varying(16) DEFAULT 'server'::character varying NOT NULL,
    state character varying(16) DEFAULT 'queued'::character varying NOT NULL,
    outcome character varying(32),
    payload_hash character varying(64),
    dispatched_at timestamp with time zone,
    dispatch_attempts integer DEFAULT 0 NOT NULL,
    last_dispatch_error character varying(512) DEFAULT ''::character varying NOT NULL,
    started_at timestamp with time zone,
    heartbeat_at timestamp with time zone,
    latency_ms integer,
    CONSTRAINT ck_cv_outcome CHECK (((outcome IS NULL) OR ((outcome)::text = ANY ((ARRAY['ok'::character varying, 'invalid_credential'::character varying, 'permission'::character varying, 'unreachable'::character varying, 'timeout'::character varying, 'unsupported'::character varying, 'infrastructure'::character varying, 'error'::character varying])::text[])))),
    CONSTRAINT ck_cv_state CHECK (((state)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'settled'::character varying, 'abandoned'::character varying])::text[]))),
    CONSTRAINT ck_cv_vantage CHECK (((vantage_kind)::text = 'server'::text))
);

CREATE TABLE public.crash_reports (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    component character varying(64) NOT NULL,
    error_type character varying(255),
    message text NOT NULL,
    stacktrace text,
    context jsonb,
    comment text,
    app_version character varying(64),
    platform character varying(128),
    logs text,
    occurred_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    read_at timestamp with time zone,
    read_by_user_id uuid
);

CREATE TABLE public.crdt_docs (
    org_id uuid NOT NULL,
    doc_type character varying(32) NOT NULL,
    doc_id character varying(255) NOT NULL,
    incarnation uuid DEFAULT gen_random_uuid() NOT NULL,
    epoch integer DEFAULT 1 NOT NULL,
    log_seq integer DEFAULT 0 NOT NULL,
    vv bytea DEFAULT '\x'::bytea NOT NULL,
    snapshot bytea DEFAULT '\x'::bytea NOT NULL,
    snapshot_log_seq integer DEFAULT 0 NOT NULL,
    snapshot_bytes integer DEFAULT 0 NOT NULL,
    log_bytes integer DEFAULT 0 NOT NULL,
    doc_schema integer DEFAULT 1 NOT NULL,
    loro_format character varying(32) NOT NULL,
    projection jsonb DEFAULT '{}'::jsonb NOT NULL,
    seeded_from character varying(160) NOT NULL,
    quarantined_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    source_etag integer,
    source_version_id uuid,
    source_sha256 character varying(64),
    source_vv bytea,
    source_epoch integer,
    source_history jsonb DEFAULT '[]'::jsonb NOT NULL,
    save_paused_reason character varying(64),
    save_retry_at timestamp with time zone,
    save_failures integer DEFAULT 0 NOT NULL,
    text_peer_until timestamp with time zone,
    text_peer_views jsonb DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT ck_crdt_docs_bytes_nonnegative CHECK (((snapshot_bytes >= 0) AND (log_bytes >= 0))),
    CONSTRAINT ck_crdt_docs_doc_schema_positive CHECK ((doc_schema >= 1)),
    CONSTRAINT ck_crdt_docs_doc_type CHECK (((doc_type)::text = ANY ((ARRAY['chat_workspace'::character varying, 'file'::character varying, 'notebook'::character varying])::text[]))),
    CONSTRAINT ck_crdt_docs_epoch_positive CHECK ((epoch >= 1)),
    CONSTRAINT ck_crdt_docs_log_seq_nonnegative CHECK ((log_seq >= 0)),
    CONSTRAINT ck_crdt_docs_snapshot_within_log CHECK (((snapshot_log_seq >= 0) AND (snapshot_log_seq <= log_seq))),
    CONSTRAINT ck_crdt_docs_source_whole CHECK ((((source_etag IS NULL) AND (source_version_id IS NULL) AND (source_sha256 IS NULL) AND (source_vv IS NULL) AND (source_epoch IS NULL)) OR ((source_etag IS NOT NULL) AND (source_sha256 IS NOT NULL) AND (source_vv IS NOT NULL) AND (source_epoch IS NOT NULL))))
);

ALTER TABLE ONLY public.crdt_docs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.crdt_peers (
    loro_peer bigint NOT NULL,
    org_id uuid NOT NULL,
    doc_type character varying(32) NOT NULL,
    doc_id character varying(255) NOT NULL,
    user_id uuid NOT NULL,
    held_by character varying(128),
    held_until timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_crdt_peers_doc_type CHECK (((doc_type)::text = ANY ((ARRAY['chat_workspace'::character varying, 'file'::character varying, 'notebook'::character varying])::text[]))),
    CONSTRAINT ck_crdt_peers_loro_peer_range CHECK (((loro_peer >= 1024) AND (loro_peer <= '9007199254740991'::bigint)))
);

CREATE SEQUENCE public.crdt_peer_seq
    START WITH 1024
    INCREMENT BY 1
    MINVALUE 1024
    MAXVALUE 9007199254740991
    CACHE 1;

ALTER SEQUENCE public.crdt_peer_seq OWNED BY public.crdt_peers.loro_peer;

CREATE TABLE public.crdt_updates (
    org_id uuid NOT NULL,
    doc_type character varying(32) NOT NULL,
    doc_id character varying(255) NOT NULL,
    epoch integer NOT NULL,
    log_seq integer NOT NULL,
    data bytea NOT NULL,
    size integer NOT NULL,
    sha256 bytea NOT NULL,
    update_id character varying(64) NOT NULL,
    loro_peer bigint NOT NULL,
    author_user_id uuid,
    agent_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_crdt_updates_epoch_positive CHECK ((epoch >= 1)),
    CONSTRAINT ck_crdt_updates_log_seq_positive CHECK ((log_seq >= 1)),
    CONSTRAINT ck_crdt_updates_sha256_length CHECK ((octet_length(sha256) = 32)),
    CONSTRAINT ck_crdt_updates_size_nonnegative CHECK ((size >= 0))
);

ALTER TABLE ONLY public.crdt_updates FORCE ROW LEVEL SECURITY;

CREATE TABLE public.dedup_domains (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    region character varying(64) DEFAULT ''::character varying NOT NULL,
    store_id uuid NOT NULL,
    kms_key_arn character varying(512),
    chunker_seed bytea NOT NULL,
    hmac_key_id character varying(128) DEFAULT ''::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    owner_marked_at timestamp with time zone,
    owner_marker_conflict_at timestamp with time zone,
    owner_marker_attempted_at timestamp with time zone
);

ALTER TABLE ONLY public.dedup_domains FORCE ROW LEVEL SECURITY;

CREATE TABLE public.deployment_health_checks (
    id uuid NOT NULL,
    check_key character varying(64) NOT NULL,
    label character varying(128) NOT NULL,
    org_team_id uuid,
    status character varying(16) NOT NULL,
    detail text DEFAULT ''::text NOT NULL,
    latency_ms integer DEFAULT 0 NOT NULL,
    ran_at timestamp with time zone NOT NULL,
    trigger character varying(16) NOT NULL,
    CONSTRAINT ck_deployment_health_checks_status CHECK (((status)::text = ANY ((ARRAY['ok'::character varying, 'warn'::character varying, 'fail'::character varying, 'skipped'::character varying])::text[]))),
    CONSTRAINT ck_deployment_health_checks_trigger CHECK (((trigger)::text = ANY ((ARRAY['scheduled'::character varying, 'manual'::character varying])::text[])))
);

CREATE TABLE public.deployment_health_runs (
    id bigint NOT NULL,
    last_run_at timestamp with time zone NOT NULL,
    last_trigger character varying(16) NOT NULL,
    last_duration_ms integer DEFAULT 0 NOT NULL,
    last_scheduled_at timestamp with time zone
);

CREATE TABLE public.device_authorizations (
    id uuid NOT NULL,
    device_code_hash character varying(96) NOT NULL,
    user_code character varying(32) NOT NULL,
    user_id uuid,
    client_id character varying(255) NOT NULL,
    scope character varying(512),
    interval_seconds integer NOT NULL,
    status character varying(16) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_polled_at timestamp with time zone,
    poll_count integer DEFAULT 0 NOT NULL,
    approved_at timestamp with time zone,
    denied_at timestamp with time zone,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    org_team_id uuid
);

CREATE TABLE public.email_domain_bans (
    id uuid NOT NULL,
    domain character varying(255) NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    created_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    lifted_at timestamp with time zone,
    lifted_by_id uuid
);

CREATE TABLE public.entitlement_grants (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    customer_slug character varying(64) NOT NULL,
    serial bigint NOT NULL,
    features bigint NOT NULL,
    expires_on date NOT NULL,
    token text NOT NULL,
    issued_by_id uuid,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    superseded_at timestamp with time zone
);

ALTER TABLE public.entitlement_grants ALTER COLUMN serial ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.entitlement_grants_serial_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.event_outbox (
    id bigint NOT NULL,
    event_id uuid NOT NULL,
    org_id uuid NOT NULL,
    type character varying(64) NOT NULL,
    entity character varying(64) NOT NULL,
    entity_id character varying(255) NOT NULL,
    version integer DEFAULT 0 NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    actor jsonb DEFAULT '{}'::jsonb NOT NULL,
    visibility character varying(64) DEFAULT 'org'::character varying NOT NULL,
    payload jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT ck_event_outbox_payload_size CHECK ((octet_length((payload)::text) <= 2097152)),
    CONSTRAINT ck_event_outbox_version_nonnegative CHECK ((version >= 0)),
    CONSTRAINT ck_event_outbox_visibility CHECK ((((visibility)::text = ANY ((ARRAY['org'::character varying, 'platform'::character varying])::text[])) OR ((visibility)::text ~ '^user:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'::text)))
);

CREATE SEQUENCE public.event_outbox_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.event_outbox_id_seq OWNED BY public.event_outbox.id;

CREATE TABLE public.file_acl_members (
    acl_id uuid NOT NULL,
    principal_kind character varying(16) NOT NULL,
    principal_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    max_role character varying(32) NOT NULL
);

ALTER TABLE ONLY public.file_acl_members FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_acls (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    body jsonb NOT NULL,
    body_hash character varying(64) NOT NULL
);

ALTER TABLE ONLY public.file_acls FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_conflicts (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    node_id uuid NOT NULL,
    base_version_id uuid,
    theirs_version_id uuid NOT NULL,
    mine_version_id uuid NOT NULL,
    actor uuid NOT NULL,
    state character varying(16) DEFAULT 'open'::character varying NOT NULL,
    resolved_at timestamp with time zone,
    copy_node_id uuid,
    arrived_from character varying(16),
    who character varying(255),
    displaced_by character varying(255),
    resolved_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_file_conflicts_arrived_from CHECK (((arrived_from)::text = ANY ((ARRAY['web'::character varying, 'holder'::character varying])::text[]))),
    CONSTRAINT ck_file_conflicts_state CHECK (((state)::text = ANY ((ARRAY['open'::character varying, 'resolved'::character varying, 'discarded'::character varying, 'auto'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_conflicts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_content_grants (
    nonce character varying(64) NOT NULL,
    org_team_id uuid NOT NULL,
    version_id uuid NOT NULL,
    session_id uuid,
    range_lo bigint,
    range_hi bigint,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone
)
WITH (autovacuum_vacuum_scale_factor='0.01');

ALTER TABLE ONLY public.file_content_grants FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_dir_stats (
    node_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    bytes bigint NOT NULL,
    files bigint NOT NULL,
    direct_children bigint NOT NULL,
    last_child_change_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.file_dir_stats FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_dir_stats_deltas (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    node_id uuid NOT NULL,
    bytes_delta bigint NOT NULL,
    files_delta bigint NOT NULL,
    direct_children_delta bigint NOT NULL,
    child_change_at timestamp with time zone,
    at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.file_dir_stats_deltas FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_drives (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    kind character varying(32) DEFAULT 'org'::character varying NOT NULL,
    store_id uuid NOT NULL,
    dedup_domain_id uuid NOT NULL,
    root_node_id uuid,
    quota_bytes bigint NOT NULL,
    quota_nodes bigint NOT NULL,
    next_ino bigint NOT NULL,
    frozen_reason character varying(32),
    CONSTRAINT ck_file_drives_frozen_reason CHECK (((frozen_reason)::text = ANY ((ARRAY['over_quota'::character varying, 'teardown'::character varying])::text[]))),
    CONSTRAINT ck_file_drives_kind CHECK (((kind)::text = 'org'::text))
);

ALTER TABLE ONLY public.file_drives FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_erasure_log (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    target character varying(1024) NOT NULL,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    certificate jsonb NOT NULL
);

ALTER TABLE ONLY public.file_erasure_log FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_history (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    node_id uuid NOT NULL,
    seq bigint NOT NULL,
    kind character varying(32) NOT NULL,
    acting_principal uuid NOT NULL,
    delegating_user uuid,
    agent_session_id uuid,
    at timestamp with time zone DEFAULT now() NOT NULL,
    before jsonb,
    after jsonb,
    op_id uuid,
    CONSTRAINT ck_file_history_kind CHECK (((kind)::text = ANY ((ARRAY['create'::character varying, 'copy'::character varying, 'rename'::character varying, 'move'::character varying, 'attrs'::character varying, 'trash'::character varying, 'restore'::character varying, 'lock'::character varying, 'hold'::character varying, 'label'::character varying, 'acl'::character varying, 'conflict_resolved'::character varying, 'conflict'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_history FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_holds (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    scope character varying(16) NOT NULL,
    node_id uuid,
    matter_ref character varying(255) NOT NULL,
    placed_by uuid NOT NULL,
    placed_at timestamp with time zone DEFAULT now() NOT NULL,
    released_at timestamp with time zone,
    CONSTRAINT ck_file_holds_scope CHECK (((scope)::text = ANY ((ARRAY['org'::character varying, 'drive'::character varying, 'subtree'::character varying, 'node'::character varying, 'custodian'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_holds FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_idempotency_keys (
    org_team_id uuid NOT NULL,
    key character varying(255) NOT NULL,
    principal_id uuid NOT NULL,
    route character varying(255) NOT NULL,
    request_hash character varying(128) NOT NULL,
    status character varying(16) DEFAULT 'in_progress'::character varying NOT NULL,
    body jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    scope character varying(64) DEFAULT 'files'::character varying NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + '24:00:00'::interval) NOT NULL,
    CONSTRAINT ck_file_idempotency_keys_status CHECK (((status)::text = ANY ((ARRAY['in_progress'::character varying, 'succeeded'::character varying, 'failed'::character varying])::text[])))
)
WITH (autovacuum_vacuum_scale_factor='0.01');

ALTER TABLE ONLY public.file_idempotency_keys FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_key_chunks (
    hmac_hash bytea NOT NULL,
    org_team_id uuid NOT NULL,
    dedup_domain_id uuid NOT NULL,
    pack_id uuid NOT NULL,
    chunk_idx bigint NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL
)
WITH (autovacuum_vacuum_scale_factor='0.01');

ALTER TABLE ONLY public.file_key_chunks FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_lease_epoch_hwm (
    node_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    hwm bigint NOT NULL
);

ALTER TABLE ONLY public.file_lease_epoch_hwm FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_lease_live_entries (
    lease_node_id uuid NOT NULL,
    node_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    state character varying(16) NOT NULL,
    lease_epoch bigint NOT NULL,
    box_size bigint,
    box_mtime_ns bigint,
    seq bigint NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_file_lease_live_entries_state CHECK (((state)::text = ANY ((ARRAY['writing'::character varying, 'uploading'::character varying, 'on_box'::character varying, 'deferred'::character varying, 'inbound'::character varying, 'inbound_delete'::character varying, 'inbound_rename'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_lease_live_entries FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_leases (
    node_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    epoch bigint NOT NULL,
    holder_principal_kind character varying(16) NOT NULL,
    holder_principal_id uuid NOT NULL,
    holder_instance_id character varying(128) NOT NULL,
    machine_id character varying(128) NOT NULL,
    purpose character varying(16) NOT NULL,
    acquired_at timestamp with time zone DEFAULT now() NOT NULL,
    heartbeat_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_sync_at timestamp with time zone,
    grantable_after timestamp with time zone DEFAULT now() NOT NULL,
    released_at timestamp with time zone,
    reaped_at timestamp with time zone,
    accepts_inbound boolean DEFAULT false NOT NULL,
    live_seq bigint DEFAULT 0 NOT NULL,
    live_cadence jsonb DEFAULT '{}'::jsonb NOT NULL,
    holder_kind character varying(16) DEFAULT 'user'::character varying NOT NULL,
    unsynced_count integer,
    unsynced_swept_at timestamp with time zone,
    CONSTRAINT ck_file_leases_holder_kind CHECK (((holder_kind)::text = ANY ((ARRAY['user'::character varying, 'pat'::character varying, 'service'::character varying, 'machine'::character varying])::text[]))),
    CONSTRAINT ck_file_leases_holder_principal_kind CHECK (((holder_principal_kind)::text = ANY ((ARRAY['user'::character varying, 'team'::character varying, 'org'::character varying, 'link'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_leases FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_links (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    node_id uuid NOT NULL,
    token_hash character varying(128) NOT NULL,
    scope character varying(16) DEFAULT 'node'::character varying NOT NULL,
    role character varying(32) NOT NULL,
    password_hash character varying(255),
    expires_at timestamp with time zone,
    params jsonb NOT NULL,
    revoked_at timestamp with time zone,
    hide_download boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_file_links_scope CHECK (((scope)::text = ANY ((ARRAY['node'::character varying, 'subtree'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_links FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_locks (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    node_id uuid NOT NULL,
    holder_principal uuid NOT NULL,
    kind character varying(16) NOT NULL,
    enforcement character varying(16) DEFAULT 'advisory'::character varying NOT NULL,
    range_lo bigint,
    range_hi bigint,
    token character varying(255) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT ck_file_locks_enforcement CHECK (((enforcement)::text = ANY ((ARRAY['advisory'::character varying, 'mandatory'::character varying])::text[]))),
    CONSTRAINT ck_file_locks_kind CHECK (((kind)::text = ANY ((ARRAY['flock'::character varying, 'range'::character varying, 'webdav'::character varying, 'wopi'::character varying, 'checkout'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_locks FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_manifests (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    dedup_domain_id uuid NOT NULL,
    content_hash character varying(128) NOT NULL,
    size_bytes bigint NOT NULL,
    chunker character varying(64) DEFAULT ''::character varying NOT NULL,
    chunker_seed_version integer DEFAULT 0 NOT NULL,
    term_count bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.file_manifests FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_nodes (
    id uuid NOT NULL,
    ino bigint NOT NULL,
    drive_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    parent_id uuid,
    kind character varying(16) NOT NULL,
    subtype character varying(32),
    name bytea NOT NULL,
    name_display character varying(1024) DEFAULT ''::character varying NOT NULL,
    name_key character varying(1024) DEFAULT ''::character varying NOT NULL,
    name_encoding character varying(32) DEFAULT 'utf-8'::character varying NOT NULL,
    flags_names jsonb NOT NULL,
    path_ids public.ltree NOT NULL,
    depth integer NOT NULL,
    target_id uuid,
    target_object_id uuid,
    head_version_id uuid,
    mode integer NOT NULL,
    uid integer NOT NULL,
    gid integer NOT NULL,
    nlink integer NOT NULL,
    size bigint NOT NULL,
    rdev bigint NOT NULL,
    atime_ns bigint NOT NULL,
    mtime_ns bigint NOT NULL,
    ctime_ns bigint NOT NULL,
    birthtime_ns bigint NOT NULL,
    xattrs jsonb NOT NULL,
    symlink_target bytea,
    symlink_kind character varying(16),
    mime_class character varying(16),
    etag bigint NOT NULL,
    flags integer NOT NULL,
    state character varying(16) DEFAULT 'live'::character varying NOT NULL,
    trust character varying(16) DEFAULT 'own'::character varying NOT NULL,
    acl_id uuid,
    default_acl_id uuid,
    traversal_only boolean NOT NULL,
    retention_label_id uuid,
    trashed_at timestamp with time zone,
    trash_op_id uuid,
    metadata jsonb NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    holder_size bigint,
    holder_mtime_ns bigint,
    holder_hash bytea,
    holder_seq bigint,
    CONSTRAINT ck_file_nodes_kind CHECK (((kind)::text = ANY ((ARRAY['folder'::character varying, 'file'::character varying, 'symlink'::character varying, 'shortcut'::character varying, 'object'::character varying, 'special'::character varying, 'document'::character varying, 'remote'::character varying])::text[]))),
    CONSTRAINT ck_file_nodes_mime_class CHECK (((mime_class)::text = ANY ((ARRAY['image'::character varying, 'tabular'::character varying, 'code'::character varying, 'archive'::character varying, 'document'::character varying, 'text'::character varying, 'other'::character varying])::text[]))),
    CONSTRAINT ck_file_nodes_name_no_separator CHECK ((POSITION(('\x2f'::bytea) IN (name)) = 0)),
    CONSTRAINT ck_file_nodes_state CHECK (((state)::text = ANY ((ARRAY['live'::character varying, 'locked'::character varying, 'moving'::character varying, 'acl_rewriting'::character varying])::text[]))),
    CONSTRAINT ck_file_nodes_symlink_kind CHECK (((symlink_kind)::text = ANY ((ARRAY['relative'::character varying, 'canonical'::character varying, 'host'::character varying])::text[]))),
    CONSTRAINT ck_file_nodes_trust CHECK (((trust)::text = ANY ((ARRAY['own'::character varying, 'shared_in'::character varying, 'imported'::character varying, 'link_uploaded'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_nodes FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_ops (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    drive_id uuid NOT NULL,
    kind character varying(32) NOT NULL,
    actor uuid NOT NULL,
    idempotency_key character varying(255),
    inverse jsonb,
    undoable_until timestamp with time zone,
    state character varying(16) DEFAULT 'queued'::character varying NOT NULL,
    heartbeat_at timestamp with time zone,
    progress jsonb NOT NULL,
    errors jsonb NOT NULL,
    conflicts jsonb NOT NULL,
    result_node_id uuid,
    result jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_file_ops_kind CHECK (((kind)::text = ANY ((ARRAY['copy'::character varying, 'move'::character varying, 'trash'::character varying, 'restore'::character varying, 'purge'::character varying, 'upload'::character varying, 'download'::character varying, 'acl_rewrite'::character varying, 'undo'::character varying, 'bulk'::character varying])::text[]))),
    CONSTRAINT ck_file_ops_state CHECK (((state)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'done'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_ops FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_packs (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    dedup_domain_id uuid NOT NULL,
    hash character varying(128) NOT NULL,
    store_id uuid NOT NULL,
    key character varying(1024) NOT NULL,
    format_major integer DEFAULT 1 NOT NULL,
    format_minor integer DEFAULT 0 NOT NULL,
    stored_bytes bigint NOT NULL,
    raw_bytes bigint NOT NULL,
    chunk_count bigint NOT NULL,
    live_bytes bigint NOT NULL,
    storage_class character varying(32) DEFAULT ''::character varying NOT NULL,
    sealed_at timestamp with time zone,
    deleted_at timestamp with time zone
);

ALTER TABLE ONLY public.file_packs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_page_grants (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    nonce text NOT NULL,
    root_node_id uuid NOT NULL,
    entry_node_id uuid NOT NULL,
    minted_by_user_id uuid NOT NULL,
    credential_id text,
    session_id uuid,
    requests_served integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone
);

ALTER TABLE ONLY public.file_page_grants FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_platform (
    id integer NOT NULL,
    restore_generation integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_file_platform_single_row CHECK ((id = 1))
);

CREATE SEQUENCE public.file_platform_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.file_platform_id_seq OWNED BY public.file_platform.id;

CREATE TABLE public.file_quarantine (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    kind character varying(32) NOT NULL,
    ref_id character varying(1024) NOT NULL,
    reason character varying(255) NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    attempts bigint NOT NULL,
    detail jsonb NOT NULL,
    resolved_at timestamp with time zone,
    CONSTRAINT ck_file_quarantine_kind CHECK (((kind)::text = ANY ((ARRAY['node'::character varying, 'version'::character varying, 'object'::character varying, 'pack'::character varying, 'upload_session'::character varying, 'stage_job'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_quarantine FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_retention_labels (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    name character varying(128) NOT NULL,
    policy jsonb NOT NULL
);

ALTER TABLE ONLY public.file_retention_labels FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_shares (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    node_id uuid NOT NULL,
    principal_kind character varying(16) NOT NULL,
    principal_id uuid NOT NULL,
    role character varying(32) NOT NULL,
    expires_at timestamp with time zone,
    conditions jsonb,
    granted_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone
);

ALTER TABLE ONLY public.file_shares FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_stage_jobs (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    lease_node_id uuid NOT NULL,
    machine_id character varying(128) NOT NULL,
    direction character varying(8) NOT NULL,
    mode character varying(32) DEFAULT ''::character varying NOT NULL,
    state character varying(16) DEFAULT 'queued'::character varying NOT NULL,
    cursor jsonb NOT NULL,
    bytes_total bigint NOT NULL,
    bytes_done bigint NOT NULL,
    failed_count bigint NOT NULL,
    quarantined_count bigint NOT NULL,
    lease_epoch bigint NOT NULL,
    CONSTRAINT ck_file_stage_jobs_direction CHECK (((direction)::text = ANY ((ARRAY['in'::character varying, 'out'::character varying])::text[]))),
    CONSTRAINT ck_file_stage_jobs_state CHECK (((state)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'done'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_stage_jobs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_stars (
    org_team_id uuid NOT NULL,
    user_id uuid NOT NULL,
    node_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.file_stars FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_stores (
    id uuid NOT NULL,
    driver character varying(32) NOT NULL,
    bucket character varying(255) DEFAULT ''::character varying NOT NULL,
    endpoint character varying(1024) DEFAULT ''::character varying NOT NULL,
    region character varying(64) DEFAULT ''::character varying NOT NULL,
    capabilities jsonb NOT NULL,
    transfer_modes text[] NOT NULL,
    CONSTRAINT ck_file_stores_driver CHECK (((driver)::text = ANY ((ARRAY['filesystem'::character varying, 's3'::character varying])::text[])))
);

CREATE TABLE public.file_sweep_shards (
    shard integer NOT NULL,
    holder character varying(128),
    expires_at timestamp with time zone,
    cursor jsonb NOT NULL,
    sweep_started_at timestamp with time zone
);

CREATE SEQUENCE public.file_sweep_shards_shard_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.file_sweep_shards_shard_seq OWNED BY public.file_sweep_shards.shard;

CREATE TABLE public.file_trash_ops (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    drive_id uuid NOT NULL,
    root_node_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    deleted_at timestamp with time zone DEFAULT now() NOT NULL,
    purge_after timestamp with time zone NOT NULL,
    reason character varying(32),
    reason_machine character varying(128)
);

ALTER TABLE ONLY public.file_trash_ops FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_upload_parts (
    session_id uuid NOT NULL,
    part_no bigint NOT NULL,
    org_team_id uuid NOT NULL,
    size bigint NOT NULL,
    checksum bytea NOT NULL,
    etag character varying(255),
    completed_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.file_upload_parts FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_upload_sessions (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    drive_id uuid NOT NULL,
    node_id uuid,
    parent_id uuid NOT NULL,
    name bytea NOT NULL,
    dedup_domain_id uuid NOT NULL,
    state character varying(16) DEFAULT 'open'::character varying NOT NULL,
    declared_size bigint NOT NULL,
    bytes_received bigint NOT NULL,
    store_upload_id character varying(1024),
    store_key character varying(1024),
    transfer_mode character varying(16) DEFAULT 'proxied'::character varying NOT NULL,
    lease_epoch bigint NOT NULL,
    quota_hold_bytes bigint NOT NULL,
    quota_hold_nodes bigint NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    lease_holder uuid,
    lease_holder_kind character varying(16),
    conflict_copy boolean DEFAULT false NOT NULL,
    conflict_of uuid,
    CONSTRAINT ck_file_upload_sessions_state CHECK (((state)::text = ANY ((ARRAY['open'::character varying, 'uploading'::character varying, 'committing'::character varying, 'done'::character varying, 'aborted'::character varying, 'expired'::character varying])::text[]))),
    CONSTRAINT ck_file_upload_sessions_transfer_mode CHECK (((transfer_mode)::text = ANY ((ARRAY['single'::character varying, 'proxied'::character varying, 'direct'::character varying])::text[])))
)
WITH (autovacuum_vacuum_scale_factor='0.01');

ALTER TABLE ONLY public.file_upload_sessions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.file_versions (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    node_id uuid NOT NULL,
    seq bigint NOT NULL,
    size_bytes bigint NOT NULL,
    content_hash character varying(128) NOT NULL,
    block_hash character varying(128) DEFAULT ''::character varying NOT NULL,
    manifest_id uuid,
    inline_bytes bytea,
    store_key character varying(1024),
    mime_sniffed character varying(255) DEFAULT 'application/octet-stream'::character varying NOT NULL,
    scan_state character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    scanned_at timestamp with time zone,
    scan_engine_version character varying(64),
    source character varying(32) NOT NULL,
    keep_forever boolean NOT NULL,
    held boolean NOT NULL,
    expires_at timestamp with time zone,
    lease_epoch bigint NOT NULL,
    metadata jsonb NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_file_versions_scan_state CHECK (((scan_state)::text = ANY ((ARRAY['pending'::character varying, 'clean'::character varying, 'infected'::character varying, 'skipped'::character varying, 'failed'::character varying])::text[]))),
    CONSTRAINT ck_file_versions_source CHECK (((source)::text = ANY ((ARRAY['upload'::character varying, 'box_export'::character varying, 'import'::character varying, 'restore'::character varying, 'document_snapshot'::character varying, 'rows'::character varying, 'copy'::character varying])::text[])))
);

ALTER TABLE ONLY public.file_versions FORCE ROW LEVEL SECURITY;

CREATE TABLE public.identity_org_creations (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    org_team_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.identity_security_events (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    event character varying(64) NOT NULL,
    org_team_id uuid,
    ip_prefix character varying(64),
    user_agent character varying(255),
    detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.invitations (
    id uuid NOT NULL,
    team_id uuid NOT NULL,
    email character varying(320) NOT NULL,
    role character varying(32) DEFAULT 'member'::character varying NOT NULL,
    token character varying(96) NOT NULL,
    status character varying(32) DEFAULT 'pending'::character varying NOT NULL,
    invited_by_id uuid,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    CONSTRAINT ck_invitations_role CHECK (((role)::text = ANY ((ARRAY['member'::character varying, 'admin'::character varying])::text[]))),
    CONSTRAINT ck_invitations_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'accepted'::character varying, 'rejected'::character varying, 'expired'::character varying, 'revoked'::character varying])::text[])))
);

CREATE TABLE public.login_lockouts (
    identifier_digest character varying(64) NOT NULL,
    failed_count integer NOT NULL,
    last_failed_at timestamp with time zone NOT NULL,
    locked_until timestamp with time zone
);

CREATE TABLE public.machine_credentials (
    id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    label character varying(128) NOT NULL,
    org_team_id uuid NOT NULL,
    machine_type_id uuid NOT NULL,
    region character varying(32) DEFAULT ''::character varying NOT NULL,
    tenancy character varying(16) NOT NULL,
    machine_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    CONSTRAINT ck_machine_credentials_tenancy CHECK (((tenancy)::text = ANY ((ARRAY['pool'::character varying, 'dedicated'::character varying, 'personal'::character varying])::text[])))
);

CREATE TABLE public.manifest_terms (
    manifest_id uuid NOT NULL,
    ord integer NOT NULL,
    org_team_id uuid NOT NULL,
    pack_id uuid NOT NULL,
    chunk_lo bigint NOT NULL,
    chunk_hi bigint NOT NULL,
    raw_bytes bigint NOT NULL
);

ALTER TABLE ONLY public.manifest_terms FORCE ROW LEVEL SECURITY;

CREATE TABLE public.model_provider_configs (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    provider character varying(16) NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    api_key_encrypted character varying(4096),
    base_url character varying(1024),
    openai_organization_id character varying(256),
    bedrock_region character varying(64),
    bedrock_auth_mode character varying(16),
    aws_access_key_id_encrypted character varying(1024),
    aws_secret_access_key_encrypted character varying(2048),
    secret_hint character varying(16),
    last_verified_at timestamp with time zone,
    last_verified_status character varying(16),
    updated_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.notebook_edits (
    id bigint NOT NULL,
    org_id uuid NOT NULL,
    item_id uuid NOT NULL,
    cell_id character varying(10) NOT NULL,
    actor_key character varying(160) NOT NULL,
    actor_kind character varying(16) NOT NULL,
    user_id uuid,
    agent_id character varying(128),
    actor_display character varying(255) NOT NULL,
    first_at timestamp with time zone NOT NULL,
    last_at timestamp with time zone NOT NULL,
    edits integer DEFAULT 1 NOT NULL,
    last_submit_id character varying(48),
    CONSTRAINT ck_notebook_edits_actor_kind CHECK (((actor_kind)::text = ANY ((ARRAY['person'::character varying, 'agent'::character varying, 'system'::character varying])::text[]))),
    CONSTRAINT ck_notebook_edits_cell_id CHECK (((cell_id)::text ~ '^[0-9a-hjkmnp-tv-z]{10}$'::text)),
    CONSTRAINT ck_notebook_edits_edits_positive CHECK ((edits >= 1)),
    CONSTRAINT ck_notebook_edits_ordered CHECK ((first_at <= last_at))
);

ALTER TABLE ONLY public.notebook_edits FORCE ROW LEVEL SECURITY;

ALTER TABLE public.notebook_edits ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME public.notebook_edits_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);

CREATE TABLE public.notebook_epoch_tails (
    org_id uuid NOT NULL,
    item_id uuid NOT NULL,
    epoch integer NOT NULL,
    snapshot bytea NOT NULL,
    vv bytea NOT NULL,
    next_epoch integer NOT NULL,
    next_base_vv bytea NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_notebook_epoch_tails_epoch_positive CHECK ((epoch >= 1)),
    CONSTRAINT ck_notebook_epoch_tails_next_after CHECK ((next_epoch > epoch))
);

ALTER TABLE ONLY public.notebook_epoch_tails FORCE ROW LEVEL SECURITY;

CREATE TABLE public.notebook_kernels (
    kernel_id character varying(64) NOT NULL,
    org_id uuid NOT NULL,
    drive_id uuid NOT NULL,
    item_id uuid NOT NULL,
    machine_id character varying(128) NOT NULL,
    state character varying(16) NOT NULL,
    seq bigint DEFAULT 0 NOT NULL,
    env_id character varying(128),
    started_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    stopped_at timestamp with time zone,
    CONSTRAINT ck_notebook_kernels_seq_nonnegative CHECK ((seq >= 0)),
    CONSTRAINT ck_notebook_kernels_state CHECK (((state)::text = ANY ((ARRAY['absent'::character varying, 'starting'::character varying, 'idle'::character varying, 'busy'::character varying, 'restarting'::character varying, 'stopped'::character varying])::text[])))
);

ALTER TABLE ONLY public.notebook_kernels FORCE ROW LEVEL SECURITY;

CREATE TABLE public.notebook_peers (
    org_id uuid NOT NULL,
    item_id uuid NOT NULL,
    actor_key character varying(160) NOT NULL,
    epoch integer NOT NULL,
    loro_peer bigint NOT NULL,
    claimed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_notebook_peers_epoch_positive CHECK ((epoch >= 1)),
    CONSTRAINT ck_notebook_peers_loro_peer_client_range CHECK ((loro_peer >= 1024))
);

ALTER TABLE ONLY public.notebook_peers FORCE ROW LEVEL SECURITY;

CREATE TABLE public.notebook_runs (
    run_id uuid NOT NULL,
    org_id uuid NOT NULL,
    drive_id uuid NOT NULL,
    item_id uuid NOT NULL,
    kernel_id character varying(64),
    client_run_id character varying(48),
    actor_kind character varying(16) NOT NULL,
    requested_by_user_id uuid,
    requested_by_agent character varying(128),
    actor_display character varying(255) NOT NULL,
    trigger character varying(16) NOT NULL,
    target jsonb NOT NULL,
    frontier text,
    frontier_included boolean DEFAULT false NOT NULL,
    submitted jsonb DEFAULT '{}'::jsonb NOT NULL,
    status character varying(24) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    reason character varying(64),
    engine_run_id character varying(64),
    CONSTRAINT ck_notebook_runs_actor_kind CHECK (((actor_kind)::text = ANY ((ARRAY['person'::character varying, 'agent'::character varying, 'system'::character varying])::text[]))),
    CONSTRAINT ck_notebook_runs_status CHECK (((status)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying, 'ok'::character varying, 'error'::character varying, 'interrupted'::character varying, 'kernel_restarted'::character varying, 'refused'::character varying, 'needs_confirmation'::character varying, 'coalesced'::character varying, 'planned'::character varying])::text[]))),
    CONSTRAINT ck_notebook_runs_trigger CHECK (((trigger)::text = ANY ((ARRAY['run'::character varying, 'run_all'::character varying, 'run_stale'::character varying, 'widget'::character varying, 'autorun'::character varying])::text[])))
);

ALTER TABLE ONLY public.notebook_runs FORCE ROW LEVEL SECURITY;

CREATE TABLE public.oauth_identities (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    provider character varying(64) NOT NULL,
    subject character varying(255) NOT NULL,
    email_at_link character varying(320),
    email_verified boolean DEFAULT false NOT NULL,
    raw_profile jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_login_at timestamp with time zone
);

CREATE TABLE public.object_payload_rows (
    object_id uuid NOT NULL,
    page integer NOT NULL,
    sha256 character varying(64) NOT NULL,
    size integer NOT NULL,
    media_type character varying(128) DEFAULT 'application/json'::character varying NOT NULL,
    page_rows jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    org_team_id uuid NOT NULL,
    CONSTRAINT ck_object_payload_rows_page_nonnegative CHECK ((page >= 0)),
    CONSTRAINT ck_object_payload_rows_size_nonnegative CHECK ((size >= 0))
);

ALTER TABLE ONLY public.object_payload_rows FORCE ROW LEVEL SECURITY;

CREATE TABLE public.org_audit_events (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    actor_id uuid,
    actor_email character varying(320) DEFAULT ''::character varying NOT NULL,
    action character varying(64) NOT NULL,
    target character varying(320),
    detail jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    prev_hash character varying(64),
    entry_hash character varying(64)
);

CREATE TABLE public.org_compute_assignments (
    org_team_id uuid NOT NULL,
    machine_id uuid NOT NULL,
    fallback_to_pool boolean DEFAULT false NOT NULL,
    assigned_by uuid,
    assigned_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.org_compute_settings (
    org_team_id uuid NOT NULL,
    shared_pool_fallback boolean DEFAULT true NOT NULL,
    min_awake_pool integer DEFAULT 0 NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    default_org_machine_id uuid,
    CONSTRAINT ck_org_compute_settings_min_awake_pool CHECK ((min_awake_pool >= 0)),
    CONSTRAINT ck_org_compute_settings_version_positive CHECK ((version >= 1))
);

ALTER TABLE ONLY public.org_compute_settings FORCE ROW LEVEL SECURITY;

CREATE TABLE public.org_machine_audiences (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    org_machine_id uuid NOT NULL,
    grantee_kind character varying(8) NOT NULL,
    team_id uuid,
    user_id uuid,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_org_machine_audiences_grantee CHECK (((((grantee_kind)::text = 'org'::text) AND (team_id IS NULL) AND (user_id IS NULL)) OR (((grantee_kind)::text = 'team'::text) AND (team_id IS NOT NULL) AND (user_id IS NULL)) OR (((grantee_kind)::text = 'user'::text) AND (user_id IS NOT NULL) AND (team_id IS NULL)))),
    CONSTRAINT ck_org_machine_audiences_grantee_kind CHECK (((grantee_kind)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying, 'user'::character varying])::text[])))
);

ALTER TABLE ONLY public.org_machine_audiences FORCE ROW LEVEL SECURITY;

CREATE TABLE public.org_machines (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    owner_team_id uuid NOT NULL,
    offering_id uuid NOT NULL,
    name character varying(64) NOT NULL,
    acquisition character varying(16) NOT NULL,
    free_until timestamp with time zone,
    use_mode character varying(16) NOT NULL,
    storage_gb integer NOT NULL,
    idle_stop_minutes integer,
    monthly_cap_nanos bigint,
    current_allocation_id uuid,
    desired_power character varying(8) DEFAULT 'on'::character varying NOT NULL,
    stop_reason character varying(32) DEFAULT ''::character varying NOT NULL,
    unfunded_since timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    version integer DEFAULT 1 NOT NULL,
    CONSTRAINT ck_org_machines_acquisition CHECK (((acquisition)::text = ANY ((ARRAY['purchased'::character varying, 'granted'::character varying])::text[]))),
    CONSTRAINT ck_org_machines_desired_power CHECK (((desired_power)::text = ANY ((ARRAY['on'::character varying, 'off'::character varying])::text[]))),
    CONSTRAINT ck_org_machines_granted_free_until CHECK ((((acquisition)::text <> 'granted'::text) OR (free_until IS NOT NULL))),
    CONSTRAINT ck_org_machines_idle_stop_minutes CHECK (((idle_stop_minutes IS NULL) OR (idle_stop_minutes >= 5))),
    CONSTRAINT ck_org_machines_monthly_cap_nonnegative CHECK (((monthly_cap_nanos IS NULL) OR (monthly_cap_nanos >= 0))),
    CONSTRAINT ck_org_machines_stop_reason CHECK (((stop_reason)::text = ANY ((ARRAY[''::character varying, 'user'::character varying, 'idle'::character varying, 'credits'::character varying, 'cap'::character varying, 'free_expired'::character varying, 'provider'::character varying, 'moved'::character varying, 'not_responding'::character varying])::text[]))),
    CONSTRAINT ck_org_machines_storage_positive CHECK ((storage_gb > 0)),
    CONSTRAINT ck_org_machines_use_mode CHECK (((use_mode)::text = ANY ((ARRAY['pool'::character varying, 'assigned'::character varying])::text[]))),
    CONSTRAINT ck_org_machines_version_positive CHECK ((version >= 1))
);

ALTER TABLE ONLY public.org_machines FORCE ROW LEVEL SECURITY;

CREATE TABLE public.org_memberships (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    credential_epoch integer DEFAULT 0 NOT NULL,
    sso_exempt boolean DEFAULT false NOT NULL,
    scim_external_id character varying(255),
    display_name character varying(255),
    joined_at timestamp with time zone DEFAULT now() NOT NULL,
    last_active_at timestamp with time zone,
    deactivated_at timestamp with time zone,
    scim_given_name character varying(255),
    scim_family_name character varying(255)
);

CREATE TABLE public.org_settings (
    org_team_id uuid NOT NULL,
    allow_login_google boolean DEFAULT true NOT NULL,
    allow_login_github boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    gate_force_rules jsonb,
    gate_pr_budget_nanos bigint,
    gate_monthly_budget_nanos bigint,
    web_search_enabled boolean,
    ownership_escalation_enabled boolean DEFAULT false NOT NULL,
    sandbox_vcpu integer,
    sandbox_memory_mb integer,
    live_editing_enabled boolean,
    workspace_project_cap integer,
    CONSTRAINT ck_org_settings_sandbox_memory_mb CHECK (((sandbox_memory_mb IS NULL) OR ((sandbox_memory_mb >= 512) AND (sandbox_memory_mb <= 262144)))),
    CONSTRAINT ck_org_settings_sandbox_vcpu CHECK (((sandbox_vcpu IS NULL) OR ((sandbox_vcpu >= 1) AND (sandbox_vcpu <= 64)))),
    CONSTRAINT ck_org_settings_workspace_project_cap CHECK (((workspace_project_cap IS NULL) OR ((workspace_project_cap >= 1) AND (workspace_project_cap <= 100000))))
);

CREATE TABLE public.org_storage_limits (
    org_team_id uuid NOT NULL,
    limit_bytes bigint,
    created_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.org_sync_settings (
    org_team_id uuid NOT NULL,
    sync_enabled boolean DEFAULT false NOT NULL,
    default_promotion character varying(32) DEFAULT 'private'::character varying NOT NULL,
    classification_strictness character varying(32) DEFAULT 'standard'::character varying NOT NULL,
    auto_promote_policy character varying(48) DEFAULT 'human_or_self_verify'::character varying NOT NULL,
    pull_cadence_seconds integer DEFAULT 300 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.personal_access_tokens (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    label character varying(255),
    scopes jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone,
    last_used_at timestamp with time zone,
    membership_id uuid,
    membership_epoch integer
);

CREATE TABLE public.proxy_tokens (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    label character varying(255),
    created_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone
);

CREATE TABLE public.rate_limit_windows (
    rate_class character varying(32) NOT NULL,
    key character varying(160) NOT NULL,
    window_start timestamp with time zone NOT NULL,
    count integer NOT NULL
);

CREATE TABLE public.realtime_docs (
    doc_type character varying(32) NOT NULL,
    doc_id character varying(255) NOT NULL,
    org_id uuid NOT NULL,
    team_id uuid,
    owner_user_id uuid,
    epoch integer DEFAULT 1 NOT NULL,
    seq integer DEFAULT 0 NOT NULL,
    state jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    turn_state character varying(32),
    turn_state_at character varying(64),
    CONSTRAINT ck_realtime_docs_doc_type CHECK (((doc_type)::text = ANY ((ARRAY['chat'::character varying, 'artifact'::character varying])::text[]))),
    CONSTRAINT ck_realtime_docs_epoch_positive CHECK ((epoch >= 1)),
    CONSTRAINT ck_realtime_docs_seq_nonnegative CHECK ((seq >= 0))
);

CREATE TABLE public.realtime_presence (
    doc_type character varying(32) NOT NULL,
    doc_id character varying(255) NOT NULL,
    peer_id character varying(64) NOT NULL,
    user_id uuid NOT NULL,
    org_id uuid NOT NULL,
    joined_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.role_assignments (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    principal_kind character varying(16) NOT NULL,
    principal_id uuid NOT NULL,
    scope_kind character varying(8) NOT NULL,
    scope_id uuid NOT NULL,
    role character varying(16) NOT NULL,
    granted_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    CONSTRAINT ck_role_assignments_principal_kind CHECK (((principal_kind)::text = ANY ((ARRAY['user'::character varying, 'agent'::character varying, 'service'::character varying, 'pat'::character varying])::text[]))),
    CONSTRAINT ck_role_assignments_role CHECK (((role)::text = ANY ((ARRAY['owner'::character varying, 'admin'::character varying, 'member'::character varying, 'viewer'::character varying, 'agent'::character varying, 'service'::character varying])::text[]))),
    CONSTRAINT ck_role_assignments_scope_kind CHECK (((scope_kind)::text = ANY ((ARRAY['org'::character varying, 'team'::character varying])::text[])))
);

CREATE TABLE public.saml_replay_assertions (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    assertion_id character varying(255) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.sso_connections (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    protocol character varying(16) DEFAULT 'oidc'::character varying NOT NULL,
    allowed_domains character varying(1024) DEFAULT ''::character varying NOT NULL,
    oidc_issuer character varying(512),
    oidc_client_id character varying(512),
    oidc_client_secret_encrypted character varying(2048),
    saml_entity_id character varying(512),
    saml_sso_url character varying(1024),
    saml_x509_cert character varying(8192),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    enforced boolean DEFAULT false NOT NULL,
    groups_mapping jsonb,
    scim_enabled boolean DEFAULT false NOT NULL,
    scim_token_hash character varying(64),
    session_max_age_seconds integer DEFAULT 86400 NOT NULL,
    CONSTRAINT ck_sso_connections_session_max_age CHECK (((session_max_age_seconds >= 3600) AND (session_max_age_seconds <= 2592000)))
);

CREATE TABLE public.sso_domain_claims (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    domain character varying(253) NOT NULL,
    assigned_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_sso_domain_claims_domain_lower CHECK (((domain)::text = lower((domain)::text)))
);

CREATE TABLE public.sso_link_requests (
    id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    org_team_id uuid NOT NULL,
    provider character varying(64) NOT NULL,
    subject character varying(255) NOT NULL,
    email character varying(320) NOT NULL,
    email_verified boolean DEFAULT false NOT NULL,
    raw_profile jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone
);

CREATE TABLE public.storage_usage_snapshots (
    org_team_id uuid NOT NULL,
    drive_id uuid NOT NULL,
    day date NOT NULL,
    billable_bytes bigint NOT NULL,
    physical_bytes bigint NOT NULL,
    head_bytes bigint NOT NULL,
    version_bytes bigint NOT NULL,
    trash_bytes bigint NOT NULL,
    inline_bytes bigint NOT NULL,
    egress_authorized_bytes bigint NOT NULL,
    egress_reconciled_bytes bigint NOT NULL,
    request_count bigint NOT NULL
);

ALTER TABLE ONLY public.storage_usage_snapshots FORCE ROW LEVEL SECURITY;

CREATE TABLE public.team_allocations (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    team_id uuid NOT NULL,
    resource character varying(16) NOT NULL,
    limit_nanos bigint,
    limit_bytes bigint,
    "window" character varying(16) DEFAULT 'cycle'::character varying NOT NULL,
    created_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_team_allocations_limit_column CHECK (((((resource)::text = 'budget'::text) AND (limit_bytes IS NULL)) OR (((resource)::text = 'storage'::text) AND (limit_nanos IS NULL)))),
    CONSTRAINT ck_team_allocations_resource CHECK (((resource)::text = ANY ((ARRAY['budget'::character varying, 'storage'::character varying])::text[]))),
    CONSTRAINT ck_team_allocations_window CHECK ((("window")::text = ANY ((ARRAY['cycle'::character varying, 'none'::character varying])::text[])))
);

CREATE TABLE public.team_connections (
    id uuid NOT NULL,
    team_id uuid NOT NULL,
    plugin character varying(64) NOT NULL,
    handle character varying(128) NOT NULL,
    auth_mode character varying(16) DEFAULT 'shared'::character varying NOT NULL,
    auth_method character varying(64) DEFAULT ''::character varying NOT NULL,
    auto_add boolean DEFAULT false NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    shared_secret_encrypted character varying(8192),
    credential_version integer DEFAULT 0 NOT NULL,
    oauth_client_id character varying(512),
    oauth_client_secret_encrypted character varying(4096),
    oauth_config jsonb,
    last_verified_at timestamp with time zone,
    last_detail text DEFAULT ''::character varying NOT NULL,
    created_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    member_fields jsonb DEFAULT '[]'::jsonb NOT NULL,
    shared_values jsonb DEFAULT '{}'::jsonb NOT NULL,
    shared_consent jsonb,
    named_secrets_encrypted jsonb DEFAULT '{}'::jsonb NOT NULL,
    last_outcome character varying(32),
    last_verification_id uuid,
    verification_state character varying(16),
    credential_state character varying(16) DEFAULT 'present'::character varying NOT NULL,
    credential_state_reason character varying(512) DEFAULT ''::character varying NOT NULL,
    owner_user_id uuid,
    org_team_id uuid NOT NULL,
    CONSTRAINT ck_tc_credential_state CHECK (((credential_state)::text = ANY ((ARRAY['present'::character varying, 'unreadable'::character varying])::text[]))),
    CONSTRAINT ck_tc_last_outcome CHECK (((last_outcome IS NULL) OR ((last_outcome)::text = ANY ((ARRAY['ok'::character varying, 'invalid_credential'::character varying, 'permission'::character varying, 'unreachable'::character varying, 'timeout'::character varying, 'unsupported'::character varying, 'infrastructure'::character varying, 'error'::character varying])::text[])))),
    CONSTRAINT ck_tc_verification_state CHECK (((verification_state IS NULL) OR ((verification_state)::text = ANY ((ARRAY['queued'::character varying, 'running'::character varying])::text[]))))
);

ALTER TABLE ONLY public.team_connections FORCE ROW LEVEL SECURITY;

CREATE TABLE public.team_memberships (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    team_id uuid NOT NULL,
    role character varying(32) DEFAULT 'member'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    org_team_id uuid NOT NULL,
    CONSTRAINT ck_team_memberships_role CHECK (((role)::text = ANY ((ARRAY['member'::character varying, 'admin'::character varying])::text[])))
);

ALTER TABLE ONLY public.team_memberships FORCE ROW LEVEL SECURITY;

CREATE TABLE public.teams (
    id uuid NOT NULL,
    parent_team_id uuid,
    name character varying(255) NOT NULL,
    is_root boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_teams_root_has_no_parent CHECK ((((is_root = true) AND (parent_team_id IS NULL)) OR (is_root = false)))
);

CREATE TABLE public.user_bans (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    reason text DEFAULT ''::text NOT NULL,
    created_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    lifted_at timestamp with time zone,
    lifted_by_id uuid
);

CREATE TABLE public.user_oauth_tokens (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    team_connection_id uuid NOT NULL,
    access_token_encrypted character varying(8192) NOT NULL,
    refresh_token_encrypted character varying(8192),
    expires_at timestamp with time zone,
    scope character varying(1024) DEFAULT ''::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    state character varying(16) DEFAULT 'present'::character varying NOT NULL,
    state_reason character varying(512) DEFAULT ''::character varying NOT NULL,
    state_changed_at timestamp with time zone,
    refreshed_at timestamp with time zone,
    org_team_id uuid NOT NULL,
    CONSTRAINT ck_uot_state CHECK (((state)::text = ANY ((ARRAY['present'::character varying, 'needs_reauth'::character varying, 'revoked'::character varying])::text[])))
);

ALTER TABLE ONLY public.user_oauth_tokens FORCE ROW LEVEL SECURITY;

CREATE TABLE public.user_org_preferences (
    user_id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    preferences jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.user_preferences (
    user_id uuid NOT NULL,
    preferences jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.user_storage_limits (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    team_id uuid,
    user_id uuid NOT NULL,
    limit_bytes bigint NOT NULL,
    created_by_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.users (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    email character varying(320) NOT NULL,
    password_hash character varying(255),
    platform_role character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    email_verified_at timestamp with time zone,
    email_verification_token character varying(96),
    email_verification_expires_at timestamp with time zone,
    password_reset_token character varying(96),
    password_reset_expires_at timestamp with time zone,
    token_epoch timestamp with time zone DEFAULT '1970-01-01 00:00:00+00'::timestamp with time zone NOT NULL,
    first_name character varying(255) DEFAULT ''::character varying NOT NULL,
    last_name character varying(255) DEFAULT ''::character varying NOT NULL,
    email_domain character varying(255) DEFAULT ''::character varying NOT NULL,
    welcome_email_sent_at timestamp with time zone,
    sso_exempt boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    failed_login_count integer DEFAULT 0 NOT NULL,
    last_failed_login_at timestamp with time zone,
    locked_until timestamp with time zone,
    mfa_secret_encrypted character varying(512),
    mfa_enabled boolean DEFAULT false NOT NULL,
    mfa_backup_codes text,
    scim_external_id character varying(255),
    signup_ip character varying(45),
    last_login_ip character varying(45),
    mfa_last_used_counter bigint,
    deleted_at timestamp with time zone,
    provisioned_by character varying(16),
    CONSTRAINT ck_users_platform_role CHECK (((platform_role IS NULL) OR ((platform_role)::text = ANY ((ARRAY['alkera_support'::character varying, 'alkera_admin'::character varying])::text[]))))
);

COMMENT ON COLUMN public.users.is_active IS 'The identity-level platform disable: a disabled identity signs in nowhere. Only platform staff set it; an org offboards on org_memberships.status.';

CREATE TABLE public.workspace_machine_moves (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    from_org_machine_id uuid,
    to_org_machine_id uuid,
    state character varying(16) DEFAULT 'requested'::character varying NOT NULL,
    stop_running boolean DEFAULT false NOT NULL,
    error_code character varying(32) DEFAULT ''::character varying NOT NULL,
    error character varying(512) DEFAULT ''::character varying NOT NULL,
    requested_by uuid,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    flushed_before_switch boolean,
    CONSTRAINT ck_workspace_machine_moves_state CHECK (((state)::text = ANY ((ARRAY['requested'::character varying, 'draining'::character varying, 'switching'::character varying, 'waking'::character varying, 'done'::character varying, 'failed'::character varying, 'canceled'::character varying])::text[])))
);

ALTER TABLE ONLY public.workspace_machine_moves FORCE ROW LEVEL SECURITY;

CREATE TABLE public.workspace_objects (
    id uuid NOT NULL,
    org_team_id uuid NOT NULL,
    logical_id character varying(255) NOT NULL,
    namespace character varying(255) DEFAULT 'workspace'::character varying NOT NULL,
    type character varying(32) NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    status character varying(32) DEFAULT 'ready'::character varying NOT NULL,
    spec jsonb DEFAULT '{}'::jsonb NOT NULL,
    owner_user_id uuid NOT NULL,
    team_id uuid,
    visibility_scope character varying(128) NOT NULL,
    content_updated_at double precision DEFAULT 0 NOT NULL,
    deleted_at double precision DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_workspace_objects_status CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'pending_upload'::character varying, 'ready'::character varying, 'failed'::character varying])::text[]))),
    CONSTRAINT ck_workspace_objects_type CHECK (((type)::text = ANY ((ARRAY['chat'::character varying, 'query'::character varying, 'result'::character varying, 'board'::character varying, 'app'::character varying, 'report'::character varying, 'chat_template'::character varying, 'workspace'::character varying])::text[]))),
    CONSTRAINT ck_workspace_objects_version_positive CHECK ((version >= 1))
);

ALTER TABLE ONLY public.workspace_objects FORCE ROW LEVEL SECURITY;

CREATE TABLE public.ws_ticket_uses (
    jti character varying(64) NOT NULL,
    consumed_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.crdt_peers ALTER COLUMN loro_peer SET DEFAULT nextval('public.crdt_peer_seq'::regclass);

ALTER TABLE ONLY public.event_outbox ALTER COLUMN id SET DEFAULT nextval('public.event_outbox_id_seq'::regclass);

ALTER TABLE ONLY public.file_platform ALTER COLUMN id SET DEFAULT nextval('public.file_platform_id_seq'::regclass);

ALTER TABLE ONLY public.file_sweep_shards ALTER COLUMN shard SET DEFAULT nextval('public.file_sweep_shards_shard_seq'::regclass);

GRANT USAGE ON SCHEMA public TO alkera_files_app;
GRANT USAGE ON SCHEMA public TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_deletion_requests TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.account_export_requests TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.audit_logs TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.auth_refresh_tokens TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.auth_session_org_grants TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.auth_tokens TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.billing_model_routes TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.billing_models TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.billing_signup_clicks TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_attachments TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_messages TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_read_marks TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.chat_workspace_states TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.ci_tokens TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.compute_allocation_events TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.compute_allocations TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.compute_grants TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.compute_machine_types TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.compute_offering_orgs TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.compute_offerings TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.connection_inventory TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.connection_verifications TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.crash_reports TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.crdt_docs TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.crdt_peers TO alkera_tenant_app;

GRANT ALL ON SEQUENCE public.crdt_peer_seq TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.crdt_updates TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dedup_domains TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dedup_domains TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.deployment_health_checks TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.deployment_health_runs TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.device_authorizations TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.email_domain_bans TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.entitlement_grants TO alkera_tenant_app;

GRANT ALL ON SEQUENCE public.entitlement_grants_serial_seq TO alkera_tenant_app;

GRANT INSERT ON TABLE public.event_outbox TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.event_outbox TO alkera_tenant_app;

GRANT SELECT(id) ON TABLE public.event_outbox TO alkera_files_app;

GRANT SELECT(ts) ON TABLE public.event_outbox TO alkera_files_app;

GRANT USAGE ON SEQUENCE public.event_outbox_id_seq TO alkera_files_app;
GRANT ALL ON SEQUENCE public.event_outbox_id_seq TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_acl_members TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_acl_members TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_acls TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_acls TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_conflicts TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_conflicts TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_content_grants TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_content_grants TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_dir_stats TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_dir_stats TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_dir_stats_deltas TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_dir_stats_deltas TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_drives TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_drives TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_erasure_log TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_erasure_log TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_history TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_history TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_holds TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_holds TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_idempotency_keys TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_idempotency_keys TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_key_chunks TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_key_chunks TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_lease_epoch_hwm TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_lease_epoch_hwm TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_lease_live_entries TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_lease_live_entries TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_leases TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_leases TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_links TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_links TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_locks TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_locks TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_manifests TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_manifests TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_nodes TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_nodes TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_ops TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_ops TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_packs TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_packs TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_page_grants TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_page_grants TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_platform TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_platform TO alkera_tenant_app;

GRANT ALL ON SEQUENCE public.file_platform_id_seq TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_quarantine TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_quarantine TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_retention_labels TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_retention_labels TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_shares TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_shares TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_stage_jobs TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_stage_jobs TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_stars TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_stars TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_stores TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_stores TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_sweep_shards TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_sweep_shards TO alkera_tenant_app;

GRANT ALL ON SEQUENCE public.file_sweep_shards_shard_seq TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_trash_ops TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_trash_ops TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_upload_parts TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_upload_parts TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_upload_sessions TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_upload_sessions TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_versions TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.file_versions TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.identity_org_creations TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.identity_security_events TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.invitations TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.login_lockouts TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.machine_credentials TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.manifest_terms TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.manifest_terms TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.model_provider_configs TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notebook_edits TO alkera_tenant_app;

GRANT ALL ON SEQUENCE public.notebook_edits_id_seq TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notebook_epoch_tails TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notebook_kernels TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notebook_peers TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notebook_runs TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.oauth_identities TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.object_payload_rows TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_audit_events TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_compute_assignments TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_compute_settings TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_machine_audiences TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_machines TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_memberships TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_settings TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_storage_limits TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.org_sync_settings TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.personal_access_tokens TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.proxy_tokens TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.rate_limit_windows TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.realtime_docs TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.realtime_presence TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.role_assignments TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.saml_replay_assertions TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sso_connections TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sso_domain_claims TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sso_link_requests TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.storage_usage_snapshots TO alkera_files_app;
GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.storage_usage_snapshots TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.team_allocations TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.team_connections TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.team_memberships TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.teams TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.user_bans TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.user_oauth_tokens TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.user_org_preferences TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.user_preferences TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.user_storage_limits TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.users TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.workspace_machine_moves TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.workspace_objects TO alkera_tenant_app;

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.ws_ticket_uses TO alkera_tenant_app;

INSERT INTO public.file_platform (id, restore_generation, updated_at) VALUES (1, 0, now());

SELECT pg_catalog.setval('public.crdt_peer_seq', 1024, false);

SELECT pg_catalog.setval('public.entitlement_grants_serial_seq', 1, false);

SELECT pg_catalog.setval('public.event_outbox_id_seq', 1, false);

SELECT pg_catalog.setval('public.file_platform_id_seq', 1, false);

SELECT pg_catalog.setval('public.file_sweep_shards_shard_seq', 1, false);

SELECT pg_catalog.setval('public.notebook_edits_id_seq', 1, false);

ALTER TABLE ONLY public.account_deletion_requests
    ADD CONSTRAINT account_deletion_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.account_export_requests
    ADD CONSTRAINT account_export_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT auth_tokens_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.billing_model_routes
    ADD CONSTRAINT billing_model_routes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.billing_models
    ADD CONSTRAINT billing_models_pkey PRIMARY KEY (id);

ALTER TABLE public.file_leases
    ADD CONSTRAINT ck_file_leases_purpose CHECK (((purpose)::text = ANY ((ARRAY['mount'::character varying, 'box'::character varying, 'share'::character varying, 'chat'::character varying, 'workspace'::character varying])::text[]))) NOT VALID;

ALTER TABLE public.users
    ADD CONSTRAINT ck_users_provisioned_by CHECK (((provisioned_by IS NULL) OR ((provisioned_by)::text = ANY ((ARRAY['scim'::character varying, 'sso'::character varying])::text[])))) NOT VALID;

ALTER TABLE ONLY public.crash_reports
    ADD CONSTRAINT crash_reports_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.crdt_docs
    ADD CONSTRAINT crdt_docs_pkey PRIMARY KEY (org_id, doc_type, doc_id);

ALTER TABLE ONLY public.crdt_peers
    ADD CONSTRAINT crdt_peers_pkey PRIMARY KEY (loro_peer);

ALTER TABLE ONLY public.crdt_updates
    ADD CONSTRAINT crdt_updates_pkey PRIMARY KEY (org_id, doc_type, doc_id, epoch, log_seq);

ALTER TABLE ONLY public.dedup_domains
    ADD CONSTRAINT dedup_domains_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.deployment_health_checks
    ADD CONSTRAINT deployment_health_checks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.deployment_health_runs
    ADD CONSTRAINT deployment_health_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.device_authorizations
    ADD CONSTRAINT device_authorizations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.entitlement_grants
    ADD CONSTRAINT entitlement_grants_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.event_outbox
    ADD CONSTRAINT event_outbox_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_acl_members
    ADD CONSTRAINT file_acl_members_pkey PRIMARY KEY (acl_id, principal_kind, principal_id);

ALTER TABLE ONLY public.file_acls
    ADD CONSTRAINT file_acls_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_conflicts
    ADD CONSTRAINT file_conflicts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_content_grants
    ADD CONSTRAINT file_content_grants_pkey PRIMARY KEY (nonce);

ALTER TABLE ONLY public.file_dir_stats_deltas
    ADD CONSTRAINT file_dir_stats_deltas_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_dir_stats
    ADD CONSTRAINT file_dir_stats_pkey PRIMARY KEY (node_id);

ALTER TABLE ONLY public.file_drives
    ADD CONSTRAINT file_drives_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_erasure_log
    ADD CONSTRAINT file_erasure_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_history
    ADD CONSTRAINT file_history_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_holds
    ADD CONSTRAINT file_holds_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_key_chunks
    ADD CONSTRAINT file_key_chunks_pkey PRIMARY KEY (hmac_hash);

ALTER TABLE ONLY public.file_lease_epoch_hwm
    ADD CONSTRAINT file_lease_epoch_hwm_pkey PRIMARY KEY (node_id);

ALTER TABLE ONLY public.file_leases
    ADD CONSTRAINT file_leases_pkey PRIMARY KEY (node_id);

ALTER TABLE ONLY public.file_links
    ADD CONSTRAINT file_links_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_locks
    ADD CONSTRAINT file_locks_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_manifests
    ADD CONSTRAINT file_manifests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_nodes
    ADD CONSTRAINT file_nodes_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_ops
    ADD CONSTRAINT file_ops_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_packs
    ADD CONSTRAINT file_packs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_platform
    ADD CONSTRAINT file_platform_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_quarantine
    ADD CONSTRAINT file_quarantine_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_retention_labels
    ADD CONSTRAINT file_retention_labels_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_shares
    ADD CONSTRAINT file_shares_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_stage_jobs
    ADD CONSTRAINT file_stage_jobs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_stars
    ADD CONSTRAINT file_stars_pkey PRIMARY KEY (org_team_id, user_id, node_id);

ALTER TABLE ONLY public.file_stores
    ADD CONSTRAINT file_stores_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_sweep_shards
    ADD CONSTRAINT file_sweep_shards_pkey PRIMARY KEY (shard);

ALTER TABLE ONLY public.file_trash_ops
    ADD CONSTRAINT file_trash_ops_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_upload_parts
    ADD CONSTRAINT file_upload_parts_pkey PRIMARY KEY (session_id, part_no);

ALTER TABLE ONLY public.file_upload_sessions
    ADD CONSTRAINT file_upload_sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.file_versions
    ADD CONSTRAINT file_versions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.identity_org_creations
    ADD CONSTRAINT identity_org_creations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.machine_credentials
    ADD CONSTRAINT machine_credentials_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.manifest_terms
    ADD CONSTRAINT manifest_terms_pkey PRIMARY KEY (manifest_id, ord);

ALTER TABLE ONLY public.model_provider_configs
    ADD CONSTRAINT model_provider_configs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.notebook_edits
    ADD CONSTRAINT notebook_edits_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.notebook_epoch_tails
    ADD CONSTRAINT notebook_epoch_tails_pkey PRIMARY KEY (org_id, item_id, epoch);

ALTER TABLE ONLY public.notebook_kernels
    ADD CONSTRAINT notebook_kernels_pkey PRIMARY KEY (kernel_id);

ALTER TABLE ONLY public.notebook_peers
    ADD CONSTRAINT notebook_peers_pkey PRIMARY KEY (org_id, item_id, actor_key);

ALTER TABLE ONLY public.notebook_runs
    ADD CONSTRAINT notebook_runs_pkey PRIMARY KEY (run_id);

ALTER TABLE ONLY public.oauth_identities
    ADD CONSTRAINT oauth_identities_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.org_compute_assignments
    ADD CONSTRAINT org_compute_assignments_pkey PRIMARY KEY (org_team_id);

ALTER TABLE ONLY public.org_settings
    ADD CONSTRAINT org_settings_pkey PRIMARY KEY (org_team_id);

ALTER TABLE ONLY public.org_sync_settings
    ADD CONSTRAINT org_sync_settings_pkey PRIMARY KEY (org_team_id);

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_refresh_tokens
    ADD CONSTRAINT pk_auth_refresh_tokens PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_session_org_grants
    ADD CONSTRAINT pk_auth_session_org_grants PRIMARY KEY (id);

ALTER TABLE ONLY public.billing_signup_clicks
    ADD CONSTRAINT pk_billing_signup_clicks PRIMARY KEY (id);

ALTER TABLE ONLY public.chat_attachments
    ADD CONSTRAINT pk_chat_attachments PRIMARY KEY (chat_id, node_id);

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT pk_chat_messages PRIMARY KEY (id);

ALTER TABLE ONLY public.chat_read_marks
    ADD CONSTRAINT pk_chat_read_marks PRIMARY KEY (chat_id, user_id);

ALTER TABLE ONLY public.chat_workspace_states
    ADD CONSTRAINT pk_chat_workspace_states PRIMARY KEY (chat_id, user_id);

ALTER TABLE ONLY public.ci_tokens
    ADD CONSTRAINT pk_ci_tokens PRIMARY KEY (id);

ALTER TABLE ONLY public.compute_allocation_events
    ADD CONSTRAINT pk_compute_allocation_events PRIMARY KEY (id);

ALTER TABLE ONLY public.compute_allocations
    ADD CONSTRAINT pk_compute_allocations PRIMARY KEY (id);

ALTER TABLE ONLY public.compute_grants
    ADD CONSTRAINT pk_compute_grants PRIMARY KEY (id);

ALTER TABLE ONLY public.compute_machine_types
    ADD CONSTRAINT pk_compute_machine_types PRIMARY KEY (id);

ALTER TABLE ONLY public.compute_offering_orgs
    ADD CONSTRAINT pk_compute_offering_orgs PRIMARY KEY (offering_id, org_team_id);

ALTER TABLE ONLY public.compute_offerings
    ADD CONSTRAINT pk_compute_offerings PRIMARY KEY (id);

ALTER TABLE ONLY public.connection_inventory
    ADD CONSTRAINT pk_connection_inventory PRIMARY KEY (id);

ALTER TABLE ONLY public.email_domain_bans
    ADD CONSTRAINT pk_email_domain_bans PRIMARY KEY (id);

ALTER TABLE ONLY public.file_lease_live_entries
    ADD CONSTRAINT pk_file_lease_live_entries PRIMARY KEY (lease_node_id, node_id);

ALTER TABLE ONLY public.file_page_grants
    ADD CONSTRAINT pk_file_page_grants PRIMARY KEY (id);

ALTER TABLE ONLY public.identity_security_events
    ADD CONSTRAINT pk_identity_security_events PRIMARY KEY (id);

ALTER TABLE ONLY public.login_lockouts
    ADD CONSTRAINT pk_login_lockouts PRIMARY KEY (identifier_digest);

ALTER TABLE ONLY public.object_payload_rows
    ADD CONSTRAINT pk_object_payload_rows PRIMARY KEY (object_id, page);

ALTER TABLE ONLY public.org_audit_events
    ADD CONSTRAINT pk_org_audit_events PRIMARY KEY (id);

ALTER TABLE ONLY public.org_compute_settings
    ADD CONSTRAINT pk_org_compute_settings PRIMARY KEY (org_team_id);

ALTER TABLE ONLY public.org_machine_audiences
    ADD CONSTRAINT pk_org_machine_audiences PRIMARY KEY (id);

ALTER TABLE ONLY public.org_machines
    ADD CONSTRAINT pk_org_machines PRIMARY KEY (id);

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT pk_org_memberships PRIMARY KEY (id);

ALTER TABLE ONLY public.org_storage_limits
    ADD CONSTRAINT pk_org_storage_limits PRIMARY KEY (org_team_id);

ALTER TABLE ONLY public.proxy_tokens
    ADD CONSTRAINT pk_proxy_tokens PRIMARY KEY (id);

ALTER TABLE ONLY public.rate_limit_windows
    ADD CONSTRAINT pk_rate_limit_windows PRIMARY KEY (rate_class, key, window_start);

ALTER TABLE ONLY public.realtime_docs
    ADD CONSTRAINT pk_realtime_docs PRIMARY KEY (org_id, doc_type, doc_id);

ALTER TABLE ONLY public.realtime_presence
    ADD CONSTRAINT pk_realtime_presence PRIMARY KEY (doc_type, doc_id, peer_id);

ALTER TABLE ONLY public.saml_replay_assertions
    ADD CONSTRAINT pk_saml_replay_assertions PRIMARY KEY (id);

ALTER TABLE ONLY public.sso_connections
    ADD CONSTRAINT pk_sso_connections PRIMARY KEY (id);

ALTER TABLE ONLY public.team_allocations
    ADD CONSTRAINT pk_team_allocations PRIMARY KEY (id);

ALTER TABLE ONLY public.connection_verifications
    ADD CONSTRAINT pk_team_connection_probes PRIMARY KEY (id);

ALTER TABLE ONLY public.team_connections
    ADD CONSTRAINT pk_team_connections PRIMARY KEY (id);

ALTER TABLE ONLY public.user_bans
    ADD CONSTRAINT pk_user_bans PRIMARY KEY (id);

ALTER TABLE ONLY public.user_oauth_tokens
    ADD CONSTRAINT pk_user_oauth_tokens PRIMARY KEY (id);

ALTER TABLE ONLY public.user_org_preferences
    ADD CONSTRAINT pk_user_org_preferences PRIMARY KEY (user_id, org_team_id);

ALTER TABLE ONLY public.user_preferences
    ADD CONSTRAINT pk_user_preferences PRIMARY KEY (user_id);

ALTER TABLE ONLY public.user_storage_limits
    ADD CONSTRAINT pk_user_storage_limits PRIMARY KEY (id);

ALTER TABLE ONLY public.workspace_machine_moves
    ADD CONSTRAINT pk_workspace_machine_moves PRIMARY KEY (id);

ALTER TABLE ONLY public.workspace_objects
    ADD CONSTRAINT pk_workspace_objects PRIMARY KEY (id);

ALTER TABLE ONLY public.ws_ticket_uses
    ADD CONSTRAINT pk_ws_ticket_uses PRIMARY KEY (jti);

ALTER TABLE ONLY public.role_assignments
    ADD CONSTRAINT role_assignments_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sso_domain_claims
    ADD CONSTRAINT sso_domain_claims_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sso_link_requests
    ADD CONSTRAINT sso_link_requests_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.storage_usage_snapshots
    ADD CONSTRAINT storage_usage_snapshots_pkey PRIMARY KEY (org_team_id, drive_id, day);

ALTER TABLE ONLY public.team_memberships
    ADD CONSTRAINT team_memberships_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.teams
    ADD CONSTRAINT teams_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.auth_session_org_grants
    ADD CONSTRAINT uq_auth_session_org_grants UNIQUE NULLS NOT DISTINCT (family_id, org_team_id, method);

ALTER TABLE ONLY public.billing_signup_clicks
    ADD CONSTRAINT uq_billing_signup_clicks_visit_token UNIQUE (visit_token);

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT uq_chat_messages_chat_event UNIQUE (chat_id, event_id);

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT uq_chat_messages_chat_seq UNIQUE (chat_id, seq);

ALTER TABLE ONLY public.compute_machine_types
    ADD CONSTRAINT uq_compute_machine_types_provider_type UNIQUE (provider, provider_type_id);

ALTER TABLE ONLY public.connection_inventory
    ADD CONSTRAINT uq_connection_inventory_user_org_workspace_plugin UNIQUE (user_id, org_team_id, workspace_key, plugin);

ALTER TABLE ONLY public.connection_inventory
    ADD CONSTRAINT uq_connection_inventory_user_workspace_plugin UNIQUE (user_id, workspace_key, plugin);

ALTER TABLE ONLY public.dedup_domains
    ADD CONSTRAINT uq_dedup_domains_org_region_store UNIQUE (org_team_id, region, store_id);

ALTER TABLE ONLY public.entitlement_grants
    ADD CONSTRAINT uq_entitlement_grants_serial UNIQUE (serial);

ALTER TABLE ONLY public.event_outbox
    ADD CONSTRAINT uq_event_outbox_event_id UNIQUE (event_id);

ALTER TABLE ONLY public.file_acls
    ADD CONSTRAINT uq_file_acls_body_hash UNIQUE (body_hash);

ALTER TABLE ONLY public.file_drives
    ADD CONSTRAINT uq_file_drives_org_team_kind UNIQUE (org_team_id, kind);

ALTER TABLE ONLY public.file_history
    ADD CONSTRAINT uq_file_history_node_seq UNIQUE (node_id, seq);

ALTER TABLE ONLY public.file_idempotency_keys
    ADD CONSTRAINT uq_file_idempotency_keys_org_key_principal_route PRIMARY KEY (org_team_id, key, principal_id, route);

ALTER TABLE ONLY public.file_links
    ADD CONSTRAINT uq_file_links_token_hash UNIQUE (token_hash);

ALTER TABLE ONLY public.file_manifests
    ADD CONSTRAINT uq_file_manifests_domain_content_hash UNIQUE (dedup_domain_id, content_hash);

ALTER TABLE ONLY public.file_nodes
    ADD CONSTRAINT uq_file_nodes_drive_ino UNIQUE (drive_id, ino);

ALTER TABLE ONLY public.file_packs
    ADD CONSTRAINT uq_file_packs_domain_hash UNIQUE (dedup_domain_id, hash);

ALTER TABLE ONLY public.file_page_grants
    ADD CONSTRAINT uq_file_page_grants_nonce UNIQUE (nonce);

ALTER TABLE ONLY public.file_retention_labels
    ADD CONSTRAINT uq_file_retention_labels_org_name UNIQUE (org_team_id, name);

ALTER TABLE ONLY public.file_stores
    ADD CONSTRAINT uq_file_stores_driver_endpoint_bucket UNIQUE (driver, endpoint, bucket);

ALTER TABLE ONLY public.file_versions
    ADD CONSTRAINT uq_file_versions_node_seq UNIQUE (node_id, seq);

ALTER TABLE ONLY public.model_provider_configs
    ADD CONSTRAINT uq_model_provider_configs_org_provider UNIQUE (org_team_id, provider);

ALTER TABLE ONLY public.notebook_edits
    ADD CONSTRAINT uq_notebook_edits_cell_actor UNIQUE (org_id, item_id, cell_id, actor_key);

ALTER TABLE ONLY public.oauth_identities
    ADD CONSTRAINT uq_oauth_identity_provider_subject UNIQUE (provider, subject);

ALTER TABLE ONLY public.oauth_identities
    ADD CONSTRAINT uq_oauth_identity_provider_user UNIQUE (provider, user_id);

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT uq_org_memberships_user_org UNIQUE (user_id, org_team_id);

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT uq_personal_access_tokens_token_hash UNIQUE (token_hash);

ALTER TABLE ONLY public.saml_replay_assertions
    ADD CONSTRAINT uq_saml_replay_org_assertion UNIQUE (org_team_id, assertion_id);

ALTER TABLE ONLY public.sso_domain_claims
    ADD CONSTRAINT uq_sso_domain_claims_domain UNIQUE (domain);

ALTER TABLE ONLY public.sso_link_requests
    ADD CONSTRAINT uq_sso_link_requests_token_hash UNIQUE (token_hash);

ALTER TABLE ONLY public.team_allocations
    ADD CONSTRAINT uq_team_allocations_team_resource UNIQUE (team_id, resource);

ALTER TABLE ONLY public.team_memberships
    ADD CONSTRAINT uq_team_memberships_user_team UNIQUE (user_id, team_id);

ALTER TABLE ONLY public.user_oauth_tokens
    ADD CONSTRAINT uq_user_oauth_tokens_user_connection UNIQUE (user_id, team_connection_id);

ALTER TABLE ONLY public.workspace_objects
    ADD CONSTRAINT uq_workspace_objects_org_namespace_logical UNIQUE (org_team_id, namespace, logical_id);

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);

CREATE INDEX ix_account_deletion_requests_due ON public.account_deletion_requests USING btree (purge_after) WHERE ((status)::text = 'scheduled'::text);

CREATE INDEX ix_account_deletion_requests_user_id ON public.account_deletion_requests USING btree (user_id);

CREATE INDEX ix_account_export_requests_status ON public.account_export_requests USING btree (status);

CREATE INDEX ix_account_export_requests_user_created ON public.account_export_requests USING btree (user_id, created_at);

CREATE INDEX ix_audit_logs_actor_id ON public.audit_logs USING btree (actor_id);

CREATE INDEX ix_audit_logs_created_at ON public.audit_logs USING btree (created_at);

CREATE INDEX ix_auth_refresh_tokens_absolute_expires_at ON public.auth_refresh_tokens USING btree (absolute_expires_at);

CREATE UNIQUE INDEX ix_auth_refresh_tokens_access_jti ON public.auth_refresh_tokens USING btree (access_jti);

CREATE INDEX ix_auth_refresh_tokens_active_org ON public.auth_refresh_tokens USING btree (active_org_team_id);

CREATE INDEX ix_auth_refresh_tokens_family_id ON public.auth_refresh_tokens USING btree (family_id);

CREATE INDEX ix_auth_refresh_tokens_successor ON public.auth_refresh_tokens USING btree (successor_id);

CREATE UNIQUE INDEX ix_auth_refresh_tokens_token_hash ON public.auth_refresh_tokens USING btree (token_hash);

CREATE INDEX ix_auth_refresh_tokens_user_id ON public.auth_refresh_tokens USING btree (user_id);

CREATE INDEX ix_auth_session_org_grants_family ON public.auth_session_org_grants USING btree (family_id);

CREATE INDEX ix_auth_tokens_expires_at ON public.auth_tokens USING btree (expires_at);

CREATE UNIQUE INDEX ix_auth_tokens_jti ON public.auth_tokens USING btree (jti);

CREATE INDEX ix_auth_tokens_membership ON public.auth_tokens USING btree (membership_id);

CREATE INDEX ix_auth_tokens_refresh_family ON public.auth_tokens USING btree (refresh_family_id);

CREATE INDEX ix_auth_tokens_user_id ON public.auth_tokens USING btree (user_id);

CREATE INDEX ix_billing_model_routes_model_id ON public.billing_model_routes USING btree (model_id);

CREATE INDEX ix_billing_signup_clicks_created_at ON public.billing_signup_clicks USING btree (created_at);

CREATE INDEX ix_billing_signup_clicks_user_id ON public.billing_signup_clicks USING btree (user_id);

CREATE INDEX ix_chat_attachments_chat_id_position ON public.chat_attachments USING btree (chat_id, "position", node_id);

CREATE INDEX ix_chat_messages_org_team_id ON public.chat_messages USING btree (org_team_id);

CREATE INDEX ix_chat_read_marks_user ON public.chat_read_marks USING btree (user_id);

CREATE INDEX ix_chat_workspace_states_user ON public.chat_workspace_states USING btree (user_id);

CREATE INDEX ix_ci_tokens_org_team_id ON public.ci_tokens USING btree (org_team_id);

CREATE UNIQUE INDEX ix_ci_tokens_token_hash ON public.ci_tokens USING btree (token_hash);

CREATE INDEX ix_compute_allocation_events_allocation_at ON public.compute_allocation_events USING btree (allocation_id, at);

CREATE INDEX ix_compute_allocations_grant_id ON public.compute_allocations USING btree (grant_id);

CREATE INDEX ix_compute_allocations_machine_type_id ON public.compute_allocations USING btree (machine_type_id);

CREATE INDEX ix_compute_allocations_org_machine_id ON public.compute_allocations USING btree (org_machine_id);

CREATE INDEX ix_compute_allocations_org_team_id ON public.compute_allocations USING btree (org_team_id);

CREATE INDEX ix_compute_allocations_provider_machine_id ON public.compute_allocations USING btree (provider_machine_id);

CREATE INDEX ix_compute_allocations_user_state ON public.compute_allocations USING btree (user_id, state);

CREATE INDEX ix_compute_grants_expires_at ON public.compute_grants USING btree (expires_at);

CREATE INDEX ix_compute_grants_org_team_id ON public.compute_grants USING btree (org_team_id);

CREATE INDEX ix_compute_offering_orgs_org_team_id ON public.compute_offering_orgs USING btree (org_team_id);

CREATE INDEX ix_compute_offerings_machine_type_id ON public.compute_offerings USING btree (machine_type_id);

CREATE INDEX ix_connection_inventory_org_team_id ON public.connection_inventory USING btree (org_team_id);

CREATE INDEX ix_connection_inventory_user_id ON public.connection_inventory USING btree (user_id);

CREATE INDEX ix_connection_verifications_team_id ON public.connection_verifications USING btree (team_id);

CREATE INDEX ix_crash_reports_component ON public.crash_reports USING btree (component);

CREATE INDEX ix_crash_reports_created_at ON public.crash_reports USING btree (created_at);

CREATE INDEX ix_crash_reports_org_team_id ON public.crash_reports USING btree (org_team_id);

CREATE INDEX ix_crash_reports_read_at ON public.crash_reports USING btree (read_at);

CREATE INDEX ix_crash_reports_user_id ON public.crash_reports USING btree (user_id);

CREATE INDEX ix_crdt_docs_org_id ON public.crdt_docs USING btree (org_id);

CREATE INDEX ix_crdt_docs_unsaved ON public.crdt_docs USING btree (save_retry_at, updated_at) WHERE ((source_etag IS NOT NULL) AND (quarantined_at IS NULL) AND (projection ? 'sha256'::text) AND ((projection ->> 'sha256'::text) <> (source_sha256)::text));

CREATE INDEX ix_crdt_peers_doc_user ON public.crdt_peers USING btree (org_id, doc_type, doc_id, user_id);

CREATE INDEX ix_crdt_peers_last_seen_at ON public.crdt_peers USING btree (last_seen_at);

CREATE INDEX ix_crdt_peers_user_id ON public.crdt_peers USING btree (user_id);

CREATE INDEX ix_crdt_updates_author_user_id ON public.crdt_updates USING btree (author_user_id);

CREATE INDEX ix_cv_connection_finished ON public.connection_verifications USING btree (connection_id, finished_at);

CREATE INDEX ix_cv_created ON public.connection_verifications USING btree (created_at);

CREATE INDEX ix_cv_state_created ON public.connection_verifications USING btree (state, created_at);

CREATE INDEX ix_dedup_domains_org_team_id ON public.dedup_domains USING btree (org_team_id);

CREATE INDEX ix_deployment_health_checks_org_team_id ON public.deployment_health_checks USING btree (org_team_id);

CREATE INDEX ix_device_auth_expires_at ON public.device_authorizations USING btree (expires_at);

CREATE UNIQUE INDEX ix_device_authorizations_device_code_hash ON public.device_authorizations USING btree (device_code_hash);

CREATE INDEX ix_device_authorizations_user_id ON public.device_authorizations USING btree (user_id);

CREATE INDEX ix_email_domain_bans_domain ON public.email_domain_bans USING btree (domain);

CREATE INDEX ix_entitlement_grants_org_team_id ON public.entitlement_grants USING btree (org_team_id);

CREATE INDEX ix_event_outbox_org_id_id ON public.event_outbox USING btree (org_id, id);

CREATE INDEX ix_file_acl_members_org_principal ON public.file_acl_members USING btree (org_team_id, principal_kind, principal_id);

CREATE INDEX ix_file_acls_org_team_id ON public.file_acls USING btree (org_team_id);

CREATE INDEX ix_file_conflicts_node_id ON public.file_conflicts USING btree (node_id);

CREATE INDEX ix_file_conflicts_org_state ON public.file_conflicts USING btree (org_team_id, state) WHERE ((state)::text = ANY ((ARRAY['open'::character varying, 'auto'::character varying])::text[]));

CREATE INDEX ix_file_content_grants_expires_at ON public.file_content_grants USING btree (expires_at);

CREATE INDEX ix_file_content_grants_version_id ON public.file_content_grants USING btree (version_id);

CREATE INDEX ix_file_dir_stats_deltas_node_at ON public.file_dir_stats_deltas USING btree (node_id, at);

CREATE INDEX ix_file_dir_stats_org_team_id ON public.file_dir_stats USING btree (org_team_id);

CREATE INDEX ix_file_drives_root_node_id ON public.file_drives USING btree (root_node_id);

CREATE INDEX ix_file_erasure_log_org_requested_at ON public.file_erasure_log USING btree (org_team_id, requested_at);

CREATE INDEX ix_file_history_org_at ON public.file_history USING btree (org_team_id, at);

CREATE INDEX ix_file_history_org_principal_at ON public.file_history USING btree (org_team_id, acting_principal, at);

CREATE INDEX ix_file_holds_node_id ON public.file_holds USING btree (node_id);

CREATE INDEX ix_file_holds_org_live ON public.file_holds USING btree (org_team_id) WHERE (released_at IS NULL);

CREATE INDEX ix_file_idempotency_keys_org_expires_at ON public.file_idempotency_keys USING btree (org_team_id, expires_at);

CREATE INDEX ix_file_key_chunks_domain_last_seen ON public.file_key_chunks USING btree (dedup_domain_id, last_seen_at);

CREATE INDEX ix_file_key_chunks_pack_id ON public.file_key_chunks USING btree (pack_id);

CREATE INDEX ix_file_lease_epoch_hwm_org_team_id ON public.file_lease_epoch_hwm USING btree (org_team_id);

CREATE INDEX ix_file_lease_live_entries_node ON public.file_lease_live_entries USING btree (node_id);

CREATE INDEX ix_file_lease_live_entries_org_lease ON public.file_lease_live_entries USING btree (org_team_id, lease_node_id);

CREATE INDEX ix_file_leases_holder_instance ON public.file_leases USING btree (holder_principal_id, holder_instance_id);

CREATE INDEX ix_file_leases_live_expires_at ON public.file_leases USING btree (expires_at) WHERE (released_at IS NULL);

CREATE INDEX ix_file_leases_org_team_id ON public.file_leases USING btree (org_team_id);

CREATE INDEX ix_file_links_node_id ON public.file_links USING btree (node_id);

CREATE INDEX ix_file_links_org_team_id ON public.file_links USING btree (org_team_id);

CREATE INDEX ix_file_locks_expires_at ON public.file_locks USING btree (expires_at);

CREATE INDEX ix_file_locks_node_id ON public.file_locks USING btree (node_id);

CREATE INDEX ix_file_locks_org_team_id ON public.file_locks USING btree (org_team_id);

CREATE INDEX ix_file_manifests_org_team_id ON public.file_manifests USING btree (org_team_id);

CREATE INDEX ix_file_nodes_drive_created_by_live_files ON public.file_nodes USING btree (drive_id, created_by) INCLUDE (size, path_ids) WHERE ((trashed_at IS NULL) AND ((kind)::text = 'file'::text));

CREATE INDEX ix_file_nodes_drive_mime_class ON public.file_nodes USING btree (drive_id, mime_class);

CREATE INDEX ix_file_nodes_drive_parent_created_by ON public.file_nodes USING btree (drive_id, parent_id, created_by);

CREATE INDEX ix_file_nodes_drive_trashed_at ON public.file_nodes USING btree (drive_id, trashed_at) WHERE (trashed_at IS NOT NULL);

CREATE INDEX ix_file_nodes_listing ON public.file_nodes USING btree (drive_id, parent_id, name_key) INCLUDE (id, name, kind, size, mtime_ns, etag, head_version_id, acl_id);

CREATE INDEX ix_file_nodes_name_key_trgm ON public.file_nodes USING gin (name_key public.gin_trgm_ops);

CREATE INDEX ix_file_nodes_org_team_id ON public.file_nodes USING btree (org_team_id);

CREATE INDEX ix_file_nodes_parent_name_key ON public.file_nodes USING btree (parent_id, name_key);

CREATE INDEX ix_file_nodes_path_ids ON public.file_nodes USING gist (public.subpath(path_ids, 0, 128) public.gist_ltree_ops (siglen='32'));

CREATE INDEX ix_file_nodes_target_id ON public.file_nodes USING btree (target_id);

CREATE INDEX ix_file_ops_org_drive_created ON public.file_ops USING btree (org_team_id, drive_id, created_at);

CREATE INDEX ix_file_ops_queued_unclaimed ON public.file_ops USING btree (created_at) WHERE (((state)::text = 'queued'::text) AND (heartbeat_at IS NULL));

CREATE INDEX ix_file_ops_result_node_id ON public.file_ops USING btree (result_node_id);

CREATE INDEX ix_file_ops_running_heartbeat ON public.file_ops USING btree (heartbeat_at) WHERE ((state)::text = 'running'::text);

CREATE INDEX ix_file_ops_undoable_until ON public.file_ops USING btree (undoable_until) WHERE (undoable_until IS NOT NULL);

CREATE INDEX ix_file_packs_domain_live_bytes ON public.file_packs USING btree (dedup_domain_id, live_bytes);

CREATE INDEX ix_file_packs_org_team_id ON public.file_packs USING btree (org_team_id);

CREATE INDEX ix_file_page_grants_expiry ON public.file_page_grants USING btree (expires_at) WHERE (revoked_at IS NULL);

CREATE INDEX ix_file_page_grants_root ON public.file_page_grants USING btree (root_node_id);

CREATE INDEX ix_file_quarantine_org_kind_first_seen ON public.file_quarantine USING btree (org_team_id, kind, first_seen_at);

CREATE INDEX ix_file_quarantine_ref_id ON public.file_quarantine USING btree (ref_id);

CREATE INDEX ix_file_shares_node_id ON public.file_shares USING btree (node_id);

CREATE INDEX ix_file_shares_org_principal ON public.file_shares USING btree (org_team_id, principal_kind, principal_id);

CREATE INDEX ix_file_stage_jobs_lease_node_id ON public.file_stage_jobs USING btree (lease_node_id);

CREATE INDEX ix_file_stage_jobs_org_state ON public.file_stage_jobs USING btree (org_team_id, state);

CREATE INDEX ix_file_stars_node_id ON public.file_stars USING btree (node_id);

CREATE INDEX ix_file_stars_org_user ON public.file_stars USING btree (org_team_id, user_id);

CREATE INDEX ix_file_sweep_shards_expires_at ON public.file_sweep_shards USING btree (expires_at);

CREATE INDEX ix_file_trash_ops_org_drive ON public.file_trash_ops USING btree (org_team_id, drive_id);

CREATE INDEX ix_file_trash_ops_purge_after ON public.file_trash_ops USING btree (purge_after);

CREATE INDEX ix_file_upload_parts_org_team_id ON public.file_upload_parts USING btree (org_team_id);

CREATE INDEX ix_file_upload_sessions_committing ON public.file_upload_sessions USING btree (expires_at) WHERE ((state)::text = 'committing'::text);

CREATE INDEX ix_file_upload_sessions_live_expires_at ON public.file_upload_sessions USING btree (expires_at) WHERE ((state)::text = ANY ((ARRAY['open'::character varying, 'uploading'::character varying, 'committing'::character varying])::text[]));

CREATE INDEX ix_file_upload_sessions_node_id ON public.file_upload_sessions USING btree (node_id);

CREATE INDEX ix_file_upload_sessions_org_drive ON public.file_upload_sessions USING btree (org_team_id, drive_id);

CREATE INDEX ix_file_upload_sessions_parent_id ON public.file_upload_sessions USING btree (parent_id);

CREATE INDEX ix_file_versions_expires_at ON public.file_versions USING btree (expires_at) WHERE (expires_at IS NOT NULL);

CREATE INDEX ix_file_versions_org_content_hash ON public.file_versions USING btree (org_team_id, content_hash);

CREATE INDEX ix_identity_org_creations_user_created ON public.identity_org_creations USING btree (user_id, created_at);

CREATE INDEX ix_identity_security_events_created_at ON public.identity_security_events USING btree (created_at);

CREATE INDEX ix_identity_security_events_user_created ON public.identity_security_events USING btree (user_id, created_at DESC);

CREATE INDEX ix_invitations_email ON public.invitations USING btree (email);

CREATE INDEX ix_invitations_team_id ON public.invitations USING btree (team_id);

CREATE UNIQUE INDEX ix_invitations_token ON public.invitations USING btree (token);

CREATE INDEX ix_login_lockouts_last_failed_at ON public.login_lockouts USING btree (last_failed_at);

CREATE INDEX ix_machine_credentials_machine_id ON public.machine_credentials USING btree (machine_id);

CREATE INDEX ix_machine_credentials_org_team_id ON public.machine_credentials USING btree (org_team_id);

CREATE UNIQUE INDEX ix_machine_credentials_token_hash ON public.machine_credentials USING btree (token_hash);

CREATE INDEX ix_manifest_terms_org_team_id ON public.manifest_terms USING btree (org_team_id);

CREATE INDEX ix_manifest_terms_pack_id ON public.manifest_terms USING btree (pack_id);

CREATE INDEX ix_model_provider_configs_org_team_id ON public.model_provider_configs USING btree (org_team_id);

CREATE INDEX ix_notebook_edits_org_item_last ON public.notebook_edits USING btree (org_id, item_id, last_at);

CREATE INDEX ix_notebook_kernels_org_item ON public.notebook_kernels USING btree (org_id, item_id);

CREATE INDEX ix_notebook_runs_org_item_created ON public.notebook_runs USING btree (org_id, item_id, created_at);

CREATE INDEX ix_oauth_identities_user_id ON public.oauth_identities USING btree (user_id);

CREATE INDEX ix_object_payload_rows_org_team_id ON public.object_payload_rows USING btree (org_team_id);

CREATE INDEX ix_org_audit_events_actor_id ON public.org_audit_events USING btree (actor_id);

CREATE INDEX ix_org_audit_events_created_at ON public.org_audit_events USING btree (created_at);

CREATE INDEX ix_org_audit_events_org_created ON public.org_audit_events USING btree (org_team_id, created_at);

CREATE INDEX ix_org_audit_events_org_team_id ON public.org_audit_events USING btree (org_team_id);

CREATE INDEX ix_org_compute_settings_default_org_machine_id ON public.org_compute_settings USING btree (default_org_machine_id);

CREATE INDEX ix_org_machine_audiences_org_team_id ON public.org_machine_audiences USING btree (org_team_id);

CREATE INDEX ix_org_machine_audiences_team_id ON public.org_machine_audiences USING btree (team_id);

CREATE INDEX ix_org_machine_audiences_user_id ON public.org_machine_audiences USING btree (user_id);

CREATE INDEX ix_org_machines_offering_id ON public.org_machines USING btree (offering_id);

CREATE INDEX ix_org_machines_org_team_id ON public.org_machines USING btree (org_team_id);

CREATE INDEX ix_org_machines_owner_team_id ON public.org_machines USING btree (owner_team_id);

CREATE INDEX ix_org_memberships_org_status ON public.org_memberships USING btree (org_team_id, status);

CREATE INDEX ix_org_memberships_user ON public.org_memberships USING btree (user_id);

CREATE INDEX ix_personal_access_tokens_org_team_id ON public.personal_access_tokens USING btree (org_team_id);

CREATE INDEX ix_personal_access_tokens_token_hash ON public.personal_access_tokens USING btree (token_hash);

CREATE INDEX ix_personal_access_tokens_user_id ON public.personal_access_tokens USING btree (user_id);

CREATE INDEX ix_proxy_tokens_org_team_id ON public.proxy_tokens USING btree (org_team_id);

CREATE UNIQUE INDEX ix_proxy_tokens_token_hash ON public.proxy_tokens USING btree (token_hash);

CREATE INDEX ix_rate_limit_windows_window_start ON public.rate_limit_windows USING btree (window_start);

CREATE INDEX ix_realtime_docs_org_id ON public.realtime_docs USING btree (org_id);

CREATE INDEX ix_realtime_docs_owner_user_id ON public.realtime_docs USING btree (owner_user_id);

CREATE INDEX ix_realtime_presence_last_seen_at ON public.realtime_presence USING btree (last_seen_at);

CREATE INDEX ix_realtime_presence_user_id ON public.realtime_presence USING btree (user_id);

CREATE INDEX ix_role_assignments_org_team_id ON public.role_assignments USING btree (org_team_id);

CREATE INDEX ix_role_assignments_principal ON public.role_assignments USING btree (principal_kind, principal_id);

CREATE INDEX ix_role_assignments_scope ON public.role_assignments USING btree (scope_kind, scope_id);

CREATE INDEX ix_saml_replay_assertions_expires_at ON public.saml_replay_assertions USING btree (expires_at);

CREATE INDEX ix_saml_replay_assertions_org_team_id ON public.saml_replay_assertions USING btree (org_team_id);

CREATE UNIQUE INDEX ix_sso_connections_org_team_id ON public.sso_connections USING btree (org_team_id);

CREATE UNIQUE INDEX ix_sso_connections_scim_token_hash ON public.sso_connections USING btree (scim_token_hash);

CREATE INDEX ix_sso_domain_claims_org_team_id ON public.sso_domain_claims USING btree (org_team_id);

CREATE INDEX ix_sso_link_requests_expires_at ON public.sso_link_requests USING btree (expires_at);

CREATE INDEX ix_sso_link_requests_org_team_id ON public.sso_link_requests USING btree (org_team_id);

CREATE INDEX ix_storage_usage_snapshots_org_day ON public.storage_usage_snapshots USING btree (org_team_id, day);

CREATE INDEX ix_team_allocations_org_team_id ON public.team_allocations USING btree (org_team_id);

CREATE INDEX ix_team_connections_org_team_id ON public.team_connections USING btree (org_team_id);

CREATE INDEX ix_team_connections_owner_user_id ON public.team_connections USING btree (owner_user_id);

CREATE INDEX ix_team_connections_team_id ON public.team_connections USING btree (team_id);

CREATE INDEX ix_team_memberships_org_team_id ON public.team_memberships USING btree (org_team_id);

CREATE INDEX ix_team_memberships_team_id ON public.team_memberships USING btree (team_id);

CREATE INDEX ix_team_memberships_user_id ON public.team_memberships USING btree (user_id);

CREATE INDEX ix_team_memberships_user_org ON public.team_memberships USING btree (user_id, org_team_id);

CREATE INDEX ix_teams_parent_team_id ON public.teams USING btree (parent_team_id);

CREATE INDEX ix_uot_state_expires ON public.user_oauth_tokens USING btree (state, expires_at);

CREATE INDEX ix_user_bans_user_id ON public.user_bans USING btree (user_id);

CREATE INDEX ix_user_oauth_tokens_org_team_id ON public.user_oauth_tokens USING btree (org_team_id);

CREATE INDEX ix_user_oauth_tokens_team_connection_id ON public.user_oauth_tokens USING btree (team_connection_id);

CREATE INDEX ix_user_oauth_tokens_user_id ON public.user_oauth_tokens USING btree (user_id);

CREATE INDEX ix_user_org_preferences_org_team_id ON public.user_org_preferences USING btree (org_team_id);

CREATE INDEX ix_user_storage_limits_user_id ON public.user_storage_limits USING btree (user_id);

CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);

CREATE INDEX ix_users_email_domain ON public.users USING btree (email_domain);

CREATE UNIQUE INDEX ix_users_email_verification_token ON public.users USING btree (email_verification_token);

CREATE INDEX ix_users_org_team_id ON public.users USING btree (org_team_id);

CREATE UNIQUE INDEX ix_users_password_reset_token ON public.users USING btree (password_reset_token);

CREATE INDEX ix_users_scim_external_id ON public.users USING btree (scim_external_id);

CREATE INDEX ix_workspace_machine_moves_from_org_machine_id ON public.workspace_machine_moves USING btree (from_org_machine_id);

CREATE INDEX ix_workspace_machine_moves_org_team_id ON public.workspace_machine_moves USING btree (org_team_id);

CREATE INDEX ix_workspace_machine_moves_to_org_machine_id ON public.workspace_machine_moves USING btree (to_org_machine_id);

CREATE INDEX ix_workspace_machine_moves_workspace_id ON public.workspace_machine_moves USING btree (workspace_id);

CREATE INDEX ix_workspace_objects_chat_machine ON public.workspace_objects USING btree (((spec ->> 'machine_id'::text))) WHERE (((type)::text = 'chat'::text) AND (deleted_at = (0)::double precision));

CREATE INDEX ix_workspace_objects_chat_unadopted ON public.workspace_objects USING btree (id) WHERE (((type)::text = 'chat'::text) AND (deleted_at = (0)::double precision) AND ((spec ->> 'workspace_id'::text) IS NULL));

CREATE INDEX ix_workspace_objects_chat_workspace ON public.workspace_objects USING btree (((spec ->> 'workspace_id'::text))) WHERE (((type)::text = 'chat'::text) AND (deleted_at = (0)::double precision) AND ((spec ->> 'workspace_id'::text) IS NOT NULL));

CREATE INDEX ix_workspace_objects_ending ON public.workspace_objects USING btree (id) WHERE ((spec ->> 'ending_workspace_id'::text) IS NOT NULL);

CREATE INDEX ix_workspace_objects_org_type_created ON public.workspace_objects USING btree (org_team_id, type, created_at);

CREATE INDEX ix_workspace_objects_owner_user_id ON public.workspace_objects USING btree (owner_user_id);

CREATE INDEX ix_workspace_objects_workspace_machine ON public.workspace_objects USING btree (((spec ->> 'machine_id'::text))) WHERE (((type)::text = 'workspace'::text) AND (deleted_at = (0)::double precision) AND ((spec ->> 'binding_authority'::text) = 'workspace'::text));

CREATE INDEX ix_ws_ticket_uses_consumed_at ON public.ws_ticket_uses USING btree (consumed_at);

CREATE UNIQUE INDEX uq_account_deletion_requests_user_scheduled ON public.account_deletion_requests USING btree (user_id) WHERE ((status)::text = 'scheduled'::text);

CREATE UNIQUE INDEX uq_device_auth_pending_user_code ON public.device_authorizations USING btree (user_code) WHERE ((status)::text = 'pending'::text);

CREATE UNIQUE INDEX uq_email_domain_bans_active_domain ON public.email_domain_bans USING btree (domain) WHERE (lifted_at IS NULL);

CREATE UNIQUE INDEX uq_file_nodes_parent_name_live ON public.file_nodes USING btree (parent_id, name) WHERE (trashed_at IS NULL);

CREATE UNIQUE INDEX uq_file_ops_org_idempotency_key ON public.file_ops USING btree (org_team_id, idempotency_key) WHERE (idempotency_key IS NOT NULL);

CREATE UNIQUE INDEX uq_file_shares_live_principal ON public.file_shares USING btree (node_id, principal_kind, principal_id) WHERE (revoked_at IS NULL);

CREATE UNIQUE INDEX uq_invitations_pending_email_team ON public.invitations USING btree (email, team_id) WHERE ((status)::text = 'pending'::text);

CREATE UNIQUE INDEX uq_notebook_runs_client_run_id ON public.notebook_runs USING btree (org_id, item_id, client_run_id) WHERE (client_run_id IS NOT NULL);

CREATE UNIQUE INDEX uq_notebook_runs_engine_run_id ON public.notebook_runs USING btree (org_id, item_id, engine_run_id) WHERE (engine_run_id IS NOT NULL);

CREATE UNIQUE INDEX uq_org_memberships_org_scim ON public.org_memberships USING btree (org_team_id, scim_external_id) WHERE (scim_external_id IS NOT NULL);

CREATE UNIQUE INDEX uq_role_assignments_live_principal_scope ON public.role_assignments USING btree (principal_kind, principal_id, scope_kind, scope_id) WHERE (revoked_at IS NULL);

CREATE UNIQUE INDEX uq_team_connections_team_plugin_handle_owner ON public.team_connections USING btree (team_id, plugin, handle, COALESCE(owner_user_id, '00000000-0000-0000-0000-000000000000'::uuid));

CREATE UNIQUE INDEX uq_user_bans_active_user ON public.user_bans USING btree (user_id) WHERE (lifted_at IS NULL);

CREATE UNIQUE INDEX uq_user_storage_limits_org_team_user ON public.user_storage_limits USING btree (org_team_id, team_id, user_id) WHERE (team_id IS NOT NULL);

CREATE UNIQUE INDEX uq_user_storage_limits_org_user_orgwide ON public.user_storage_limits USING btree (org_team_id, user_id) WHERE (team_id IS NULL);

CREATE UNIQUE INDEX uq_workspace_objects_one_spare_per_owner_org ON public.workspace_objects USING btree (owner_user_id, org_team_id) WHERE (((type)::text = 'chat'::text) AND (deleted_at = (0)::double precision) AND ((spec ->> 'spare'::text) = 'true'::text));

CREATE UNIQUE INDEX ux_compute_allocations_id_tenant_org ON public.compute_allocations USING btree (id, tenant_org_id);

CREATE UNIQUE INDEX ux_org_compute_assignments_machine_id ON public.org_compute_assignments USING btree (machine_id);

CREATE UNIQUE INDEX ux_org_machine_audiences_grant ON public.org_machine_audiences USING btree (org_machine_id, grantee_kind, team_id, user_id) NULLS NOT DISTINCT;

CREATE UNIQUE INDEX ux_org_machines_current_allocation_id ON public.org_machines USING btree (current_allocation_id);

CREATE UNIQUE INDEX ux_org_machines_id_org ON public.org_machines USING btree (id, org_team_id);

CREATE UNIQUE INDEX ux_org_machines_org_name_live ON public.org_machines USING btree (org_team_id, lower((name)::text)) WHERE (deleted_at IS NULL);

CREATE UNIQUE INDEX ux_workspace_machine_moves_active ON public.workspace_machine_moves USING btree (workspace_id) WHERE ((state)::text <> ALL ((ARRAY['done'::character varying, 'failed'::character varying, 'canceled'::character varying])::text[]));

CREATE TRIGGER chat_attachments_mirror_legacy_array AFTER INSERT OR UPDATE OF spec ON public.workspace_objects FOR EACH ROW WHEN (((new.type)::text = 'chat'::text)) EXECUTE FUNCTION public.chat_attachments_mirror_legacy_array();

CREATE TRIGGER files_share_principal_in_org BEFORE INSERT OR UPDATE ON public.file_shares FOR EACH ROW EXECUTE FUNCTION public.files_share_principal_in_org();

CREATE TRIGGER trg_compute_allocations_tenant_org_immutable BEFORE UPDATE OF tenant_org_id ON public.compute_allocations FOR EACH ROW EXECUTE FUNCTION public.fn_compute_allocations_tenant_org_immutable();

CREATE TRIGGER trg_connection_inventory_org BEFORE INSERT ON public.connection_inventory FOR EACH ROW EXECUTE FUNCTION public.fn_connection_inventory_org();

CREATE TRIGGER trg_event_outbox_append_only BEFORE DELETE OR UPDATE ON public.event_outbox FOR EACH ROW EXECUTE FUNCTION public.event_outbox_append_only();

CREATE TRIGGER trg_event_outbox_notify AFTER INSERT ON public.event_outbox FOR EACH ROW WHEN (((new.visibility)::text <> 'platform'::text)) EXECUTE FUNCTION public.event_outbox_notify();

CREATE TRIGGER trg_object_payload_rows_org BEFORE INSERT OR UPDATE OF object_id, org_team_id ON public.object_payload_rows FOR EACH ROW EXECUTE FUNCTION public.fn_object_payload_rows_org();

CREATE TRIGGER trg_org_machine_audiences_same_org BEFORE INSERT OR UPDATE OF team_id, org_team_id ON public.org_machine_audiences FOR EACH ROW EXECUTE FUNCTION public.fn_org_machine_audiences_same_org();

CREATE TRIGGER trg_team_connections_org BEFORE INSERT OR UPDATE OF team_id, org_team_id ON public.team_connections FOR EACH ROW EXECUTE FUNCTION public.fn_team_connections_org();

CREATE TRIGGER trg_team_memberships_org BEFORE INSERT OR UPDATE OF team_id, org_team_id ON public.team_memberships FOR EACH ROW EXECUTE FUNCTION public.fn_team_memberships_org();

CREATE TRIGGER trg_user_oauth_tokens_org BEFORE INSERT OR UPDATE OF team_connection_id, org_team_id ON public.user_oauth_tokens FOR EACH ROW EXECUTE FUNCTION public.fn_user_oauth_tokens_org();

CREATE TRIGGER trg_users_home_membership AFTER INSERT ON public.users FOR EACH ROW EXECUTE FUNCTION public.fn_users_home_membership();

CREATE TRIGGER trg_users_home_org_immutable BEFORE UPDATE OF org_team_id ON public.users FOR EACH ROW EXECUTE FUNCTION public.fn_users_home_org_immutable();

CREATE TRIGGER trg_workspace_machine_moves_same_org BEFORE INSERT OR UPDATE OF workspace_id, org_team_id ON public.workspace_machine_moves FOR EACH ROW EXECUTE FUNCTION public.fn_workspace_machine_moves_same_org();

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT auth_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.billing_model_routes
    ADD CONSTRAINT billing_model_routes_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.billing_models(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.crash_reports
    ADD CONSTRAINT crash_reports_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.crash_reports
    ADD CONSTRAINT crash_reports_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.deployment_health_checks
    ADD CONSTRAINT deployment_health_checks_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.device_authorizations
    ADD CONSTRAINT device_authorizations_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.entitlement_grants
    ADD CONSTRAINT entitlement_grants_issued_by_id_fkey FOREIGN KEY (issued_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.entitlement_grants
    ADD CONSTRAINT entitlement_grants_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.account_deletion_requests
    ADD CONSTRAINT fk_account_deletion_requests_requested_by_id_users FOREIGN KEY (requested_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.account_deletion_requests
    ADD CONSTRAINT fk_account_deletion_requests_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.account_export_requests
    ADD CONSTRAINT fk_account_export_requests_requested_by_id_users FOREIGN KEY (requested_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.account_export_requests
    ADD CONSTRAINT fk_account_export_requests_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.auth_refresh_tokens
    ADD CONSTRAINT fk_auth_refresh_tokens_active_org FOREIGN KEY (active_org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.auth_refresh_tokens
    ADD CONSTRAINT fk_auth_refresh_tokens_successor FOREIGN KEY (successor_id) REFERENCES public.auth_refresh_tokens(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.auth_refresh_tokens
    ADD CONSTRAINT fk_auth_refresh_tokens_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.auth_session_org_grants
    ADD CONSTRAINT fk_auth_session_org_grants_org FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT fk_auth_tokens_membership FOREIGN KEY (membership_id) REFERENCES public.org_memberships(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.auth_tokens
    ADD CONSTRAINT fk_auth_tokens_org FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.billing_signup_clicks
    ADD CONSTRAINT fk_billing_signup_clicks_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.chat_attachments
    ADD CONSTRAINT fk_chat_attachments_chat_id_workspace_objects FOREIGN KEY (chat_id) REFERENCES public.workspace_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT fk_chat_messages_chat_id_workspace_objects FOREIGN KEY (chat_id) REFERENCES public.workspace_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.chat_messages
    ADD CONSTRAINT fk_chat_messages_org_team_id_teams FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.chat_read_marks
    ADD CONSTRAINT fk_chat_read_marks_chat FOREIGN KEY (chat_id) REFERENCES public.workspace_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.chat_read_marks
    ADD CONSTRAINT fk_chat_read_marks_user FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.chat_workspace_states
    ADD CONSTRAINT fk_chat_workspace_states_chat FOREIGN KEY (chat_id) REFERENCES public.workspace_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.chat_workspace_states
    ADD CONSTRAINT fk_chat_workspace_states_user FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.ci_tokens
    ADD CONSTRAINT fk_ci_tokens_created_by_id FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.ci_tokens
    ADD CONSTRAINT fk_ci_tokens_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.compute_allocation_events
    ADD CONSTRAINT fk_compute_allocation_events_allocation_id FOREIGN KEY (allocation_id) REFERENCES public.compute_allocations(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.compute_allocations
    ADD CONSTRAINT fk_compute_allocations_grant_id FOREIGN KEY (grant_id) REFERENCES public.compute_grants(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.compute_allocations
    ADD CONSTRAINT fk_compute_allocations_machine_type_id FOREIGN KEY (machine_type_id) REFERENCES public.compute_machine_types(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.compute_allocations
    ADD CONSTRAINT fk_compute_allocations_org_machine_tenant FOREIGN KEY (org_machine_id, tenant_org_id) REFERENCES public.org_machines(id, org_team_id) ON DELETE SET NULL (org_machine_id);

ALTER TABLE ONLY public.compute_allocations
    ADD CONSTRAINT fk_compute_allocations_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.compute_allocations
    ADD CONSTRAINT fk_compute_allocations_user_id FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.compute_grants
    ADD CONSTRAINT fk_compute_grants_created_by FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.compute_grants
    ADD CONSTRAINT fk_compute_grants_machine_type_id FOREIGN KEY (machine_type_id) REFERENCES public.compute_machine_types(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.compute_grants
    ADD CONSTRAINT fk_compute_grants_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.compute_offering_orgs
    ADD CONSTRAINT fk_compute_offering_orgs_offering_id FOREIGN KEY (offering_id) REFERENCES public.compute_offerings(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.compute_offering_orgs
    ADD CONSTRAINT fk_compute_offering_orgs_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.compute_offerings
    ADD CONSTRAINT fk_compute_offerings_created_by FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.compute_offerings
    ADD CONSTRAINT fk_compute_offerings_machine_type_id FOREIGN KEY (machine_type_id) REFERENCES public.compute_machine_types(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.connection_inventory
    ADD CONSTRAINT fk_connection_inventory_org_team_id_teams FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.connection_inventory
    ADD CONSTRAINT fk_connection_inventory_user_id FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.connection_verifications
    ADD CONSTRAINT fk_connection_verifications_connection_id_team_connections FOREIGN KEY (connection_id) REFERENCES public.team_connections(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.crash_reports
    ADD CONSTRAINT fk_crash_reports_read_by_user_id_users FOREIGN KEY (read_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.crdt_docs
    ADD CONSTRAINT fk_crdt_docs_org_id_teams FOREIGN KEY (org_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.crdt_peers
    ADD CONSTRAINT fk_crdt_peers_org_id_teams FOREIGN KEY (org_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.crdt_peers
    ADD CONSTRAINT fk_crdt_peers_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.crdt_updates
    ADD CONSTRAINT fk_crdt_updates_author_user_id_users FOREIGN KEY (author_user_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.crdt_updates
    ADD CONSTRAINT fk_crdt_updates_doc FOREIGN KEY (org_id, doc_type, doc_id) REFERENCES public.crdt_docs(org_id, doc_type, doc_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.dedup_domains
    ADD CONSTRAINT fk_dedup_domains_store_id_file_stores FOREIGN KEY (store_id) REFERENCES public.file_stores(id);

ALTER TABLE ONLY public.device_authorizations
    ADD CONSTRAINT fk_device_authorizations_org FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.email_domain_bans
    ADD CONSTRAINT fk_email_domain_bans_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.email_domain_bans
    ADD CONSTRAINT fk_email_domain_bans_lifted_by_id_users FOREIGN KEY (lifted_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.file_acl_members
    ADD CONSTRAINT fk_file_acl_members_acl_id_file_acls FOREIGN KEY (acl_id) REFERENCES public.file_acls(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.file_conflicts
    ADD CONSTRAINT fk_file_conflicts_base_version_id_file_versions FOREIGN KEY (base_version_id) REFERENCES public.file_versions(id);

ALTER TABLE ONLY public.file_conflicts
    ADD CONSTRAINT fk_file_conflicts_mine_version_id_file_versions FOREIGN KEY (mine_version_id) REFERENCES public.file_versions(id);

ALTER TABLE ONLY public.file_conflicts
    ADD CONSTRAINT fk_file_conflicts_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_conflicts
    ADD CONSTRAINT fk_file_conflicts_theirs_version_id_file_versions FOREIGN KEY (theirs_version_id) REFERENCES public.file_versions(id);

ALTER TABLE ONLY public.file_content_grants
    ADD CONSTRAINT fk_file_content_grants_version_id_file_versions FOREIGN KEY (version_id) REFERENCES public.file_versions(id);

ALTER TABLE ONLY public.file_dir_stats_deltas
    ADD CONSTRAINT fk_file_dir_stats_deltas_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_dir_stats
    ADD CONSTRAINT fk_file_dir_stats_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.file_drives
    ADD CONSTRAINT fk_file_drives_dedup_domain_id_dedup_domains FOREIGN KEY (dedup_domain_id) REFERENCES public.dedup_domains(id);

ALTER TABLE ONLY public.file_drives
    ADD CONSTRAINT fk_file_drives_root_node_id_file_nodes FOREIGN KEY (root_node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_drives
    ADD CONSTRAINT fk_file_drives_store_id_file_stores FOREIGN KEY (store_id) REFERENCES public.file_stores(id);

ALTER TABLE ONLY public.file_history
    ADD CONSTRAINT fk_file_history_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_holds
    ADD CONSTRAINT fk_file_holds_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_key_chunks
    ADD CONSTRAINT fk_file_key_chunks_dedup_domain_id_dedup_domains FOREIGN KEY (dedup_domain_id) REFERENCES public.dedup_domains(id);

ALTER TABLE ONLY public.file_key_chunks
    ADD CONSTRAINT fk_file_key_chunks_pack_id_file_packs FOREIGN KEY (pack_id) REFERENCES public.file_packs(id);

ALTER TABLE ONLY public.file_lease_epoch_hwm
    ADD CONSTRAINT fk_file_lease_epoch_hwm_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_lease_live_entries
    ADD CONSTRAINT fk_file_lease_live_entries_lease_node_id_file_leases FOREIGN KEY (lease_node_id) REFERENCES public.file_leases(node_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.file_lease_live_entries
    ADD CONSTRAINT fk_file_lease_live_entries_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.file_leases
    ADD CONSTRAINT fk_file_leases_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_links
    ADD CONSTRAINT fk_file_links_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_locks
    ADD CONSTRAINT fk_file_locks_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_manifests
    ADD CONSTRAINT fk_file_manifests_dedup_domain_id_dedup_domains FOREIGN KEY (dedup_domain_id) REFERENCES public.dedup_domains(id);

ALTER TABLE ONLY public.file_nodes
    ADD CONSTRAINT fk_file_nodes_acl_id_file_acls FOREIGN KEY (acl_id) REFERENCES public.file_acls(id);

ALTER TABLE ONLY public.file_nodes
    ADD CONSTRAINT fk_file_nodes_default_acl_id_file_acls FOREIGN KEY (default_acl_id) REFERENCES public.file_acls(id);

ALTER TABLE ONLY public.file_nodes
    ADD CONSTRAINT fk_file_nodes_drive_id_file_drives FOREIGN KEY (drive_id) REFERENCES public.file_drives(id);

ALTER TABLE ONLY public.file_nodes
    ADD CONSTRAINT fk_file_nodes_head_version_id_file_versions FOREIGN KEY (head_version_id) REFERENCES public.file_versions(id);

ALTER TABLE ONLY public.file_nodes
    ADD CONSTRAINT fk_file_nodes_parent_id_file_nodes FOREIGN KEY (parent_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_nodes
    ADD CONSTRAINT fk_file_nodes_retention_label_id_file_retention_labels FOREIGN KEY (retention_label_id) REFERENCES public.file_retention_labels(id);

ALTER TABLE ONLY public.file_nodes
    ADD CONSTRAINT fk_file_nodes_target_id_file_nodes FOREIGN KEY (target_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_nodes
    ADD CONSTRAINT fk_file_nodes_trash_op_id_file_trash_ops FOREIGN KEY (trash_op_id) REFERENCES public.file_trash_ops(id);

ALTER TABLE ONLY public.file_ops
    ADD CONSTRAINT fk_file_ops_drive_id_file_drives FOREIGN KEY (drive_id) REFERENCES public.file_drives(id);

ALTER TABLE ONLY public.file_ops
    ADD CONSTRAINT fk_file_ops_result_node_id_file_nodes FOREIGN KEY (result_node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_packs
    ADD CONSTRAINT fk_file_packs_dedup_domain_id_dedup_domains FOREIGN KEY (dedup_domain_id) REFERENCES public.dedup_domains(id);

ALTER TABLE ONLY public.file_packs
    ADD CONSTRAINT fk_file_packs_store_id_file_stores FOREIGN KEY (store_id) REFERENCES public.file_stores(id);

ALTER TABLE ONLY public.file_shares
    ADD CONSTRAINT fk_file_shares_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.file_stage_jobs
    ADD CONSTRAINT fk_file_stage_jobs_lease_node_id_file_leases FOREIGN KEY (lease_node_id) REFERENCES public.file_leases(node_id);

ALTER TABLE ONLY public.file_stars
    ADD CONSTRAINT fk_file_stars_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.file_trash_ops
    ADD CONSTRAINT fk_file_trash_ops_drive_id_file_drives FOREIGN KEY (drive_id) REFERENCES public.file_drives(id);

ALTER TABLE ONLY public.file_upload_parts
    ADD CONSTRAINT fk_file_upload_parts_session_id_file_upload_sessions FOREIGN KEY (session_id) REFERENCES public.file_upload_sessions(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.file_upload_sessions
    ADD CONSTRAINT fk_file_upload_sessions_dedup_domain_id_dedup_domains FOREIGN KEY (dedup_domain_id) REFERENCES public.dedup_domains(id);

ALTER TABLE ONLY public.file_upload_sessions
    ADD CONSTRAINT fk_file_upload_sessions_drive_id_file_drives FOREIGN KEY (drive_id) REFERENCES public.file_drives(id);

ALTER TABLE ONLY public.file_upload_sessions
    ADD CONSTRAINT fk_file_upload_sessions_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_upload_sessions
    ADD CONSTRAINT fk_file_upload_sessions_parent_id_file_nodes FOREIGN KEY (parent_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.file_versions
    ADD CONSTRAINT fk_file_versions_node_id_file_nodes FOREIGN KEY (node_id) REFERENCES public.file_nodes(id);

ALTER TABLE ONLY public.identity_security_events
    ADD CONSTRAINT fk_identity_security_events_org FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.identity_security_events
    ADD CONSTRAINT fk_identity_security_events_user FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.manifest_terms
    ADD CONSTRAINT fk_manifest_terms_manifest_id_file_manifests FOREIGN KEY (manifest_id) REFERENCES public.file_manifests(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.manifest_terms
    ADD CONSTRAINT fk_manifest_terms_pack_id_file_packs FOREIGN KEY (pack_id) REFERENCES public.file_packs(id);

ALTER TABLE ONLY public.notebook_edits
    ADD CONSTRAINT fk_notebook_edits_org_id_teams FOREIGN KEY (org_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.notebook_edits
    ADD CONSTRAINT fk_notebook_edits_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.notebook_epoch_tails
    ADD CONSTRAINT fk_notebook_epoch_tails_org_id_teams FOREIGN KEY (org_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.notebook_kernels
    ADD CONSTRAINT fk_notebook_kernels_org_id_teams FOREIGN KEY (org_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.notebook_peers
    ADD CONSTRAINT fk_notebook_peers_org_id_teams FOREIGN KEY (org_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.notebook_runs
    ADD CONSTRAINT fk_notebook_runs_kernel_id_notebook_kernels FOREIGN KEY (kernel_id) REFERENCES public.notebook_kernels(kernel_id) ON DELETE SET NULL;

ALTER TABLE ONLY public.notebook_runs
    ADD CONSTRAINT fk_notebook_runs_org_id_teams FOREIGN KEY (org_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.notebook_runs
    ADD CONSTRAINT fk_notebook_runs_requested_by_users FOREIGN KEY (requested_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.object_payload_rows
    ADD CONSTRAINT fk_object_payload_rows_object_id_workspace_objects FOREIGN KEY (object_id) REFERENCES public.workspace_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.object_payload_rows
    ADD CONSTRAINT fk_object_payload_rows_org_team_id_teams FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_audit_events
    ADD CONSTRAINT fk_org_audit_events_actor_id FOREIGN KEY (actor_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.org_audit_events
    ADD CONSTRAINT fk_org_audit_events_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_compute_settings
    ADD CONSTRAINT fk_org_compute_settings_default_org_machine_tenant FOREIGN KEY (default_org_machine_id, org_team_id) REFERENCES public.org_machines(id, org_team_id) ON DELETE SET NULL (default_org_machine_id);

ALTER TABLE ONLY public.org_compute_settings
    ADD CONSTRAINT fk_org_compute_settings_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_machine_audiences
    ADD CONSTRAINT fk_org_machine_audiences_created_by FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.org_machine_audiences
    ADD CONSTRAINT fk_org_machine_audiences_org_machine_tenant FOREIGN KEY (org_machine_id, org_team_id) REFERENCES public.org_machines(id, org_team_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_machine_audiences
    ADD CONSTRAINT fk_org_machine_audiences_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_machine_audiences
    ADD CONSTRAINT fk_org_machine_audiences_team_id FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_machine_audiences
    ADD CONSTRAINT fk_org_machine_audiences_user_id FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_machines
    ADD CONSTRAINT fk_org_machines_created_by FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.org_machines
    ADD CONSTRAINT fk_org_machines_current_allocation_tenant FOREIGN KEY (current_allocation_id, org_team_id) REFERENCES public.compute_allocations(id, tenant_org_id) ON DELETE SET NULL (current_allocation_id);

ALTER TABLE ONLY public.org_machines
    ADD CONSTRAINT fk_org_machines_offering_id FOREIGN KEY (offering_id) REFERENCES public.compute_offerings(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.org_machines
    ADD CONSTRAINT fk_org_machines_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_machines
    ADD CONSTRAINT fk_org_machines_owner_team_id FOREIGN KEY (owner_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT fk_org_memberships_org FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_memberships
    ADD CONSTRAINT fk_org_memberships_user FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_storage_limits
    ADD CONSTRAINT fk_org_storage_limits_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.org_storage_limits
    ADD CONSTRAINT fk_org_storage_limits_org_team_id_teams FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT fk_personal_access_tokens_membership FOREIGN KEY (membership_id) REFERENCES public.org_memberships(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.proxy_tokens
    ADD CONSTRAINT fk_proxy_tokens_created_by_id FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.proxy_tokens
    ADD CONSTRAINT fk_proxy_tokens_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.realtime_docs
    ADD CONSTRAINT fk_realtime_docs_org_id_teams FOREIGN KEY (org_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.realtime_docs
    ADD CONSTRAINT fk_realtime_docs_owner_user_id_users FOREIGN KEY (owner_user_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.realtime_docs
    ADD CONSTRAINT fk_realtime_docs_team_id_teams FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.realtime_presence
    ADD CONSTRAINT fk_realtime_presence_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.saml_replay_assertions
    ADD CONSTRAINT fk_saml_replay_assertions_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sso_connections
    ADD CONSTRAINT fk_sso_connections_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sso_domain_claims
    ADD CONSTRAINT fk_sso_domain_claims_assigned_by_id_users FOREIGN KEY (assigned_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.sso_domain_claims
    ADD CONSTRAINT fk_sso_domain_claims_org_team_id_teams FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.sso_link_requests
    ADD CONSTRAINT fk_sso_link_requests_org FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.storage_usage_snapshots
    ADD CONSTRAINT fk_storage_usage_snapshots_drive_id_file_drives FOREIGN KEY (drive_id) REFERENCES public.file_drives(id);

ALTER TABLE ONLY public.team_connections
    ADD CONSTRAINT fk_tc_owner_user_id FOREIGN KEY (owner_user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.team_allocations
    ADD CONSTRAINT fk_team_allocations_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.team_allocations
    ADD CONSTRAINT fk_team_allocations_org_team_id_teams FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.team_allocations
    ADD CONSTRAINT fk_team_allocations_team_id_teams FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.connection_verifications
    ADD CONSTRAINT fk_team_connection_probes_requested_by_id FOREIGN KEY (requested_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.connection_verifications
    ADD CONSTRAINT fk_team_connection_probes_team_id FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.team_connections
    ADD CONSTRAINT fk_team_connections_created_by_id FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.team_connections
    ADD CONSTRAINT fk_team_connections_org_team_id_teams FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.team_connections
    ADD CONSTRAINT fk_team_connections_team_id FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.team_memberships
    ADD CONSTRAINT fk_team_memberships_org_membership FOREIGN KEY (user_id, org_team_id) REFERENCES public.org_memberships(user_id, org_team_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE ONLY public.user_bans
    ADD CONSTRAINT fk_user_bans_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.user_bans
    ADD CONSTRAINT fk_user_bans_lifted_by_id_users FOREIGN KEY (lifted_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.user_bans
    ADD CONSTRAINT fk_user_bans_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_oauth_tokens
    ADD CONSTRAINT fk_user_oauth_tokens_org_team_id_teams FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_oauth_tokens
    ADD CONSTRAINT fk_user_oauth_tokens_team_connection_id FOREIGN KEY (team_connection_id) REFERENCES public.team_connections(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_oauth_tokens
    ADD CONSTRAINT fk_user_oauth_tokens_user_id FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_org_preferences
    ADD CONSTRAINT fk_user_org_preferences_org FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_org_preferences
    ADD CONSTRAINT fk_user_org_preferences_user FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_preferences
    ADD CONSTRAINT fk_user_preferences_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_storage_limits
    ADD CONSTRAINT fk_user_storage_limits_created_by_id_users FOREIGN KEY (created_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.user_storage_limits
    ADD CONSTRAINT fk_user_storage_limits_org_team_id_teams FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_storage_limits
    ADD CONSTRAINT fk_user_storage_limits_team_id_teams FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.user_storage_limits
    ADD CONSTRAINT fk_user_storage_limits_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.workspace_machine_moves
    ADD CONSTRAINT fk_workspace_machine_moves_from_org_machine_tenant FOREIGN KEY (from_org_machine_id, org_team_id) REFERENCES public.org_machines(id, org_team_id) ON DELETE SET NULL (from_org_machine_id);

ALTER TABLE ONLY public.workspace_machine_moves
    ADD CONSTRAINT fk_workspace_machine_moves_org_team_id FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.workspace_machine_moves
    ADD CONSTRAINT fk_workspace_machine_moves_requested_by FOREIGN KEY (requested_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.workspace_machine_moves
    ADD CONSTRAINT fk_workspace_machine_moves_to_org_machine_tenant FOREIGN KEY (to_org_machine_id, org_team_id) REFERENCES public.org_machines(id, org_team_id) ON DELETE SET NULL (to_org_machine_id);

ALTER TABLE ONLY public.workspace_machine_moves
    ADD CONSTRAINT fk_workspace_machine_moves_workspace_id FOREIGN KEY (workspace_id) REFERENCES public.workspace_objects(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.workspace_objects
    ADD CONSTRAINT fk_workspace_objects_org_team_id_teams FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.workspace_objects
    ADD CONSTRAINT fk_workspace_objects_owner_user_id_users FOREIGN KEY (owner_user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.workspace_objects
    ADD CONSTRAINT fk_workspace_objects_team_id_teams FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.identity_org_creations
    ADD CONSTRAINT identity_org_creations_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.identity_org_creations
    ADD CONSTRAINT identity_org_creations_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_invited_by_id_fkey FOREIGN KEY (invited_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.invitations
    ADD CONSTRAINT invitations_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.machine_credentials
    ADD CONSTRAINT machine_credentials_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.machine_credentials
    ADD CONSTRAINT machine_credentials_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.compute_allocations(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.machine_credentials
    ADD CONSTRAINT machine_credentials_machine_type_id_fkey FOREIGN KEY (machine_type_id) REFERENCES public.compute_machine_types(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.machine_credentials
    ADD CONSTRAINT machine_credentials_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.model_provider_configs
    ADD CONSTRAINT model_provider_configs_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.model_provider_configs
    ADD CONSTRAINT model_provider_configs_updated_by_id_fkey FOREIGN KEY (updated_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.oauth_identities
    ADD CONSTRAINT oauth_identities_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_compute_assignments
    ADD CONSTRAINT org_compute_assignments_assigned_by_fkey FOREIGN KEY (assigned_by) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.org_compute_assignments
    ADD CONSTRAINT org_compute_assignments_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.compute_allocations(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_compute_assignments
    ADD CONSTRAINT org_compute_assignments_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_settings
    ADD CONSTRAINT org_settings_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.org_sync_settings
    ADD CONSTRAINT org_sync_settings_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.personal_access_tokens
    ADD CONSTRAINT personal_access_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.role_assignments
    ADD CONSTRAINT role_assignments_granted_by_id_fkey FOREIGN KEY (granted_by_id) REFERENCES public.users(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.role_assignments
    ADD CONSTRAINT role_assignments_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.role_assignments
    ADD CONSTRAINT role_assignments_scope_id_fkey FOREIGN KEY (scope_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.team_memberships
    ADD CONSTRAINT team_memberships_team_id_fkey FOREIGN KEY (team_id) REFERENCES public.teams(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.team_memberships
    ADD CONSTRAINT team_memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.teams
    ADD CONSTRAINT teams_parent_team_id_fkey FOREIGN KEY (parent_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_org_team_id_fkey FOREIGN KEY (org_team_id) REFERENCES public.teams(id) ON DELETE RESTRICT;

ALTER TABLE public.chat_messages ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.crdt_docs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.crdt_updates ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.dedup_domains ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_acl_members ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_acls ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_conflicts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_content_grants ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_dir_stats ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_dir_stats_deltas ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_drives ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_erasure_log ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_history ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_holds ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_idempotency_keys ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_key_chunks ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_lease_epoch_hwm ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_lease_live_entries ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_leases ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_links ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_locks ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_manifests ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_nodes ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_ops ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_packs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_page_grants ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_quarantine ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_retention_labels ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_shares ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_stage_jobs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_stars ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_trash_ops ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_upload_parts ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_upload_sessions ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.file_versions ENABLE ROW LEVEL SECURITY;

CREATE POLICY files_tenant_isolation ON public.dedup_domains USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_acl_members USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_acls USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_conflicts USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_content_grants USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_dir_stats USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_dir_stats_deltas USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_drives USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_erasure_log USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_history USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_holds USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_idempotency_keys USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_key_chunks USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_lease_epoch_hwm USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_lease_live_entries USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_leases USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_links USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_locks USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_manifests USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_nodes USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_ops USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_packs USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_page_grants USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_quarantine USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_retention_labels USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_shares USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_stage_jobs USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_stars USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_trash_ops USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_upload_parts USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_upload_sessions USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.file_versions USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.manifest_terms USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

CREATE POLICY files_tenant_isolation ON public.storage_usage_snapshots USING ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid)) WITH CHECK ((org_team_id = (NULLIF(current_setting('alkera.org_id'::text, true), ''::text))::uuid));

ALTER TABLE public.manifest_terms ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.notebook_edits ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.notebook_epoch_tails ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.notebook_kernels ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.notebook_peers ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.notebook_runs ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.object_payload_rows ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.org_compute_settings ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.org_machine_audiences ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.org_machines ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.storage_usage_snapshots ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.team_connections ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.team_memberships ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON public.chat_messages TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.crdt_docs TO alkera_tenant_app USING ((org_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.crdt_updates TO alkera_tenant_app USING ((org_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.dedup_domains TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_acl_members TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_acls TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_conflicts TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_content_grants TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_dir_stats TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_dir_stats_deltas TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_drives TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_erasure_log TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_history TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_holds TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_idempotency_keys TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_key_chunks TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_lease_epoch_hwm TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_lease_live_entries TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_leases TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_links TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_locks TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_manifests TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_nodes TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_ops TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_packs TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_page_grants TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_quarantine TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_retention_labels TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_shares TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_stage_jobs TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_stars TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_trash_ops TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_upload_parts TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_upload_sessions TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.file_versions TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.manifest_terms TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.notebook_edits TO alkera_tenant_app USING ((org_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.notebook_epoch_tails TO alkera_tenant_app USING ((org_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.notebook_kernels TO alkera_tenant_app USING ((org_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.notebook_peers TO alkera_tenant_app USING ((org_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.notebook_runs TO alkera_tenant_app USING ((org_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.object_payload_rows TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.org_compute_settings TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.org_machine_audiences TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.org_machines TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.storage_usage_snapshots TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.team_connections TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.team_memberships TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.user_oauth_tokens TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.workspace_machine_moves TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

CREATE POLICY tenant_isolation ON public.workspace_objects TO alkera_tenant_app USING ((org_team_id = ANY (public.alkera_org_ids()))) WITH CHECK ((org_team_id = ANY (public.alkera_org_ids())));

ALTER TABLE public.user_oauth_tokens ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.workspace_machine_moves ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.workspace_objects ENABLE ROW LEVEL SECURITY;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO alkera_tenant_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO alkera_tenant_app;
