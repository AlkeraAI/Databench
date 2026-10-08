"""What a test module says about sharing, checked against what it actually does.

``make test-py`` distributes the Python suite with ``--dist loadgroup``: work is
handed out one TEST at a time, and only the tests that share an ``xdist_group``
mark are kept together on one worker. Every test carries such a mark — the
repo-root ``conftest.py`` gives each ungrouped item its own module's path as its
group — so the DEFAULT is that a module stays whole, and the per-test parallelism
comes from the modules that opt out of it::

    pytestmark = [pytest.mark.spread]

This file guards both directions, and it does so without importing anything:
it reads every module under the ``testpaths`` in ``pyproject.toml`` with ``ast``
(importing 1400 test modules would pull the whole app and cost more than the suite
it guards). It fails when:

1. A module that keeps state across its own tests carries no explicit
   ``pytestmark = pytest.mark.xdist_group("<short-stable-name>")``. The automatic
   module group already holds such a module together, so this rule is no longer
   the thing that makes it correct — it is what keeps it correct: it is the only
   written record of which modules are deliberately held together, and it is what
   makes ``spread`` on one of them impossible to add by accident (rule 2 reads the
   same detector).
2. A module says ``spread`` while doing exactly that kind of sharing. That
   declaration is the one way to give up the safe default, so it has to be true.

The rule, in full. A module keeps state across its own tests when any of these
holds:

1. It defines a fixture with ``scope="module"`` or ``scope="class"``. Under
   ``loadgroup`` such a fixture is built once per worker that receives one of the
   module's tests, so a split module rebuilds it N times. For a fixture that only
   parses a file that is waste; for one that owns a resource, a connection or a
   row in the database it is also a second owner of the thing. Both are reasons to
   keep the module together, so the rule does not try to tell them apart.
2. It defines ``setUpClass`` / ``setup_class`` — the ``unittest`` spelling of the
   same shared setup.
3. One of its tests or fixtures asks, by parameter name, for a module- or
   class-scoped fixture defined in a ``conftest.py`` in its own directory or any
   directory above it. A conftest fixture is only counted when it is actually
   requested: a shared-scope fixture sitting unused in a conftest constrains
   nothing.
4. A ``conftest.py`` above it defines an ``autouse`` module- or class-scoped
   fixture, which every module in that subtree gets whether it asks or not.

What the rule deliberately does NOT cover:

- **Session- and package-scoped fixtures.** Every xdist worker runs its own
  session, so those are per-worker under ``loadscope``, ``load`` and ``loadgroup``
  alike. A group mark cannot make one shared; it would only pin the module.
- **Module-level mutable state** (a global list a test appends to and a later test
  reads). ``ast`` can see the mutation but not whether a second test depends on
  seeing it, and every such global in this repo today is keyed per test and reset
  by the test that uses it. Ordering dependence of that kind is a bug in its own
  right; this file does not try to guess at it.

Only a module-level ``pytestmark`` counts as declaring either mark. A per-test
``@pytest.mark.xdist_group`` decorator keeps *that test* with the group but leaves
its neighbours free to land elsewhere, which is not what any of the four cases
above needs; a per-test ``spread`` is the same mistake in the other direction.
"""

from __future__ import annotations

import ast
import importlib
import re
import tomllib
from collections.abc import Iterable
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# One xdist worker for this module: the scan below reads every test file in the
# repository, and the cache that keeps it to one read is per worker process.
pytestmark = pytest.mark.xdist_group("xdist_groups_meta")

#: The fixture scopes that tie a module's tests to one another. See the module
#: docstring for why session/package are not here.
SHARED_SCOPES = frozenset({"module", "class"})

#: ``python_files`` is left at pytest's default, so these are the collected names.
TEST_FILE_GLOBS = ("test_*.py", "*_test.py")

#: Modules that trip the rule above and are still allowed to distribute per test,
#: with the reason. Empty today: every module the rule names carries the mark.
#: An entry here is a claim that the shared-scope fixture is safe to rebuild per
#: worker AND cheap enough that rebuilding it is not worth pinning the module —
#: both halves have to be true, and the reason has to say why.
ALLOWED_WITHOUT_GROUP: dict[str, str] = {}


