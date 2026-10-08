"""A box's supervisor events reach the alarms, and nothing else leaves the box.

Two routes off a box, both proven end to end here:

- an EC2 box writes straight to the node log group on its instance role; the
  line the shipper writes there for a crash loop the real supervisor reported
  is matched by the metric filter the log-alarms module renders for that group;
- any other box posts to ``POST /api/v1/machines/me/logs`` on its machine
  credential; the route decides through ``compute.machine_credential`` (the
  status contract and the ``authz.decision`` row are pinned), re-applies the
  allowlist, and re-emits each event to the backend's own log as the line the
  app log group's metric filter counts.

The metric filter semantics are CloudWatch's; the patterns are read from the
module and evaluated for their two shapes (a JSON ``$.event`` equality for the
app group, a quoted term for the node group).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from alkera_cli.supervisor import org_events
from alkera_cli.supervisor.box_log_shipping import BackendSink, CloudWatchSink, install
from alkera_cli.supervisor.crash_loop import CRASH_LOOP_EXITS
from alkera_cli.supervisor.org_routing import FileRoutingFeed
from alkera_cli.supervisor.service import Supervisor
from alkera_cli.supervisor.slots import SlotTable
from alkera_core.compute import box_logs
from alkera_core.compute.box_isolation import IsolationReport
from alkera_core.compute.box_logs import INGEST_PATH, MAX_BATCH_EVENTS
from alkera_core.db.session import AsyncSessionLocal
from alkera_core.machine_refusals import MACHINE_CREDENTIAL_REFUSED, MACHINE_CREDENTIAL_REQUIRED
from alkera_core.models.machine_credential import MachineCredential
from alkera_core.schemas.box_logs import BoxLogBatch
from backend.services.compute import box_logs as box_log_service
from httpx import AsyncClient
from sqlalchemy import update
from structlog.contextvars import bound_contextvars, merge_contextvars
from structlog.testing import capture_logs
from tests.conftest import OrgWithAdmin, login
from tests.files._boxes import chat_on_box
from tests.test_log_alarms_terraform import MODULE, _catalogue, _narrowed_by
from tests.test_machine_principal_routes import Box, _box, _decisions, _effects, _error

pytestmark = [pytest.mark.asyncio, pytest.mark.compute_rows]

ORG = "8ef423bc-531e-4d2a-baa5-8bfb6e9e8863"
CHAT = "be0a3393-082f-4acf-918d-6eac00904c9b"
STAMP = "2026-10-05T12:00:00+00:00"
SECRETS = (
    "alkm_3f9a1c2b7d8e4f60a1b2c3d4e5f6a7b8",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlc2lnbmF0dXJl",
    "sk-proj-AbCdEfGhIjKlMnOpQrStUv",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "summarise the board deck for the acquisition",
)


# -- the metric filters, as the module renders them ----------------------------------

#: The pattern shapes the module renders, verbatim: a change to any must be
#: reflected in the evaluation below. A family the module narrows by fields
#: adds `&&` equalities (JSON) or required terms (a node's lines).
_JSON_TEMPLATE = (
    '"{ ${join(" || ", [for event in each.value.events : "($.event = \\"${event}\\")"])} }"'
)
_JSON_NARROWED_TEMPLATE = (
    '"{ (${join(" || ", [for event in each.value.events : "($.event = \\"${event}\\")"])}) '
    '&& ${join(" && ", [for field, wanted in each.value.fields : '
    '"($.${field} = \\"${wanted}\\")"])} }"'
)
_TERM_TEMPLATE = 'join(" ", [for event in each.value.events : "?\\"${event}\\""])'
_TERM_NARROWED_TEMPLATE = (
    'join(" ", concat([for event in each.value.events : "\\"${event}\\""], '
    '[for field, wanted in each.value.fields : "\\"${wanted}\\""]))'
)


def _patterns(alarm: str) -> tuple[str, str]:
    """The app (JSON) and box (term) patterns the module renders for ``alarm``."""
    text = MODULE.read_text()
    for template in (
        _JSON_TEMPLATE,
        _JSON_NARROWED_TEMPLATE,
        _TERM_TEMPLATE,
        _TERM_NARROWED_TEMPLATE,
    ):
        assert template in text, f"the pattern shapes changed: {template}"
    events = _catalogue()[alarm]["events"]
    fields = _narrowed_by().get(alarm, {})
    either = " || ".join(f'($.event = "{e}")' for e in events)
    if not fields:
        return "{ " + either + " }", " ".join(f'?"{e}"' for e in events)
    equal = " && ".join(f'($.{k} = "{v}")' for k, v in fields.items())
    terms = [f'"{e}"' for e in events] + [f'"{v}"' for v in fields.values()]
    return "{ (" + either + ") && " + equal + " }", " ".join(terms)


def _json_filter_matches(pattern: str, line: str) -> bool:
    wanted = set(re.findall(r'\(\$\.event = "([^"]+)"\)', pattern))
    assert wanted, pattern
    required = {
        k: v for k, v in re.findall(r'\(\$\.([a-z_]+) = "([^"]+)"\)', pattern) if k != "event"
    }
    try:
        parsed = json.loads(line)
    except ValueError:
        return False
    return (
        isinstance(parsed, dict)
        and parsed.get("event") in wanted
        and all(parsed.get(k) == v for k, v in required.items())
    )


def _term_filter_matches(pattern: str, line: str) -> bool:
    """CloudWatch's term syntax: ``?"a" ?"b"`` matches either term; plain
    ``"a" "b"`` needs every term."""
    either = re.findall(r'\?"([^"]+)"', pattern)
    if either:
        return any(term in line for term in either)
    every = re.findall(r'"([^"]+)"', pattern)
    assert every, pattern
    return all(term in line for term in every)


def _refusal_line(reason: str) -> str:
    """The line the supervisor writes for a refusal, as the box ships it."""
    return json.dumps(
        {
            "timestamp": STAMP,
            "level": "error",
            "event": box_logs.ORG_ADMISSION_REFUSED,
            "slot": 1,
            "org_id": ORG,
            "chat_id": CHAT,
            "reason": reason,
            "count": 1,
        }
    )


@pytest.mark.parametrize(
    ("reason", "counted"),
    [
        pytest.param("another_org", True, id="another-org-is-counted"),
        pytest.param("not_routed", False, id="not-routed-is-not"),
        pytest.param("other", False, id="other-is-not"),
    ],
)
async def test_the_cross_org_alarm_counts_only_another_org_refusals(
    reason: str, counted: bool
) -> None:
    app, box = _patterns("box_cross_org_refused")
    line = _refusal_line(reason)
    assert _json_filter_matches(app, line) is counted
    assert _term_filter_matches(box, line) is counted
    assert (
        org_events.refusal_code(
            next(r for r, code in org_events.REFUSAL_CODES.items() if code == "another_org")
        )
        == "another_org"
    ), "the code the alarm counts is the one a worker refuses with"


async def test_the_box_alarms_count_the_box_group_and_the_app_group() -> None:
    for alarm in ("box_worker_crash_loop", "box_cross_org_refused"):
        assert set(_catalogue()[alarm]["sources"]) == {"app", "box"}


async def test_the_backend_tells_ec2_nodes_the_group_the_box_alarms_count() -> None:
    root = MODULE.parents[2]
    for stack in ("envs/prod/app", "envs/staging/app"):
        monitoring = (root / stack / "monitoring.tf").read_text()
        params = (root / stack / "deploy-parameters.tf").read_text()
        assert "box = { name = one(module.compute_node[*].log_group_name)" in monitoring
        assert "ALKERA_EC2_LOG_GROUP            = module.compute_node[0].log_group_name" in params
    node = (root / "modules/compute-node/main.tf").read_text()
    grant = node.split('sid    = "ShipLogs"', 1)[1].split("}", 2)[0:2]
    assert '"logs:CreateLogStream"' in grant[0] and '"logs:PutLogEvents"' in grant[0]
    assert 'resources = ["${aws_cloudwatch_log_group.box.arn}:*"]' in "}".join(grant)
    assert 'output "log_group_name"' in (root / "modules/compute-node/outputs.tf").read_text()


# -- an EC2 box: the real supervisor's crash loop, into the node group ------------------


class _Logs:
    """Stands in for CloudWatch Logs: AWS is the costly boundary."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.streams: set[str] = set()

    def create_log_stream(self, *, logGroupName: str, logStreamName: str) -> None:  # noqa: N803
        self.streams.add(logStreamName)

    def put_log_events(self, *, logGroupName: str, logStreamName: str, logEvents: Any) -> None:  # noqa: N803
        self.messages.extend(e["message"] for e in logEvents)


