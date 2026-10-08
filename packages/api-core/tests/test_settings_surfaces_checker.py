"""The operator-surface checker (`scripts/check_settings_surfaces.py`) itself.

`make lint` runs the checker against this repo, which proves the repo is honest
but not that the checker is. The rules it enforces are what stop a capacity
number shipping its Alkera-hosted default to every self-hosted install, and
each of them has a way of quietly stopping working:

* a deployment surface is supposed to RENDER a setting onto a container. Before
  this test existed, a name left behind in a COMMENT satisfied the rule -- which
  is the exact state the checker was written to end (terraform "carried"
  `GATEWAY_MAX_STREAM_SECONDS` that way);
* `.env.example` is supposed to ASSIGN the name, not discuss it;
* both allowlists must fail in BOTH directions, or they decay into permanent
  exemptions.

So the rules are driven here against hand-written surfaces in `tmp_path` and
hand-built rows, each one asserted to refuse what it exists to refuse.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from alkera_core.config import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPO_ROOT / "scripts" / "check_settings_surfaces.py"


def _load() -> ModuleType:
    """Import the checker by path -- `scripts/` is not an importable package."""
    spec = importlib.util.spec_from_file_location("check_settings_surfaces", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the checker declares dataclasses, and
    # `@dataclass` resolves its annotations through `sys.modules[__module__]`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load()


@pytest.fixture
def surface_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the checker's file resolution at a throwaway tree, run from it."""
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _surface(name: str = "surface.yaml") -> object:
    return checker.Surface("compose", "probe", (name,))


def _row(field: str, *, present: frozenset[str] = frozenset()) -> object:
    return checker.SettingRow(
        field=field,
        env_name=field.upper(),
        group="probe",
        default="1",
        comment=(),
        present=present,
    )


#: The surfaces the rule tests hold a row to, named here so a rule test does not
#: depend on which surfaces the checkout running it declares.
_DEPLOYMENT = ("helm", "compose", "terraform")
_DOCS = ("deploy_notes",)


def _check(rows: list[object]) -> list[str]:
    problems: list[str] = checker.check(rows, _DEPLOYMENT, _DOCS)
    return problems


# --------------------------------------------------------------------------
# a deployment surface counts where it renders, not where it talks
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("  FILES_MAX_UPLOAD_PARTS: 10000", id="yaml-mapping"),
        pytest.param("  FILES_MAX_UPLOAD_PARTS: ${FILES_MAX_UPLOAD_PARTS:-10000}", id="compose"),
        pytest.param(
            "  FILES_MAX_UPLOAD_PARTS: {{ .Values.files.limits.maxUploadParts | quote }}",
            id="helm-template",
        ),
        pytest.param('    FILES_MAX_UPLOAD_PARTS = "10000"', id="hcl-attribute"),
        pytest.param("      - FILES_MAX_UPLOAD_PARTS=10000", id="shell-env-list"),
        pytest.param("        - name: FILES_MAX_UPLOAD_PARTS", id="container-env-entry"),
        pytest.param('  "FILES_MAX_UPLOAD_PARTS": "10000"', id="quoted-key"),
    ],
)
def test_a_rendered_setting_is_found_on_a_deployment_surface(surface_root: Path, line: str) -> None:
    (surface_root / "surface.yaml").write_text(f"{line}\n", encoding="utf-8")
    assert checker._renders(_surface(), {"FILES_MAX_UPLOAD_PARTS"}) == {"FILES_MAX_UPLOAD_PARTS"}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("  # FILES_MAX_UPLOAD_PARTS: 10000\n", id="commented-out-render"),
        pytest.param("# FILES_MAX_UPLOAD_PARTS = 10000\n", id="commented-out-hcl"),
        pytest.param(
            "  # sized against FILES_MAX_UPLOAD_PARTS, which bounds the manifest\n",
            id="named-in-prose",
        ),
        pytest.param(
            "{{/* FILES_MAX_UPLOAD_PARTS: the manifest bound */}}\n",
            id="helm-template-comment",
        ),
        pytest.param(
            "{{- /*\n  FILES_MAX_UPLOAD_PARTS: 10000\n*/ -}}\n",
            id="multi-line-helm-comment",
        ),
        pytest.param("  budget: ${FILES_MAX_UPLOAD_PARTS}\n", id="read-as-a-value-not-rendered"),
    ],
)
def test_a_setting_a_surface_only_talks_about_is_not_rendered(
    surface_root: Path, text: str
) -> None:
    """A name that survives only in a comment reaches no running process, so it
    cannot stand in for putting the value on a container."""
    (surface_root / "surface.yaml").write_text(text, encoding="utf-8")
    assert checker._renders(_surface(), {"FILES_MAX_UPLOAD_PARTS"}) == set()