def _decorator_scope(decorator: ast.expr) -> tuple[str, str, bool] | None:
    """``(fixture name suffix, scope, autouse)`` if this decorator is a fixture."""
    call = decorator if isinstance(decorator, ast.Call) else None
    target = call.func if call is not None else decorator
    if not isinstance(target, (ast.Name, ast.Attribute)):
        return None
    dotted = ast.unparse(target)
    if dotted.split(".")[-1] != "fixture":
        return None
    scope, autouse, name = "function", False, ""
    if call is not None:
        for keyword in call.keywords:
            if not isinstance(keyword.value, ast.Constant):
                continue
            if keyword.arg == "scope":
                scope = str(keyword.value.value)
            elif keyword.arg == "autouse":
                autouse = bool(keyword.value.value)
            elif keyword.arg == "name":
                name = str(keyword.value.value)
    return name, scope, autouse


def _shared_fixtures(tree: ast.Module) -> dict[str, tuple[str, bool]]:
    """``{fixture name: (scope, autouse)}`` for the module/class-scoped fixtures."""
    found: dict[str, tuple[str, bool]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            parsed = _decorator_scope(decorator)
            if parsed is None:
                continue
            override, scope, autouse = parsed
            if scope in SHARED_SCOPES:
                found[override or node.name] = (scope, autouse)
    return found


def _requested_fixture_names(tree: ast.Module) -> set[str]:
    """Every name a test or a fixture in this module asks for as a parameter."""
    requested: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_fixture = any(_decorator_scope(d) is not None for d in node.decorator_list)
        if not (is_fixture or node.name.startswith("test")):
            continue
        args = node.args
        for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            if argument.arg not in {"self", "cls"}:
                requested.add(argument.arg)
    return requested


def _defines_unittest_class_setup(tree: ast.Module) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"setUpClass", "setup_class"}
        for node in ast.walk(tree)
    )


def _module_level_marks(tree: ast.Module) -> list[ast.expr]:
    """The expressions assigned to a top-level ``pytestmark``, flattened."""
    marks: list[ast.expr] = []
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        value = node.value
        if isinstance(value, (ast.List, ast.Tuple)):
            marks.extend(value.elts)
        elif value is not None:
            marks.append(value)
    return marks


def _declared_group_names(tree: ast.Module) -> set[str]:
    """The group names a module-level ``pytestmark`` pins this module to."""
    names: set[str] = set()
    for mark in _module_level_marks(tree):
        for node in ast.walk(mark):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, (ast.Name, ast.Attribute)):
                continue
            if ast.unparse(node.func).split(".")[-1] != "xdist_group":
                continue
            for argument in (*node.args, *[k.value for k in node.keywords]):
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    names.add(argument.value)
    return names


def declares_spread(tree: ast.Module) -> bool:
    """Whether a module-level ``pytestmark`` opts this module out of its module group.

    ``pytest.mark.spread`` is an attribute, not a call, so the group detector above
    would never see it.
    """
    for mark in _module_level_marks(tree):
        for node in ast.walk(mark):
            if isinstance(node, ast.Attribute) and ast.unparse(node).endswith("mark.spread"):
                return True
    return False


def reasons_a_module_cannot_be_split(
    tree: ast.Module,
    inherited: dict[str, tuple[str, bool]],
) -> list[str]:
    """Why this module's tests have to stay on one worker, in reader's order.

    ``inherited`` is the module/class-scoped fixtures the conftests above it
    define, ``{name: (scope, autouse)}``. An empty result means the module is free
    to distribute per test.
    """
    reasons: list[str] = []
    for name, (scope, _autouse) in sorted(_shared_fixtures(tree).items()):
        reasons.append(f'it defines the {scope}-scoped fixture "{name}"')
    if _defines_unittest_class_setup(tree):
        reasons.append("it defines unittest class-level setup (setUpClass/setup_class)")
    requested = _requested_fixture_names(tree)
    for name, (scope, autouse) in sorted(inherited.items()):
        if autouse:
            reasons.append(f'a conftest above it applies the autouse {scope}-scoped "{name}"')
        elif name in requested:
            reasons.append(f'it requests the {scope}-scoped conftest fixture "{name}"')
    return reasons


