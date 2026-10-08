"""One source of truth for "is this reference the engine's own metadata, not user data?"

The table grain and the column grain must agree about which relations exist. If the table
grain refused to graph ``information_schema.columns`` while the column grain minted edges to
it, the graph would hold column edges whose source relation has no node.

Everything here is keyed by DIALECT, never branched on inside a parser. A construct that is
system metadata on one engine is user data on another (``system`` is Databricks' metadata
catalog and an ordinary schema name elsewhere), so the knowledge belongs in a table, not in
an ``if`` inside the extractor.

``SqlEngineSpec`` carries its own copies for the table grain. ``test_sql_base`` asserts the
two never disagree about a reference, so a spec edit that diverges from these tables fails.
"""

from __future__ import annotations

__all__ = [
    "LISTING_HIDDEN_NAMESPACES",
    "SYSTEM_CATALOGS",
    "SYSTEM_RELATION_NAMES",
    "SYSTEM_RELATION_PREFIXES",
    "SYSTEM_SCHEMAS",
    "hidden_from_listing",
    "is_system_reference",
]

#: The fallback for a dialect absent from the tables below — the generic_sql connector
#: derives its dialect from the backend name, so ``spark``/``athena``/``presto``/``oracle``
#: and anything sqlglot merely recognizes reach here. It MUST mirror
#: ``SqlEngineSpec.system_schemas``'s default, or the table grain filters a reference the
#: column grain graphs and the two disagree again — the exact split this module closes.
_DEFAULT_SYSTEM_SCHEMAS: frozenset[str] = frozenset({"information_schema", "pg_catalog"})
_DEFAULT_SYSTEM_CATALOGS: frozenset[str] = frozenset()

#: Schemas holding engine metadata. ``information_schema`` is ANSI and present everywhere.
SYSTEM_SCHEMAS: dict[str, frozenset[str]] = {
    "redshift": frozenset({"information_schema", "pg_catalog", "pg_internal", "pg_toast"}),
    "postgres": frozenset({"information_schema", "pg_catalog", "pg_toast"}),
    "snowflake": frozenset({"information_schema"}),
    "databricks": frozenset({"information_schema"}),
    "bigquery": frozenset({"information_schema"}),
    "trino": frozenset({"information_schema"}),
    "duckdb": frozenset({"information_schema", "pg_catalog"}),
    "sqlite": frozenset({"information_schema"}),
    "mysql": frozenset(),
    "clickhouse": frozenset(),
}

#: Databases/catalogs that are entirely engine metadata.
SYSTEM_CATALOGS: dict[str, frozenset[str]] = {
    "redshift": frozenset(),
    "postgres": frozenset(),
    #: SNOWFLAKE.ACCOUNT_USAGE.* is the most-referenced metadata source on real accounts.
    "snowflake": frozenset({"snowflake"}),
    #: Unity Catalog's system.access.* / system.billing.*.
    "databricks": frozenset({"system"}),
    "bigquery": frozenset(),
    "trino": frozenset({"system", "jmx"}),
    "duckdb": frozenset({"system", "temp"}),
    "sqlite": frozenset(),
    "mysql": frozenset({"information_schema", "mysql", "performance_schema", "sys"}),
    "clickhouse": frozenset({"system", "information_schema"}),
}

#: Relation-NAME prefixes. These are the only handle on a system relation referenced
#: UNQUALIFIED — which is how catalog SQL is essentially always written (``FROM pg_class c``,
#: not ``FROM pg_catalog.pg_class c``). Without them an unqualified ref resolves into
#: whatever schema the reading relation lives in and mints a relation that does not exist.
SYSTEM_RELATION_PREFIXES: dict[str, frozenset[str]] = {
    #: Redshift surfaces metadata as pg_* tables AND the stl_/stv_/svl_/svv_/svcs_ view
    #: families, which live in pg_catalog but are always written bare.
    #:
    #: ``sys_`` is DELIBERATELY EXCLUDED. Redshift does expose SYS_QUERY_HISTORY et al., but
    #: ``sys_`` is a common prefix for user tables (``sys_config``, ``sys_users``), and this
    #: rule fires on an unqualified name with no namespace to disambiguate. Dropping a real
    #: relation loses lineage; keeping a system one costs a phantom node. The asymmetry
    #: favours keeping, so the collision-prone prefix stays out.
    "redshift": frozenset({"pg_", "stl_", "stv_", "svl_", "svv_", "svcs_"}),
    "postgres": frozenset({"pg_"}),
    "duckdb": frozenset({"duckdb_", "pragma_", "sqlite_"}),
    "sqlite": frozenset({"sqlite_"}),
    "snowflake": frozenset(),
    "databricks": frozenset(),
    "bigquery": frozenset(),
    "trino": frozenset(),
    "mysql": frozenset(),
    "clickhouse": frozenset(),
}

