"""The event outbox: how a committed mutation announces itself.

A producer calls :func:`emit` inside its own transaction; the row lands when
that transaction commits, and Postgres notifies ``alkera_events`` with the row
id at the same instant. One :class:`PgListener` per process hears that,
reads the table by id and publishes to the process's :class:`EventHub`, whose
subscribers own bounded queues. The vocabulary is :class:`EventType`; the
actor document is the authorization layer's ``ActorChainRecord``.
"""

from __future__ import annotations

from alkera_core.events.actor import actor_for_ci_token, actor_for_user, actor_system
from alkera_core.events.hub import (
    EventHub,
    HubEvent,
    Lane,
    Predicate,
    ResetMarker,
    Subscription,
    get_hub,
)
from alkera_core.events.listener import (
    DURABLE_CHANNEL,
    EPHEMERAL_CHANNEL,
    PgListener,
    asyncpg_dsn,
    ephemeral_payload,
    parse_ephemeral,
)
from alkera_core.events.outbox import (
    MAX_FRAME_BYTES,
    MAX_PAYLOAD_BYTES,
    emit,
    latest_id,
    read_after,
    read_ids,
)
from alkera_core.events.scope import org_root_for_team
from alkera_core.events.types import (
    ACCESS_CHANGED_KEY,
    BOUND_MACHINE_KEY,
    CHAT_DELETED_REASON,
    REALTIME_EVENT_TYPE_VALUES,
    SERVER_ONLY_EVENT_TYPES,
    USER_VISIBILITY_PATTERN,
    VISIBILITY_ORG,
    VISIBILITY_PLATFORM,
    Entity,
    EventType,
    RealtimeEventType,
    coerce_event_type,
    is_realtime_event_type,
    user_id_of_visibility,
    user_visibility,
    validate_visibility,
)

__all__ = [
    "ACCESS_CHANGED_KEY",
    "BOUND_MACHINE_KEY",
    "CHAT_DELETED_REASON",
    "DURABLE_CHANNEL",
    "EPHEMERAL_CHANNEL",
    "MAX_FRAME_BYTES",
    "MAX_PAYLOAD_BYTES",
    "REALTIME_EVENT_TYPE_VALUES",
    "SERVER_ONLY_EVENT_TYPES",
    "USER_VISIBILITY_PATTERN",
    "VISIBILITY_ORG",
    "VISIBILITY_PLATFORM",
    "Entity",
    "EventHub",
    "EventType",
    "HubEvent",
    "Lane",
    "PgListener",
    "Predicate",
    "RealtimeEventType",
    "ResetMarker",
    "Subscription",
    "actor_for_ci_token",
    "actor_for_user",
    "actor_system",
    "asyncpg_dsn",
    "coerce_event_type",
    "emit",
    "ephemeral_payload",
    "get_hub",
    "is_realtime_event_type",
    "latest_id",
    "org_root_for_team",
    "parse_ephemeral",
    "read_after",
    "read_ids",
    "user_id_of_visibility",
    "user_visibility",
    "validate_visibility",
]