def test_the_deploy_notes_still_count_prose(surface_root: Path) -> None:
    """The notes are read, not applied: a table row describing the setting is
    exactly what they are asked for, so they keep the looser rule."""
    (surface_root / "notes.md").write_text(
        "| `FILES_MAX_UPLOAD_PARTS` | 10000 | Parts one session may span. |\n", encoding="utf-8"
    )
    notes = checker.Surface("deploy_notes", "notes", ("notes.md",))
    assert checker._mentions(notes, {"FILES_MAX_UPLOAD_PARTS"}) == {"FILES_MAX_UPLOAD_PARTS"}


def test_a_surface_glob_reaches_a_nested_template(surface_root: Path) -> None:
    nested = surface_root / "chart" / "templates"
    nested.mkdir(parents=True)
    (nested / "files.yaml").write_text('  FILES_ENABLED: "true"\n', encoding="utf-8")
    globbed = checker.Surface("helm", "helm", (), ("chart/**/*.yaml",))
    assert checker._renders(globbed, {"FILES_ENABLED"}) == {"FILES_ENABLED"}


# --------------------------------------------------------------------------
# .env.example must assign, not discuss
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "found"),
    [
        pytest.param("FILES_ENABLED=false", True, id="assignment"),
        pytest.param("# FILES_ENABLED=false", True, id="documented-default-still-counts"),
        pytest.param("#FILES_ENABLED=", True, id="no-space-empty-default"),
        pytest.param("# Turn FILES_ENABLED on to serve uploads", False, id="prose-does-not-count"),
        pytest.param("  indented FILES_ENABLED", False, id="bare-mention"),
    ],
)
def test_the_registry_counts_assignments_only(surface_root: Path, line: str, found: bool) -> None:
    (surface_root / ".env.example").write_text(f"{line}\n", encoding="utf-8")
    assert ("FILES_ENABLED" in checker.registry_assignments(surface_root / ".env.example")) is found


# --------------------------------------------------------------------------
# the two rules, both directions
# --------------------------------------------------------------------------


def test_a_setting_on_no_surface_at_all_fails_and_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "DEV_ONLY", {})
    monkeypatch.setattr(checker, "OPERATOR_TUNED", {})
    monkeypatch.setattr(checker, "EVERY_DEPLOYMENT", {})
    problems = _check([_row("brand_new_knob")])
    assert len(problems) == 1
    assert "BRAND_NEW_KNOB" in problems[0]


def test_a_setting_in_the_registry_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "DEV_ONLY", {})
    monkeypatch.setattr(checker, "OPERATOR_TUNED", {})
    monkeypatch.setattr(checker, "EVERY_DEPLOYMENT", {})
    assert _check([_row("brand_new_knob", present=frozenset({"env_example"}))]) == []


def test_a_dev_only_entry_excuses_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checker, "DEV_ONLY", {"brand_new_knob": "a local-only affordance"})
    monkeypatch.setattr(checker, "OPERATOR_TUNED", {})
    monkeypatch.setattr(checker, "EVERY_DEPLOYMENT", {})
    assert _check([_row("brand_new_knob")]) == []


