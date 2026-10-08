"""Persisted test-catalog shapes -- the schema-change gate's authored-test models.

``TestDefinition`` (a reusable, typed check that compiles to a failing-rows
SELECT -- 0 rows = pass) is split from ``TestSpec`` (a definition bound to one or
more target URNs with severity + enablement), as DataHub and OpenMetadata do.
``GatePolicy`` is the repo-level ``alkera/gate.yml`` shape.

All three are persisted (authored files round-trip through them; specs are
embedded in ``GateReport``), so they are ``VersionedModel``s with fixtures and a
lineage test. The taxonomy fields (``kind``, ``severity``, ``mode``) are plain
``str`` on the wire so a newer writer's value survives an older reader.

Multi-target bindings are mandatory by design: ``refs`` carries EVERY input URN
of a test, so a join-two-tables test is selected when EITHER input changes
(siblings are not lineage-connected -- a single-target ref would silently skip
it, the worst false-green the adversarial panel found).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import Field

from alkera_core.versioning import VersionedModel


class TestKind(StrEnum):
    """The KNOWN typed test kinds (the closed set; the field stays ``str`` on the
    wire). Each SQL kind compiles to a failing-rows SELECT; ``PYTHON`` is the
    pytest-native ``@alkera.test`` surface executed through the runner seam;
    ``SCHEMA`` is evaluated against the lineage store's introspected schema
    (the absolute twin of the gate's relative diff)."""

    __test__ = False  # not a pytest test class despite the Test* name

    NOT_NULL = "not_null"
    UNIQUE = "unique"
    UNIQUE_COMBINATION = "unique_combination"
    """``params: {columns: [a, b, ...]}`` (>= 2) -- the multi-column tuple must be
    unique. Binds EVERY named column as a ref so column-grain selection fires
    when ANY of them changes. Group grain, so ``mostly:`` does not apply."""
    ACCEPTED_VALUES = "accepted_values"
    RELATIONSHIPS = "relationships"
    ROW_COUNT_BETWEEN = "row_count_between"
    CUSTOM_SQL = "custom_sql"
    PYTHON = "python"
    AGGREGATE_BETWEEN = "aggregate_between"
    """``params: {aggregate: min|max|avg|sum|stddev, min?, max?}`` — the named
    aggregate of the bound column must fall inside the bounds."""
    NUMERIC_RANGE = "numeric_range"
    """``params: {min?, max?}`` — every non-NULL value inside the bounds
    (NULLs are ``not_null``'s job, the GX convention)."""
    REGEX_MATCH = "regex_match"
    """``params: {pattern}`` — every non-NULL value matches the pattern."""
    STRING_LENGTH_BETWEEN = "string_length_between"
    """``params: {min?, max?}`` — every non-NULL value's length inside the bounds."""
    FRESHNESS = "freshness"
    """``params: {max_age_minutes}`` — the bound timestamp column's MAX must be
    younger than the threshold, evaluated warehouse-side (``now()`` is the
    warehouse clock). An empty relation FAILS — no data is stale data."""
    SCHEMA = "schema"
    """``params: {columns: {name: {data_type?, nullable?} | "<type>"}, strict?}``
    — column presence / dtype / nullability asserted against the LIVE
    introspected relation; type comparison runs through the dialect lattice so
    a synonym respelling never fails."""
    UNIT = "unit"
    """``params: {given: [{input, rows}], expect: {rows}}`` — the model's
    compiled SELECT executed over inline fixture rows (every referenced input
    rewritten to a fixture CTE, dbt-style), failing rows = the symmetric
    difference against the expected rows. Reads no live data by construction:
    an input the model references without a ``given`` entry refuses to
    compile."""


class TestSeverity(StrEnum):
    """How a failing test affects the gate verdict. ``WARN`` runs + annotates but
    never blocks; ``BLOCKING`` fails the check."""

    __test__ = False

    BLOCKING = "blocking"
    WARN = "warn"


class GateMode(StrEnum):
    """Repo-level enforcement mode: ``BLOCKING`` gates, ``INFORMATIONAL`` computes
    everything and reports neutrally (the Codecov shadow-deployment pattern)."""

    BLOCKING = "blocking"
    INFORMATIONAL = "informational"


class SandboxData(StrEnum):
    """How the sandbox tier materializes the per-PR sandbox: ``ZERO_ROW`` proves DDL and
    column shape at no scan cost; ``CLONE`` builds the proposed SELECTs with
    real rows so the changed relations' tests execute against the proposed
    data, not prod's."""

    ZERO_ROW = "zero_row"
    CLONE = "clone"


class TestDefinition(VersionedModel):
    """A reusable, typed check. ``sql`` is only meaningful for ``custom_sql``
    (a SELECT returning failing rows); the declarative kinds compile from the
    bound target + params at run time.

    1.1.0 widened the ``kind`` vocabulary (aggregate_between, numeric_range,
    regex_match, string_length_between, freshness, schema) and recognized the
    ``mostly:`` tolerance param on row-grain kinds -- no field changes.

    1.3.0 widened the vocabulary again (``unique_combination``); the version
    jumps past the 1.2.0 fixture corpus, which is keyed by the max across
    testing models -- still no field changes.

    1.5.0 widened the vocabulary again (``unit``); the version jumps past the
    1.4.0 fixture corpus, which is keyed by the max across testing models --
    still no field changes."""

    __test__ = False

    SCHEMA_VERSION: ClassVar[str] = "1.5.0"

    kind: str = TestKind.CUSTOM_SQL.value
    sql: str = ""
    """The failing-rows SELECT for ``custom_sql`` tests. May reference the bound
    relations via ``{<relation label>}`` placeholders (resolved from ``refs``)."""
    description: str = ""


class TestSpec(VersionedModel):
    """A definition bound to concrete targets -- the unit the catalog stores, the
    sync compiles into binding rows, and the gate selects.

    ``id`` is the deterministic test URN
    (``test://<relation-path>/<column?>/<kind>/<params-hash>``) so re-sync is
    idempotent and orphan detection exact. ``refs`` lists EVERY input URN (table
    or column grain); selection is eager across all of them.

    1.1.0 added the optional ``warn_if`` / ``error_if`` failing-row thresholds
    (additive, defaulted to ""). They refine an executed result's grade: a count
    matching ``error_if`` is a blocking-grade fail, ``warn_if`` a warn-grade fail,
    below both a pass. Absent both (the default) is exactly the prior behavior --
    any failing row fails at ``severity``. They are identity: the same failing
    count grades differently under different thresholds, so a threshold joins the
    test URN (and the receipt carry key) whenever it is set -- while a
    threshold-less test keeps the URN it minted before thresholds existed."""

    __test__ = False

    SCHEMA_VERSION: ClassVar[str] = "1.1.0"

    id: str = ""
    name: str = ""
    """Human-readable node ID, e.g. ``orders.tests.yml::orders.amount::not_null``."""
    definition: TestDefinition = Field(default_factory=TestDefinition)
    params: dict[str, Any] = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list)
    """ALL bound target URNs. The first ref is the primary target (it names the
    test URN); every ref becomes a binding row."""
    severity: str = TestSeverity.BLOCKING.value
    warn_if: str = ""
    """Optional ``<op> <int>`` threshold on the failing-row count (e.g. ``> 10``):
    when it matches, the result is a warn-grade fail. "" = no threshold."""
    error_if: str = ""
    """Optional ``<op> <int>`` threshold on the failing-row count (e.g. ``> 100``):
    when it matches, the result is a blocking-grade fail; it wins over
    ``warn_if``. "" = no threshold."""
    enabled: bool = True
    source: str = ""
    """Repo-relative path of the authored file this spec was compiled from."""
    surface: str = ""
    """Which authoring surface produced it: ``yaml`` | ``sql`` | ``dbt`` | ``pytest``."""