@lru_cache(maxsize=1)
def _testpaths() -> tuple[Path, ...]:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    paths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    return tuple(REPO_ROOT / entry for entry in paths)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


#: Nothing either rule reports can exist in a module whose source matches none of
#: these: a shared-scope fixture has to spell its scope, unittest setup has to
#: spell its method name, and a declared group or a ``spread`` has to spell its
#: mark. Reading the text and looking is far cheaper than parsing 1400 modules
#: that say none of it.
_TRIGGERS = re.compile(r"scope\s*=|setUpClass|setup_class|xdist_group|mark\.spread")


def might_carry_a_grouping_reason(source: str, inherited: Iterable[str]) -> bool:
    """Whether ``source`` is worth parsing — a superset of what the rules report."""
    if _TRIGGERS.search(source) is not None:
        return True
    return any(name in source for name in inherited)


def _test_modules() -> list[Path]:
    """Every file pytest would collect under ``testpaths``, deduplicated."""
    found: set[Path] = set()
    for testpath in _testpaths():
        for glob in TEST_FILE_GLOBS:
            found.update(testpath.rglob(glob))
    return sorted(found)


@lru_cache(maxsize=1)
def _scan() -> tuple[dict[Path, list[str]], dict[Path, set[str]], set[Path]]:
    """``({module: reasons it cannot be split}, {module: group names}, {spread modules})``.

    Only the ``testpaths`` are walked, never the repository root: the root holds
    ``.venv``, ``node_modules`` and the vendored opencode tree, and a rule that
    walks those costs more than the suite it guards.
    """
    conftests: dict[Path, dict[str, tuple[str, bool]]] = {}
    root_conftest = REPO_ROOT / "conftest.py"
    if root_conftest.is_file():
        conftests[REPO_ROOT] = _shared_fixtures(_tree(root_conftest))
    for testpath in _testpaths():
        for conftest in testpath.rglob("conftest.py"):
            conftests[conftest.parent] = _shared_fixtures(_tree(conftest))

    reasons: dict[Path, list[str]] = {}
    groups: dict[Path, set[str]] = {}
    spreads: set[Path] = set()
    for module in _test_modules():
        inherited: dict[str, tuple[str, bool]] = {}
        for ancestor in module.parents:
            for name, spec in conftests.get(ancestor, {}).items():
                inherited.setdefault(name, spec)
            if ancestor == REPO_ROOT:
                break
        source = module.read_text(encoding="utf-8")
        if not might_carry_a_grouping_reason(source, inherited):
            continue
        tree = ast.parse(source, filename=str(module))
        declared = _declared_group_names(tree)
        if declared:
            groups[module] = declared
        if declares_spread(tree):
            spreads.add(module)
        needs = reasons_a_module_cannot_be_split(tree, inherited)
        if needs:
            reasons[module] = needs
    return reasons, groups, spreads


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_every_module_that_cannot_be_split_declares_its_xdist_group() -> None:
    """The gate: under ``--dist loadgroup``, an undeclared module IS split."""
    reasons, groups, _ = _scan()
    offenders = {
        module: why
        for module, why in reasons.items()
        if not groups.get(module) and _relative(module) not in ALLOWED_WITHOUT_GROUP
    }
    report = "\n".join(
        f"  {_relative(module)}\n"
        + "".join(f"      - {why}\n" for why in offenders[module])
        + '      fix: pytestmark = pytest.mark.xdist_group("<short-stable-name>")'
        for module in sorted(offenders)
    )
    assert not offenders, (
        "these modules keep state across their own tests but let --dist loadgroup "
        f"spread those tests over different workers:\n{report}"
    )


