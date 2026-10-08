"""The two halves of the display-name policy have to agree.

``validate_display_name`` refuses a link-bearing name at the write path;
``scrub_display_name`` rewrites one at the email-render path. They are meant to
be one rule stated twice, and the write half is the only one present on the
paths that never render an invitation email — so a payload the renderer would
rewrite but the writer accepts is a payload that gets stored verbatim and shown
somewhere no scrub runs.

The `www.` rule was exactly that gap: the writer anchored on start-of-string or
a preceding space/bracket, so `Acme Support-www.evil.example` was persisted while
the renderer's `\\bwww\\.` still rewrote it.
"""

from __future__ import annotations

import pytest
from alkera_core.validation.display_name import (
    MAX_DISPLAY_NAME_LENGTH,
    DisplayNameError,
    normalize_display_name,
    scrub_display_name,
    validate_display_name,
)

# Each of these puts `www.` after a character the old anchor did not recognize as
# a boundary. Every one of them was accepted and stored verbatim.
WWW_AFTER_A_NON_SPACE = [
    pytest.param("Acme Support-www.alkera-verify.example", id="after-hyphen"),
    pytest.param("Acme Support:www.alkera-verify.example", id="after-colon"),
    pytest.param("Acme.www.alkera-verify.example", id="after-dot"),
    pytest.param("Acme,www.alkera-verify.example", id="after-comma"),
    pytest.param('Acme "www.alkera-verify.example"', id="after-quote"),
    pytest.param("Acme/www.alkera-verify.example", id="after-slash"),
    pytest.param("Acme'www.alkera-verify.example", id="after-apostrophe"),
    pytest.param("Acme;www.alkera-verify.example", id="after-semicolon"),
]

# The boundary cases the rule must keep answering the way it always did.
WWW_ALREADY_REFUSED = [
    pytest.param("www.alkera-verify.example", id="at-the-start"),
    pytest.param("Acme www.alkera-verify.example", id="after-a-space"),
    pytest.param("Acme (www.alkera-verify.example)", id="inside-parentheses"),
]


@pytest.mark.parametrize("payload", WWW_AFTER_A_NON_SPACE + WWW_ALREADY_REFUSED)
def test_a_www_host_is_refused_wherever_it_sits(payload: str) -> None:
    with pytest.raises(DisplayNameError, match="web address"):
        validate_display_name(payload)


@pytest.mark.parametrize(
    "name",
    [
        # `www` has to be its own token. A name that merely ENDS in those letters
        # is a name, and refusing it would be the false positive the policy is
        # explicitly written to avoid.
        pytest.param("Bwww.io", id="www-inside-a-word"),
        pytest.param("Acme.io", id="bare-dotted-token-stays-legal"),
        pytest.param("Wow Widgets", id="ordinary-name"),
        pytest.param("Müller & Söhne GmbH", id="non-ascii-name"),
        pytest.param("Anne-Marie O'Brien", id="punctuated-person-name"),
    ],
)
def test_a_name_without_a_link_is_still_accepted(name: str) -> None:
    assert validate_display_name(name) == name


# Everything the renderer knows how to rewrite, plus the names it leaves alone.
# The corpus is shared by the agreement test below so the two layers are checked
# against ONE list rather than two that can drift apart.
CORPUS = [
    "Acme Support-www.alkera-verify.example",
    "Acme Support:www.x.example",
    "Acme.www.x.example",
    "Acme,www.x.example",
    "Acme/www.x.example",
    "www.x.example",
    "Acme (www.x.example)",
    "http://attacker.example/",
    "Portal-http://attacker.example/login",
    "strawberry://settings/team",
    "Acme evil.example/login",
    "Acme-evil.example/login",
    "billing@alkera-support.example",
    "Contact billing@alkera-support.example now",
    "Team <h1>tags</h1>",
    "Acme <script>alert(1)</script>",
    "Acme.io",
    "Acme Corp",
    "Anne-Marie O'Brien",
    "Müller & Söhne GmbH",
    "Wow Widgets",
    "Bwww.io",
]


@pytest.mark.parametrize("name", CORPUS, ids=CORPUS)
def test_the_write_rule_and_the_render_rule_agree(name: str) -> None:
    """A name the renderer would rewrite is a name the writer refuses, and a name
    the writer accepts survives the renderer untouched.

    Stated as an equivalence rather than as two lists, because the failure this
    guards against is the two lists drifting: the gap only becomes exploitable
    on a path where the renderer never runs, which is precisely where nobody
    notices the disagreement.
    """
    assert len(name) <= MAX_DISPLAY_NAME_LENGTH, "the corpus tests the link rules, not the cap"
    normalized = normalize_display_name(name)
    rewritten = scrub_display_name(name) != normalized
    if rewritten:
        with pytest.raises(DisplayNameError):
            validate_display_name(name)
    else:
        assert validate_display_name(name) == normalized
