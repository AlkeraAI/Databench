"""Every Jinja environment in the codebase escapes, is strict, and is registered.

Autoescape is the one property that keeps a user-typed org name from becoming markup
in an email, and it is a per-``Environment`` switch — so a second environment built
elsewhere with the default (``autoescape=False``) would silently reopen the hole.
These tests pin the property on every registered environment AND prove, by walking
the source tree, that no environment (or bare ``Template`` / ``from_string`` /
``Markup``) exists outside the registry.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest
from alkera_core.email.render import ENVIRONMENTS
from jinja2 import Environment, StrictUndefined

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTRY_FILE = Path("packages/api-core/alkera_core/email/render.py")

#: Source roots the audit walks. `ops/` holds no Python that renders templates and
#: `vendor/` is third-party; both are outside the trust boundary this test guards.
SOURCE_ROOTS = ("apps", "packages", "scripts")
#: Path segments that are never production code.
SKIPPED_PARTS = frozenset({"tests", "vendor", "_generated", "node_modules", ".venv", "dist"})

#: The calls that create an escaping context. No leading word boundary on purpose, so
#: subclasses (``SandboxedEnvironment(``, ``NativeTemplate(``) count too; whitespace
#: before the paren is allowed because the census runs over space-joined tokens.
ENVIRONMENT_CALL = re.compile(r"Environment\s*\(")
TEMPLATE_CALLS = (
    re.compile(r"Template\s*\("),
    re.compile(r"\.\s*from_string\s*\("),
    re.compile(r"Markup\s*\("),
)

ENV_IDS = [name for name, _ in ENVIRONMENTS]
ENVS = [env for _, env in ENVIRONMENTS]


def _autoescape(env: Environment, name: str | None) -> bool:
    """What the environment decides for a template called ``name``.

    ``autoescape`` is either a bool or a ``select_autoescape`` callable; ``None`` is
    the name Jinja passes for a string template (``from_string``).
    """
    decision = env.autoescape
    return decision(name) if callable(decision) else bool(decision)


def test_registry_is_non_empty() -> None:
    assert ENVIRONMENTS
    assert len(set(ENV_IDS)) == len(ENV_IDS), "registry names must be unique"
    assert all(isinstance(env, Environment) for env in ENVS)


@pytest.mark.parametrize("env", ENVS, ids=ENV_IDS)
@pytest.mark.parametrize(
    "ext",
    [
        pytest.param(".mjml", id="mjml"),
        pytest.param(".html", id="html"),
        pytest.param(".htm", id="htm"),
        pytest.param(".xml", id="xml"),
        pytest.param(".MJML", id="mjml-uppercase"),
        pytest.param(None, id="string-template"),
    ],
)
def test_every_environment_escapes_markup_templates(env: Environment, ext: str | None) -> None:
    # `.mjml` is NOT in select_autoescape's built-in enabled list; only `default=True`
    # keeps it escaped — which is exactly the setting this case pins.
    name = None if ext is None else f"template{ext}"
    assert _autoescape(env, name) is True


@pytest.mark.parametrize("env", ENVS, ids=ENV_IDS)
@pytest.mark.parametrize("name", ["notice.txt", "NOTICE.TXT"])
def test_every_environment_leaves_plain_text_unescaped(env: Environment, name: str) -> None:
    # A plain-text template must not grow `&amp;` — the one deliberate opt-out.
    assert _autoescape(env, name) is False


@pytest.mark.parametrize("env", ENVS, ids=ENV_IDS)
def test_every_environment_uses_strict_undefined(env: Environment) -> None:
    assert env.undefined is StrictUndefined


@pytest.mark.parametrize("env", ENVS, ids=ENV_IDS)
def test_string_templates_escape_by_default(env: Environment) -> None:
    # `default_for_string=True`: an ad-hoc `from_string` template is escaped too, so a
    # future one-off render cannot bypass the policy by not being a file.
    assert env.from_string("{{ x }}").render(x='<b>&"') == "&lt;b&gt;&amp;&#34;"


@pytest.mark.parametrize("env", ENVS, ids=ENV_IDS)
def test_string_templates_are_strict_too(env: Environment) -> None:
    from jinja2 import UndefinedError

    with pytest.raises(UndefinedError):
        env.from_string("{{ missing }}").render()


def _production_python_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            if SKIPPED_PARTS.isdisjoint(path.relative_to(REPO_ROOT).parts):
                files.append(path)
    return files


def _code_only(source: str) -> str:
    """``source`` with comments and string literals blanked, so prose that mentions
    ``Environment(`` (a docstring, a comment like the one in the registry module)
    does not count as a call. Falls back to the raw text if the file will not
    tokenize — over-counting is the safe failure direction here."""
    dropped = {tokenize.COMMENT, tokenize.STRING}
    dropped |= {
        getattr(tokenize, name)
        for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
        if hasattr(tokenize, name)
    }
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return source
    return " ".join(tok.string for tok in tokens if tok.type not in dropped)


def test_no_unregistered_jinja_environment_exists() -> None:
    """Walk the tree: the ONLY file that builds an environment is the registry module,
    and it builds exactly as many as it registers.

    Restricting the census to files that import ``jinja2`` / ``markupsafe`` is what
    makes an unrelated ``Environment`` (a StrEnum of deploy environments, say) or a
    SQLAlchemy ``autoescape=`` keyword a non-hit without a hand-maintained allowlist.
    """
    files = _production_python_files()
    assert len(files) > 100, "the walk found suspiciously few files — wrong root?"

    hits: dict[Path, dict[str, int]] = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        if "jinja2" not in text and "markupsafe" not in text:
            continue
        code = _code_only(text)
        counts = {
            ENVIRONMENT_CALL.pattern: len(ENVIRONMENT_CALL.findall(code)),
            **{p.pattern: len(p.findall(code)) for p in TEMPLATE_CALLS},
        }
        if any(counts.values()):
            hits[path.relative_to(REPO_ROOT)] = counts

    assert set(hits) == {REGISTRY_FILE}, (
        f"Jinja environments/templates built outside the registry: {hits}. "
        "Route the render through alkera_core.email.render (or register the new "
        "environment in ENVIRONMENTS and extend this audit)."
    )
    assert hits[REGISTRY_FILE][ENVIRONMENT_CALL.pattern] == len(ENVIRONMENTS)
    # The registry module renders files through the environment, never ad hoc.
    assert all(hits[REGISTRY_FILE][p.pattern] == 0 for p in TEMPLATE_CALLS)


TEMPLATE_SUFFIXES = frozenset({".mjml", ".html", ".htm", ".j2", ".jinja"})
#: The filters that hand markup back out of a user value: ``safe`` marks it trusted
#: verbatim, ``urlize`` turns a URL inside it into a live ``<a href>``, ``xmlattr``
#: builds attributes from it. Each returns ``Markup``, so autoescape leaves the
#: result alone — the environment's decision is bypassed either way. ``escape`` /
#: ``forceescape`` / ``tojson`` also return ``Markup`` but only ever escape, so they
#: are not bypasses.
MARKUP_FILTERS = ("safe", "urlize", "xmlattr")
_FILTER_NAMES = "|".join(MARKUP_FILTERS)
ESCAPE_BYPASS = re.compile(
    rf"\|\s*(?:{_FILTER_NAMES})\b"  # {{ value|safe }}
    rf"|\{{%-?\s*filter\s+(?:{_FILTER_NAMES})\b"  # {% filter urlize %}…{% endfilter %}
    r"|\{%-?\s*autoescape\b"  # {% autoescape false %}
)


def _template_files() -> list[Path]:
    email_templates = sorted(
        (REPO_ROOT / "packages/api-core/alkera_core/email/templates").glob("*.mjml")
    )
    found = set(email_templates)
    for root in ("apps", "packages"):
        for folder in ("templates", "email_templates"):
            for path in (REPO_ROOT / root).rglob(f"{folder}/**/*"):
                if path.suffix in TEMPLATE_SUFFIXES and SKIPPED_PARTS.isdisjoint(
                    path.relative_to(REPO_ROOT).parts
                ):
                    found.add(path)
    return sorted(found)


@pytest.mark.parametrize(
    ("snippet", "is_bypass"),
    [
        pytest.param("{{ name|safe }}", True, id="safe"),
        pytest.param("{{ name | safe }}", True, id="safe-spaced"),
        pytest.param("{{ name|urlize }}", True, id="urlize"),
        pytest.param("{{ name|urlize(40, true) }}", True, id="urlize-with-args"),
        pytest.param("{{ attrs|xmlattr }}", True, id="xmlattr"),
        pytest.param("{{ name|trim|urlize }}", True, id="urlize-after-another-filter"),
        pytest.param(
            "{% filter urlize %}{{ name }}{% endfilter %}", True, id="filter-block-urlize"
        ),
        pytest.param(
            "{%- filter safe -%}{{ name }}{%- endfilter -%}", True, id="filter-block-safe"
        ),
        pytest.param("{% filter  xmlattr %}", True, id="filter-block-xmlattr-two-spaces"),
        pytest.param("{% autoescape false %}", True, id="autoescape-off"),
        pytest.param("{%- autoescape true %}", True, id="autoescape-on-is-still-a-mode-flip"),
        pytest.param("{{ name|e }}", False, id="escape-short"),
        pytest.param("{{ name|escape }}", False, id="escape"),
        pytest.param("{{ name|forceescape }}", False, id="forceescape"),
        pytest.param("{{ name|tojson }}", False, id="tojson"),
        pytest.param("{{ name|trim }}", False, id="trim"),
        pytest.param("{{ name|safer_name }}", False, id="safe-is-a-prefix-not-the-filter"),
        pytest.param("{{ name|unsafe }}", False, id="safe-is-a-suffix-not-the-filter"),
        pytest.param("{% filter trim %}{{ name }}{% endfilter %}", False, id="filter-block-trim"),
        pytest.param("{% if safe %}x{% endif %}", False, id="a-variable-named-safe"),
    ],
)
def test_escape_bypass_pattern(snippet: str, is_bypass: bool) -> None:
    """The audit's regex, case by case: every filter that returns markup, in both
    the pipe and the block form, and the mode switch — but not the escaping filters
    that happen to return ``Markup`` too, and not identifiers that merely contain
    a filter's name."""
    assert bool(ESCAPE_BYPASS.search(snippet)) is is_bypass


def test_no_template_disables_escaping() -> None:
    """No template opts a value out of escaping (``|safe``), turns it into markup
    (``|urlize``, ``|xmlattr`` — a URL typed into a display name would become a live
    link), or flips the mode (``{% autoescape %}``) — the environment's decision is
    the only decision."""
    templates = _template_files()
    assert len(templates) >= 14, templates  # the base, the macro and the 12 leaves
    offenders = {
        str(path.relative_to(REPO_ROOT)): ESCAPE_BYPASS.findall(path.read_text(encoding="utf-8"))
        for path in templates
    }
    assert {k: v for k, v in offenders.items() if v} == {}
