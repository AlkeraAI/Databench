"""The standard-library Markdown renderer: what it renders, and what it
refuses to pass through."""

from __future__ import annotations

import re

import pytest
from alkera._markdown import dedent, render, safe_url


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("# One", "<h1>One</h1>", id="h1"),
        pytest.param("###### Six ###", "<h6>Six</h6>", id="h6-closing-hashes"),
        pytest.param("####### seven", "<p>####### seven</p>", id="seven-hashes-is-text"),
        pytest.param("#nospace", "<p>#nospace</p>", id="hash-without-space-is-text"),
        pytest.param("a **b** c", "<p>a <strong>b</strong> c</p>", id="strong-stars"),
        pytest.param("a __b__ c", "<p>a <strong>b</strong> c</p>", id="strong-underscores"),
        pytest.param("a *b* c", "<p>a <em>b</em> c</p>", id="em-star"),
        pytest.param("a _b_ c", "<p>a <em>b</em> c</p>", id="em-underscore"),
        pytest.param("snake_case_name", "<p>snake_case_name</p>", id="intraword-underscore"),
        pytest.param("2 * 3 * 4", "<p>2 * 3 * 4</p>", id="spaced-stars-are-text"),
        pytest.param("***both***", "<p><strong><em>both</em></strong></p>", id="strong-em"),
        pytest.param("~~gone~~", "<p><del>gone</del></p>", id="strike"),
        pytest.param("use `x < y`", "<p>use <code>x &lt; y</code></p>", id="code-span-escaped"),
        pytest.param("``a ` b``", "<p><code>a ` b</code></p>", id="double-backtick-span"),
        pytest.param(
            "`**not bold**`", "<p><code>**not bold**</code></p>", id="no-emphasis-in-code"
        ),
        pytest.param(r"\*literal\*", "<p>*literal*</p>", id="backslash-escape"),
        pytest.param("one  \ntwo", "<p>one<br />\ntwo</p>", id="hard-break-spaces"),
        pytest.param("one\\\ntwo", "<p>one<br />\ntwo</p>", id="hard-break-backslash"),
        pytest.param("one\ntwo", "<p>one\ntwo</p>", id="soft-break"),
        pytest.param("a\n\nb", "<p>a</p>\n<p>b</p>", id="two-paragraphs"),
        pytest.param("---", "<hr />", id="rule-dashes"),
        pytest.param("* * *", "<hr />", id="rule-spaced-stars"),
        pytest.param(
            "> quoted *x*", "<blockquote>\n<p>quoted <em>x</em></p>\n</blockquote>", id="quote"
        ),
        pytest.param(
            "```python\nif a < b:\n    pass\n```",
            '<pre><code class="language-python">if a &lt; b:\n    pass\n</code></pre>',
            id="fenced-with-language",
        ),
        pytest.param(
            "~~~\n# not a heading\n~~~",
            "<pre><code># not a heading\n</code></pre>",
            id="tilde-fence",
        ),
        pytest.param("```\nunclosed", "<pre><code>unclosed\n</code></pre>", id="unclosed-fence"),
        pytest.param(
            "    indented\n    code", "<pre><code>indented\ncode\n</code></pre>", id="indented-code"
        ),
        pytest.param("- a\n- b", "<ul>\n<li>a</li>\n<li>b</li>\n</ul>", id="bullets"),
        pytest.param("1. a\n2. b", "<ol>\n<li>a</li>\n<li>b</li>\n</ol>", id="ordered"),
        pytest.param(
            "3) a\n4) b", '<ol start="3">\n<li>a</li>\n<li>b</li>\n</ol>', id="ordered-start"
        ),
        pytest.param(
            "- a\n  - b\n  - c\n- d",
            "<ul>\n<li>a\n<ul>\n<li>b</li>\n<li>c</li>\n</ul></li>\n<li>d</li>\n</ul>",
            id="nested",
        ),
        pytest.param(
            "- a\n\n- b", "<ul>\n<li><p>a</p></li>\n<li><p>b</p></li>\n</ul>", id="loose-list"
        ),
        pytest.param(
            "- a\n1. b", "<ul>\n<li>a</li>\n</ul>\n<ol>\n<li>b</li>\n</ol>", id="list-kind-change"
        ),
        pytest.param(
            "text\n- item", "<p>text</p>\n<ul>\n<li>item</li>\n</ul>", id="list-after-paragraph"
        ),
        pytest.param(
            "[site](https://example.com)",
            '<p><a href="https://example.com">site</a></p>',
            id="link",
        ),
        pytest.param(
            '[s](https://e.com "Title")',
            '<p><a href="https://e.com" title="Title">s</a></p>',
            id="link-title",
        ),
        pytest.param(
            "[**b**](/x)", '<p><a href="/x"><strong>b</strong></a></p>', id="link-emphasis"
        ),
        pytest.param("[a](#top)", '<p><a href="#top">a</a></p>', id="fragment-link"),
        pytest.param(
            "<https://e.com/a?b=1&c=2>",
            '<p><a href="https://e.com/a?b=1&amp;c=2">https://e.com/a?b=1&amp;c=2</a></p>',
            id="autolink",
        ),
        pytest.param("![cat](cat.png)", '<p><img src="cat.png" alt="cat" /></p>', id="image"),
    ],
)
def test_renders(source: str, expected: str) -> None:
    assert render(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("[x](javascript:alert(1))", id="javascript"),
        pytest.param("[x](JavaScript:alert(1))", id="javascript-case"),
        pytest.param("[x](java\tscript:alert(1))", id="javascript-tab"),
        pytest.param("[x](&#106;avascript:alert(1))", id="javascript-entity"),
        pytest.param("[x](vbscript:msgbox)", id="vbscript"),
        pytest.param("[x](data:text/html;base64,PHNjcmlwdD4=)", id="data-html"),
        pytest.param("![x](javascript:alert(1))", id="image-javascript"),
        pytest.param("![x](data:text/html;base64,PHNjcmlwdD4=)", id="image-data-html"),
        pytest.param("<javascript:alert(1)>", id="autolink-javascript"),
    ],
)
def test_unsafe_urls_never_become_attributes(source: str) -> None:
    out = render(source)
    assert "href" not in out and "src" not in out, out
    assert "x" in out or "javascript" in out


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("<script>alert(1)</script>", id="script-tag"),
        pytest.param("<img src=x onerror=alert(1)>", id="img-onerror"),
        pytest.param("**<b>x</b>**", id="tag-inside-emphasis"),
        pytest.param("# <i>h</i>", id="tag-in-heading"),
        pytest.param("- <u>i</u>", id="tag-in-list"),
        pytest.param("> <s>q</s>", id="tag-in-quote"),
    ],
)
def test_raw_html_is_escaped(source: str) -> None:
    out = render(source)
    tags = set(re.findall(r"<(/?[a-z0-9]+)", out))
    assert tags <= {
        "p",
        "/p",
        "strong",
        "/strong",
        "h1",
        "/h1",
        "ul",
        "/ul",
        "li",
        "/li",
        "blockquote",
        "/blockquote",
    }, out
    assert "&lt;" in out


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            '[x](https://e.com "a\\"onmouseover=\\"alert(1))', id="escaped-quote-in-title"
        ),
        pytest.param('![`" onerror="alert(1)`](a.png)', id="code-span-in-alt"),
        pytest.param('![\\" onerror=\\"alert(1)](a.png)', id="escaped-quote-in-alt"),
        pytest.param('[x](https://e.com/\\"onclick=\\"alert(1))', id="escaped-quote-in-href"),
        pytest.param('<https://e.com/\\"onclick=alert(1)>', id="escaped-quote-in-autolink"),
    ],
)
def test_attributes_cannot_be_broken_out_of(source: str) -> None:
    out = render(source)
    # Every attribute value is a quoted string with no raw quote inside.
    for tag in re.findall(r"<[a-z]+\s[^>]*>", out):
        stripped = re.sub(r'\s[a-z]+="[^"]*"', "", tag)
        assert re.fullmatch(r"<[a-z]+\s*/?>", stripped), (tag, out)