def test_a_module_that_says_spread_really_shares_nothing() -> None:
    """The other gate: ``spread`` is the one way to lose the automatic module group.

    An unmarked module is kept whole whatever it does, so the detector above is a
    warning there. Here it is load-bearing — a module that declares ``spread``
    while owning a module- or class-scoped fixture (its own, or one it takes from a
    conftest) has its tests handed to different workers, and each of those workers
    builds that fixture again.
    """
    reasons, _, spreads = _scan()
    offenders = {module: reasons[module] for module in spreads if module in reasons}
    report = "\n".join(
        f"  {_relative(module)}\n" + "".join(f"      - {why}\n" for why in offenders[module])
        for module in sorted(offenders)
    )
    assert not offenders, (
        "these modules declare pytest.mark.spread but keep state across their own "
        f"tests, so their tests must not be split:\n{report}"
        "      fix: drop the spread mark (or the shared-scope fixture it contradicts)"
    )


def test_the_allowlist_only_names_modules_the_rule_would_otherwise_catch() -> None:
    """A stale exemption is a rule nobody is enforcing any more."""
    reasons, _, _ = _scan()
    named = {_relative(module) for module in reasons}
    stale = sorted(set(ALLOWED_WITHOUT_GROUP) - named)
    assert not stale, (
        f"these allowlist entries no longer trip the rule and should be deleted: {stale}"
    )


def test_a_group_name_is_never_shared_across_directories() -> None:
    """Two unrelated modules on one name would serialize onto one worker.

    Sharing a name inside one directory is how a directory-wide conftest fixture
    is declared (the Files perf lane does it); sharing it across directories is
    always a copy-paste that quietly rebuilds the straggler loadgroup removes.
    """
    _, groups, _ = _scan()
    directories: dict[str, set[str]] = {}
    for module, names in groups.items():
        for name in names:
            directories.setdefault(name, set()).add(_relative(module.parent))
    collisions = {name: sorted(dirs) for name, dirs in directories.items() if len(dirs) > 1}
    assert not collisions, f"one xdist_group name is claimed from several directories: {collisions}"


def test_the_walk_actually_reaches_the_suite() -> None:
    """A rule that silently scans nothing passes forever."""
    _, groups, spreads = _scan()
    scanned = len(_test_modules())
    assert scanned > 1000, f"only {scanned} test modules found under testpaths"
    assert groups, "no module in the repository declares an xdist_group"
    assert spreads, "no module in the repository declares spread"


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(
            'import pytest\n@pytest.fixture(scope="module")\ndef f(): ...\n',
            ['it defines the module-scoped fixture "f"'],
            id="module-scoped-fixture",
        ),
        pytest.param(
            'import pytest\n@pytest.fixture(scope="class")\ndef f(): ...\n',
            ['it defines the class-scoped fixture "f"'],
            id="class-scoped-fixture",
        ),
        pytest.param(
            'import pytest\n@pytest.fixture(scope="module", name="renamed")\ndef f(): ...\n',
            ['it defines the module-scoped fixture "renamed"'],
            id="name-override-is-the-fixture-name",
        ),
        pytest.param(
            'import pytest_asyncio\n@pytest_asyncio.fixture(scope="module")\nasync def f(): ...\n',
            ['it defines the module-scoped fixture "f"'],
            id="pytest-asyncio-fixture",
        ),
        pytest.param(
            "class T:\n    @classmethod\n    def setUpClass(cls): ...\n",
            ["it defines unittest class-level setup (setUpClass/setup_class)"],
            id="unittest-class-setup",
        ),
        pytest.param(
            'import pytest\n@pytest.fixture(scope="session")\ndef f(): ...\n',
            [],
            id="session-scope-is-per-worker-anyway",
        ),
        pytest.param(
            "import pytest\n@pytest.fixture\ndef f(): ...\n",
            [],
            id="function-scope-is-free-to-split",
        ),
        pytest.param(
            "_SEEN = []\ndef test_a():\n    _SEEN.append(1)\n",
            [],
            id="module-level-state-is-out-of-scope-for-this-rule",
        ),
    ],
)
def test_the_rule_names_exactly_the_shapes_it_claims_to(source: str, expected: list[str]) -> None:
    """The detector, driven with plain source — the gate above is only as good."""
    assert reasons_a_module_cannot_be_split(ast.parse(source), {}) == expected


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(
            "def test_a(tlc_traces): ...\n",
            ['it requests the module-scoped conftest fixture "tlc_traces"'],
            id="requested-by-a-test",
        ),
        pytest.param(
            "import pytest\n@pytest.fixture\ndef rig(tlc_traces): ...\n",
            ['it requests the module-scoped conftest fixture "tlc_traces"'],
            id="requested-by-a-fixture",
        ),
        pytest.param(
            "def helper(tlc_traces): ...\n",
            [],
            id="a-plain-helpers-parameter-is-not-a-request",
        ),
        pytest.param(
            "def test_a(): ...\n",
            [],
            id="unrequested-conftest-fixture-constrains-nothing",
        ),
    ],
)
def test_a_conftest_fixture_counts_only_where_it_is_requested(
    source: str, expected: list[str]
) -> None:
    inherited = {"tlc_traces": ("module", False)}
    assert reasons_a_module_cannot_be_split(ast.parse(source), inherited) == expected