@pytest.fixture
def events_logged() -> Iterator[None]:
    log = logging.getLogger(org_events.LOGGER)
    level = log.level
    log.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        log.setLevel(level)


def _crash_loop(tmp_path: Path) -> None:
    """The real supervisor sees an org's worker exit one time past the limit
    inside the window, and reports the crash loop."""
    sup = Supervisor(
        api=None,  # type: ignore[arg-type]
        feed=FileRoutingFeed(tmp_path / "routing.json"),
        slots=SlotTable(tmp_path / "slots.json"),
        orgs_root=tmp_path / "orgs",
        env={},
        isolation=IsolationReport(frozenset()),
        resume_path=tmp_path / "resume.json",
    )
    for n in range(CRASH_LOOP_EXITS + 1):
        sup._crashed(ORG, now=float(n), backoff=1.0)


def _wait(predicate: Any) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


async def test_an_ec2_boxs_crash_loop_line_is_counted_by_the_node_group_filter(
    tmp_path: Path, events_logged: None
) -> None:
    machine = "12345678-1234-5678-1234-567812345678"
    client = _Logs()
    sink = CloudWatchSink(
        group="/ec2/example-chat-box",
        stream=machine,
        region="us-east-1",
        client_factory=lambda: client,
    )
    shipper = install({"ALKERA_ALLOCATION_ID": machine}, sink=sink, flush_seconds=0.01)
    assert shipper is not None
    try:
        _crash_loop(tmp_path)
        _wait(lambda: client.messages)
    finally:
        shipper.close()
    assert client.streams == {machine}
    (line,) = client.messages
    assert json.loads(line)["machine_id"] == machine
    _app, crash_box = _patterns("box_worker_crash_loop")
    _app2, refused_box = _patterns("box_cross_org_refused")
    assert _term_filter_matches(crash_box, line)
    assert not _term_filter_matches(refused_box, line)


