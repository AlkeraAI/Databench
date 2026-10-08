"""Canonical cross-plugin asset URNs.

Relations use ``{system}://{authority}/{catalog.schema.relation}[#column]``.
Authorities isolate physical systems; relation and column parts fold according to
their owning system. Foreign warehouse URIs also normalize into the legacy
``warehouse://`` namespace where account-independent logical identity is required.

The dotted convention deliberately has no component escape. A literal dot inside a
quoted SQL identifier, Mongo collection or field path, or Kafka topic loses its
original arity and can over-fold with another spelling. Case folding can likewise
merge case-distinct names on systems configured as insensitive. Catalog producers
must diagnose collisions within a snapshot; introducing another codec would fork the
join key used by existing producers.
"""

from __future__ import annotations

import re
from collections.abc import Set as AbstractSet

from alkera_cli.plugins.plugin_base.identifier_case import fold_column_identifier

WAREHOUSE_SCHEME = "warehouse://"

#: Separates the column name from its table URN in a column-grain URN. Stripped from
#: every identifier part (see :data:`_STRIP`) so it can never appear inside a
#: relation/column name, making a column URN split back to (table_urn, column)
#: unambiguously (the table part uses ``.``).
COLUMN_SEP = "#"

#: Characters stripped from each identifier part: quote/bracket/backslash characters, and
#: the column separator ``#`` (so no identifier can collide with the column-grain structure,
#: AND a name interpolated into a quoted SQL literal — e.g. introspect ``describe()`` —
#: can't break out via a trailing backslash escaping the closing quote).
_STRIP = re.compile(r"""["'`\[\]#\\]""")

#: URI schemes that denote a warehouse relation — normalized into the shared
#: ``warehouse://`` namespace so a reference from ANY tool (a warehouse connector, a
#: dbt relation, an Airflow/Dagster dataset URI, a Glue table) folds onto one node.
#: Broad + future-proof: add a new warehouse/connector scheme here. Public so the
#: knowledge matcher (and the fixed-point test that covers every scheme) derive
#: their foldable set from THIS registry rather than a hand-copied list.
#: HAND-MIRRORED by the web adapter's DOTTED_WAREHOUSE set
#: (the editor's lineage graph adapter); update both, and add the
#: scheme's row to the locationOf sweep in adaptGraph.test.ts.
WAREHOUSE_SCHEMES = frozenset(
    {
        "warehouse",
        "snowflake",
        "bigquery",
        "redshift",
        "databricks",
        "trino",
        "presto",
        "athena",
        "glue",
        "spark",
        "hive",
        "postgres",
        "postgresql",
        # 2-level server warehouses (database.table). Their authority is dropped on a foreign URI
        # (a server, not a catalog) and re-resolved by connection-linking, like every server scheme
        # — so an Airflow asset / Tableau custom-SQL URI in one of these folds onto the connector
        # node. (DuckDB/SQLite are deliberately NOT here: file-based, their authority IS the file
        # stem — a foreign URI can't supply it, and they're never referenced as a foreign asset.)
        "mysql",
        "mariadb",
        "clickhouse",
        "sqlserver",
        "mssql",
        "tsql",
        # An OracleOperator mints oracle:// relation nodes, so a foreign oracle:// asset URI
        # must normalize + relink onto them. Otherwise the two split. (warehouse_authority
        # returns '' for oracle, so both sides are authority-less and fold.)
        "oracle",
    }
)

#: The RARE schemes whose URI authority is a CATALOG (part of the fully-qualified
#: relation name), not a server/account — so the authority is KEPT as the leading
#: relation part instead of dropped. BigQuery's project is the canonical case
#: (``bigquery://project/dataset.table`` ≡ ``warehouse://project.dataset.table``).
#: Every other scheme's authority is a server/account and is dropped (the default).
_CATALOG_AUTHORITY_SCHEMES = frozenset({"bigquery"})

#: Engine aliases → the ONE canonical system every producer mints under, so a dbt model on the
#: ``mariadb`` adapter, an Airflow ``MsSqlOperator`` (system ``mssql``), a ``presto`` source, and
#: the dedicated/generic connector for the same engine all fold onto ONE node. (``generic_sql``'s
#: backend map + the permission classifier already collapse these; this is the URN-mint side.)
_SYSTEM_ALIASES = {
    "postgresql": "postgres",
    "mariadb": "mysql",
    "mssql": "sqlserver",
    "tsql": "sqlserver",
    "presto": "trino",
}


