"""Smoke test for the ``AlkeraClient.errors`` namespace against a mock transport.

The backend suite covers the routes end-to-end; this guarantees the wrapper
posts the right bodies to the right URLs and decodes the responses.
"""

from __future__ import annotations

import json

import httpx
from alkera_sdk import AlkeraClient
from alkera_sdk._generated.models.client_error_event import ClientErrorEvent
from alkera_sdk._generated.models.crash_report_create import CrashReportCreate
from alkera_sdk._generated.models.crash_report_create_component import CrashReportCreateComponent


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "POST" and path == "/api/v1/errors/reports":
        body = json.loads(request.content)
        assert body["component"] == "cli"
        assert body["message"] == "boom"
        return httpx.Response(
            201,
            json={
                "id": "00000000-0000-0000-0000-000000000001",
                "user_id": "00000000-0000-0000-0000-000000000002",
                "org_team_id": "00000000-0000-0000-0000-000000000003",
                "component": "cli",
                "error_type": None,
                "message": "boom",
                "stacktrace": None,
                "context": None,
                "comment": None,
                "app_version": None,
                "platform": None,
                "occurred_at": None,
                "created_at": "2026-06-08T00:00:00Z",
            },
        )
    if request.method == "POST" and path == "/api/v1/errors/events":
        return httpx.Response(200, json={"received": True, "trace_id": "trace-1"})
    return httpx.Response(404, json={"error": {"code": "not_found"}})


def test_submit_crash_report_posts_and_parses() -> None:
    transport = httpx.MockTransport(_handler)
    with AlkeraClient(base_url="http://test", httpx_args={"transport": transport}) as c:
        report = c.errors.submit_crash_report(
            CrashReportCreate(component=CrashReportCreateComponent.CLI, message="boom")
        )
        assert str(report.id) == "00000000-0000-0000-0000-000000000001"
        assert report.component == "cli"


def test_report_client_error_posts_and_parses() -> None:
    transport = httpx.MockTransport(_handler)
    with AlkeraClient(base_url="http://test", httpx_args={"transport": transport}) as c:
        # component defaults to "web" server-side when omitted.
        ack = c.errors.report_client_error(ClientErrorEvent(message="oops"))
        assert ack.received is True
        assert ack.trace_id == "trace-1"