def test_an_autouse_conftest_fixture_reaches_every_module_below_it() -> None:
    """Nothing in the module names it, and it still runs there."""
    inherited = {"shallow_tree": ("module", True)}
    assert reasons_a_module_cannot_be_split(ast.parse("def test_a(): ...\n"), inherited) == [
        'a conftest above it applies the autouse module-scoped "shallow_tree"'
    ]


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(
            'import pytest\npytestmark = pytest.mark.xdist_group("solo")\n',
            {"solo"},
            id="single-mark",
        ),
        pytest.param(
            "import pytest\n"
            'pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group("in-a-list")]\n',
            {"in-a-list"},
            id="inside-a-list",
        ),
        pytest.param(
            'import pytest\npytestmark = pytest.mark.xdist_group(name="by-keyword")\n',
            {"by-keyword"},
            id="passed-by-keyword",
        ),
        pytest.param(
            'import pytest\n@pytest.mark.xdist_group("per-test")\ndef test_a(): ...\n',
            set(),
            id="a-per-test-decorator-does-not-pin-the-module",
        ),
        pytest.param(
            "import pytest\npytestmark = pytest.mark.asyncio\n",
            set(),
            id="an-unrelated-pytestmark",
        ),
    ],
)
def test_only_a_module_level_pytestmark_declares_the_group(source: str, expected: set[str]) -> None:
    assert _declared_group_names(ast.parse(source)) == expected


@pytest.mark.parametrize(
    "source, expected",
    [
        pytest.param(
            "import pytest\npytestmark = [pytest.mark.spread]\n",
            True,
            id="inside-a-list",
        ),
        pytest.param(
            "import pytest\npytestmark = pytest.mark.spread\n",
            True,
            id="on-its-own",
        ),
        pytest.param(
            "import pytest\n"
            "pytestmark = [pytest.mark.skipif(True, reason='x'), pytest.mark.spread]\n",
            True,
            id="beside-another-mark",
        ),
        pytest.param(
            "from pytest import mark\npytestmark = [mark.spread]\n",
            True,
            id="imported-mark",
        ),
        pytest.param(
            "import pytest\n@pytest.mark.spread\ndef test_a(): ...\n",
            False,
            id="a-per-test-decorator-does-not-free-the-module",
        ),
        pytest.param(
            "import pytest\npytestmark = pytest.mark.asyncio\n",
            False,
            id="an-unrelated-pytestmark",
        ),
        pytest.param(
            "SPREAD = 'the word in prose, not a mark'\n",
            False,
            id="the-word-alone-is-not-a-declaration",
        ),
    ],
)
def test_only_a_module_level_pytestmark_declares_spread(source: str, expected: bool) -> None:
    assert declares_spread(ast.parse(source)) is expected


