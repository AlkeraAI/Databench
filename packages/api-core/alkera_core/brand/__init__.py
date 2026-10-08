"""The brand a person sees: product name, sender name and terminal art.

One seam. The open platform shows Databench; a product registers its own
:class:`Brand` into :data:`BRAND` during composition and every surface follows:
transactional email, the MFA issuer, the API title, ``GET /api/v1/config`` (which
the portal reads for its page titles) and the terminal banner. Code reads
:func:`current_brand` or :func:`product_name`; none names a brand or compares an
edition string.

A deployment may still rename the product without a build through
``BRAND_PRODUCT_NAME``; :func:`product_name` honours it.
"""

from __future__ import annotations

from dataclasses import dataclass

from alkera_core.config import settings
from alkera_core.extensions import ExtensionError, ExtensionPoint


@dataclass(frozen=True, slots=True)
class Brand:
    """Everything that names the product to a person."""

    key: str
    product_name: str
    #: The From display name on transactional email.
    sender_name: str
    #: The credit line where one reads naturally (an about panel, a footer).
    attribution: str
    #: The block-letter wordmark the terminal prints at open, rows of equal width.
    terminal_wordmark: tuple[str, ...]
    #: The small mark the terminal shows when the wordmark does not fit.
    terminal_mark: tuple[str, ...]
    #: What an agent is called where an actor is named ("<agent> for <person>");
    #: the system itself is called by :func:`product_name`.
    agent_name: str = "Agent"
    #: Where people write for help, when the brand has a support inbox.
    support_email: str | None = None
    #: Where people write about an Enterprise plan.
    sales_email: str | None = None
    #: The welcome email's From and Reply-To, a person rather than a no-reply box.
    welcome_email: str | None = None


DATABENCH = Brand(
    key="databench",
    product_name="Databench",
    sender_name="Databench",
    attribution="Databench by Alkera",
    terminal_wordmark=(
        "██████   █████  ████████  █████  ██████  ███████ ███    ██  ██████ ██   ██",
        "██   ██ ██   ██    ██    ██   ██ ██   ██ ██      ████   ██ ██      ██   ██",
        "██   ██ ███████    ██    ███████ ██████  █████   ██ ██  ██ ██      ███████",
        "██   ██ ██   ██    ██    ██   ██ ██   ██ ██      ██  ██ ██ ██      ██   ██",
        "██████  ██   ██    ██    ██   ██ ██████  ███████ ██   ████  ██████ ██   ██",
    ),
    terminal_mark=(
        "╭─────────╮",
        "│ ██████  │",
        "│ ██   ██ │",
        "│ ██   ██ │",
        "│ ██████  │",
        "╰─────────╯",
    ),
)

#: The product brand. At most one is registered; with none, Databench is shown.
BRAND: ExtensionPoint[Brand] = ExtensionPoint("alkera.brand")


def current_brand() -> Brand:
    """The registered product brand, else Databench. Freezes :data:`BRAND`."""
    registered = BRAND.items()
    if len(registered) > 1:
        names = ", ".join(brand.key for brand in registered)
        raise ExtensionError(f"only one brand may be registered, found {names}")
    return registered[0] if registered else DATABENCH


def product_name() -> str:
    """The product name: the deployment's ``BRAND_PRODUCT_NAME``, else the brand's."""
    return settings.brand_product_name or current_brand().product_name


def agent_name() -> str:
    """What an agent is called where an actor is named."""
    return current_brand().agent_name


def sender_name() -> str:
    """The From display name: ``SMTP_FROM_NAME`` when set (empty sends a bare
    address), else the brand's."""
    configured = settings.smtp_from_name
    return current_brand().sender_name if configured is None else configured


def founders_sender_name() -> str:
    """The welcome email's From display name, under the same rule."""
    configured = settings.email_founders_from_name
    return current_brand().sender_name if configured is None else configured


def support_email() -> str | None:
    """The support address: ``BRAND_SUPPORT_EMAIL``, else the brand's, else none."""
    return settings.brand_support_email or current_brand().support_email


def sales_email() -> str | None:
    """The Enterprise sales address: ``BRAND_SALES_EMAIL``, else the brand's, else none."""
    return settings.brand_sales_email or current_brand().sales_email


def welcome_email() -> str | None:
    """The welcome email's address: ``EMAIL_FOUNDERS_FROM``, else the brand's, else
    none (it then goes out from ``SMTP_FROM`` with replies to :func:`support_email`)."""
    return settings.email_founders_from or current_brand().welcome_email


__all__ = [
    "BRAND",
    "DATABENCH",
    "Brand",
    "agent_name",
    "current_brand",
    "founders_sender_name",
    "product_name",
    "sales_email",
    "sender_name",
    "support_email",
    "welcome_email",
]
