"""The account lifecycle emails: deletion scheduled, cancelled, blocked and
completed.

All four share one template (``account_notice``): a heading, a few sentences,
an optional button and an optional footnote, sent through :func:`send_notice`,
which another domain's account emails use too. The worker sends the blocked
and completed notices; the backend sends scheduled and cancelled. Each returns
whether the send went out, never raises.
"""

from __future__ import annotations

from datetime import datetime

from alkera_core.account.notices import AccountNotice, DeletionBlocked, DeletionCompleted
from alkera_core.brand import product_name
from alkera_core.config import settings
from alkera_core.email import _build_message, _format_date, _render_html, _send, text_greeting

#: Where the account settings live in the SPA; the deletion section has this anchor.
ACCOUNT_SETTINGS_PATH = "/settings/profile"
DELETION_ANCHOR = "#delete-account"

#: The sentence a blocked erasure's email gives for each blocker code.
BLOCKER_SENTENCES: dict[str, str] = {
    "last_admin": "you are the only admin of an organization that still has other members",
    "live_compute": "a machine is still running for you",
    "org_machines": "an organization that would close with your account still has machines",
    "paid_plan": "a paid plan on your account still renews",
    "unpaid_balance": "a plan on your account has an unpaid balance",
    "legal_hold": "some of your data is under a legal hold",
    "platform_staff": "the account still holds a platform staff role",
}


def _settings_url(anchor: str = "") -> str:
    return f"{settings.frontend_base_url.rstrip('/')}{ACCOUNT_SETTINGS_PATH}{anchor}"


async def send_notice(
    *,
    to: str,
    subject: str,
    heading: str,
    lines: list[str],
    log_event: str,
    cta_url: str | None = None,
    cta_label: str | None = None,
    footnote: str | None = None,
) -> bool:
    """Send one account notice in the ``account_notice`` template."""
    text = "\n\n".join([heading, *lines])
    if cta_url:
        text += f"\n\n{cta_label}:\n  {cta_url}"
    if footnote:
        text += f"\n\n{footnote}"
    html = _render_html(
        "account_notice",
        heading=heading,
        lines=lines,
        cta_url=cta_url,
        cta_label=cta_label,
        footnote=footnote,
    )
    message = _build_message(to=to, subject=subject, text_body=text + "\n", html_body=html)
    return await _send(message, log_event=log_event, to=to)


async def send_deletion_scheduled(to: str, *, name: str, purge_after: datetime) -> bool:
    product = product_name()
    return await send_notice(
        to=to,
        subject=f"Your {product} account will be deleted on {_format_date(purge_after)}",
        heading=text_greeting(name),
        lines=[
            f"Your {product} account is scheduled for deletion on {_format_date(purge_after)}.",
            "Until then you can sign in and cancel from your account settings.",
        ],
        cta_url=_settings_url(DELETION_ANCHOR),
        cta_label="Keep my account",
        footnote=(
            "If you didn't ask for this, sign in, cancel the deletion and change your password."
        ),
        log_event="email.account_deletion_scheduled.sent",
    )


async def send_deletion_cancelled(to: str, *, name: str) -> bool:
    product = product_name()
    return await send_notice(
        to=to,
        subject=f"Your {product} account will not be deleted",
        heading=text_greeting(name),
        lines=[f"The scheduled deletion of your {product} account was cancelled."],
        log_event="email.account_deletion_cancelled.sent",
    )


async def send_deletion_blocked(to: str, *, name: str, code: str) -> bool:
    product = product_name()
    reason = BLOCKER_SENTENCES.get(code, "something still needs to be settled")
    return await send_notice(
        to=to,
        subject=f"Your {product} account deletion is on hold",
        heading=text_greeting(name),
        lines=[
            f"We couldn't delete your {product} account on schedule: {reason}.",
            "Once that is settled the deletion goes ahead. You can also cancel it.",
        ],
        cta_url=_settings_url(DELETION_ANCHOR),
        cta_label="Open account settings",
        log_event="email.account_deletion_blocked.sent",
    )


async def send_deletion_completed(to: str, *, name: str) -> bool:
    product = product_name()
    return await send_notice(
        to=to,
        subject=f"Your {product} account has been deleted",
        heading=text_greeting(name),
        lines=[
            f"Your {product} account and the personal data in it have been deleted.",
            "Billing and audit records we must keep no longer carry your name or address.",
        ],
        log_event="email.account_deletion_completed.sent",
    )


async def send_account_notice(notice: AccountNotice) -> None:
    """Deliver one of the account lifecycle's notices by email: the
    :data:`~alkera_core.account.notices.AccountNoticeSender` the worker hands
    the lifecycle."""
    match notice:
        case DeletionBlocked():
            await send_deletion_blocked(notice.to, name=notice.name, code=notice.code)
        case DeletionCompleted():
            await send_deletion_completed(notice.to, name=notice.name)


__all__ = [
    "BLOCKER_SENTENCES",
    "send_account_notice",
    "send_deletion_blocked",
    "send_deletion_cancelled",
    "send_deletion_completed",
    "send_deletion_scheduled",
    "send_notice",
]