class TestBinding(VersionedModel):
    """One compiled ``(test, target)`` binding row -- the derived index shape the
    sync writes into ``lineage_test_binding`` and the snapshot embeds. Derived,
    idempotently rebuilt from authored files; never hand-edited."""

    __test__ = False

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    test_id: str = ""
    target_urn: str = ""
    table_urn: str = ""
    """The relation the target belongs to (== ``target_urn`` for a table-grain ref)."""
    severity: str = TestSeverity.BLOCKING.value
    enabled: bool = True
    spec_hash: str = ""
    """Content hash of the compiled spec — changes when the authored test changes."""


class GatePolicy(VersionedModel):
    """The repo-level policy from ``alkera/gate.yml`` (resolved from the BASE ref
    so a PR can't disarm the gate in its own diff).

    Every layer reports skipped -- never suppresses: disabling or demoting a rule
    reclassifies findings with appended provenance, it does not delete them.
    ``force`` rules are un-waivable and cannot be demoted below blocking except
    by a PR-reviewed change to this file itself.

    1.2.0 added ``carry_forward_max_age_hours`` (additive, defaulted; the
    version jumps past the 1.1.0 fixture corpus, which is keyed by the max
    across testing models).

    1.4.0 added ``sandbox_data`` (additive, defaulted; the version jumps past
    the 1.3.0 fixture corpus, which is keyed by the max across testing
    models)."""

    SCHEMA_VERSION: ClassVar[str] = "1.4.0"

    enabled: bool = True
    mode: str = GateMode.BLOCKING.value
    severity: dict[str, str] = Field(default_factory=dict)
    """Per-rule severity remap: ``{rule_id: blocking|warn}``. Unlisted rules keep
    their built-in defaults."""
    ignore_only: dict[str, list[str]] = Field(default_factory=dict)
    """Per-rule URN globs whose findings are reclassified to informational (with
    provenance) — never removed from the report."""
    force: list[str] = Field(default_factory=lambda: ["TABLE_DROPPED", "COLUMN_DROPPED"])
    """Rule IDs that can never be demoted or ignored — the un-waivable floor."""
    carry_forward_max_age_hours: int = 0
    """How long a verified prior execution result may satisfy an unchanged test
    (0 = carry-forward off). Honoring always re-verifies the receipt against
    the warehouse's own query history + the relation's last-altered epoch;
    this is only the freshness ceiling."""
    sandbox_data: str = SandboxData.ZERO_ROW.value
    """How the sandbox tier materializes its data: ``zero_row`` (the free default;
    DDL + shape proof only, tests keep prod names) or ``clone`` (real rows via
    the proposed SELECTs; the changed relations' tests run against the sandbox
    names). Unknown values refuse at policy load. Like every policy knob this
    resolves from the BASE ref, so a PR cannot flip its own gate into the paid
    clone mode in the same diff."""


__all__ = [
    "GateMode",
    "GatePolicy",
    "SandboxData",
    "TestBinding",
    "TestDefinition",
    "TestKind",
    "TestSeverity",
    "TestSpec",
]
