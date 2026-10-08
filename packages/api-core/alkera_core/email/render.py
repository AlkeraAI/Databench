"""Render branded HTML email bodies from MJML templates, entirely in-process.

Pipeline: Jinja2 fills the ``.mjml`` template (variables + the ``tokens`` brand module)
→ MRML (the Rust MJML engine, ``mrml`` wheel) compiles the filled MJML to Outlook-safe
HTML with inlined styles. No Node, no subprocess, no network — it slots straight into
the synchronous send path in ``alkera_core.email``.

Templates ship as package data under ``alkera_core/email/templates/`` (Jinja2's
``PackageLoader`` resolves them via ``importlib`` in both an editable checkout and the
built wheel / Docker image, exactly like ``backend.auth`` loads its data file).
"""

from __future__ import annotations

from functools import cache
from typing import Any

import mrml
from jinja2 import ChoiceLoader, Environment, PackageLoader, StrictUndefined, select_autoescape

from alkera_core.brand import product_name, support_email
from alkera_core.config import settings
from alkera_core.email import tokens
from alkera_core.money import usd_display

# StrictUndefined: a typo'd variable raises at render time (caught by the sender's
# defensive guard + the render tests) rather than silently emitting an empty string.
# autoescape: every interpolated value lands in HTML/XML context, and template inputs
# (display names, org/team names) are user-controlled — escape them. The `tokens`
# brand values round-trip through MRML's XML parser unharmed.
#
# The escaping decision is made by file extension so it is auditable per template:
# `.mjml` is not in select_autoescape's built-in html/htm/xml list, so `default=True`
# is what keeps it escaped; `.txt` is the only opt-out (a future plain-text template
# must not grow HTML entities), and no such template exists today; a string template
# (`from_string`) escapes too. Escaping is also what guarantees the MJML handed to
# MRML is well-formed XML — a bare `&` or `<` in an org name would otherwise be a
# parse error that the sender's guard turns into a silently text-only email.
_SHARED_TEMPLATES = PackageLoader("alkera_core.email", "templates")

_env = Environment(
    loader=_SHARED_TEMPLATES,
    autoescape=select_autoescape(
        default_for_string=True, default=True, disabled_extensions=("txt",)
    ),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals["tokens"] = tokens
# Every amount of money an email names reads through the product's one money rule.
_env.filters["usd"] = usd_display

#: Every Jinja ``Environment`` in the codebase, by name. A test enumerates this
#: registry (escaping + StrictUndefined per entry) and greps the tree for any
#: ``Environment(`` / ``Template(`` / ``from_string(`` / ``Markup(`` outside it, so a
#: second environment cannot appear without being registered and audited here.
ENVIRONMENTS: tuple[tuple[str, Environment], ...] = (("email", _env),)


@cache
def environment_for(package: str | None) -> Environment:
    """The email environment, reading ``package``'s ``email_templates/`` folder after
    the shared one.

    A distribution keeps the templates only it sends beside its senders and names its
    package when it renders them. The shared folder comes first, so such a folder
    adds templates that extend ``base.mjml`` and can never replace a shared one. The
    overlay keeps the escaping, undefined handling, globals and filters of ``_env``.
    """
    if package is None:
        return _env
    return _env.overlay(
        loader=ChoiceLoader([_SHARED_TEMPLATES, PackageLoader(package, "email_templates")])
    )


def render_email(name: str, /, *, package: str | None = None, **context: Any) -> str:
    """Render ``templates/{name}.mjml`` to a full HTML document, or with ``package``
    set, the template of that name in its ``email_templates/`` folder.

    Raises on an unknown template, a missing template variable (``StrictUndefined``),
    or MJML that MRML can't parse. Callers in ``alkera_core.email`` wrap this in a
    try/except so a render failure degrades to a text-only email instead of surfacing
    to (and 500-ing / leaking from) the request.

    Branding (``product_name`` / ``support_email`` / ``logo_url``) is injected from
    settings on every render so a self-hosted deployment renders under its own brand;
    an explicit keyword in ``context`` still wins.

    ``logo_url`` is opt-in: only a set ``brand_email_logo_url`` produces the header
    wordmark image. Unset (or empty) → falsy → the header degrades to the text
    wordmark, so a self-hosted instance that doesn't configure a logo just shows the
    product-name text.
    """
    ctx: dict[str, Any] = {
        "product_name": product_name(),
        "support_email": support_email() or "",
        "logo_url": settings.brand_email_logo_url or "",
        **context,
    }
    mjml_source = environment_for(package).get_template(f"{name}.mjml").render(**ctx)
    return mrml.to_html(mjml_source).content
