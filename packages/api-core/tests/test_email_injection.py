"""Injection tests for the outbound email surface.

Two layers are under test.

The HTML body. Every template variable of every leaf template — the human-typed ones
(a display name, an org or team name) and the system-formatted ones alike — plus every
attribute context (the button and pasteable-link ``href``, the wordmark ``src`` and
``alt``) is fed a markup payload and must come out as text. The primary assertion is a
structural census: the rendered document has exactly the tags and attribute names of
the benign render, so a payload can never add an element or an event handler. That
invariant does not depend on which entities the MJML engine happens to emit; the
entity checks (``&lt;``, ``&amp;``, ``&#34;``) are secondary and pin today's engine.
A second table proves legitimate punctuation (``&``, quotes, an apostrophe, a query
string) still renders — escaping must neutralise markup, not mangle names or links.

The Subject header and the ``text/plain`` alternative. Those are hand-built f-strings
that autoescape never sees, so the only guard is ``scrub_display_name``: each
human-name sender is driven end-to-end and the captured ``EmailMessage`` is checked.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from html import unescape
from html.parser import HTMLParser
from types import SimpleNamespace
from uuid import uuid4

import alkera_core.email as email_mod
import pytest
from alkera_core.config import settings
from alkera_core.email import tokens
from alkera_core.email.render import ENVIRONMENTS, render_email
from alkera_core.validation.display_name import LINK_PLACEHOLDER
from jinja2 import meta
from test_email_render import CASE_IDS, CASES, LOGO_URL

# --- Payloads --------------------------------------------------------------------

#: A tag, an event handler, and the three characters that must become entities.
PAYLOAD = "<img src=x onerror=alert(1)>&\"'"
#: Closes the surrounding attribute value and opens an event handler.
ATTR_PAYLOAD = 'https://x/" onerror="alert(1)'
#: Closes ``alt="..."`` and opens a script element.
ALT_PAYLOAD = '"><script>x</script>'
#: Legitimate punctuation a real name can carry — must render, not break.
PUNCTUATED_NAME = 'Tom & "Jerry" O\'Neil'
#: A bare URL and a bare host inside a text value. Escaping keeps them text; a
#: linkifying filter (``urlize``) on the slot would turn either into a live
#: ``<a href>`` that the markup payload above never reveals, because ``urlize``
#: escapes tags while it links.
LINK_PAYLOAD = "see http://evil.example/x or www.evil.example now"
#: A legitimate link with a query string — the ``&`` must survive as an entity.
QUERY_URL = "https://invoice.stripe.com/i/INVOICE?locale=en&s=1"

# --- Slot tables -----------------------------------------------------------------

#: Injected by ``render_email`` from settings, read only by the shared shell.
BRAND_SLOTS = frozenset({"product_name", "logo_url"})

#: Every slot that names an amount of money. It is read as a number through the
#: ``usd`` filter, so a value that is not one never reaches the document at all —
#: not even escaped — and reads as the dash instead.
MONEY_SLOTS = frozenset({"amount_usd"})

#: Every text slot of every leaf template. The human-typed ones today are
#: ``invitation.inviter/org_name/team_name``, ``verification.name``,
#: ``password_reset.name``, ``welcome.name`` and ``enterprise_enrolled.org_name``; the
#: rest are system-formatted. All are covered: the classification of a slot can change
#: without the template changing, and the sinks are the same either way.
TEXT_SLOTS = [
    pytest.param(template, slot, id=f"{template}-{slot}")
    for template in CASE_IDS
    for slot in CASES[template]
    # A flag (``urgent``) picks between fixed copies; it carries no reader text.
    if not slot.endswith("_url")
    and slot not in MONEY_SLOTS
    and not isinstance(CASES[template][slot], bool)
]
#: Every URL slot — each lands in an ``href`` twice (the button and the pasteable
#: link) and once as the link's visible text.
URL_SLOTS = [
    pytest.param(template, slot, id=f"{template}-{slot}")
    for template in CASE_IDS
    for slot in CASES[template]
    if slot.endswith("_url")
]
URL_PAYLOADS = [
    pytest.param(ATTR_PAYLOAD, id="attribute-breakout"),
    pytest.param(QUERY_URL, id="query-string"),
]

#: Every ``&`` in a rendered document must start an entity; a bare one is either an
#: unescaped value or malformed markup.
BARE_AMPERSAND = re.compile(r"&(?![A-Za-z][A-Za-z0-9]*;|#[0-9]+;|#[xX][0-9A-Fa-f]+;)")

Attrs = dict[str, str | None]
#: One entry per start tag: the tag name and its attribute names, in document order.
Census = list[tuple[str, tuple[str, ...]]]


@pytest.fixture(autouse=True)
def _pin_logo(monkeypatch: pytest.MonkeyPatch) -> None:
    # A configured logo makes the baseline carry exactly one legitimate <img>, so the
    # census proves the payload adds none rather than passing on an empty set.
    monkeypatch.setattr(settings, "brand_email_logo_url", LOGO_URL)


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, Attrs]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Self-closing tags route here too (HTMLParser.handle_startendtag delegates).
        self.tags.append((tag, dict(attrs)))


def _tags(html: str) -> list[tuple[str, Attrs]]:
    parser = _TagCollector()
    parser.feed(html)
    parser.close()
    return parser.tags


def _names(html: str) -> list[str]:
    return [tag for tag, _ in _tags(html)]


def _census(html: str) -> Census:
    return [(tag, tuple(sorted(attrs))) for tag, attrs in _tags(html)]


def _render(template: str, **overrides: object) -> str:
    return render_email(template, **{**CASES[template], **overrides})


def _in_slot(template: str, slot: str, payload: str) -> object:
    """``payload`` as the value of ``slot``. A list slot (one paragraph per item)
    takes it as its first item and keeps the rest, so the benign render it is
    compared with has the same number of paragraphs."""
    benign = CASES[template][slot]
    if isinstance(benign, list):
        return [payload, *benign[1:]]
    return payload


def _assert_only_text_changed(rendered: str, baseline: str) -> None:
    """The engine-agnostic invariant: an escaped payload changes text, never structure.

    Same tags with the same attribute names in the same order as the benign render —
    so no element and no attribute the payload carried made it into the document.
    """
    assert _census(rendered) == _census(baseline)
    assert "script" not in _names(rendered)
    assert not any("onerror" in attrs for _, attrs in _tags(rendered))


def _assert_brand_intact(rendered: str, template: str) -> None:
    """The render did not silently degrade (the sender's guard would drop the HTML)."""
    (url,) = [v for k, v in CASES[template].items() if k.endswith("_url")]
    assert str(url) in rendered
    assert tokens.CTA_BG in rendered
    assert "Newsreader" in rendered and "IBM Plex Sans" in rendered


def _template_variables(name: str) -> set[str]:
    env = dict(ENVIRONMENTS)["email"]
    assert env.loader is not None
    source, _, _ = env.loader.get_source(env, f"{name}.mjml")
    return meta.find_undeclared_variables(env.parse(source))


# --- The slot tables are complete ---------------------------------------------------


@pytest.mark.parametrize("template", CASE_IDS)
def test_every_template_variable_has_an_injection_case(template: str) -> None:
    """The slot tables derive from CASES; this pins CASES to the template source.

    A new ``{{ variable }}`` in a leaf template therefore fails here until its render
    case exists — and with it, every payload case above.
    """
    variables = _template_variables(template) - {"cta"} - BRAND_SLOTS
    assert variables == set(CASES[template])


def test_shared_shell_reads_only_the_brand_slots() -> None:
    """The only variables outside the leaves are the two settings-injected brand slots."""
    assert _template_variables("base") == BRAND_SLOTS
    assert _template_variables("_macros") == set()


def test_every_leaf_template_has_a_render_case() -> None:
    env = dict(ENVIRONMENTS)["email"]
    leaves = {
        name.removesuffix(".mjml")
        for name in env.list_templates(extensions=["mjml"])
        if name not in ("base.mjml", "_macros.mjml")
    }
    assert leaves == set(CASES)


# --- Text slots ------------------------------------------------------------------


@pytest.mark.parametrize(("template", "slot"), TEXT_SLOTS)
def test_text_slot_never_opens_a_tag(template: str, slot: str) -> None:
    baseline = _render(template)
    rendered = _render(template, **{slot: _in_slot(template, slot, PAYLOAD)})

    _assert_only_text_changed(rendered, baseline)
    # Exactly the one legitimate image (the wordmark) survives, in every template.
    assert _names(rendered).count("img") == 1 == _names(baseline).count("img")

    # Secondary, engine-specific: the payload is on the wire only in escaped form, and
    # the ampersand and quotes came through as entities rather than being dropped.
    assert PAYLOAD not in rendered
    assert "<img src=x" not in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered
    assert "&amp;" in rendered
    assert '&"' not in rendered
    assert PAYLOAD in unescape(rendered)


@pytest.mark.parametrize(("template", "slot"), TEXT_SLOTS)
def test_text_slot_never_becomes_a_link(template: str, slot: str) -> None:
    """A URL typed into a text slot stays text. The structural census catches the
    anchor a linkifying filter would add; the href check names the vector — a
    live link to an attacker's host in a message the product signed."""
    baseline = _render(template)
    rendered = _render(template, **{slot: _in_slot(template, slot, LINK_PAYLOAD)})

    _assert_only_text_changed(rendered, baseline)
    hrefs = [attrs.get("href") or "" for tag, attrs in _tags(rendered) if tag == "a"]
    assert not any("evil.example" in href for href in hrefs), hrefs
    assert len(hrefs) == len([1 for tag, _ in _tags(baseline) if tag == "a"])
    # The text itself is intact — escaping neutralises markup, it does not eat URLs.
    assert LINK_PAYLOAD in unescape(rendered)


@pytest.mark.parametrize(("template", "slot"), TEXT_SLOTS)
def test_text_slot_survives_ampersand_and_quotes(template: str, slot: str) -> None:
    baseline = _render(template)
    rendered = _render(template, **{slot: _in_slot(template, slot, PUNCTUATED_NAME)})

    _assert_only_text_changed(rendered, baseline)
    _assert_brand_intact(rendered, template)
    # The name reads back verbatim once the client decodes it...
    assert PUNCTUATED_NAME in unescape(rendered)
    # ...but is never on the wire raw: a bare `&` would be malformed XML for the MJML
    # engine (which the sender's guard would turn into a silently text-only email).
    assert PUNCTUATED_NAME not in rendered
    assert BARE_AMPERSAND.search(rendered) is None


# --- Attribute contexts -----------------------------------------------------------


@pytest.mark.parametrize("payload", URL_PAYLOADS)
@pytest.mark.parametrize(("template", "slot"), URL_SLOTS)
def test_url_slot_cannot_break_out_of_its_attribute(template: str, slot: str, payload: str) -> None:
    baseline = _render(template)
    rendered = _render(template, **{slot: payload})

    _assert_only_text_changed(rendered, baseline)
    hrefs = [attrs.get("href") for tag, attrs in _tags(rendered) if tag == "a"]
    # Button + pasteable link, and the WHOLE payload stayed inside each value.
    assert hrefs == [payload, payload]
    # The pasteable link also shows the URL as text, decoded back to the original.
    assert payload in unescape(rendered)
    # Both payloads carry a character that must travel as an entity.
    assert payload not in rendered
    assert BARE_AMPERSAND.search(rendered) is None


@pytest.mark.parametrize("template", CASE_IDS)
def test_logo_url_cannot_break_out_of_src(template: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Operator-controlled rather than user-controlled, but the same attribute context.
    baseline = _render(template)
    monkeypatch.setattr(settings, "brand_email_logo_url", ATTR_PAYLOAD)
    rendered = _render(template)

    _assert_only_text_changed(rendered, baseline)
    (image,) = [attrs for tag, attrs in _tags(rendered) if tag == "img"]
    assert image["src"] == ATTR_PAYLOAD
    assert ATTR_PAYLOAD not in rendered


@pytest.mark.parametrize("template", CASE_IDS)
def test_product_name_cannot_break_out_of_alt(
    template: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = _render(template)
    monkeypatch.setattr(settings, "brand_product_name", ALT_PAYLOAD)
    rendered = _render(template)

    _assert_only_text_changed(rendered, baseline)
    (image,) = [attrs for tag, attrs in _tags(rendered) if tag == "img"]
    assert image["alt"] == ALT_PAYLOAD
    assert "<script" not in rendered
    # The same value also lands as text in the <title> and the footer of every leaf.
    assert rendered.count("&lt;script&gt;") >= 2
    assert ALT_PAYLOAD in unescape(rendered)


# --- Subject + text/plain: the surfaces Jinja never sees -------------------------

# Markup AND a link in one value: the scrubber must strip the tag and replace the URL
# with the visible placeholder, so a leak of either half is a distinct failure.
EVIL_NAME = "<b>Evil</b> http://evil.example/x"

Sender = Callable[[], Awaitable[None]]


def _invitation() -> Awaitable[None]:
    invitation = SimpleNamespace(
        email="invitee@example.com",
        id=uuid4(),
        expires_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
    )
    team = SimpleNamespace(name=EVIL_NAME, is_root=False, id=uuid4())
    return email_mod.send_invitation_email(
        invitation,  # type: ignore[arg-type]
        token="INVITE_TOKEN",
        team=team,  # type: ignore[arg-type]
        org_name=EVIL_NAME,
        inviter_display_name=EVIL_NAME,
    )


def _user() -> SimpleNamespace:
    return SimpleNamespace(display_name=EVIL_NAME, email="user@example.com", id=uuid4())


def _verification() -> Awaitable[None]:
    return email_mod.send_email_verification(_user(), token="VERIFY_TOKEN")  # type: ignore[arg-type]


def _password_reset() -> Awaitable[None]:
    return email_mod.send_password_reset(_user(), token="RESET_TOKEN")  # type: ignore[arg-type]


def _welcome() -> Awaitable[None]:
    return email_mod.send_welcome_email(_user())  # type: ignore[arg-type]


@pytest.fixture
def captured_message(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Sender], Awaitable[EmailMessage]]:
    """Run a sender with SMTP replaced by a capture and hand back the one message."""

    async def _run(send: Sender) -> EmailMessage:
        captured: list[EmailMessage] = []

        async def _capture(message: EmailMessage, **_kw: object) -> None:
            captured.append(message)

        monkeypatch.setattr(email_mod, "_send", _capture)
        await send()
        (message,) = captured
        return message

    return _run


def _part(message: EmailMessage, subtype: str) -> str | None:
    body = message.get_body(preferencelist=(subtype,))
    return None if body is None else str(body.get_content())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("send", "subject_names_someone"),
    [
        pytest.param(_invitation, True, id="invitation"),
        pytest.param(_verification, False, id="verification"),
        pytest.param(_password_reset, False, id="password_reset"),
        pytest.param(_welcome, False, id="welcome"),
    ],
)
async def test_sender_subject_and_text_carry_no_markup_or_link(
    captured_message: Callable[[Sender], Awaitable[EmailMessage]],
    send: Sender,
    subject_names_someone: bool,
) -> None:
    """Every human-typed name a sender interpolates is scrubbed on ALL three surfaces.

    The Subject and the ``text/plain`` part bypass Jinja entirely, so autoescape is no
    help there: a raw ``<b>`` or a live URL in either is exactly the phishing vector
    the scrubber exists to close. Where a name is interpolated the visible
    ``[link removed]`` placeholder must appear — proof the scrubbed value was used, not
    that the name was silently dropped.
    """
    message = await captured_message(send)

    subject = str(message["Subject"])
    text = _part(message, "plain")
    assert text is not None

    for surface, value in (("Subject", subject), ("text/plain", text)):
        assert "<" not in value and ">" not in value, f"{surface} carries markup: {value!r}"
        assert "evil.example" not in value, f"{surface} carries a live link: {value!r}"

    assert LINK_PLACEHOLDER in text
    if subject_names_someone:
        assert LINK_PLACEHOLDER in subject

    html = _part(message, "html")
    assert html is not None, "the HTML alternative was dropped (render failed)"
    assert "<b>" not in html and "evil.example" not in html
    assert LINK_PLACEHOLDER in html