def is_warehouse_scheme(scheme: str) -> bool:
    """Whether ``scheme`` names a warehouse relation rather than an opaque object.

    The line between a URN whose identifiers are a SPELLING (``snowflake://acct/DB.S.T``,
    case-folded per system) and one whose path is the object's IDENTITY (``s3://bucket/Key``,
    where case is significant). A caller that folds or re-mints a URN must check this
    first, or it merges two distinct objects. Alias-tolerant, so ``postgresql`` and
    ``mariadb`` answer the same as their canonical names."""
    s = scheme.strip().lower()
    return s in WAREHOUSE_SCHEMES or canonical_system(s) in WAREHOUSE_SCHEMES


def canonical_system(system: str) -> str:
    """The canonical system name for ``system`` (lowercased, alias-collapsed). The single key every
    per-system rule + URN scheme routes through, so wire-compatible / renamed engines
    (mariadb↔mysql, mssql/tsql↔sqlserver, presto↔trino, postgresql↔postgres) mint an IDENTICAL URN
    regardless of which producer named the engine."""
    s = system.strip().lower()
    return _SYSTEM_ALIASES.get(s, s)


def clean_part(part: str) -> str:
    """De-quote/de-bracket + lowercase one identifier part (a db/schema/table/column
    name) — the single normalizer every producer routes identifiers through so two
    tools mint the IDENTICAL URN for the same element."""
    return _STRIP.sub("", part).strip().lower()


def normalize_relation(fqn: str) -> str:
    """The dotted ``[catalog.]schema.table`` identity from a fully-qualified name or a
    scheme-prefixed connector URI — lowercased, de-quoted/bracketed. ``""`` if there
    is no usable relation.

    The URI authority is dropped (a server/account) unless its scheme is a
    catalog-authority one (BigQuery), in which case it is kept — so the URI form and
    the from-parts form fold to ONE URN. See the module docstring.

    Examples::

        '"ANALYTICS"."MARTS"."ORDERS"'        -> 'analytics.marts.orders'
        '[ANALYTICS].[MARTS].[ORDERS]'        -> 'analytics.marts.orders'
        'snowflake://acct/DB.PUBLIC.ORDERS'   -> 'db.public.orders'   (account dropped)
        'bigquery://my-proj/analytics.orders' -> 'my-proj.analytics.orders'  (project kept)
        'warehouse://db.schema.tbl'           -> 'db.schema.tbl'
    """
    s = fqn.strip()
    if "://" in s:
        scheme, _, rest = s.partition("://")
        if "/" in rest:
            authority, tail = rest.split("/", 1)
            # Keep the authority only when it is a catalog (BigQuery project); drop a
            # server/account authority (the default) so the relation is host-agnostic.
            s = f"{authority}/{tail}" if scheme.lower() in _CATALOG_AUTHORITY_SCHEMES else tail
        else:
            s = rest
    s = s.replace("/", ".")
    parts = [p for p in (clean_part(x) for x in s.split(".")) if p]
    return ".".join(parts)


def warehouse_urn(fqn: str) -> str:
    """``warehouse://<[catalog.]schema.table>`` for a relation FQN/URI, or ``""``
    when no relation can be derived."""
    rel = normalize_relation(fqn)
    return f"{WAREHOUSE_SCHEME}{rel}" if rel else ""


def warehouse_urn_from_parts(*parts: str) -> str:
    """``warehouse://`` URN from already-split ``([catalog,] schema, table)`` parts
    (each de-quoted/lowercased). ``""`` if all parts are empty. This and
    :func:`warehouse_urn` mint the IDENTICAL URN for the same relation."""
    cleaned = [p for p in (clean_part(x) for x in parts) if p]
    return f"{WAREHOUSE_SCHEME}{'.'.join(cleaned)}" if cleaned else ""