def test_a_dev_only_entry_that_is_in_the_registry_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reverse direction: the list says the name must NOT be committed, so a
    line that commits it is the contradiction, not a pass."""
    monkeypatch.setattr(checker, "DEV_ONLY", {"brand_new_knob": "a local-only affordance"})
    monkeypatch.setattr(checker, "OPERATOR_TUNED", {})
    monkeypatch.setattr(checker, "EVERY_DEPLOYMENT", {})
    problems = _check([_row("brand_new_knob", present=frozenset({"env_example"}))])
    assert len(problems) == 1
    assert "remove one of the two" in problems[0]


@pytest.mark.parametrize("allowlist", ["DEV_ONLY", "OPERATOR_TUNED", "EVERY_DEPLOYMENT"])
def test_an_allowlist_entry_for_a_deleted_field_fails(
    monkeypatch: pytest.MonkeyPatch, allowlist: str
) -> None:
    monkeypatch.setattr(checker, "DEV_ONLY", {})
    monkeypatch.setattr(checker, "OPERATOR_TUNED", {})
    monkeypatch.setattr(checker, "EVERY_DEPLOYMENT", {})
    monkeypatch.setattr(checker, allowlist, {"knob_that_was_deleted": "a reason that outlived it"})
    problems = _check([_row("brand_new_knob", present=frozenset({"env_example"}))])
    assert len(problems) == 1
    assert "no longer a Settings field" in problems[0]


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        pytest.param(
            frozenset({"env_example", "compose"}),
            "is not in the operator docs (deploy_notes)",
            id="documented-nowhere",
        ),
        pytest.param(
            frozenset({"env_example", "deploy_notes"}),
            "no deployment surface renders it",
            id="described-but-never-rendered",
        ),
    ],
)
def test_an_operator_tuned_setting_needs_the_notes_and_a_render(
    monkeypatch: pytest.MonkeyPatch, present: frozenset[str], expected: str
) -> None:
    monkeypatch.setattr(checker, "DEV_ONLY", {})
    monkeypatch.setattr(checker, "OPERATOR_TUNED", {"brand_new_knob": "must fit the container"})
    monkeypatch.setattr(checker, "EVERY_DEPLOYMENT", {})
    problems = _check([_row("brand_new_knob", present=present)])
    assert len(problems) == 1
    assert expected in problems[0]


def test_an_operator_tuned_setting_documented_and_rendered_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checker, "DEV_ONLY", {})
    monkeypatch.setattr(checker, "OPERATOR_TUNED", {"brand_new_knob": "must fit the container"})
    monkeypatch.setattr(checker, "EVERY_DEPLOYMENT", {})
    row = _row("brand_new_knob", present=frozenset({"env_example", "deploy_notes", "helm"}))
    assert _check([row]) == []


@pytest.mark.parametrize("missing", ["helm", "compose", "terraform"])
def test_a_required_setting_one_deployment_surface_forgets_fails(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setattr(checker, "DEV_ONLY", {})
    monkeypatch.setattr(checker, "OPERATOR_TUNED", {})
    monkeypatch.setattr(checker, "EVERY_DEPLOYMENT", {"brand_new_secret": "boot refuses it"})
    present = frozenset({"env_example", "helm", "compose", "terraform"} - {missing})
    problems = _check([_row("brand_new_secret", present=present)])
    assert len(problems) == 1
    assert "must reach every deployment" in problems[0]
    assert f"{missing} does not render it" in problems[0]


def test_a_required_setting_every_deployment_surface_renders_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checker, "DEV_ONLY", {})
    monkeypatch.setattr(checker, "OPERATOR_TUNED", {})
    monkeypatch.setattr(checker, "EVERY_DEPLOYMENT", {"brand_new_secret": "boot refuses it"})
    row = _row(
        "brand_new_secret", present=frozenset({"env_example", "helm", "compose", "terraform"})
    )
    assert _check([row]) == []


def test_the_repository_renders_every_required_setting_on_every_deployment() -> None:
    """The real surfaces, not hand-built rows: the rule holds for the repo."""
    rows = [row for row in checker.collect_rows() if row.field in checker.EVERY_DEPLOYMENT]
    problems = [p for p in checker.check(rows) if "must reach every deployment" in p]
    assert problems == []


# --------------------------------------------------------------------------
# the shipped lists describe the settings that exist
# --------------------------------------------------------------------------


@pytest.mark.parametrize("allowlist", ["DEV_ONLY", "OPERATOR_TUNED", "EVERY_DEPLOYMENT"])
def test_the_shipped_allowlists_name_live_settings_with_a_reason(allowlist: str) -> None:
    entries: dict[str, str] = getattr(checker, allowlist)
    assert entries, f"{allowlist} is empty; the rule it governs enforces nothing"
    unknown = set(entries) - set(Settings.model_fields)
    assert not unknown, unknown
    assert all(reason.strip() for reason in entries.values())


# --------------------------------------------------------------------------
# a tree declares its own surfaces, and a composed checkout reads both trees
# --------------------------------------------------------------------------

_COMPOSE = "deploy/docker/compose.prod.example.yml"
_HELM = "chart/alkera/values.yaml"


def _write(root: Path, relative: str, text: str = "x: 1\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _declare(root: Path, where: str, key: str, path: str, kind: str = "deployment") -> None:
    _write(
        root,
        f"{where}/settings-surfaces.toml",
        f'[[surface]]\nkey = "{key}"\nkind = "{kind}"\npaths = ["{path}"]\n',
    )


def test_a_tree_holds_only_the_surfaces_it_declares(surface_root: Path) -> None:
    _write(surface_root, _COMPOSE)
    _write(surface_root, _HELM)
    _declare(surface_root, "deploy/docker", "compose", _COMPOSE)
    # The chart's file is here, but nothing declares it: it is not a surface.
    assert checker.active_deployment_surfaces() == ("compose",)


def test_a_declared_surface_with_no_file_is_not_held(surface_root: Path) -> None:
    _declare(surface_root, "deploy/docker", "compose", _COMPOSE)
    _declare(surface_root, "handbook", "notes", "handbook/deploy-notes.md", kind="docs")
    assert checker.active_deployment_surfaces() == ()
    assert checker.active_docs_surfaces() == ()


def test_a_composed_checkout_reads_its_own_declarations_and_the_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checker lives in the open tree; run from the checkout that composes
    it, the composing checkout's surfaces are held too, each relative to its own
    root."""
    open_tree = tmp_path / "Open"
    _write(open_tree, _COMPOSE)
    _declare(open_tree, "deploy/docker", "compose", _COMPOSE)
    _write(tmp_path, _HELM)
    _declare(tmp_path, "chart", "helm", _HELM)
    _write(tmp_path, "handbook/deploy-notes.md", "notes\n")
    _declare(tmp_path, "handbook", "deploy_notes", "handbook/deploy-notes.md", kind="docs")
    monkeypatch.setattr(checker, "REPO_ROOT", open_tree)

    monkeypatch.chdir(open_tree)
    assert checker.active_deployment_surfaces() == ("compose",)

    monkeypatch.chdir(tmp_path)
    assert sorted(checker.active_deployment_surfaces()) == ["compose", "helm"]
    assert checker.active_docs_surfaces() == ("deploy_notes",)


