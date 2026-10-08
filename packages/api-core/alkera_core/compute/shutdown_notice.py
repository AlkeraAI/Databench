"""A provider's notice that it is about to stop a machine, and the signed call
that turns one into a drain.

Providers say a machine is going away before they stop it — EC2's scheduled
events (retirement, a host stop) and RunPod's host-maintenance emails. A box
that is going away should stop taking chats and hand the ones it holds on
while it still can, which is exactly a drain. The backend accepts such a
notice at ``POST /api/v1/compute/shutdown-notices``, from anything that
holds the deployment's shared notice secret: an EventBridge rule's API
destination, or the email hook this module ships.

The wire. The body is a :class:`ShutdownNotice` as JSON. It is signed with
HMAC-SHA256 over ``"<unix seconds>.<body>"`` under the secret and sent as
``X-Alkera-Signature: t=<unix seconds>,v1=<hex digest>``. The backend refuses
a signature older or newer than :data:`SIGNATURE_TOLERANCE_SECONDS`; a drain
is idempotent, so a replay inside that window changes nothing.

The email hook. :func:`notices_from_email` reads the machine ids out of a
provider's notice email, and ``python -m alkera_core.compute.shutdown_notice``
reads one RFC 822 message on stdin and posts a signed notice for each
machine it names, to ``ALKERA_API_URL`` under ``ALKERA_SHUTDOWN_NOTICE_SECRET``.
Wire it behind whatever receives the mailbox the provider writes to (an SES
receipt rule's Lambda, a procmail rule).
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import hmac
import os
import re
import sys
import time
from collections.abc import Iterable
from email.message import EmailMessage
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from alkera_core.compute.provider import EC2, RUNPOD

#: The header the signature travels in.
SIGNATURE_HEADER: Final = "X-Alkera-Signature"
#: How far a signature's timestamp may be from the receiver's clock.
SIGNATURE_TOLERANCE_SECONDS: Final = 300
#: Where the backend accepts a notice.
NOTICE_PATH: Final = "/api/v1/compute/shutdown-notices"
#: The shortest secret the backend will verify against.
MIN_SECRET_LENGTH: Final = 32

#: A provider's machine id: what ``provider_machine_id`` holds. Kept to a plain
#: charset so a notice can never smuggle anything into a query or a log line.
_MACHINE_ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"


class ShutdownNotice(BaseModel):
    """One machine a provider is about to stop."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(pattern=f"^({EC2}|{RUNPOD})$")
    machine_id: str = Field(pattern=f"^{_MACHINE_ID}$")
    reason: str = Field(default="", max_length=400)
    #: What delivered the notice (``email``, ``eventbridge``, ...), for the
    #: machine's history.
    source: str = Field(default="api", pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    #: Release the machine once the drain has emptied it. A machine a provider
    #: is retiring is not coming back, so this is the default.
    auto_terminate: bool = True


class ShutdownNoticeResult(BaseModel):
    """What the backend did with a notice."""

    allocation_id: str
    state: str
    #: False when the machine was already draining (a repeated notice).
    drained: bool


def sign(secret: str, body: bytes, *, timestamp: int) -> str:
    """The ``X-Alkera-Signature`` value for ``body`` sent at ``timestamp``."""
    digest = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(
    secret: str,
    body: bytes,
    header: str,
    *,
    now: float | None = None,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Whether ``header`` is a signature of ``body`` under ``secret``, made
    within ``tolerance`` seconds of ``now``. Constant-time in the digest; any
    malformed header is simply false."""
    fields: dict[str, str] = {}
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if sep:
            fields[key] = value
    raw_t, given = fields.get("t", ""), fields.get("v1", "")
    if not raw_t.isdigit() or not given:
        return False
    timestamp = int(raw_t)
    moment = time.time() if now is None else now
    if abs(moment - timestamp) > tolerance:
        return False
    expected = sign(secret, body, timestamp=timestamp).partition("v1=")[2]
    return hmac.compare_digest(expected, given)


#: An EC2 instance id, wherever it appears in the message.
_EC2_ID = re.compile(r"\b(i-[0-9a-f]{8,17})\b")
#: A RunPod pod id is a bare token, so it is read only where the message names
#: it as one ("Pod ID: abc123", "pod abc123xyz is scheduled ...").
_RUNPOD_ID = re.compile(r"\bpod(?:\s+id)?\s*[:#]?\s*([a-z0-9]{8,24})\b", re.IGNORECASE)


def notices_from_email(subject: str, body: str) -> list[ShutdownNotice]:
    """The machines a provider's notice email names, one notice each.

    EC2 ids are recognised anywhere; a RunPod pod id only where the text calls
    it a pod, since a bare token proves nothing. Nothing is invented: an email
    that names no machine yields no notice. The subject becomes the reason."""
    text = f"{subject}\n{body}"
    reason = " ".join(subject.split())[:400]
    found: list[ShutdownNotice] = []
    seen: set[tuple[str, str]] = set()
    for provider, ids in (
        (EC2, _EC2_ID.findall(text)),
        (RUNPOD, [m for m in _RUNPOD_ID.findall(text) if not m.startswith("i-")]),
    ):
        for machine_id in ids:
            key = (provider, machine_id)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                ShutdownNotice(
                    provider=provider, machine_id=machine_id, reason=reason, source="email"
                )
            )
    return found


def _message_text(raw: bytes) -> tuple[str, str]:
    """The subject and the plain-text body of one RFC 822 message."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    subject = str(message.get("subject", ""))
    if not isinstance(message, EmailMessage):
        return subject, ""
    part = message.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if isinstance(part, EmailMessage) else ""
    return subject, str(body)


def post_notices(
    notices: Iterable[ShutdownNotice], *, api_url: str, secret: str, timeout: float = 15.0
) -> list[tuple[ShutdownNotice, int]]:
    """Send each notice, signed; the HTTP status each one got back."""
    import httpx

    sent: list[tuple[ShutdownNotice, int]] = []
    with httpx.Client(base_url=api_url.rstrip("/"), timeout=timeout) as client:
        for notice in notices:
            body = notice.model_dump_json().encode("utf-8")
            header = sign(secret, body, timestamp=int(time.time()))
            resp = client.post(
                NOTICE_PATH,
                content=body,
                headers={SIGNATURE_HEADER: header, "Content-Type": "application/json"},
            )
            sent.append((notice, resp.status_code))
    return sent


def main(argv: list[str] | None = None) -> int:
    """Read one email on stdin and post a notice for each machine it names.

    Exit 0 when every notice was accepted (or the email named none), 1 when
    any was refused, 2 when the hook is not configured."""
    api_url = os.environ.get("ALKERA_API_URL", "")
    secret = os.environ.get("ALKERA_SHUTDOWN_NOTICE_SECRET", "")
    if not api_url or len(secret) < MIN_SECRET_LENGTH:
        print("ALKERA_API_URL and ALKERA_SHUTDOWN_NOTICE_SECRET must be set", file=sys.stderr)
        return 2
    subject, body = _message_text(sys.stdin.buffer.read())
    notices = notices_from_email(subject, body)
    if not notices:
        print("the message names no machine", file=sys.stderr)
        return 0
    results = post_notices(notices, api_url=api_url, secret=secret)
    for notice, status in results:
        print(f"{notice.provider} {notice.machine_id}: {status}", file=sys.stderr)
    return 0 if all(200 <= status < 300 for _n, status in results) else 1


if __name__ == "__main__":  # pragma: no cover - the entry point
    sys.exit(main())


__all__ = [
    "MIN_SECRET_LENGTH",
    "NOTICE_PATH",
    "SIGNATURE_HEADER",
    "SIGNATURE_TOLERANCE_SECONDS",
    "ShutdownNotice",
    "ShutdownNoticeResult",
    "main",
    "notices_from_email",
    "post_notices",
    "sign",
    "verify",
]
