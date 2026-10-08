"""The one policy for human-typed display names -- org, team, and person names.

These names are not merely rendered in the product UI: the invitation email
embeds the org name, the team name, and the inviter's display name in its prose
and its subject line. Mail clients auto-link URL-shaped text, so an org named
``Security Compliance Portal (verify your account at https://evil.example/login
before accepting)`` ships a live attacker link inside a message sent from our own
domain and our own sending reputation -- past the recipient's spam filter and
into a message they have every reason to trust. Jinja's autoescape stops markup
from *rendering*, it does nothing about that.

So the value itself is constrained at the door. Two entry points, one rule set:

``validate_display_name``  the write path -- raises, so a bad name is a 422 the
                           caller sees rather than a surprise in someone's inbox.
``scrub_display_name``     the email-render path -- neutralizes instead of
                           raising, because rows written before this policy
                           existed still have to render (and a send is the wrong
                           place to start failing).

Deliberately NOT rejected: a bare dotted token like ``Acme.io``. Startup org
names look exactly like that, and some clients will still auto-link it -- the
false-positive cost of banning it is far higher than the marginal phishing value
of a link with no path. The residual is accepted knowingly; every payload with a
scheme, a host + path, ``www.``, an address, or markup is refused.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated

from pydantic import AfterValidator, ValidationInfo

#: Generous for a real org/team/person name, short enough that a name can't
#: become a paragraph of persuasion in an email subject line.
MAX_DISPLAY_NAME_LENGTH = 100

#: What a scrubbed link becomes. Visible on purpose -- a silently vanished
#: phrase reads as a rendering bug; this reads as a policy.
LINK_PLACEHOLDER = "[link removed]"


class DisplayNameError(ValueError):
    """A display name violates the policy. Raised by ``validate_display_name``;
    the Pydantic field validators surface it as a 422."""


# Unicode general categories that must never survive: Cc/Cf are the C0/C1
# controls plus the format characters (zero-width joiners, and the bidi
# overrides behind Trojan-Source-style spoofing, where the rendered order of a
# name differs from the bytes anyone reviewing it would read). Cs/Co/Cn are
# surrogates, private-use, and unassigned -- nothing a person types on purpose.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})

# Any scheme, not just http/https: the reported payloads included
# `strawberry://settings/team`, and a client will happily linkify an unknown
# scheme (or hand it to a registered protocol handler).
_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
# `www.` at a token boundary -- the scheme-less form every mail client links.
# The boundary is `\b`, the SAME rule the render-time pattern below uses: an
# earlier form anchored on start-of-string or whitespace/bracket, which let
# `Acme Support-www.alkera-verify.example` through the write path even though
# the renderer would still rewrite it. Two layers of one policy have to agree by
# construction, or the layer that is absent on a path is the one that mattered.
_WWW = re.compile(r"\bwww\.", re.IGNORECASE)
# A dotted host followed by a path separator: `evil.example/login`. The slash is
# what separates this from a plain company name like `Acme.io`.
_HOST_PATH = re.compile(r"[a-z0-9\-]+(?:\.[a-z0-9\-]+)+/", re.IGNORECASE)
# An address -- clients turn it into a `mailto:` link, and it reads as an
# official contact ("billing@alkera-support.example").
_EMAIL_LIKE = re.compile(r"[^\s@]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+", re.IGNORECASE)
# Markup. Autoescape neutralizes it in HTML, but the plain-text alternative part
# of every one of our emails carries the raw string, and so does the subject.
_ANGLE = re.compile(r"[<>]")
# A whole tag, removed as a unit when scrubbing so no debris is left behind
# (dropping only the brackets turns "<h1>x</h1>" into the nonsense "h1x/h1").
_TAG = re.compile(r"<[^<>]*>")

# Order matters: the scheme form is matched before the bare host+path form so a
# full URL is replaced once, not twice.
_LINKISH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[a-z][a-z0-9+.\-]*://\S*", re.IGNORECASE),
    re.compile(r"\bwww\.\S*", re.IGNORECASE),
    _EMAIL_LIKE,
    re.compile(r"[a-z0-9\-]+(?:\.[a-z0-9\-]+)+/\S*", re.IGNORECASE),
)


def normalize_display_name(value: str) -> str:
    """NFC-normalize, drop forbidden control/format characters, and collapse
    every run of whitespace to a single space.

    Normalization is not cosmetic here. Composed and decomposed forms of the
    same name compare unequal, and a name padded with tabs or newlines breaks
    out of a single line of email prose -- so both are settled before any of the
    rules below look at the string.
    """
    normalized = unicodedata.normalize("NFC", value)
    kept = [
        ch
        for ch in normalized
        # Whitespace is its own category (Zs) plus the ASCII controls we want to
        # fold rather than delete -- keep those so the collapse below turns them
        # into a single space instead of joining two words together.
        if ch in " \t\r\n" or unicodedata.category(ch) not in _FORBIDDEN_CATEGORIES
    ]
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def validate_display_name(value: str, *, field: str = "name") -> str:
    """The normalized name, or ``DisplayNameError`` naming what is wrong.

    ``field`` only shapes the message ("Organization name", "Team name") so the
    422 tells the user which box to fix.
    """
    name = normalize_display_name(value)
    if not name:
        raise DisplayNameError(f"{field} cannot be empty")
    if _ANGLE.search(name):
        raise DisplayNameError(f"{field} cannot contain the characters < or >")
    if _SCHEME.search(name) or _WWW.search(name) or _HOST_PATH.search(name):
        raise DisplayNameError(
            f"{field} cannot contain a web address. Names appear in invitation "
            "emails, so links are not allowed here."
        )
    if _EMAIL_LIKE.search(name):
        raise DisplayNameError(
            f"{field} cannot contain an email address. Names appear in invitation "
            "emails, so addresses are not allowed here."
        )
    # Length last, so a payload that is BOTH over-long and link-bearing is told
    # about the link -- the reason it will still be rejected once shortened.
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise DisplayNameError(
            f"{field} cannot be longer than {MAX_DISPLAY_NAME_LENGTH} characters"
        )
    return name


def scrub_display_name(value: str | None) -> str:
    """The same policy applied as a repair rather than a refusal -- for names
    already stored before the write path enforced it.

    Every linkifiable run becomes {LINK_PLACEHOLDER}, markup characters are
    dropped, and the result is truncated. A name that already satisfies
    ``validate_display_name`` passes through with only whitespace normalized, so
    this is safe to apply unconditionally at render time.
    """
    if not value:
        return ""
    name = normalize_display_name(value)
    for pattern in _LINKISH_PATTERNS:
        name = pattern.sub(LINK_PLACEHOLDER, name)
    name = _ANGLE.sub("", _TAG.sub("", name))
    # The substitutions can leave doubled spaces behind ("at [link removed] .").
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        name = name[: MAX_DISPLAY_NAME_LENGTH - 1].rstrip() + "…"
    return name


# --- Pydantic wiring --------------------------------------------------------
#
# Annotated aliases so a schema opts in with a type, not a copy-pasted
# validator. `AfterValidator` (not Before) so Pydantic's own `min_length` /
# `max_length` constraints report first, and so the NORMALIZED value is what
# gets stored -- a name is persisted exactly as it will be rendered.

_FIELD_LABELS = {
    "name": "Name",
    "org_name": "Organization name",
    "first_name": "First name",
    "last_name": "Last name",
    "label": "Label",
}


def _label_for(field_name: str | None) -> str:
    if not field_name:
        return "Name"
    return _FIELD_LABELS.get(field_name, field_name.replace("_", " ").capitalize())


def _validate_field(value: str, info: ValidationInfo) -> str:
    return validate_display_name(value, field=_label_for(info.field_name))


def _validate_optional_field(value: str | None, info: ValidationInfo) -> str | None:
    if value is None:
        return None
    return _validate_field(value, info)


def _validate_blankable_field(value: str, info: ValidationInfo) -> str:
    return value if not value.strip() else _validate_field(value, info)


#: A required human-typed name. Use for org / team / person name write fields.
DisplayNameStr = Annotated[str, AfterValidator(_validate_field)]
#: The same policy where the field is optional (``None`` means "not supplied"
#: and is left alone; the empty string is still rejected).
OptionalDisplayNameStr = Annotated[str | None, AfterValidator(_validate_optional_field)]
#: For the fields where the EMPTY STRING is a real value rather than an absence:
#: a minimal signup stores ``""`` as the "profile incomplete" sentinel that the
#: complete-profile gate reads. The field stays a plain ``str`` (callers hand it
#: straight to the service layer), and the policy applies to anything actually
#: typed.
BlankableDisplayNameStr = Annotated[str, AfterValidator(_validate_blankable_field)]


__all__ = [
    "LINK_PLACEHOLDER",
    "MAX_DISPLAY_NAME_LENGTH",
    "BlankableDisplayNameStr",
    "DisplayNameError",
    "DisplayNameStr",
    "OptionalDisplayNameStr",
    "normalize_display_name",
    "scrub_display_name",
    "validate_display_name",
]