def test_a_surface_declared_twice_is_refused(surface_root: Path) -> None:
    _declare(surface_root, "a", "compose", _COMPOSE)
    _declare(surface_root, "b", "compose", _COMPOSE)
    with pytest.raises(ValueError, match="declared twice"):
        checker.load_surfaces()


@pytest.mark.parametrize("kind", ["registry", "chart"])
def test_a_declared_kind_must_be_docs_or_deployment(tmp_path: Path, kind: str) -> None:
    with pytest.raises(ValueError, match="kind must be docs or deployment"):
        checker.parse_declaration(f'[[surface]]\nkey = "x"\nkind = "{kind}"\n', tmp_path)


def _every_deployment_problems(
    monkeypatch: pytest.MonkeyPatch, present: frozenset[str], deployment: tuple[str, ...]
) -> list[str]:
    monkeypatch.setattr(checker, "DEV_ONLY", {})
    monkeypatch.setattr(checker, "OPERATOR_TUNED", {})
    monkeypatch.setattr(checker, "EVERY_DEPLOYMENT", {"brand_new_knob": "a boot secret"})
    row = _row("brand_new_knob", present=frozenset({"env_example"}) | present)
    return checker.check([row], deployment)


def test_the_open_tree_holds_a_boot_setting_to_compose_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _every_deployment_problems(monkeypatch, frozenset({"compose"}), ("compose",)) == []


def test_a_composed_checkout_still_holds_it_to_helm_and_terraform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problems = _every_deployment_problems(
        monkeypatch, frozenset({"compose"}), ("helm", "compose", "terraform")
    )
    assert len(problems) == 1
    assert "helm, terraform does not render it" in problems[0]


def test_a_checkout_with_no_deployment_surface_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    problems = _every_deployment_problems(monkeypatch, frozenset(), ())
    assert len(problems) == 1
    assert problems[0].startswith("No deployment surface is in this checkout")


def test_the_report_shows_only_the_surfaces_held(surface_root: Path) -> None:
    _write(surface_root, _COMPOSE)
    _write(surface_root, _HELM)
    _write(surface_root, ".env.example", "X=1\n")
    _write(
        surface_root,
        "deploy/docker/settings-surfaces.toml",
        f'[[surface]]\nkey = "compose"\nlabel = "compose.prod"\npaths = ["{_COMPOSE}"]\n'
        f'[[surface]]\nkey = "helm"\npaths = ["chart/missing.yaml"]\n',
    )
    header = checker.report([_row("brand_new_knob")]).splitlines()[0]
    assert header == "| setting | group | default | .env.example | compose.prod |"


# --- Settings sections: one owner per setting, one registry per owner ---------


class OpenProbe(Settings.__mro__[1]):  # type: ignore[misc]
    """An open section stand-in (a ``SettingsSection``)."""

    probe_open_limit: int = 3