def warehouse_urn_or_passthrough(uri: str) -> str:
    """Normalize a warehouse-relation URI to the canonical ``warehouse://`` form, but
    pass a NON-warehouse URI (``s3://``, a file path, an opaque dataset id) through
    unchanged — used by orchestrators (Airflow / Dagster) whose dataset URIs are
    user-defined and may legitimately not be warehouse tables."""
    s = uri.strip()
    if not s:
        return s
    if s.startswith(WAREHOUSE_SCHEME):
        return warehouse_urn(s) or s
    if "://" in s:
        scheme = s.split("://", 1)[0].lower()
        # A recognized warehouse scheme → normalize so it joins the graph; anything
        # else (s3, gs, file, http, …) keeps its declared identity.
        return warehouse_urn(s) or s if scheme in WAREHOUSE_SCHEMES else s
    # No scheme: a bare dotted relation (catalog.schema.table) → normalize.
    return warehouse_urn(s) or s


def foreign_dataset_urn(uri: str) -> str:
    """A FOREIGN reference (an Airflow dataset URI, a Tableau custom-SQL source) → an
    account-AGNOSTIC per-system dataset URN when the system is recognizable, else
    passthrough. A BI tool / orchestrator references a relation by its logical name and
    does NOT own the warehouse account, so it mints without an authority (the URI's
    authority position is ambiguous — account vs. database — so it's dropped, matching
    ``warehouse_urn_or_passthrough``); it folds with the owning warehouse connector once
    that connection carries a matching authority (connection-linking). Choosing the
    per-system scheme over ``warehouse://`` keeps a snowflake table from colliding with a
    postgres table of the same ``db.schema.table``."""
    s = uri.strip()
    if "://" in s:
        scheme, _, rest = s.partition("://")
        scheme = scheme.lower()
        if scheme in WAREHOUSE_SCHEMES and scheme != "warehouse":
            # Keep a catalog authority (BigQuery project), drop a server one — same rule as
            # normalize_relation — but split the RAW, case-PRESERVED parts so dataset_urn applies
            # the per-system case-fold itself (BigQuery preserves case; pre-lowercasing here would
            # split a mixed-case BQ relation off its connector/dbt/Tableau node).
            if "/" in rest:
                authority, tail = rest.split("/", 1)
                rel = f"{authority}/{tail}" if scheme in _CATALOG_AUTHORITY_SCHEMES else tail
            else:
                rel = rest
            parts = split_relation_name(rel)
            return dataset_urn(scheme, "", *parts) or warehouse_urn_or_passthrough(s)
    return warehouse_urn_or_passthrough(s)


def column_identity(node_urn: str, column: str, *, quoted: bool | None = None) -> str:
    """Fold ``column`` under the identifier rule of the system owning ``node_urn``."""
    system = node_urn.split("://", 1)[0] if "://" in node_urn else ""
    return fold_column_identifier(system, column, quoted)


def column_readings(relation_ref: str, column: str) -> tuple[str, ...]:
    """Return the distinct inferred and exact identities for an authored column name."""
    inferred = column_identity(relation_ref, column)
    exact = column_identity(relation_ref, column, quoted=True)
    return (inferred,) if inferred == exact else (inferred, exact)


def attested_column(node_urn: str, column: str, known: AbstractSet[str]) -> str:
    """Return the one identity ``column`` names in ``known``, or refuse ambiguity."""
    attested = [reading for reading in column_readings(node_urn, column) if reading in known]
    return attested[0] if len(attested) == 1 else ""


def column_urn(table_urn: str, column: str, quoted: bool | None = None) -> str:
    """``<table_urn>#<column>`` — the column-grain URN for ``column`` on the relation
    identified by ``table_urn`` (the table URN is taken as-is). ``column`` folds by the
    OWNING SYSTEM's identifier case rule — the table URN's scheme picks the sqlglot
    dialect (:func:`fold_column_identifier`), so two producers mint the IDENTICAL
    column URN the way the warehouse itself resolves the name. ``quoted=True`` marks a
    quoted SQL identifier or a system-canonical stored name (identifies exactly);
    ``False`` an unquoted SQL reference (Snowflake UPPER, most others lower); ``None``
    infers ``True`` from quote characters embedded in ``column``. A system with no SQL
    dialect (a BI cell, Fivetran, the legacy ``warehouse://`` namespace) folds
    lowercase regardless. ``""`` if either part is empty so a missing column never
    produces a dangling ``...#`` node. The per-system column path (:func:`dataset_urn`
    with ``column=``) applies the SAME fold, so a column folds across producers no
    matter which path minted it."""
    table = table_urn.strip()
    if not table:
        return ""
    col = column_identity(table, column, quoted=quoted)
    if not col:
        return ""
    return f"{table}{COLUMN_SEP}{col}"


