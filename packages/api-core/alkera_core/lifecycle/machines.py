"""The product's long-running state machines, declared with their bounds.

Each entry says, for every state a row can wait in, how long it may wait and
who ends it; or that it waits on a person or on another machine's bound; or,
as a :class:`Gap`, that nothing ends it yet. Registration fails on a state
with no bound, a bound that cannot reach rest, an equal pair of deadlines that
wait on each other, and a failure code with no single outcome. The gate test
walks this registry; the gap list may only shrink.

Settings are read when a bound is measured, never at import, so a deployment's
values are the ones checked (:func:`alkera_core.lifecycle.recheck`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Final

from alkera_core.config import get_settings
from alkera_core.lifecycle.contract import (
    Bound,
    Exempt,
    Gap,
    Interval,
    Outcome,
    StateMachine,
    register,
    register_interval,
)


def _seconds(name: str) -> Callable[[], timedelta]:
    return lambda: timedelta(seconds=int(getattr(get_settings(), name)))


def _minutes(name: str) -> Callable[[], timedelta]:
    return lambda: timedelta(minutes=int(getattr(get_settings(), name)))


def _edges(**edges: tuple[str, ...]) -> dict[str, frozenset[str]]:
    return {state: frozenset(targets) for state, targets in edges.items()}


#: The interval sweeps that end rows run at; how late an ender may be.
SWEEP: Final = timedelta(seconds=15)
RECONCILE: Final = timedelta(seconds=60)
JANITOR: Final = timedelta(minutes=5)

# ---- deadlines other bounds wait on ------------------------------------------

register_interval("files.grant_delay", Interval(_seconds("files_lease_grant_delay_seconds")))
#: A departed box's time to hand a moved workspace back, then the grant delay
#: before the target may take its folder.
register_interval(
    "move.hand_back",
    Interval(
        lambda: timedelta(seconds=60 + int(get_settings().files_lease_grant_delay_seconds)),
        slack=timedelta(seconds=2),
    ),
)
register_interval("chat.turn_silence", Interval(_seconds("chat_turn_silence_seconds")))
#: How long a waiting message or wake reads "waking" before it reads stalled;
#: the server ends the wait only after a reader has been told.
register_interval("chat.turn_start", Interval(_seconds("chat_turn_start_seconds")))
register_interval("machine.ready_window", Interval(_seconds("compute_heartbeat_ready_seconds")))

# ---- the machines ---------------------------------------------------------------

#: The words a failed move says, by code (``workspace_move.ERROR_WORDS`` reads
#: them from here once the move's fail path is moved onto the outcome table).
MOVE_OUTCOMES: Final = {
    "not_started": Outcome("failed", ("restore_pin",), "The move didn't start."),
    "drain_timeout": Outcome(
        "failed", ("restore_pin",), "The chats couldn't be stopped on the current machine."
    ),
    "target_capacity": Outcome(
        "failed",
        ("chats_back", "restore_pin"),
        "The machine has no hardware available right now.",
    ),
    "target_boot_failed": Outcome(
        "failed", ("chats_back", "restore_pin"), "The machine couldn't start."
    ),
    "target_deleted": Outcome("failed", ("chats_back", "restore_pin"), "The machine was deleted."),
    "wake_timeout": Outcome(
        "failed",
        ("chats_back", "restore_pin"),
        "The machine didn't take the workspace in time.",
    ),
    "not_allowed": Outcome(
        "failed", ("chats_back", "restore_pin"), "This workspace can't run on that machine."
    ),
}

WORKSPACE_MOVE = register(
    StateMachine(
        name="workspace_move",
        columns=("workspace_machine_moves.state",),
        states=frozenset(
            {"requested", "draining", "switching", "waking", "done", "failed", "canceled"}
        ),
        rest=frozenset({"done", "failed", "canceled"}),
        edges=_edges(
            requested=("draining", "failed", "canceled"),
            draining=("switching", "failed", "canceled"),
            switching=("waking", "failed"),
            waking=("done", "failed"),
        ),
        bounds={
            # Long enough for the recovery sweep (every minute, once a move is
            # two minutes quiet) to have offered a runner several times.
            "requested": Bound(
                timedelta(minutes=10),
                "failed",
                "not_started",
                slack=RECONCILE,
                ender="the move's recovery sweep",
            ),
            "draining": Bound(
                lambda: timedelta(
                    seconds=int(get_settings().move_turn_grace_seconds) + 30 + 60 + 60
                ),
                "failed",
                "drain_timeout",
                slack=RECONCILE,
                ender="the move workflow, and its recovery sweep",
            ),
            "switching": Bound(
                timedelta(minutes=2),
                "failed",
                "wake_timeout",
                slack=RECONCILE,
                ender="the move workflow, and its recovery sweep",
            ),
            "waking": Bound(
                _seconds("move_wake_timeout_seconds"),
                "failed",
                "wake_timeout",
                must_exceed=("move.hand_back",),
                slack=RECONCILE,
                ender="the move workflow, and its recovery sweep",
            ),
        },
        outcomes=MOVE_OUTCOMES,
        ceiling=timedelta(hours=1),
    )
)

CHAT_TURN = register(
    StateMachine(
        name="chat_turn",
        columns=("realtime_docs.turn_state",),
        states=frozenset({"idle", "working"}),
        rest=frozenset({"idle"}),
        edges=_edges(working=("idle",)),
        bounds={
            "working": Bound(
                _seconds("chat_turn_abandon_seconds"),
                "idle",
                "turn_lost",
                must_exceed=("chat.turn_silence", "machine.ready_window"),
                slack=SWEEP,
                ender="the compute sweep, from the turn stamp's age",
            ),
        },
        outcomes={
            "turn_lost": Outcome(
                "idle",
                ("turn_idle", "turn_end_reason"),
                "Stopped because the agent stopped reporting progress.",
            ),
        },
    )
)

CHAT_WAKE = register(
    StateMachine(
        name="chat_wake",
        columns=("workspace_objects.spec.wake_requested_at", "workspace_objects.spec.slot_wait_at"),
        states=frozenset({"asleep", "waking", "queued", "awake"}),
        rest=frozenset({"asleep", "awake"}),
        edges=_edges(waking=("awake", "queued", "asleep"), queued=("awake", "asleep")),
        bounds={
            # Counted from the later of the wake and the machine coming up; a
            # machine that does not answer is waited on through its own bounds.
            "waking": Bound(
                _seconds("chat_wake_deadline_seconds"),
                "asleep",
                "not_taken",
                must_exceed=("chat.turn_start", "files.grant_delay", "machine.ready_window"),
                slack=SWEEP,
                ender="the compute sweep, on a machine that answers",
            ),
            "queued": Bound(
                _seconds("chat_queue_deadline_seconds"),
                "asleep",
                "no_slot",
                must_exceed=("chat.turn_silence",),
                slack=SWEEP,
                ender="the compute sweep, on a machine that answers",
            ),
        },
        outcomes={
            "not_taken": Outcome(
                "asleep",
                ("clear_wake", "prompts_not_run", "turn_end_reason"),
                "No machine picked this up. Send it again to retry.",
            ),
            "no_slot": Outcome(
                "asleep",
                ("clear_wake", "prompts_not_run", "turn_end_reason"),
                "No slot freed up on its machine. Send it again to retry.",
            ),
        },
    )
)

FILE_LEASE = register(
    StateMachine(
        name="file_lease",
        columns=("file_leases.released_at", "file_leases.reaped_at"),
        states=frozenset({"live", "lapsed", "released", "reaped"}),
        rest=frozenset({"released", "reaped"}),
        edges=_edges(live=("lapsed", "released"), lapsed=("reaped", "released")),
        bounds={
            "live": Bound(
                _seconds("files_lease_ttl_seconds"),
                "lapsed",
                slack=timedelta(0),
                ender="read time: expires_at past now",
            ),
            "lapsed": Bound(JANITOR, "reaped", slack=JANITOR, ender="the Files janitor's reaper"),
        },
    )
)

COMPUTE_ALLOCATION = register(
    StateMachine(
        name="compute_allocation",
        columns=("compute_allocations.state",),
        states=frozenset(
            {
                "pending",
                "provisioning",
                "bootstrapping",
                "ready",
                "draining",
                "releasing",
                "asleep",
                "released",
                "failed",
                "lost",
            }
        ),
        rest=frozenset({"released", "failed"}),
        edges=_edges(
            pending=("provisioning", "failed", "releasing"),
            provisioning=("bootstrapping", "ready", "failed", "releasing"),
            bootstrapping=("ready", "failed", "releasing"),
            ready=("draining", "asleep", "lost", "releasing"),
            draining=("ready", "asleep", "lost", "releasing"),
            asleep=("ready", "lost", "releasing"),
            releasing=("released", "failed"),
            lost=("released",),
        ),
        bounds={
            "pending": Bound(
                _seconds("compute_unconfirmed_create_seconds"),
                "failed",
                slack=RECONCILE,
                ender="the pod reconcile",
            ),
            "provisioning": Bound(
                timedelta(minutes=10), "failed", slack=RECONCILE, ender="the node reconcile"
            ),
            "bootstrapping": Bound(
                timedelta(minutes=15), "failed", slack=RECONCILE, ender="the node reconcile"
            ),
            "ready": Exempt("serving is a healthy machine's resting state", "a person"),
            "draining": Gap(
                "A drain without auto_terminate has no deadline the server enforces; "
                "compute_drain_ceiling_seconds is only handed to the box."
            ),
            "asleep": Gap(
                "A replaced allocation left asleep is never released, and keeps its disk."
            ),
            "releasing": Gap("A provider that keeps refusing terminate is retried with no cap."),
            "lost": Bound(RECONCILE, "released", slack=RECONCILE, ender="the node reconcile"),
        },
    )
)

ORG_MACHINE = register(
    StateMachine(
        name="org_machine",
        columns=("org_machines.desired_power",),
        states=frozenset({"on", "off", "deleted"}),
        rest=frozenset({"on", "off", "deleted"}),
        edges=_edges(on=("off", "deleted"), off=("on", "deleted")),
        bounds={},
    )
)

MACHINE_CREDENTIAL = register(
    StateMachine(
        name="machine_credential",
        columns=("machine_credentials.revoked_at",),
        states=frozenset({"unclaimed", "claimed", "revoked"}),
        rest=frozenset({"claimed", "revoked"}),
        edges=_edges(unclaimed=("claimed", "revoked"), claimed=("revoked",)),
        bounds={"unclaimed": Gap("A minted credential nobody claims stays live.")},
    )
)

UPLOAD_SESSION = register(
    StateMachine(
        name="upload_session",
        columns=("file_upload_sessions.state",),
        states=frozenset({"open", "uploading", "committing", "done", "aborted", "expired"}),
        rest=frozenset({"done", "aborted", "expired"}),
        edges=_edges(
            open=("uploading", "committing", "aborted", "expired"),
            uploading=("committing", "aborted", "expired"),
            committing=("done", "aborted"),
        ),
        bounds={
            "open": Bound(timedelta(days=7), "expired", slack=JANITOR, ender="ExpiredSessions"),
            "uploading": Bound(
                timedelta(days=7), "expired", slack=JANITOR, ender="ExpiredSessions"
            ),
            "committing": Gap("Each re-drive resets its eligibility; nothing caps the total."),
        },
        ceiling=timedelta(days=8),
    )
)

FILE_OP = register(
    StateMachine(
        name="file_op",
        columns=("file_ops.state", "file_stage_jobs.state"),
        states=frozenset({"queued", "running", "done", "failed", "cancelled"}),
        rest=frozenset({"done", "failed", "cancelled"}),
        edges=_edges(
            queued=("running", "failed", "cancelled"), running=("done", "failed", "cancelled")
        ),
        bounds={
            "queued": Gap(
                "A queued operation of a kind with no runner is reported and left queued."
            ),
            "running": Bound(
                timedelta(minutes=10), "failed", slack=JANITOR, ender="OperationWatchdog"
            ),
        },
    )
)

NODE_FLAG = register(
    StateMachine(
        name="file_node_flag",
        columns=("file_nodes.state",),
        states=frozenset({"live", "locked", "moving", "acl_rewriting"}),
        rest=frozenset({"live", "locked"}),
        edges=_edges(moving=("live",), acl_rewriting=("live",)),
        bounds={
            "moving": Bound(timedelta(minutes=10), "live", slack=JANITOR, ender="MovingStale"),
            "acl_rewriting": Bound(
                timedelta(minutes=10), "live", slack=JANITOR, ender="AclRewriteStale"
            ),
        },
    )
)

VERSION_SCAN = register(
    StateMachine(
        name="version_scan",
        columns=("file_versions.scan_state",),
        states=frozenset({"pending", "clean", "infected", "skipped", "failed"}),
        rest=frozenset({"clean", "infected", "skipped", "failed"}),
        edges=_edges(pending=("clean", "infected", "skipped", "failed")),
        bounds={
            "pending": Exempt(
                "no scanner is deployed, and the serve gate treats pending as servable",
                "a scanner that does not exist yet",
            ),
        },
    )
)

NOTEBOOK_KERNEL = register(
    StateMachine(
        name="notebook_kernel",
        columns=("notebook_kernels.state", "notebook_runs.status"),
        states=frozenset({"absent", "starting", "idle", "busy", "restarting", "stopped"}),
        rest=frozenset({"absent", "stopped"}),
        edges=_edges(
            starting=("idle", "busy", "stopped"),
            idle=("busy", "restarting", "stopped"),
            busy=("idle", "restarting", "stopped"),
            restarting=("idle", "stopped"),
        ),
        bounds={
            "starting": Gap("Only the kernel's box ends a start; a lost box leaves it starting."),
            "idle": Gap("A live kernel on a box that went away is never stopped by the server."),
            "busy": Gap("A busy kernel on a box that went away is never stopped by the server."),
            "restarting": Gap("Only the kernel's box ends a restart."),
        },
    )
)

RESULT_PROMOTE = register(
    StateMachine(
        name="result_promote",
        columns=("workspace_objects.status",),
        states=frozenset({"draft", "pending_upload", "ready", "failed"}),
        rest=frozenset({"draft", "ready", "failed"}),
        edges=_edges(pending_upload=("ready", "failed")),
        bounds={
            "pending_upload": Bound(
                _seconds("objects_promote_deadline_seconds"),
                "failed",
                slack=SWEEP,
                ender="expire_stalled_promotes, for results only",
            ),
        },
    )
)

INVITATION = register(
    StateMachine(
        name="invitation",
        columns=("invitations.status",),
        states=frozenset({"pending", "accepted", "rejected", "expired", "revoked"}),
        rest=frozenset({"accepted", "rejected", "expired", "revoked"}),
        edges=_edges(pending=("accepted", "rejected", "expired", "revoked")),
        bounds={
            "pending": Bound(
                timedelta(days=7), "expired", slack=timedelta(0), ender="read time: expires_at"
            ),
        },
        ceiling=timedelta(days=8),
    )
)

__all__ = [
    "CHAT_TURN",
    "CHAT_WAKE",
    "COMPUTE_ALLOCATION",
    "FILE_LEASE",
    "FILE_OP",
    "INVITATION",
    "MACHINE_CREDENTIAL",
    "MOVE_OUTCOMES",
    "NODE_FLAG",
    "NOTEBOOK_KERNEL",
    "ORG_MACHINE",
    "RESULT_PROMOTE",
    "UPLOAD_SESSION",
    "VERSION_SCAN",
    "WORKSPACE_MOVE",
]