class ProductProbe(Settings.__mro__[1]):  # type: ignore[misc]
    """A product section stand-in, declared for the product registry."""

    probe_product_token: str | None = None


class ClashingProbe(Settings.__mro__[1]):  # type: ignore[misc]
    probe_open_limit: int = 4


def _sections(root: Path, *pairs: tuple[str, str]) -> tuple[object, ...]:
    return tuple(checker.Section(f"{__name__}:{name}", registry, root) for name, registry in pairs)


def _put(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


_SPLIT = (("OpenProbe", ".env.example"), ("ProductProbe", "product.env.example"))


def test_each_section_is_held_to_its_own_registry(surface_root: Path) -> None:
    _put(surface_root, ".env.example", "# PROBE_OPEN_LIMIT=3\n")
    _put(surface_root, "product.env.example", "PROBE_PRODUCT_TOKEN=\n")
    rows = {row.field: row for row in checker.collect_rows(_sections(surface_root, *_SPLIT))}
    assert "env_example" in rows["probe_open_limit"].present
    assert "env_example" in rows["probe_product_token"].present
    assert checker.ownership_problems(_sections(surface_root, *_SPLIT)) == []


def test_a_product_setting_missing_from_the_product_registry_is_not_reachable(
    surface_root: Path,
) -> None:
    """Listing it in the open example does not count: that is the wrong registry."""
    _put(surface_root, ".env.example", "PROBE_OPEN_LIMIT=3\nPROBE_PRODUCT_TOKEN=\n")
    _put(surface_root, "product.env.example", "")
    rows = {row.field: row for row in checker.collect_rows(_sections(surface_root, *_SPLIT))}
    assert "env_example" not in rows["probe_product_token"].present


def test_a_product_setting_in_the_open_registry_fails(surface_root: Path) -> None:
    _put(surface_root, ".env.example", "PROBE_OPEN_LIMIT=3\n# PROBE_PRODUCT_TOKEN=\n")
    _put(surface_root, "product.env.example", "PROBE_PRODUCT_TOKEN=\n")
    problems = checker.ownership_problems(_sections(surface_root, *_SPLIT))
    assert len(problems) == 1
    assert "`PROBE_PRODUCT_TOKEN`" in problems[0] and ".env.example lists" in problems[0]


def test_an_open_setting_in_the_product_registry_fails(surface_root: Path) -> None:
    _put(surface_root, ".env.example", "PROBE_OPEN_LIMIT=3\n")
    _put(surface_root, "product.env.example", "PROBE_PRODUCT_TOKEN=\nPROBE_OPEN_LIMIT=9\n")
    problems = checker.ownership_problems(_sections(surface_root, *_SPLIT))
    assert [("PROBE_OPEN_LIMIT" in p, "product.env.example lists" in p) for p in problems] == [
        (True, True)
    ]


def test_a_setting_declared_by_two_sections_fails(surface_root: Path) -> None:
    _put(surface_root, ".env.example", "PROBE_OPEN_LIMIT=3\n")
    sections = _sections(
        surface_root, ("OpenProbe", ".env.example"), ("ClashingProbe", ".env.example")
    )
    problems = checker.ownership_problems(sections)
    assert len(problems) == 1 and "declared by both" in problems[0]


def test_a_section_is_read_from_a_tree_declaration(surface_root: Path) -> None:
    (surface_root / "pkg").mkdir()
    _put(
        surface_root / "pkg",
        "settings-surfaces.toml",
        f'[[settings]]\nsection = "{__name__}:ProductProbe"\nregistry = "product.env.example"\n',
    )
    [section] = checker.load_sections()
    assert (section.target, section.registry_path) == (
        f"{__name__}:ProductProbe",
        surface_root / "product.env.example",
    )


def test_the_shipped_open_registry_lists_no_product_setting() -> None:
    """The repository itself: the open example names only open settings."""
    assert checker.ownership_problems() == []
    sections = {section.target: section for section in checker.load_sections()}
    assert sections["alkera_core.config:Settings"].registry == ".env.example"


def test_the_shipped_open_registry_turns_no_mock_provider_on() -> None:
    """The mock OAuth provider puts a test button on the login page; no example
    a stranger copies may turn it on."""
    examples = sorted(REPO_ROOT.glob(".env*.example"))
    assert REPO_ROOT / ".env.example" in examples
    for path in examples:
        text = path.read_text(encoding="utf-8")
        assert "OAUTH_MOCK_ENABLED=" not in text, path.name