@pytest.mark.parametrize(
    "source, inherited",
    [
        pytest.param(
            'import pytest\n@pytest.fixture(scope="module")\ndef f(): ...\n',
            (),
            id="module-scoped-fixture",
        ),
        pytest.param(
            "import pytest\n@pytest.fixture(scope = 'class')\ndef f(): ...\n",
            (),
            id="spaces-around-the-scope-keyword",
        ),
        pytest.param(
            "class T:\n    @classmethod\n    def setUpClass(cls): ...\n",
            (),
            id="unittest-class-setup",
        ),
        pytest.param(
            'import pytest\npytestmark = pytest.mark.xdist_group("g")\n',
            (),
            id="an-already-declared-group",
        ),
        pytest.param(
            "import pytest\npytestmark = [pytest.mark.spread]\n",
            (),
            id="a-declared-spread",
        ),
        pytest.param(
            "def test_a(tlc_traces): ...\n",
            ("tlc_traces",),
            id="requests-a-shared-scope-conftest-fixture",
        ),
    ],
)
def test_the_prefilter_never_drops_a_module_the_rule_would_report(
    source: str, inherited: tuple[str, ...]
) -> None:
    """The scan only parses what this admits, so a miss here is a silent pass."""
    assert might_carry_a_grouping_reason(source, inherited)


def test_the_prefilter_drops_an_ordinary_module() -> None:
    """Otherwise it saves nothing and the scan reads 1400 syntax trees."""
    source = "import pytest\n\n\ndef test_a(tmp_path):\n    assert tmp_path.is_dir()\n"
    assert not might_carry_a_grouping_reason(source, ("tlc_traces",))


# --- the hook that hands the default group out ---------------------------------------

#: The node-id prefix the probe modules below are collected under. It names no real
#: directory, so the fixtures pytest registers while collecting them can never be
#: matched by a real item.
PROBE_PREFIX = "_xdist_group_probe"


def _root_conftest(config: pytest.Config) -> ModuleType:
    """The repo-root ``conftest.py``, as this session registered it.

    Taken from the plugin manager rather than imported afresh: that module's body
    provisions this xdist worker's database — importing it a second time would DROP
    the one the session is running on. It also means the hook under test is the one
    the run is actually using, not a second copy of the file.
    """
    wanted = REPO_ROOT / "conftest.py"
    for _name, plugin in config.pluginmanager.list_name_plugin():
        path = getattr(plugin, "__file__", None)
        if path and Path(str(path)) == wanted:
            return plugin
    raise AssertionError(f"the root conftest ({wanted}) is not a registered plugin")


def _collect(request: pytest.FixtureRequest, tmp_path: Path, name: str, source: str) -> list[Any]:
    """Real ``pytest.Function`` items for ``source``, under a fake node id.

    Real items rather than stand-ins because what the hook reads is
    ``iter_markers`` over an item's whole chain — module ``pytestmark`` included —
    and a hand-built fake would have to reimplement exactly the part under test.

    The import finder lists a directory once and lists it again only when the
    directory's mtime moves. A second probe module written into ``tmp_path`` in
    the same filesystem-clock tick as the first (a few ms on Linux, ~15 ms on
    Windows) leaves the mtime where it was, so the finder answers from the listing
    that predates the file: ``ModuleNotFoundError``. Dropping the finders' caches
    after the write is what the import system asks of code that creates modules
    at run time.
    """
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    importlib.invalidate_caches()
    module = pytest.Module.from_parent(
        request.session, path=path, nodeid=f"{PROBE_PREFIX}/{name}.py"
    )
    return list(module.collect())


def _group_names(item: Any) -> set[str]:
    """The groups xdist would read off this item (``xdist/remote.py`` does the same)."""
    return {
        str(mark.args[0]) if mark.args else str(mark.kwargs.get("name", "default"))
        for mark in item.iter_markers("xdist_group")
    }