def parse_column_urn(urn: str) -> tuple[str, str]:
    """Split a column URN back into ``(table_urn, column)``. A URN without a
    ``#`` separator (a plain table/node URN) returns ``(urn, "")`` — so callers can
    pass any URN and learn whether it is column-grained by the empty column."""
    table, sep, col = urn.partition(COLUMN_SEP)
    return (urn, "") if not sep else (table, col)


# ---------------------------------------------------------------------------
# Per-system dataset identity (OpenLineage-aligned)
#
# A dataset URN is OpenLineage's ``(namespace, name)`` flattened to one opaque string:
#
#     {system}://{authority}/{container.schema.relation}[#column]
#
# ``{system}://{authority}`` is the OL *namespace* — the dev≠prod isolation boundary
# (a Snowflake account, a host:port, a DuckDB file) that must NEVER be dropped, else two
# physically distinct warehouses sharing ``db.schema.table`` collide onto one node. The
# ``container.schema.relation`` is the OL *name* — the logical 3-part identity every
# warehouse and dbt agree on. The per-system authority + the case-fold below are ported
# from OpenLineage's reference dbt integration so OUR connectors mint the IDENTICAL URN
# for one physical table regardless of which tool observed it — and so future OpenLineage
# ingestion lines up with no translation. Two deliberate, documented deviations from OL:
# DuckDB authority is the file STEM (portable + derivable from a dbt manifest, unlike
# OL's absolute path), and identifiers are case-folded PER SYSTEM (OL trusts the source's
# stored case, which mis-folds Snowflake when one producer upper-cases and another lower).
# ---------------------------------------------------------------------------

#: Systems whose unquoted identifiers fold to UPPER case (Snowflake's storage default).
_FOLD_UPPER = frozenset({"snowflake"})
#: Systems whose observed physical identities remain opaque and case-sensitive.
OPAQUE_AUTHORITY_SYSTEMS = frozenset({"elasticsearch", "kafka", "mongodb"})
#: Systems whose identifiers are CASE-SIGNIFICANT and must be preserved (BigQuery
#: datasets/tables). Every other system folds to lower (the default).
_FOLD_PRESERVE = frozenset({"bigquery", "druid", *OPAQUE_AUTHORITY_SYSTEMS})


def fold_identifier(system: str, part: str) -> str:
    """De-quote/de-bracket one identifier part and apply ``system``'s unquoted-identifier
    case rule: Snowflake → UPPER, BigQuery → preserve, everything else → lower. This is
    what makes two producers for the same relation mint byte-identical parts."""
    p = _STRIP.sub("", part).strip()
    s = system.strip().lower()
    if s in _FOLD_UPPER:
        return p.upper()
    if s in _FOLD_PRESERVE:
        return p
    return p.lower()


def fix_account_name(account: str) -> str:
    """Canonicalize a Snowflake account identifier to ONE stable form (ported from
    OpenLineage). First strip a host/PrivateLink suffix — a value pasted as the full host
    (``xy123.us-east-1.aws.snowflakecomputing.com``) or a PrivateLink form
    (``xy123.us-east-1.privatelink``) must canonicalize to the SAME namespace as the bare
    account, else the connector (full host) and dbt (account form) split into two nodes. Then:
    an org-account form (contains ``-``) is returned as-is; a legacy account locator gets its
    default region/cloud filled in (``xy123`` → ``xy123.us-west-1.aws``; ``xy123.us-east-1`` →
    ``xy123.us-east-1.aws``) so the locator and org-account forms can't mint two namespaces."""
    acct = account.strip()
    for suffix in (".snowflakecomputing.com", ".privatelink"):
        if acct.lower().endswith(suffix):
            acct = acct[: -len(suffix)]
    parts = acct.split(".")
    a = parts[0]
    if "-" in a:
        return a
    if len(parts) == 1:
        return f"{a}.us-west-1.aws"
    if len(parts) == 2:
        return f"{a}.{parts[1]}.aws"
    return acct


