"""Output objects render by themselves: ``_repr_mimebundle_`` for the IPython
display protocol, ``_mime_`` for marimo."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import alkera
import pytest
from alkera._outputs import Image, Output, html_of, sniff, text_of

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
SVG = b'<?xml version="1.0"?>\n<!-- drawn -->\n<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script></svg>'


def test_md_bundle_carries_html_markdown_and_plain() -> None:
    out = alkera.md("""
        # Title

        Some *text*
    """)
    bundle = out._repr_mimebundle_()
    assert bundle["text/markdown"] == "# Title\n\nSome *text*"
    assert bundle["text/plain"] == "# Title\n\nSome *text*"
    assert "<h1>Title</h1>" in bundle["text/html"]
    assert out._mime_() == ("text/html", bundle["text/html"])


@pytest.mark.parametrize(
    ("include", "exclude", "mimes"),
    [
        pytest.param(None, None, {"text/html", "text/markdown", "text/plain"}, id="all"),
        pytest.param({"text/plain"}, None, {"text/plain"}, id="include"),
        pytest.param(None, {"text/html", "text/markdown"}, {"text/plain"}, id="exclude"),
        pytest.param({"text/html", "text/plain"}, {"text/plain"}, {"text/html"}, id="both"),
    ],
)
def test_mimebundle_honours_include_and_exclude(
    include: set[str] | None, exclude: set[str] | None, mimes: set[str]
) -> None:
    assert set(alkera.md("x")._repr_mimebundle_(include=include, exclude=exclude)) == mimes


def test_html_is_passed_through_and_plain_text_is_its_visible_text() -> None:
    out = alkera.html("<div><b>Bold</b><script>steal()</script><style>p{}</style><p>para</p></div>")
    bundle = out._repr_mimebundle_()
    assert bundle["text/html"].startswith("<div><b>Bold</b><script>")
    assert bundle["text/plain"] == "Bold\npara"
    assert "text/markdown" not in bundle


@pytest.mark.parametrize(
    "bad",
    [pytest.param(b"bytes", id="bytes"), pytest.param(3, id="int")],
)
@pytest.mark.parametrize(
    "factory", [pytest.param(alkera.md, id="md"), pytest.param(alkera.html, id="html")]
)
def test_text_outputs_refuse_non_text(factory: Any, bad: object) -> None:
    with pytest.raises(TypeError, match="takes text"):
        factory(bad)


@pytest.mark.parametrize(
    ("data", "mimetype"),
    [
        pytest.param(PNG, "image/png", id="png"),
        pytest.param(JPEG, "image/jpeg", id="jpeg"),
        pytest.param(GIF, "image/gif", id="gif"),
        pytest.param(b"GIF87a" + b"\x00" * 4, "image/gif", id="gif87"),
        pytest.param(WEBP, "image/webp", id="webp"),
        pytest.param(SVG, "image/svg+xml", id="svg-with-prolog"),
        pytest.param(b"\xef\xbb\xbf  <svg viewBox='0 0 1 1'/>", "image/svg+xml", id="svg-bom"),
        pytest.param(b"not an image", None, id="text"),
        pytest.param(b"<html><svg></svg></html>", None, id="html-with-svg"),
    ],
)
def test_sniff(data: bytes, mimetype: str | None) -> None:
    assert sniff(data) == mimetype


def test_image_bundle_is_base64_for_raster_and_an_img_tag_for_html() -> None:
    bundle = alkera.image(PNG)._repr_mimebundle_()
    assert base64.b64decode(bundle["image/png"]) == PNG
    assert bundle["text/html"].startswith('<img src="data:image/png;base64,')
    assert bundle["text/plain"] == f"<image/png image, {len(PNG)} bytes>"


def test_svg_is_shown_through_img_so_its_scripts_never_run() -> None:
    out = alkera.image(SVG)
    bundle = out._repr_mimebundle_()
    assert bundle["image/svg+xml"] == SVG.decode()
    assert "<script" not in bundle["text/html"]
    assert bundle["text/html"].startswith('<img src="data:image/svg+xml;base64,')


def test_image_from_a_path(tmp_path: Path) -> None:
    path = tmp_path / "pic.bin"
    path.write_bytes(GIF)
    assert alkera.image(path).mimetype == "image/gif"
    assert alkera.image(str(path)).data == GIF


@pytest.mark.parametrize(
    ("data", "mimetype", "error", "match"),
    [
        pytest.param(b"????", None, ValueError, "could not tell", id="unknown"),
        pytest.param(PNG, "application/pdf", ValueError, "does not show", id="unsupported-type"),
        pytest.param(12, None, TypeError, "bytes or a path", id="not-bytes"),
    ],
)
def test_image_refusals(
    data: Any, mimetype: str | None, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        alkera.image(data, mimetype=mimetype)


def test_explicit_mimetype_wins_over_sniffing() -> None:
    assert alkera.image(b"????", mimetype="image/png").mimetype == "image/png"


def test_hstack_and_vstack_lay_out_their_items() -> None:
    row = alkera.hstack([alkera.md("**a**"), "*b*", 7])
    html = row._repr_mimebundle_()["text/html"]
    assert "flex-direction: row" in html
    assert html.index("<strong>a</strong>") < html.index("<em>b</em>") < html.index("<pre>7</pre>")
    assert row._repr_mimebundle_()["text/plain"] == "**a**  *b*  7"
    column = alkera.vstack([alkera.md("a"), alkera.md("b")], gap=1)
    assert "flex-direction: column" in column._mime_()[1]
    assert "gap: 1rem" in column._mime_()[1]
    assert column._repr_mimebundle_()["text/plain"] == "a\nb"


@pytest.mark.parametrize(
    "items",
    [pytest.param("ab", id="string"), pytest.param(3, id="int")],
)
def test_stacks_refuse_a_non_list(items: Any) -> None:
    with pytest.raises(TypeError, match="list of items"):
        alkera.hstack(items)


def test_stacks_nest() -> None:
    inner = alkera.vstack([alkera.md("x")])
    outer = alkera.hstack([inner, alkera.callout("y", "info")])
    html = outer._repr_mimebundle_()["text/html"]
    assert html.count("alkera-stack") == 2
    assert "alkera-callout-info" in html


class _RichOnly:
    def _repr_mimebundle_(self, include: Any = None, exclude: Any = None) -> dict[str, Any]:
        return {"image/png": base64.b64encode(PNG).decode(), "text/plain": "rich"}


class _MarimoLike:
    def _mime_(self) -> tuple[str, str]:
        return "text/html", "<span>from marimo</span>"


class _HtmlRepr:
    def _repr_html_(self) -> str:
        return "<table></table>"


class _SvgRepr:
    def _repr_svg_(self) -> str:
        return "<svg xmlns='http://www.w3.org/2000/svg'></svg>"


class _MarkdownRepr:
    def _repr_markdown_(self) -> str:
        return "**md repr**"


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        pytest.param(_RichOnly(), '<img src="data:image/png;base64,', id="png-bundle"),
        pytest.param(_MarimoLike(), "<span>from marimo</span>", id="mime"),
        pytest.param(_HtmlRepr(), "<table></table>", id="repr-html"),
        pytest.param(_SvgRepr(), '<img src="data:image/svg+xml;base64,', id="repr-svg"),
        pytest.param(_MarkdownRepr(), "<p><strong>md repr</strong></p>", id="repr-markdown"),
        pytest.param(
            {"a": "<b>"}, "<pre>{&#x27;a&#x27;: &#x27;&lt;b&gt;&#x27;}</pre>", id="repr-escaped"
        ),
    ],
)
def test_items_inside_a_stack_use_their_richest_html(item: object, expected: str) -> None:
    assert html_of(item).startswith(expected)


@pytest.mark.parametrize("kind", ["neutral", "info", "success", "warn", "danger"])
def test_callout_kinds(kind: str) -> None:
    out = alkera.callout(alkera.md("**Heads up**"), kind)
    bundle = out._repr_mimebundle_()
    assert f"alkera-callout-{kind}" in bundle["text/html"]
    assert "<strong>Heads up</strong>" in bundle["text/html"]
    assert bundle["text/plain"] == f"[{kind}] **Heads up**"
    assert bundle["text/markdown"].startswith(f"> **{kind.capitalize()}**")


def test_callout_refuses_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="neutral, info, success, warn, danger"):
        alkera.callout("x", "warning")


def test_callout_of_a_non_text_object_has_no_markdown() -> None:
    assert "text/markdown" not in alkera.callout(7, "info")._repr_mimebundle_()


def test_output_text_property_and_repr() -> None:
    out = alkera.md("x" * 100)
    assert out.text == out._mime_()[1]
    assert repr(out) == f"Markdown({'x' * 57 + '...'!r})"


def test_base_output_has_no_html() -> None:
    with pytest.raises(NotImplementedError):
        Output()._repr_mimebundle_()


def test_text_of_skips_scripts_and_styles() -> None:
    assert text_of("<p>a</p><script>b</script><style>c</style><p>d</p>") == "a\nd"


def test_image_class_is_exported_for_isinstance_checks() -> None:
    assert isinstance(alkera.image(PNG), Image)
