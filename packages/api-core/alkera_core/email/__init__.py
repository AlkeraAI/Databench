"""Outbound email — invitations, verification, and password-reset links.

For local dev, Mailpit accepts unauthenticated SMTP on port 1025 and exposes
received messages at http://localhost:8025. Production relays speak STARTTLS
and want a username/password pair — both are env-driven via `SMTP_USERNAME`,
`SMTP_PASSWORD`, and `SMTP_USE_TLS`. This works unchanged against SES or Resend
(`smtp.resend.com`, port 587, STARTTLS — username `resend`, password = API key).

In tests, monkeypatch the three senders to no-ops (see
`apps/backend/tests/conftest.py`). SMTP failures here are *logged*, never
raised: the persisted token/invitation row is the source of truth and the
user can re-request the email.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr

import aiosmtplib

from alkera_core.brand import (
    founders_sender_name,
    product_name,
    sender_name,
    support_email,
    welcome_email,
)
from alkera_core.config import settings
from alkera_core.email.render import render_email
from alkera_core.logging import get_logger
from alkera_core.models import Invitation, Team, User
from alkera_core.validation.display_name import scrub_display_name

log = get_logger(__name__)

#: Month names spelled here rather than taken from ``strftime("%b")``, which reads the
#: process's LC_TIME: a relay host with a non-English locale would otherwise send one
#: recipient "sept." and another "Sep" for the same row. Every other word in these
#: messages is English, so the date is too.
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _format_date(value: datetime) -> str:
    """Render an instant as a person reads it, with its zone: ``Sep 25, 2026 at
    11:52 PM UTC``.

    The reader's zone is unknowable server-side, and a bare calendar day is
    the UTC day — a different day than the portal shows a reader west of UTC,
    who then sees the same invitation expire on two dates. Naming the zone
    makes the instant exact for every reader; a machine timestamp
    ("2026-09-25T23:52:24+00:00") would be exact too, and unreadable.
    """
    day = value.astimezone(UTC) if value.tzinfo is not None else value
    hour = day.hour % 12 or 12
    meridiem = "AM" if day.hour < 12 else "PM"
    return (
        f"{_MONTHS[day.month - 1]} {day.day}, {day.year} at {hour}:{day.minute:02d} {meridiem} UTC"
    )


def _brand_suffix(org_name: str) -> str:
    """`" on Alkera"` — or nothing, when the org name already carries the brand.

    An invitation is the one message that must name the organization AND the product:
    the recipient knows the org and has never heard of us. But an org that named itself
    after the product turns that pairing into a stutter — "You're invited to Alkera Dev
    on Alkera" — so the trailing mention is dropped when the org name already says it.
    The From header carries the brand either way.
    """
    product = product_name()
    return "" if product.casefold() in org_name.casefold() else f" on {product}"


# Every human-typed name that reaches an outbound message goes through
# `scrub_display_name` first. The write path already refuses links and markup
# (alkera_core.validation.display_name), but rows written BEFORE that policy
# existed are still in the database, and a send is the wrong moment to start
# raising — so the render path repairs rather than refuses. A name that already
# satisfies the write policy passes through with only whitespace normalized.


def _build_message(
    *,
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    from_addr: str | None = None,
    from_name: str | None = None,
    reply_to: str | None = None,
) -> EmailMessage:
    """Assemble a message with the branded From header and an optional HTML part.

    `formataddr` renders `Databench <no-reply@example.com>` (and falls back to a
    bare address when the display name is empty). This is the single place the
    From header is built, so every sender stays consistently branded. `from_addr`
    / `from_name` override that default for senders that need a distinct identity
    (the welcome speaks as the founders); `reply_to`, when given, routes replies
    to a human inbox. Both default to the no-reply branding so existing senders
    are unaffected.

    `text_body` is always set as the `text/plain` part. When `html_body` is given,
    `add_alternative` appends it as `text/html`, producing a `multipart/alternative`
    in MIME's least-to-most-preferred order (text first), so a capable client shows
    the HTML and everything else falls back to the plain text.
    """
    message = EmailMessage()
    message["From"] = formataddr(
        (
            sender_name() if from_name is None else from_name,
            settings.smtp_from if from_addr is None else from_addr,
        )
    )
    message["To"] = to
    message["Subject"] = subject
    if reply_to is not None:
        message["Reply-To"] = reply_to
    message.set_content(text_body)
    if html_body is not None:
        message.add_alternative(html_body, subtype="html")
    return message


def _render_html(template: str, /, *, package: str | None = None, **context: object) -> str | None:
    """Render an HTML body for `template`, or `None` if rendering fails.

    A render failure must never propagate. `_send` swallows SMTP errors, and the
    public password-reset route returns a constant response on purpose so it can't be
    used to probe which emails have accounts — a render exception escaping the sender
    would reintroduce a 500-vs-200 enumeration oracle. So any failure here logs and
    degrades to a text-only email instead of raising.
    """
    try:
        return render_email(template, package=package, **context)
    except Exception as exc:
        # Deliberately broad: ANY render failure must degrade to text-only, never raise.
        log.warning("email.render_failed", template=template, error=str(exc))
        return None


_IN_FLIGHT_SENDS: set[asyncio.Task[object]] = set()
"""Strong references to the sends in flight: without them the event loop is
free to collect a task mid-await and the email silently never goes out."""


def _finish_background_send(task: asyncio.Task[object]) -> None:
    _IN_FLIGHT_SENDS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # `_send` already logs relay failures; this catches anything else a
        # sender raised, which would otherwise vanish with the task.
        log.warning("email.background_send.failed", task=task.get_name(), error=str(exc))


def send_in_background(send: Coroutine[object, object, object], *, name: str) -> None:
    """Run a send off the request path: the caller answers without waiting on
    the relay.

    For a public route whose answer must not depend on whether an address has an
    account. Awaiting the SMTP round-trip only on the branch that found one makes
    the response time say what the constant body hides. Failures are logged,
    never raised, exactly as an inline send's are.
    """
    task = asyncio.get_running_loop().create_task(send, name=name)
    _IN_FLIGHT_SENDS.add(task)
    task.add_done_callback(_finish_background_send)


async def drain_background_sends() -> None:
    """Wait for every send handed to :func:`send_in_background` so far."""
    while _IN_FLIGHT_SENDS:
        await asyncio.gather(*tuple(_IN_FLIGHT_SENDS), return_exceptions=True)


async def _send(message: EmailMessage, *, log_event: str, to: str, **log_fields: object) -> bool:
    """Single SMTP send path. Picks up auth + TLS from settings.

    Returns whether the relay accepted the message (a no-email deployment counts
    as accepted: nothing failed). A relay failure is logged, never raised; a
    caller that tells the user "we sent it" checks the result first.

    `aiosmtplib.send` skips AUTH only when username/password are None. An EMPTY
    string — what an unset `${SMTP_USERNAME:-}` becomes in compose/Helm — would make
    it ATTEMPT auth and fail "AUTH not supported" against an anonymous relay, so we
    coalesce empty → None for the common internal IP-allowlisted (no-auth) relay.

    No-email mode: when `email_enabled` is False the send is a logged no-op — a
    self-hosted / air-gapped deployment runs with no relay, and email-gated flows
    degrade elsewhere (verification auto-satisfied, invitations accepted in-app).
    """
    if not settings.email_enabled:
        skipped = f"{log_event.rsplit('.', 1)[0]}.skipped_no_email"
        log.info(skipped, to=to, **log_fields)
        return True
    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_use_tls,
            timeout=settings.smtp_timeout_seconds,
        )
        log.info(log_event, to=to, **log_fields)
    except (aiosmtplib.errors.SMTPException, OSError) as exc:
        fail_event = f"{log_event.rsplit('.', 1)[0]}.send_failed"
        log.warning(fail_event, to=to, error=str(exc), **log_fields)
        return False
    return True


async def send_invitation_email(
    invitation: Invitation,
    *,
    token: str,
    team: Team,
    org_name: str,
    inviter_display_name: str | None,
) -> None:
    """Compose + send a plain-text invitation email.

    `token` is the RAW invitation token (the row stores only its hash). The
    accept URL points at the SPA's signup page. The recipient lands on
    `/signup?invite=<token>`; the SPA either prompts new-user signup or, if
    the email already corresponds to a logged-in user, redirects them to
    `/dashboard/invites` to accept.
    """
    accept_url = f"{settings.frontend_base_url.rstrip('/')}/signup?invite={token}"
    # The three attacker-reachable strings in this message: whoever sends the
    # invite controls all of them, and the recipient has never seen our product
    # before — a link here is a phishing link wearing our sending domain.
    inviter = scrub_display_name(inviter_display_name) or "An admin"
    safe_org_name = scrub_display_name(org_name)
    safe_team_name = scrub_display_name(team.name)
    target = (
        f"the {safe_team_name!r} team in {safe_org_name!r}"
        if not team.is_root
        else f"the {safe_org_name!r} organization"
    )

    expires = _format_date(invitation.expires_at)
    brand_suffix = _brand_suffix(safe_org_name)

    body = (
        f"Hi,\n\n"
        f"{inviter} invited you to join {target}{brand_suffix}.\n\n"
        f"Accept your invitation:\n  {accept_url}\n\n"
        f"This invitation expires on {expires}.\n\n"
        f"If you didn't expect this email, you can safely ignore it.\n"
    )

    html_body = _render_html(
        "invitation",
        inviter=inviter,
        org_name=safe_org_name,
        team_name=None if team.is_root else safe_team_name,
        accept_url=accept_url,
        expires=expires,
        brand_suffix=brand_suffix,
    )
    message = _build_message(
        to=invitation.email,
        subject=f"You're invited to {safe_org_name}{brand_suffix}",
        text_body=body,
        html_body=html_body,
    )

    await _send(
        message,
        log_event="email.invitation.sent",
        to=invitation.email,
        team_id=str(team.id),
        invitation_id=str(invitation.id),
    )


def text_greeting(name: str) -> str:
    """The plain-text salutation: "Hi <name>," or "Hi," when there is no name to use."""
    safe = scrub_display_name(name)
    return f"Hi {safe}," if safe else "Hi,"


async def send_email_verification(user: User, *, token: str) -> bool:
    """Send the verification link to ``user.email``; True when the relay took it."""
    safe_name = scrub_display_name(user.display_name)
    verify_url = f"{settings.frontend_base_url.rstrip('/')}/verify-email/{token}"
    body = (
        f"{text_greeting(safe_name)}\n\n"
        f"Welcome to {product_name()}. Please confirm this "
        f"email address by visiting:\n"
        f"  {verify_url}\n\n"
        f"If you didn't create a {product_name()} account, "
        f"you can safely ignore this message.\n"
    )

    html_body = _render_html("verification", name=safe_name, verify_url=verify_url)
    message = _build_message(
        to=user.email,
        subject=f"Verify your {product_name()} email",
        text_body=body,
        html_body=html_body,
    )

    return await _send(
        message, log_event="email.verification.sent", to=user.email, user_id=str(user.id)
    )


async def send_password_reset(user: User, *, token: str) -> None:
    """Send a password-reset link."""
    safe_name = scrub_display_name(user.display_name)
    reset_url = f"{settings.frontend_base_url.rstrip('/')}/reset-password/{token}"
    body = (
        f"{text_greeting(safe_name)}\n\n"
        f"Someone requested a password reset for your {product_name()} account. "
        f"To choose a new password, visit:\n"
        f"  {reset_url}\n\n"
        f"This link expires in 1 hour. If you didn't request a reset, "
        f"you can ignore this email. Your password won't change.\n"
    )

    html_body = _render_html("password_reset", name=safe_name, reset_url=reset_url)
    message = _build_message(
        to=user.email,
        subject=f"Reset your {product_name()} password",
        text_body=body,
        html_body=html_body,
    )

    await _send(message, log_event="email.password_reset.sent", to=user.email, user_id=str(user.id))


async def send_welcome_email(user: User) -> None:
    """Welcome a freshly-verified user, in the founders' voice.

    Sent from and replying to :func:`alkera_core.brand.welcome_email` so a new
    user's reply lands with a person rather than the no-reply box. With no such
    address it goes out from the deployment's own sender (``smtp_from``) and
    replies go to the support address, if there is one. Like every sender, an
    SMTP failure is logged, never raised: the user is verified regardless.
    """
    product = product_name()
    app_url = settings.frontend_base_url.rstrip("/")
    safe_name = scrub_display_name(user.display_name)
    body = (
        f"{text_greeting(safe_name)}\n\n"
        f"Welcome to {product}! We're happy to be a part of your data journey.\n\n"
        f"We'd love to hear your feedback or questions. Just reply to "
        f"this email and it'll reach us directly.\n\n"
        f"Open {product}: {app_url}\n\n"
        f"— The {product} team\n"
    )

    html_body = _render_html("welcome", name=safe_name, app_url=app_url)
    message = _build_message(
        to=user.email,
        subject=f"Welcome to {product}",
        text_body=body,
        html_body=html_body,
        from_addr=welcome_email(),
        from_name=founders_sender_name(),
        reply_to=welcome_email() or support_email(),
    )

    await _send(message, log_event="email.welcome.sent", to=user.email, user_id=str(user.id))


__all__ = [
    # Re-exported (not defined here) so the test seams that patch
    # `alkera_core.email.aiosmtplib` / `render_email` resolve under strict reexport.
    "aiosmtplib",
    "drain_background_sends",
    "render_email",
    "send_email_verification",
    "send_in_background",
    "send_invitation_email",
    "send_password_reset",
    "send_welcome_email",
]