def dataset_urn(
    system: str, authority: str, *relation_parts: str, column: str = "", quoted: bool | None = None
) -> str:
    """Mint a dataset (or column) URN: ``{system}://{authority}/{relation}[#column]``.

    ``relation_parts`` are the logical ``[container,] schema, table`` (empty parts
    dropped), case-folded per ``system``. Locator authorities are normalized and
    case-insensitive; observed Elasticsearch, Kafka, and MongoDB cluster identities
    remain opaque and case-sensitive. An empty authority yields
    ``{system}://{relation}``. Returns ``""`` if no relation part survives.

    An EMPTY ``system`` returns ``""`` (never a garbage ``://relation`` URN): a relation
    whose warehouse system couldn't be resolved (e.g. an Airflow ``SQLExecuteQueryOperator``
    whose ``conn_id`` names no known engine) must NOT mint a scheme-less node that folds with
    nothing — the caller emits a foreign/under-qualified URN or skips instead."""
    sys = canonical_system(system)
    if not sys:
        return ""
    parts = [p for p in (fold_identifier(sys, x) for x in relation_parts) if p]
    if not parts:
        return ""
    relation = ".".join(parts)
    # Observed ES/Kafka/Mongo identities are opaque and case-sensitive. Their locator
    # fallbacks are already normalized by ``warehouse_authority``.
    auth = authority.strip() if sys in OPAQUE_AUTHORITY_SYSTEMS else authority.strip().lower()
    base = f"{sys}://{auth}/{relation}" if auth else f"{sys}://{relation}"
    # The column folds by the system's COLUMN identifier rule. It is sqlglot-backed and
    # distinct from the relation-part fold above, because DuckDB/BigQuery COLUMNS match
    # case-insensitively while their relations don't. The same fold :func:`column_urn`
    # applies, so a column folds across producers no matter which path minted it.
    col = fold_column_identifier(sys, column, quoted)
    return f"{base}{COLUMN_SEP}{col}" if col else base


def parse_dataset_urn(urn: str) -> tuple[str, str, list[str], str]:
    """Decompose any dataset URN into ``(system, authority, relation_parts, column)`` —
    the single place that splits a URN, so the scheme can change without touching callers.

    Handles the per-system form (``snowflake://acct/db.schema.t#c``), the legacy
    ``warehouse://db.schema.t#c`` form (system ``"warehouse"``, authority ``""``), the
    authority-less form (``trino://cat.schema.t``), and a bare/opaque id (no ``://`` →
    system ``""``). Inverse of :func:`dataset_urn` on a round-trip."""
    base, sep, col = urn.partition(COLUMN_SEP)
    column = col if sep else ""
    scheme, scheme_sep, rest = base.partition("://")
    if not scheme_sep:
        return ("", "", [p for p in base.split(".") if p], column)
    authority, slash, relation = rest.partition("/")
    if not slash:
        relation, authority = authority, ""
    parts = [p for p in relation.split(".") if p]
    return (scheme, authority, parts, column)


def urn_home_system(urn: str) -> str:
    """The canonical system a URN's scheme names: where the object LIVES, never who
    reported it. ``""`` for a scheme-less URN and for the legacy system-agnostic
    ``warehouse://`` namespace, both of which genuinely name no home. Alias-collapsed
    and case-insensitive via :func:`canonical_system`, so a passthrough URI with an
    uppercase scheme still folds. The vocabulary is deliberately open: an s3/glue/hex
    scheme is a real home, and an unrecognized scheme passes through for the consumer
    to clamp."""
    scheme, sep, _ = urn.partition("://")
    if not sep:
        return ""
    system = canonical_system(scheme)
    return "" if system == "warehouse" else system


def urn_label(urn: str) -> str:
    """The short human label for ANY URN — the relation/object name. Scheme- and
    grain-agnostic: drop a ``#column``, then take the last path then last dotted segment,
    so it works for per-system, legacy ``warehouse://``, and passthrough (``s3://`` /
    ``file://``) URNs alike. The one shared implementation behind every display label."""
    base = urn.split(COLUMN_SEP, 1)[0]
    return base.rsplit("/", 1)[-1].rsplit(".", 1)[-1] or base


#: Systems whose top container (catalog/database) IS the dev≠prod isolation boundary —
#: there is no separate server/account, so the container becomes the URN authority (a
#: DuckDB/SQLite *file*). Every other system keeps the container in the relation and
#: resolves its authority from observed identity or connection locator (see
#: :func:`warehouse_authority`).
_CONTAINER_IS_AUTHORITY = frozenset({"duckdb", "sqlite"})
#: Systems with NO schema below the database (``database.table``): the database is the
#: container, there is no sub-schema. A dbt model for one of these mints a 2-part relation,
#: which :func:`DbtModel.warehouse_urn` must collapse to ``container.table`` to fold with
#: the connector's 2-level URN (not ``database.schema.table``).
TWO_LEVEL_SYSTEMS = frozenset({"mysql", "mariadb", "clickhouse", "tinybird"})