# -- any other box: through the ingestion route --------------------------------------------


class _Capture(BaseHTTPRequestHandler):
    bodies: ClassVar[list[bytes]] = []

    def do_POST(self) -> None:
        type(self).bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def captured_posts() -> Iterator[tuple[str, list[bytes]]]:
    handler = type("Handler", (_Capture,), {"bodies": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler.bodies
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _fresh_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    budget = box_log_service.EventBudget()
    monkeypatch.setattr(box_log_service, "event_budget", lambda: budget)


async def _post(client: AsyncClient, box: Box, body: bytes | dict[str, Any]) -> Any:
    if isinstance(body, bytes):
        return await client.post(
            INGEST_PATH,
            content=body,
            headers={**box.headers, "Content-Type": "application/json"},
        )
    return await client.post(INGEST_PATH, json=body, headers=box.headers)


def _event(event: str, level: str = "error", **fields: Any) -> dict[str, Any]:
    return {"timestamp": STAMP, "level": level, "event": event, "fields": fields}


async def test_a_boxs_crash_loop_posted_by_its_shipper_is_counted_by_the_app_group_filter(
    client: AsyncClient,
    org_admin: OrgWithAdmin,
    tmp_path: Path,
    events_logged: None,
    captured_posts: tuple[str, list[bytes]],
) -> None:
    box = await _box(org_admin, tenancy="pool")
    url, bodies = captured_posts
    shipper = install({}, sink=BackendSink(api_url=url, credential=box.raw), flush_seconds=0.01)
    assert shipper is not None
    try:
        _crash_loop(tmp_path)
        _wait(lambda: bodies)
    finally:
        shipper.close()
    with capture_logs() as logs:
        resp = await _post(client, box, bodies[0])
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"accepted": 1, "dropped": 0}
    (entry,) = [e for e in logs if e.get("origin") == "box"]
    assert entry["machine_id"] == str(box.machine_id)
    assert entry["log_level"] == "error"
    line = json.dumps(entry, default=str)
    crash_app, _crash_box = _patterns("box_worker_crash_loop")
    refused_app, _refused_box = _patterns("box_cross_org_refused")
    assert _json_filter_matches(crash_app, line)
    assert not _json_filter_matches(refused_app, line)
    assert _effects(await _decisions(org_admin.org_id, "machine_credential"))[-1] == (
        "allow",
        "machine_self",
    )


async def test_nothing_outside_the_allowlist_lands_in_the_backends_log(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    """A box changed to send more than its shipper allows: secrets in the one
    free-text field, content-like fields on an allowed event, events that are
    not system events at all. The route logs the allowed remainder only."""
    box = await _box(org_admin, tenancy="pool")
    body = {
        "events": [
            _event(
                "supervisor.org.reap_failed",
                slot=1,
                error=" ".join(f"Bearer {s}" for s in SECRETS[:4]),
                prompt=SECRETS[4],
                content=SECRETS[4],
                token=SECRETS[0],
                machine_id="00000000-0000-0000-0000-000000000000",
                origin="forged",
            ),
            _event(
                "supervisor.org_admission.refused",
                slot=2,
                org_id=ORG,
                chat_id=CHAT,
                reason=SECRETS[4],
                count=1,
            ),
            _event("chat.message.sent", content=SECRETS[4]),
            _event("harness.tool.output", output=SECRETS[0]),
            {
                "timestamp": SECRETS[0],
                "level": "loud",
                "event": "supervisor.worker.killed",
                "fields": {"slot": 3},
            },
        ]
    }
    with capture_logs() as logs:
        resp = await _post(client, box, body)
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"accepted": 3, "dropped": 2}
    landed = [e for e in logs if e.get("origin") == "box"]
    rendered = json.dumps(logs, default=str)
    for secret in SECRETS:
        assert secret not in rendered
    assert [e["event"] for e in landed] == [
        "supervisor.org.reap_failed",
        "supervisor.org_admission.refused",
        "supervisor.worker.killed",
    ]
    reap, refused, killed = landed
    assert set(reap) == {
        "event",
        "log_level",
        "origin",
        "machine_id",
        "box_timestamp",
        "slot",
        "org_id",
        "error",
    }
    # The event named no org, so the line names none (never the request's).
    assert reap["org_id"] is None
    # The machine is the one the credential holds, whatever the body says.
    assert {e["machine_id"] for e in landed} == {str(box.machine_id)}
    assert "reason" not in refused
    assert killed["box_timestamp"] is None and killed["log_level"] == "info"


async def test_a_machine_past_its_event_budget_has_the_rest_dropped_and_counted(
    client: AsyncClient, org_admin: OrgWithAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    budget = box_log_service.EventBudget(per_minute=60, burst=5, clock=lambda: now[0])
    monkeypatch.setattr(box_log_service, "event_budget", lambda: budget)
    box = await _box(org_admin, tenancy="pool")
    other = await _box(org_admin, tenancy="pool")
    batch = {"events": [_event("supervisor.worker.killed", slot=n) for n in range(8)]}
    with capture_logs() as logs:
        first = await _post(client, box, batch)
        # Another machine's budget is its own.
        theirs = await _post(client, other, batch)
        now[0] += 2.0  # two events' worth refilled
        again = await _post(client, box, batch)
    assert first.json() == {"accepted": 5, "dropped": 3}
    assert theirs.json() == {"accepted": 5, "dropped": 3}
    assert again.json() == {"accepted": 2, "dropped": 6}
    throttled = [e for e in logs if e["event"] == box_log_service.THROTTLED_EVENT]
    assert [(e["machine_id"], e["count"]) for e in throttled] == [
        (str(box.machine_id), 3),
        (str(other.machine_id), 3),
        (str(box.machine_id), 6),
    ]


async def test_a_batch_past_its_bound_is_refused_whole(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _box(org_admin, tenancy="pool")
    too_many = {"events": [_event("supervisor.worker.killed", slot=1)] * (MAX_BATCH_EVENTS + 1)}
    too_wide = {"events": [_event("supervisor.worker.killed", **{f"f{n}": n for n in range(17)})]}
    nested = {"events": [_event("supervisor.worker.killed", slot={"a": 1})]}
    with capture_logs() as logs:
        for body in (too_many, too_wide, nested):
            resp = await _post(client, box, body)
            assert resp.status_code == 422, resp.text
    assert [e for e in logs if e.get("origin") == "box"] == []


async def test_a_person_is_refused_as_needing_a_machine_credential(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    await login(client, org_admin.admin_email, org_admin.admin_password)
    with capture_logs() as logs:
        resp = await client.post(
            INGEST_PATH, json={"events": [_event("supervisor.worker.killed", slot=1)]}
        )
    assert resp.status_code == 401, resp.text
    assert _error(resp)["code"] == MACHINE_CREDENTIAL_REQUIRED
    assert [e for e in logs if e.get("origin") == "box"] == []
    assert ("deny", "machine_credential_required") in _effects(
        await _decisions(org_admin.org_id, "machine_credential")
    )


async def test_a_revoked_credential_is_refused_and_logs_nothing(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    box = await _box(org_admin, tenancy="pool")
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(MachineCredential)
            .where(MachineCredential.id == box.credential_id)
            .values(revoked_at=MachineCredential.created_at)
        )
        await session.commit()
    with capture_logs() as logs:
        resp = await _post(client, box, {"events": [_event("supervisor.worker.killed", slot=1)]})
    assert resp.status_code == 401, resp.text
    assert _error(resp)["code"] == MACHINE_CREDENTIAL_REFUSED
    assert [e for e in logs if e.get("origin") == "box"] == []


async def test_an_org_workers_credential_cannot_post_as_the_machine(
    client: AsyncClient, org_admin: OrgWithAdmin
) -> None:
    from alkera_core.auth import mint_machine_worker_token

    box = await _box(org_admin, tenancy="pool")
    async with AsyncSessionLocal() as session:
        await chat_on_box(
            session,
            org_id=org_admin.org_id,
            owner_id=org_admin.admin_id,
            machine=str(box.machine_id),
        )
    worker, _claims = mint_machine_worker_token(
        credential_id=box.credential_id, machine_id=box.machine_id, org_id=org_admin.org_id
    )
    with capture_logs() as logs:
        resp = await client.post(
            INGEST_PATH,
            json={"events": [_event("supervisor.worker.killed", slot=1)]},
            headers={"Authorization": f"Bearer {worker}"},
        )
    assert resp.status_code == 404, resp.text
    assert [e for e in logs if e.get("origin") == "box"] == []
    reasons = {
        r.payload["reason"] for r in await _decisions(org_admin.org_id, "machine_credential")
    }
    assert "org_bound_credential" in reasons


# -- whose event it is -----------------------------------------------------------------

OTHER_ORG = "f4dda5f5-b76f-4c6c-8cdc-8147fef8bd4a"


async def test_an_event_is_logged_under_the_org_it_names_never_the_requests() -> None:
    """B's worker exit was logged under A: the event named no org, and the
    line took the org the box credential's request had bound to the log
    context. Each event now names its worker's org, and the backend logs it
    under that org or none."""
    batch = BoxLogBatch.model_validate(
        {
            "events": [
                _event("supervisor.worker.exited", "warning", slot=2, org_id=ORG, ran=389.4),
                _event("supervisor.earlier_workers.outlasted", "info", waited=1.0, killed=False),
            ]
        }
    )
    with bound_contextvars(org_id=OTHER_ORG), capture_logs([merge_contextvars]) as logs:
        accepted, dropped = box_log_service.ingest("m-1", batch)
    assert (accepted, dropped) == (2, 0)
    exited, outlasted = [e for e in logs if e.get("origin") == "box"]
    assert exited["org_id"] == ORG
    assert outlasted["org_id"] is None