def test_a_placeholder_character_in_the_text_cannot_inject_held_fragments() -> None:
    # The renderer holds fragments behind NUL markers; the same characters
    # typed by a person must stay text.
    out = render("`<b>` and \x000\x00 here")
    assert out.count("<code>") == 1
    assert "\ufffd0\ufffd" in out


@pytest.mark.parametrize(
    ("url", "image", "allowed"),
    [
        pytest.param("https://e.com", False, True, id="https"),
        pytest.param("mailto:a@b.c", False, True, id="mailto"),
        pytest.param("relative/page", False, True, id="relative"),
        pytest.param("ftp://e.com", False, False, id="ftp"),
        pytest.param("file:///etc/passwd", False, False, id="file"),
        pytest.param("data:image/png;base64,AAAA", True, True, id="data-png-image"),
        pytest.param("data:image/png;base64,AAAA", False, False, id="data-png-link"),
        pytest.param("data:image/svg+xml;base64,AAAA", True, False, id="data-svg-image"),
    ],
)
def test_safe_url(url: str, image: bool, allowed: bool) -> None:
    assert (safe_url(url, image=image) is not None) is allowed


def test_dedent_strips_indentation_and_blank_edges() -> None:
    text = """
        # Title

        Body
    """
    assert dedent(text) == "# Title\n\nBody"
    assert render(dedent(text)) == "<h1>Title</h1>\n<p>Body</p>"


def test_crlf_and_tabs_are_normalised() -> None:
    assert render("- a\r\n-\tb") == "<ul>\n<li>a</li>\n<li>b</li>\n</ul>"