def test_an_ungrouped_item_is_given_its_own_module(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The default that replaces loadscope: the module's path is the group."""
    items = _collect(
        request,
        tmp_path,
        "test_probe_plain",
        "def test_a(): ...\ndef test_b(): ...\n",
    )
    _root_conftest(request.config).assign_module_groups(items)
    assert [item.name for item in items] == ["test_a", "test_b"]
    assert [_group_names(item) for item in items] == [
        {f"{PROBE_PREFIX}/test_probe_plain.py"},
        {f"{PROBE_PREFIX}/test_probe_plain.py"},
    ]


def test_an_item_in_a_spread_module_is_left_ungrouped(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Without a group, loadgroup hands the tests out one at a time."""
    items = _collect(
        request,
        tmp_path,
        "test_probe_spread",
        "import pytest\n\npytestmark = [pytest.mark.spread]\n\n"
        "def test_a(): ...\ndef test_b(): ...\n",
    )
    _root_conftest(request.config).assign_module_groups(items)
    assert [_group_names(item) for item in items] == [set(), set()]


def test_an_item_that_names_its_own_group_keeps_it(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """An explicit group is the answer, not a starting point to add to."""
    items = _collect(
        request,
        tmp_path,
        "test_probe_explicit",
        "import pytest\n\n"
        '@pytest.mark.xdist_group("chosen")\n'
        "def test_a(): ...\n\n"
        "def test_b(): ...\n",
    )
    _root_conftest(request.config).assign_module_groups(items)
    assert [_group_names(item) for item in items] == [
        {"chosen"},
        {f"{PROBE_PREFIX}/test_probe_explicit.py"},
    ]


def test_a_module_level_group_is_kept_for_every_test_in_it(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The mark thirty modules carry: the whole module answers to one name."""
    items = _collect(
        request,
        tmp_path,
        "test_probe_module_group",
        'import pytest\n\npytestmark = pytest.mark.xdist_group("shared")\n\n'
        "def test_a(): ...\ndef test_b(): ...\n",
    )
    _root_conftest(request.config).assign_module_groups(items)
    assert [_group_names(item) for item in items] == [{"shared"}, {"shared"}]


def test_the_group_is_the_module_path_alone(request: pytest.FixtureRequest, tmp_path: Path) -> None:
    """A parametrized test must not take its parameters into its group name."""
    items = _collect(
        request,
        tmp_path,
        "test_probe_parametrized",
        'import pytest\n\n@pytest.mark.parametrize("n", [1, 2])\ndef test_a(n): ...\n',
    )
    _root_conftest(request.config).assign_module_groups(items)
    assert [item.nodeid for item in items] == [
        f"{PROBE_PREFIX}/test_probe_parametrized.py::test_a[1]",
        f"{PROBE_PREFIX}/test_probe_parametrized.py::test_a[2]",
    ]
    assert {name for item in items for name in _group_names(item)} == {
        f"{PROBE_PREFIX}/test_probe_parametrized.py"
    }


# --- and the hook that keeps the ungrouped ones apart ---------------------------------


def _probe_items(
    request: pytest.FixtureRequest, tmp_path: Path, suffix: str, count: int, *, spread: bool
) -> list[Any]:
    """``count`` real items from one probe module, which either spreads or does not.

    The file name carries the requesting test's own id because pytest's default
    import mode keys a module on its basename: two probe files sharing one would be
    an import clash, not a second module.
    """
    header = "import pytest\n\n" + ("pytestmark = [pytest.mark.spread]\n\n" if spread else "")
    source = f'{header}@pytest.mark.parametrize("n", range({count}))\ndef test_a(n): ...\n'
    name = "test_probe_" + re.sub(r"\W+", "_", f"{request.node.name}_{suffix}")
    return _collect(request, tmp_path, name, source)


def _is_spread(item: Any) -> bool:
    """Whether the hook left this item ungrouped — a work unit of one, to xdist."""
    return not _group_names(item)


def _through_the_hook(
    request: pytest.FixtureRequest, tmp_path: Path, spread_count: int, grouped_count: int
) -> tuple[list[Any], list[Any], list[Any]]:
    """Drive the real hook over a collection whose spread items arrive adjacent.

    Adjacent is how they always arrive: they are one module's tests, and pytest
    collects a module in one run. Returns ``(result, grouped input, spread input)``.
    """
    grouped = _probe_items(request, tmp_path, "grouped", grouped_count, spread=False)
    spread = _probe_items(request, tmp_path, "spread", spread_count, spread=True)
    items = [*grouped, *spread]
    _root_conftest(request.config).pytest_collection_modifyitems(items)
    return items, grouped, spread


@pytest.mark.parametrize(
    "spread_count, grouped_count",
    [
        pytest.param(1, 12, id="one-among-many"),
        pytest.param(3, 30, id="a-few-among-many"),
        pytest.param(14, 140, id="the-shape-the-files-corpus-has"),
        pytest.param(5, 5, id="exactly-as-many-as-there-is-room-for"),
    ],
)
def test_two_spread_items_never_end_up_next_to_each_other(
    request: pytest.FixtureRequest, tmp_path: Path, spread_count: int, grouped_count: int
) -> None:
    """The point of the whole exercise: neighbours in the collection are neighbours
    in the scheduler's queue, and a worker is topped up with several units at once.

    The head of the list matters for the same reason — it is the first batch handed
    out — so nothing spread starts there while there are grouped items to put first.
    """
    items, _, _ = _through_the_hook(request, tmp_path, spread_count, grouped_count)
    neighbours = [
        (left.nodeid, right.nodeid)
        for left, right in pairwise(items)
        if _is_spread(left) and _is_spread(right)
    ]
    assert not neighbours, f"these spread items would go to one worker back to back: {neighbours}"
    assert not _is_spread(items[0])


def test_more_spread_items_than_room_starts_the_collection_with_one(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Below one grouped item per spread item there is nothing left to separate them
    with, so the spacing is all the guarantee there is (pinned below) and the head
    of the list goes to a spread item like any other position."""
    items, _, _ = _through_the_hook(request, tmp_path, 7, 3)
    assert _is_spread(items[0])


def test_both_kinds_keep_the_order_they_were_collected_in(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Only the gaps between them change. pytest orders a collection for cheap
    fixture reuse, and a module's own tests still run in the order they are written.
    """
    items, grouped, spread = _through_the_hook(request, tmp_path, 4, 20)
    assert [item for item in items if not _is_spread(item)] == grouped
    assert [item for item in items if _is_spread(item)] == spread


def test_a_collection_with_nothing_to_spread_is_left_exactly_as_it_was(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Which is every run of every suite but the one module that opts out."""
    grouped = _probe_items(request, tmp_path, "grouped", 12, spread=False)
    items = list(grouped)
    _root_conftest(request.config).pytest_collection_modifyitems(items)
    assert all(left is right for left, right in zip(items, grouped, strict=True))


@pytest.mark.parametrize(
    "spread_count, grouped_count",
    [
        pytest.param(1, 12, id="a-single-one"),
        pytest.param(3, 10, id="an-uneven-division"),
        pytest.param(5, 5, id="one-grouped-item-each"),
        pytest.param(7, 3, id="more-than-there-is-room-for"),
    ],
)
def test_the_stretches_between_spread_items_are_as_even_as_they_divide(
    request: pytest.FixtureRequest, tmp_path: Path, spread_count: int, grouped_count: int
) -> None:
    """Evenly spaced is what makes a batch of any size carry at most one of them:
    every stretch of grouped tests before a spread one is the same length, give or
    take the remainder that will not divide."""
    items, grouped, spread = _through_the_hook(request, tmp_path, spread_count, grouped_count)
    assert sorted(map(id, items)) == sorted(map(id, [*grouped, *spread])), (
        "the collection handed on is the collection that was collected"
    )
    positions = [index for index, item in enumerate(items) if _is_spread(item)]
    stretches = [positions[0], *(b - a - 1 for a, b in pairwise(positions))]
    even = {grouped_count // spread_count, -(-grouped_count // spread_count)}
    assert set(stretches) <= even, f"uneven stretches {stretches}, expected each of {sorted(even)}"
