"""Registering a person's own box through the device flow.

A person registers a machine they run as their own box by approving it in
the browser. The approval mints a MACHINE credential bound to their org and to
them (``personal`` tenancy): the box serves their own private chats in that org
and nothing else, and it stands only while they remain a member. It is never a
session: nothing here reads or writes ``auth.yml``, so the box holds no login
any code path could act as.

The credential is kept in ``<ALKERA_HOME>/personal_box.json`` (0600) beside the
pod id the box claimed its machine with, and ``cloud-mirror run`` reads it the
way a platform box reads ``ALKERA_MACHINE_CREDENTIAL``. The device flow and the
claim take an ``httpx`` transport and the clock as seams, so the whole flow is
driven in a test against a mock or a real backend.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import httpx
from alkera_core.atomic_io import write_text_atomic
from alkera_core.auth.machine_token import (
    looks_like_machine_token,
    looks_like_machine_worker_token,
)
from alkera_core.versioning import VersionedModel
from pydantic import Field, ValidationError

from alkera_cli.account import device_flow
from alkera_cli.cloud.rest import CloudApiError, CloudRestClient
from alkera_cli.host import paths

#: The scope the box asks for; the server names the client on the consent page.
BOX_SCOPE = "box"
#: How many chats a person's own box holds at once unless told otherwise.
DEFAULT_CAPACITY = 2


class PersonalBoxError(Exception):
    """Registration could not finish: the reason is the message."""


class PersonalBoxRecord(VersionedModel):
    """What a registered personal box keeps on disk. The credential is a bearer
    secret: the file is 0600, and the field never appears in a repr."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    api_url: str = ""
    credential: str = Field(default="", repr=False)
    provider_pod_id: str = ""
    name: str = ""
    machine_id: str = ""


def personal_box_path() -> Path:
    """Where the record lives, under this process's ``ALKERA_HOME``."""
    return paths.ALKERA_HOME / "personal_box.json"


def load_personal_box(path: Path | None = None) -> PersonalBoxRecord | None:
    """The stored record, or ``None`` when there is none or it cannot be read
    as one (an unreadable record is no credential, never a partial one)."""
    target = path or personal_box_path()
    try:
        record = PersonalBoxRecord.model_validate_json(target.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError):
        return None
    if not record.credential or not record.api_url or not record.provider_pod_id:
        return None
    return record


def save_personal_box(record: PersonalBoxRecord, path: Path | None = None) -> Path:
    target = path or personal_box_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target, record.model_dump_json(indent=2), mode=0o600)
    return target


def new_pod_id() -> str:
    """A stable id for this box's machine, made once at registration."""
    return f"personal-{uuid.uuid4().hex}"


def register_personal_box(
    api_url: str,
    *,
    name: str,
    daemon_version: str,
    provider_pod_id: str | None = None,
    announce: Callable[[device_flow.DeviceCodeResponse], None],
    capacity: int = DEFAULT_CAPACITY,
    transport: httpx.BaseTransport | None = None,
    claim_transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    path: Path | None = None,
) -> PersonalBoxRecord:
    """Run the device flow as a personal box, claim the machine on the
    credential it returns, and keep the record. ``announce`` shows the person
    where to approve. Raises :class:`PersonalBoxError` (or a
    :class:`~alkera_cli.account.device_flow.DeviceFlowError`) on failure; on any
    failure nothing is written. ``transport`` is the device flow's seam and
    ``claim_transport`` the claim's."""
    base = api_url.rstrip("/")
    code = device_flow.request_device_code(
        base, client_id=device_flow.CLIENT_ID_BOX, scope=BOX_SCOPE, transport=transport
    )
    announce(code)
    credential = device_flow.poll_for_token(
        base,
        code.device_code,
        client_id=device_flow.CLIENT_ID_BOX,
        interval=code.interval,
        expires_in=code.expires_in,
        sleep=sleep,
        now=now,
        transport=transport,
    )
    if not looks_like_machine_token(credential) or looks_like_machine_worker_token(credential):
        # A server that answered with a session (or anything else) is not one
        # this box may run on: it must never hold a login.
        raise PersonalBoxError("the server did not issue a machine credential for this box")
    pod_id = provider_pod_id or new_pod_id()
    rest = CloudRestClient(api_url=base, token=credential, agent_id=None, transport=claim_transport)
    try:
        claimed = asyncio.run(
            rest.claim_machine(
                credential=credential,
                name=name,
                provider_pod_id=pod_id,
                capacity=capacity,
                daemon_version=daemon_version,
            )
        )
    except (CloudApiError, httpx.HTTPError) as exc:
        raise PersonalBoxError(f"the server refused this box's claim: {exc}") from exc
    machine_id = str(claimed.get("id") or "")
    record = PersonalBoxRecord(
        api_url=base,
        credential=credential,
        provider_pod_id=pod_id,
        name=name,
        machine_id=machine_id,
    )
    save_personal_box(record, path)
    return record


__all__ = [
    "BOX_SCOPE",
    "PersonalBoxError",
    "PersonalBoxRecord",
    "load_personal_box",
    "new_pod_id",
    "personal_box_path",
    "register_personal_box",
    "save_personal_box",
]