#: System relations with no distinguishing prefix.
SYSTEM_RELATION_NAMES: dict[str, frozenset[str]] = {
    #: BigQuery's per-dataset meta-relations.
    "bigquery": frozenset({"__tables__", "__tables_summary__", "__partitions_summary__"}),
}


def is_system_reference(dialect: str, catalog: str, schema: str, name: str) -> bool:
    """Whether a parsed relation reference names engine metadata rather than user data.

    The name-level rule applies ONLY to an UNQUALIFIED reference. That restriction is the
    whole point: the rule exists because an unqualified ref carries no namespace for the
    schema/catalog test to look at. A ref that IS qualified has already told us its
    namespace, so the name must not be second-guessed — a customer is entitled to a table
    called ``analytics.sys_audit`` or ``reporting.pg_backup``, and dropping it would delete
    real lineage, which is strictly worse than the phantom the rule prevents.
    """
    d = (dialect or "").strip().lower()
    cat = (catalog or "").strip().strip('"').lower()
    sch = (schema or "").strip().strip('"').lower()
    rel = (name or "").strip().strip('"').lower()

    # A system namespace can also ride INSIDE the relation name. BigQuery's
    # INFORMATION_SCHEMA is a pseudo-dataset, so `ds.INFORMATION_SCHEMA.TABLES` parses as
    # db='ds', name='INFORMATION_SCHEMA.TABLES' — the namespace test never sees it and the
    # bare-name test does not match either. Check any dotted prefix of the name against the
    # schema set so the pseudo-dataset spelling is caught wherever an engine uses it.
    if "." in rel:
        head = rel.rsplit(".", 1)[0]
        if head in (SYSTEM_SCHEMAS.get(d, frozenset()) | _DEFAULT_SYSTEM_SCHEMAS):
            return True

    if cat or sch:
        # Qualified: the namespace is stated, so trust it and NOTHING else.
        #
        # Each slot is tested against its OWN table only. Cross-testing (schema against the
        # catalog set) would drop real user data: duckdb ``main.temp.x``, snowflake
        # ``db.snowflake.x`` and trino ``cat.system.x`` all name a legitimate user schema
        # that merely shares a word with another slot's system namespace. Losing a real
        # relation is strictly worse than keeping a phantom, and ``keep_relation`` — the
        # table grain's equivalent — does not cross-test either, so cross-testing here also
        # re-split the two grains this module exists to keep aligned.
        # UNION with the default floor rather than replacing it: a per-dialect entry lists
        # what that engine ADDS, so a dialect table can never accidentally drop below the
        # ANSI baseline the specs already enforce.
        sys_cat = SYSTEM_CATALOGS.get(d, frozenset()) | _DEFAULT_SYSTEM_CATALOGS
        sys_sch = SYSTEM_SCHEMAS.get(d, frozenset()) | _DEFAULT_SYSTEM_SCHEMAS
        return bool((cat and cat in sys_cat) or (sch and sch in sys_sch))
    if not rel:
        return False
    # Unqualified: the name is all we have. Note an unknown dialect gets NO prefix rule —
    # prefixes are engine-specific and guessing one would delete user tables.
    return rel in SYSTEM_RELATION_NAMES.get(d, frozenset()) or any(
        rel.startswith(p) for p in SYSTEM_RELATION_PREFIXES.get(d, frozenset())
    )


#: Namespaces a schema LISTING hides from the agent: the engine's own metadata,
#: plus the schema a managed provider installs its extensions into. This is not
#: a lineage rule and deliberately does NOT reuse the dialect tables above — a
#: listing has no dialect in hand, and its cost is different in kind: the model
#: pays for every row in context. A PlanetScale Postgres whose entire listing
#: was ``pscale_extensions.hypopg_hidden_indexes`` / ``…_list_indexes`` answered
#: a data question with pure noise and no user table at all.
#:
#: Only names that are engine or provider metadata in EVERY engine that has
#: them are listed. Ambiguous ones (``sys``, ``system``, ``temp``) stay out for
#: the same reason the prefix table leaves ``sys_`` out: hiding a customer's
#: real table is worse than listing one of ours. Nothing here is unreachable —
#: ``verbose`` lists them, and ``describe`` still resolves them by name.
LISTING_HIDDEN_NAMESPACES: frozenset[str] = frozenset(
    {
        "information_schema",
        "pg_catalog",
        "pg_internal",
        "pg_toast",
        "performance_schema",
        #: PlanetScale Postgres installs hypopg et al. here.
        "pscale_extensions",
    }
)


def hidden_from_listing(display_name: str) -> bool:
    """Whether a relation's display name sits in a namespace a listing hides.

    ``display_name`` is the introspection's dotted spelling — ``table``,
    ``schema.table`` or ``catalog.schema.table`` — so every segment BUT the
    last is a namespace and is tested. A relation with no namespace is never
    hidden: there is nothing to judge it by.
    """
    parts = [p.strip().strip('"').lower() for p in display_name.split(".")]
    return any(part in LISTING_HIDDEN_NAMESPACES for part in parts[:-1])