def kafka_topic_urn(
    authority: str, topic: str, *, column: str = "", quoted: bool | None = True
) -> str:
    """Mint the shared Kafka topic or topic-field URN used by stream producers."""
    return dataset_urn("kafka", authority, topic, column=column, quoted=quoted)


def split_relation_name(fqn: str) -> list[str]:
    """Split a fully-qualified relation name into raw, CASE-PRESERVED parts (quotes /
    brackets / backticks stripped). The case-fold is applied LATER, per system, by
    :func:`dataset_urn` — so a Snowflake ``'"ANALYTICS"."MARTS"."ORDERS"'`` keeps its case
    here and folds UPPER at mint while a BigQuery name is preserved. Empty parts dropped.

    QUOTE-AWARE: a ``.`` inside a quoted/bracketed identifier (``"fiscal.year"``,
    ``[a.b]``) is part of the NAME, not a separator — splitting the raw string on ``.``
    would over-split it (dropping the container + mangling the table). So we only break on a
    ``.`` outside a quote span."""
    s = fqn.replace("/", ".")
    closer = {'"': '"', "'": "'", "`": "`", "[": "]"}
    parts: list[str] = []
    buf: list[str] = []
    waiting = ""  # the close char we're inside a span for, or "" outside any span
    for ch in s:
        if waiting:
            buf.append(ch)
            if ch == waiting:
                waiting = ""
        elif ch in closer:
            waiting = closer[ch]
            buf.append(ch)
        elif ch == ".":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p for p in (_STRIP.sub("", x).strip() for x in parts) if p]


def dataset_relation_fqn(urn: str) -> list[str]:
    """The relation parts AS THEY APPEAR IN SQL for ``urn`` — used to build the sqlglot
    schema and to match parsed table refs against graph nodes. For a file-based system
    (DuckDB) the authority IS the SQL catalog, so it's prepended; for a server-based
    system the authority (account / host) never appears in SQL, so only the relation parts
    are returned. (``snowflake://acct/DB.S.T`` → ``[DB, S, T]``; ``duckdb://f/main.t`` →
    ``[f, main, t]``; legacy ``warehouse://db.s.t`` → ``[db, s, t]``.)"""
    system, authority, parts, _ = parse_dataset_urn(urn)
    if authority and system in _CONTAINER_IS_AUTHORITY:
        return [authority, *parts]
    return parts


def warehouse_relation_urn(
    system: str,
    *,
    container: str = "",
    schema: str = "",
    table: str = "",
    authority: str = "",
    column: str = "",
) -> str:
    """Mint a dataset URN for a warehouse relation, applying the per-system container rule
    so the warehouse connector and the dbt provider for ONE relation mint the IDENTICAL
    URN. For a file-based system (DuckDB) the top container IS the authority; for a
    server-based system the container stays in the relation and ``authority`` (account /
    host, from :func:`warehouse_authority`) is carried separately. BigQuery is the natural
    server case with an empty authority — its project rides in the relation."""
    s = canonical_system(system)
    if s in _CONTAINER_IS_AUTHORITY:
        return dataset_urn(s, container, schema, table, column=column)
    return dataset_urn(s, authority, container, schema, table, column=column)


__all__ = [
    "COLUMN_SEP",
    "OPAQUE_AUTHORITY_SYSTEMS",
    "TWO_LEVEL_SYSTEMS",
    "WAREHOUSE_SCHEME",
    "WAREHOUSE_SCHEMES",
    "attested_column",
    "clean_part",
    "column_identity",
    "column_readings",
    "column_urn",
    "dataset_relation_fqn",
    "dataset_urn",
    "fix_account_name",
    "fold_identifier",
    "foreign_dataset_urn",
    "is_warehouse_scheme",
    "kafka_topic_urn",
    "normalize_relation",
    "parse_column_urn",
    "parse_dataset_urn",
    "split_relation_name",
    "urn_home_system",
    "urn_label",
    "warehouse_relation_urn",
    "warehouse_urn",
    "warehouse_urn_from_parts",
    "warehouse_urn_or_passthrough",
]
