/* eslint-disable */
/**
 * Auto-generated from packages/shared-openapi/tool-manifest.json.
 * Do not edit by hand. Run `make gen-tool-manifest` after changing
 * an alkera tool's Input/Output Pydantic models.
 */

export type Category = string;
export type NodeType = string;
export type Reason = string;
export type Transformation = string;
export type Urn = string;
export type Description = string;
export type Name = string;
export type DurationSeconds = number;
export type FilesRead = number;
export type Model = string | null;
export type ToolCalls = number;
export type Truncated = boolean;
/**
 * The caveat that applies on THIS asset and not on the item's other assets, such as 'the pre-refund copy'. Empty when the item's own body says it all.
 */
export type Note = string;
/**
 * What this asset is to the meaning: canonical, copy, deprecated, or whatever the team says. Free text; empty is fine.
 */
export type Role = string;
/**
 * The lineage asset this item describes, as the URN a lineage result reported. Never guess a URN; lineage_find turns a name into one.
 */
export type Urn1 = string;
export type JobId = string;
export type Cancelled = boolean;
export type JobId1 = string;
export type State = string;
export type ChildSessionId = string | null;
export type CompletedAgo = string | null;
export type Error = string | null;
export type JobId2 = string;
export type Kind = string;
export type OutputPreview = string | null;
export type StartedAgo = string;
export type State1 = string;
export type Title = string;
export type JobId3 = string | null;
export type Jobs = BackgroundJobView[];
/**
 * Run this command in the BACKGROUND: return a job id immediately instead of blocking, and notify you with the command's full output when it finishes. Use for a slow, independent command (a build, a full test run, a long-running script, a dev server). Permissions are still checked up front. Don't poll; you'll be notified. Leave false when you need the output for your next step.
 */
export type Background = boolean;
/**
 * The command to execute. Runs in a fresh, stateless shell — chain dependent steps in one call with `&&`; set `workdir` instead of `cd`.
 */
export type Command = string;
/**
 * Clear, concise description of what this command does in 5-10 words.
 */
export type Description1 = string;
/**
 * Optional timeout in milliseconds. The command is killed if it exceeds this. Leave it unset to let the command run as long as it needs; it is then stopped only if it sits idle, printing nothing and using no CPU.
 */
export type Timeout = number | null;
/**
 * The working directory to run the command in (relative to the workspace root, or absolute under it). Defaults to the workspace root. Use this instead of a `cd` in the command — `cd` does not persist.
 */
export type Workdir = string | null;
export type ExitCode = number | null;
export type JobId4 = string;
export type Note1 = string;
export type Output = string;
export type OutputPath = string | null;
export type Truncated1 = boolean;
export type Columns = string[] | null;
/**
 * A SHORT, human-friendly name for the result you expect (5 words or fewer, e.g. 'Q3 revenue by region', 'failed login counts'). Always provide it: when the result is large it's stored and shown under this name in the editor, so a good label makes it findable. Describe the result, not the query.
 */
export type ResultName = string;
export type Rows = unknown[][] | null;
export type Text = string | null;
export type MediaType = string;
export type SchemaVersion = string;
export type Sha256 = string;
export type Size = number;
export type Kind1 = string;
export type ResultName1 = string;
export type Total = number;
export type Handle = string;
export type Deleted = boolean;
export type FreedBytes = number;
export type Message = string;
export type Descending = boolean;
export type Distinct = boolean;
export type Handle1 = string;
export type Limit = number | null;
/**
 * Columns to sort by.
 */
export type OrderBy = string[] | null;
/**
 * A SHORT, human-friendly name for the result you expect (5 words or fewer, e.g. 'Q3 revenue by region', 'failed login counts'). Always provide it: when the result is large it's stored and shown under this name in the editor, so a good label makes it findable. Describe the result, not the query.
 */
export type ResultName2 = string;
/**
 * Columns to keep (omit for all).
 */
export type SelectColumns = string[] | null;
/**
 * A SQL boolean predicate over the columns (e.g. "amount > 100").
 */
export type Where = string | null;
export type Handle2 = string;
export type Columns1 = string[];
export type ContentType = string;
export type Kind2 = string;
export type SizeBytes = number;
export type Total1 = number;
/**
 * Optional file name (a single name, no path). The right extension is added automatically; defaults to a name derived from the handle.
 */
export type Filename = string;
export type Format = "parquet" | "csv" | "json" | "arrow";
export type Handle3 = string;
export type Bytes = number;
export type CharCount = number;
export type Columns2 = string[];
export type Format1 = string;
/**
 * Absolute path to the written file (under the chat sandbox). Run your own code over it — e.g. `python -c "import pandas as pd; df = pd.read_parquet(...)"`. Reference it in a reply by its name relative to the working directory.
 */
export type Path = string;
export type RowCount = number;
export type Handle4 = string;
export type TopK = number;
export type DistinctCapped = boolean;
export type DistinctCount = number | null;
export type Dtype = string;
export type Name1 = string;
export type NullCount = number;
export type Count = number;
export type TopK1 = TopValue[];
export type Columns3 = ColumnProfile[];
export type RowCount1 = number;
export type Handle5 = string;
/**
 * A SHORT, human-friendly name for the result you expect (5 words or fewer, e.g. 'Q3 revenue by region', 'failed login counts'). Always provide it: when the result is large it's stored and shown under this name in the editor, so a good label makes it findable. Describe the result, not the query.
 */
export type ResultName3 = string;
export type Sql = string;
/**
 * BigQuery connection handle.
 */
export type Connection = string;
/**
 * The job id to cancel (from bigquery.list_jobs).
 */
export type JobId5 = string;
/**
 * The job's location (from bigquery.list_jobs) — required when the job did not run in the project's default location.
 */
export type Location = string | null;
export type JobId6 = string;
export type Requested = string;
export type State2 = string;
export type DatasetId = string;
export type Location1 = string;
export type Description2 = string;
export type Mode = string;
export type Name2 = string;
export type Type = string;
/**
 * BigQuery connection handle.
 */
export type Connection1 = string;
/**
 * The job id (from bigquery.list_jobs).
 */
export type JobId7 = string;
/**
 * The job's location (from bigquery.list_jobs) — required when the job did not run in the project's default location.
 */
export type Location2 = string | null;
export type CacheHit = boolean;
export type CreationTimeMs = number;
export type EndTimeMs = number;
export type ErrorMessage = string;
export type JobId8 = string;
export type Query = string;
export type StartTimeMs = number;
export type State3 = string;
export type TotalBytesProcessed = number;
/**
 * BigQuery connection handle.
 */
export type Connection2 = string;
/**
 * The dataset id.
 */
export type Dataset = string;
/**
 * The table id (from bigquery.list_tables).
 */
export type Table = string;
export type Dataset1 = string;
export type Description3 = string;
export type Fields = BqFieldCard[];
export type NumBytes = number;
export type NumRows = number;
export type Table1 = string;
export type Type1 = string;
export type CreationTimeMs1 = number;
export type JobId9 = string;
export type Location3 = string;
export type Query1 = string;
export type State4 = string;
export type TotalBytesProcessed1 = number;
export type UserEmail = string;
/**
 * BigQuery connection handle (call sql.connections).
 */
export type Connection3 = string;
/**
 * Max datasets to return.
 */
export type Limit1 = number;
export type Datasets = BqDatasetCard[];
export type Truncated2 = boolean;
/**
 * Include every user's jobs (needs project-level permission), not just the authenticated user's.
 */
export type AllUsers = boolean;
/**
 * BigQuery connection handle.
 */
export type Connection4 = string;
/**
 * Max jobs to return.
 */
export type Limit2 = number;
/**
 * Only jobs in this state; omit for all states.
 */
export type StateFilter = ("running" | "pending" | "done") | null;
export type Jobs1 = BqJobCard[];
/**
 * BigQuery connection handle.
 */
export type Connection5 = string;
/**
 * The dataset id (from bigquery.list_datasets).
 */
export type Dataset2 = string;
/**
 * Max tables to return.
 */
export type Limit3 = number;
export type CreationTimeMs2 = number;
export type TableId = string;
export type Type2 = string;
export type Tables = BqTableCard[];
/**
 * The assets you mean to break, as the refusal named them. A table covers its own columns.
 */
export type Assets = string[];
/**
 * Why breaking them is the right move for this task.
 */
export type Reason1 = string;
export type CreationDate = string;
export type Name3 = string;
export type Region = string;
/**
 * Bucket; defaults to the connection's bucket.
 */
export type Bucket = string;
/**
 * AWS connection handle.
 */
export type Connection6 = string;
export type Bucket1 = string;
export type Encryption = string;
export type KmsKeyId = string;
export type LifecycleRules = {
  [k: string]: unknown;
}[];
export type PublicAccessBlocked = boolean;
export type Region1 = string;
export type ReplicationRules = {
  [k: string]: unknown;
}[];
export type Unavailable = string[];
export type Versioning = string;
/**
 * Python to run. `graph`, `algorithms`, `render`, GraphNode/GraphEdge and `save(g=None)` are pre-bound. Print what you want returned.
 */
export type Code = string;
/**
 * Short (5-10 word) summary of what this does; shown in the approval prompt.
 */
export type Description4 = string;
/**
 * The .alkgraph file to load (relative to the workspace root). Bound as the `graph` variable.
 */
export type Path1 = string;
/**
 * Where `save(...)` writes (must end in .alkgraph). Leave empty for a read-only analysis; set it to `path` to edit the graph in place.
 */
export type SavePath = string;
/**
 * Optional timeout in milliseconds; the child is killed if it exceeds this.
 */
export type TimeoutMs = number | null;
export type ExitCode1 = number | null;
export type Description5 = string;
export type EdgeCount = number;
export type Directed = boolean;
export type Kind3 = string;
export type Label = string;
export type Source = string;
export type Target = string;
export type Edges = GraphEdgeCard[];
export type Name4 = string;
export type NodeCount = number;
export type Id = string;
export type Kind4 = string;
export type Label1 = string;
export type Nodes = GraphNodeCard[];
export type Path2 = string;
export type RefType = string;
export type Truncated3 = boolean;
export type Output1 = string;
export type OutputPath1 = string | null;
export type SavedPath = string;
export type Truncated4 = boolean;
/**
 * Run in the BACKGROUND (mode='run' only): return a job id immediately instead of blocking, and notify you with the output when it finishes. Use for a slow SDK op (a long pipeline refresh, a cluster spin-up, a large export). The code is still APPROVED up front, then runs detached — don't poll; you'll be notified. Inspect with background_status / stop with background_cancel. Leave false when you need the output for your next step.
 */
export type Background1 = boolean;
/**
 * Inline Python to run (mode='run'). `connection` is pre-bound to the live SDK client. Print what you want returned. Provide this OR `file`, not both.
 */
export type Code1 = string | null;
/**
 * The connection handle whose SDK client to use (call sql.connections to list handles — never guess).
 */
export type Connection7 = string;
/**
 * Short (5-10 word) summary of what this does; shown in the approval prompt.
 */
export type Description6 = string;
/**
 * Path to a .py file to run instead of inline `code` (relative to the workspace root, e.g. a script you wrote into the sandbox). Provide this OR `code`.
 */
export type File = string | null;
/**
 * 'run' executes your Python against the bound `connection` client. 'env' takes no code and returns what's importable for this connection (client type + SDK modules + package versions) — use it to discover the environment first.
 */
export type Mode1 = "run" | "env";
/**
 * Optional timeout in milliseconds; the child is killed if it exceeds this.
 */
export type TimeoutMs1 = number | null;
export type ClientLabel = string;
export type ExitCode2 = number | null;
export type JobId10 = string;
export type Note2 = string;
export type Output2 = string;
export type OutputPath2 = string | null;
export type SdkModules = string[];
export type Truncated5 = boolean;
export type Name5 = string;
/**
 * Databricks connection handle.
 */
export type Connection8 = string;
/**
 * The run id to cancel (from databricks.list_runs).
 */
export type RunId = number;
export type Requested1 = string;
export type RunId1 = number;
export type CatalogType = string;
export type Comment = string;
export type Name6 = string;
export type Owner = string;
export type Deleted1 = boolean;
export type Id1 = string;
export type Index = number | null;
export type Kind5 = string;
export type Name7 = string;
export type Status =
  | (
      | "fresh"
      | "edited"
      | "stale"
      | "not_run"
      | "queued"
      | "running"
      | "error"
      | "interrupted"
      | "skipped"
      | "stopped"
      | "disabled"
    )
  | null;
export type By = string | null;
export type CellId = string | null;
export type Kind6 = string;
export type Message1 = string;
export type Engine = string;
export type Name8 = string;
/**
 * ClickHouse connection handle.
 */
export type Connection9 = string;
/**
 * The query_id to kill (from clickhouse.running_queries).
 */
export type QueryId = string;
/**
 * How many running queries the KILL matched (one result row each). 0 means no such running query existed — nothing was killed.
 */
export type Matched = number;
export type QueryId1 = string;
export type Requested2 = string;
/**
 * ClickHouse connection handle (call sql.connections).
 */
export type Connection10 = string;
export type Databases = ChDatabaseCard[];
export type Truncated6 = boolean;
export type Elapsed = number;
export type MemoryUsage = number;
export type Query2 = string;
export type QueryId2 = string;
export type ReadRows = number;
export type User = string;
/**
 * ClickHouse connection handle.
 */
export type Connection11 = string;
/**
 * Max processes to return.
 */
export type Limit4 = number;
export type Processes = ChProcessCard[];
export type Truncated7 = boolean;
export type Database = string;
export type Engine1 = string;
export type Name9 = string;
export type TotalBytes = number | null;
export type TotalRows = number | null;
/**
 * ClickHouse connection handle.
 */
export type Connection12 = string;
/**
 * Max tables to return.
 */
export type Limit5 = number;
export type Tables1 = ChTableSizeCard[];
export type Truncated8 = boolean;
export type Affected = AffectedCard[];
export type AgentNote = string | null;
export type Category1 = string;
export type DirectCategory = string;
export type Body = string;
export type Column = string;
export type ItemId = string;
export type Kind7 = string;
export type Origin = string;
export type Title1 = string;
export type Trust = string;
export type Urn2 = string;
export type Knowledge = KnowledgeAnnotation[];
/**
 * Up to 10 distinct item IDs from the highest-ranked omitted relationships. Pass each ID to context_get. Empty when the bounded overflow details do not fit beside the maximal knowledge prefix.
 */
export type ItemIds = string[];
/**
 * Total knowledge-to-asset relationships omitted.
 */
export type Omitted = number;
/**
 * Up to 10 distinct asset URNs from the highest-ranked omitted relationships. Pass these as context_search urns. Empty when the bounded overflow details do not fit beside the maximal knowledge prefix.
 */
export type Urns = string[];
export type ClusterId = string;
export type Requested3 = string;
export type ClusterId1 = string;
export type Name10 = string;
export type NodeTypeId = string;
export type NumWorkers = number | null;
export type Source1 = string;
export type SparkVersion = string;
export type State5 = string;
/**
 * The cluster id (from databricks.list_clusters).
 */
export type ClusterId2 = string;
/**
 * Databricks connection handle.
 */
export type Connection13 = string;
export type DataType = string;
export type Name11 = string;
export type Nullable = boolean;
export type DataType1 = string;
export type Name12 = string;
export type Nullable1 = boolean;
export type Dialect = string;
export type Environment = string;
export type Handle6 = string;
/**
 * AWS connection handle.
 */
export type Connection14 = string;
/**
 * The DB instance/cluster identifier.
 */
export type Identifier = string;
/**
 * AWS region; defaults to the connection's.
 */
export type Region2 = string;
/**
 * Resolve a DB instance or a DB cluster endpoint.
 */
export type Target1 = "instance" | "cluster";
export type CaCertificateIdentifier = string;
export type Database1 = string;
export type Engine2 = string;
export type EngineVersion = string;
export type Host = string;
export type IamAuthAvailable = boolean;
export type Identifier1 = string;
export type MasterUsername = string;
export type Notes = string[];
export type Port = number;
export type PubliclyAccessible = boolean;
export type SecurityGroups = string[];
export type SqlSupported = boolean;
export type SqlSystem = string;
export type TlsAvailable = boolean;
export type VpcId = string;
export type Body1 = string;
export type Confirmations = number;
export type Domain = string;
export type FileSources = string[];
export type IsMine = boolean;
export type ItemId1 = string;
export type Kind8 = string;
export type LineageUrn = string;
export type MatchedBy = string[];
export type Origin1 = string;
export type OwnerTeams = string[];
export type Score = number | null;
export type SharedBy = string;
export type SharedByEmail = string;
export type SourceClass = string;
export type StaleSources = string[];
export type Status1 = string;
export type Synonyms = string[];
export type Title2 = string;
export type Trust1 = string;
export type TrustSource = string;
export type UpdatedAgo = string;
export type UpdatedAt = number;
export type Urns1 = string[];
export type Visibility = string;
export type VisibilityScope = string;
/**
 * Replace the assets this item is attached to; omit to keep, [] to detach it from everything. Attaching one meaning to every asset that carries it is what makes a duplicated definition visible instead of ten copies of one note.
 */
export type Attachments = AttachmentInput[] | null;
/**
 * Replace the item's body. Omit to keep.
 */
export type Body2 = string | null;
/**
 * Replace the supporting file paths; omit to keep. Editing the body after re-reading a changed source is the time to set these and clear staleness.
 */
export type FileSources1 = string[] | null;
/**
 * The business area this belongs to ('billing', 'growth'). Grouping only; leave it unset rather than inventing one.
 */
export type Domain1 = string | null;
/**
 * File this ON a lineage node, so it shows on that asset's own record for the next person and the next chat — the highest-value place knowledge can land. Set it whenever the user tells you what a table/column/dashboard MEANS, how a metric is defined, a data-quality caveat, an owner, or a gotcha. Must be a URN the lineage graph actually holds: take it from a `lineage_find` match or any lineage result, never invent one — an unresolvable URN is refused, and the refusal names the closest nodes so you can re-issue against one. On an edit, omit to keep the node it is on and pass "" to take it off. Omit for knowledge that is not about a specific asset.
 */
export type LineageUrn1 = string | null;
/**
 * The teams accountable for this meaning, the ones a reader should ask before building on or changing it. Team names and team:<uuid> tokens both work; an unknown name refuses the write and lists the teams that exist. Unset keeps the stored owners.
 */
export type OwnerTeams1 = string[] | null;
/**
 * Set 'deprecated' to retire a meaning the team no longer stands behind, or 'active' to bring it back. A deprecated item still reads, so the history of what a term used to mean survives.
 */
export type Status2 = ("active" | "deprecated") | null;
/**
 * The other names people use for this meaning, so a teammate searching either one finds it.
 */
export type Synonyms1 = string[] | null;
/**
 * The item to refine, from a search hit or a note receipt.
 */
export type ItemId2 = string;
/**
 * Re-file the entry: true when it is about this codebase, false when it holds anywhere and every project should get it. Omit to keep. Once the entry has synced its namespace is fixed, so this re-files a local entry before it has been offered, not one the team already holds.
 */
export type RepoSpecific = boolean | null;
/**
 * Replace the item's title. Omit to keep.
 */
export type Title3 = string | null;
/**
 * Graduate to trusted once you have VERIFIED the item against concrete evidence or the human confirmed it, never because it seems plausible; recorded as agent_asserted unless a human already vouched. False demotes to unverified. Omit to keep.
 */
export type Trusted = boolean | null;
/**
 * Change who can see the item; omit to keep. 'shared' saves it at your team's scope and offers it to the team at once, 'private' withdraws the team's copy and keeps it local-only.
 */
export type Visibility1 = ("private" | "shared") | null;
/**
 * One item ID from context_search or a lineage annotation; call once per ID.
 */
export type ItemId3 = string;
/**
 * Every asset this governs, each with its own role and caveat. One meaning is ONE item attached to N assets; repeating the call per asset makes N copies nobody can tell apart.
 */
export type Attachments1 = AttachmentInput[] | null;
/**
 * Project-relative paths to the files this fact is derived from, such as 'models/marts/fct_orders.sql', so a later reader can re-verify it and gets a staleness warning when a file changes after the write.
 */
export type FileSources2 = string[] | null;
/**
 * learned_fact (default) | reference_sql | schema_card | concept. Use concept for business meaning that lives in more than one place ('active customer'), attached to every asset that carries it.
 */
export type Kind9 = "learned_fact" | "reference_sql" | "schema_card" | "concept";
/**
 * Whether this is about THIS codebase — its files, its conventions, its schema, a decision made here — rather than a fact that holds anywhere, like a warehouse column's meaning, a vendor's behaviour, or a business rule. Defaults to true, the common case. False files the entry org-wide so every project the team works on gets it; a workspace with no git remote is not a repository, so an entry written there is filed org-wide whatever you set.
 */
export type RepoSpecific1 = boolean;
/**
 * The fact to keep. Specific and self-contained, so it stands on its own.
 */
export type Text1 = string;
/**
 * One line a teammate would search for. Defaults to the first line of the text.
 */
export type Title4 = string;
/**
 * A HIGH bar: set true only when you VERIFIED the fact against concrete evidence (read the source or data, ran a confirming query, re-derived it) or the human stated it. A plausible but unchecked inference stays false. True is recorded as agent_asserted, so a future reader knows an agent claimed it, and it still decays until independently confirmed.
 */
export type Trusted1 = boolean;
/**
 * Who this is for; decide deliberately. 'shared' saves it at your team's scope and offers it to the team as it lands: undocumented data caveats, reusable queries, conventions, gotchas a teammate would hit too. 'private' keeps it on this machine for user-specific or session context. NEVER mark a secret, credential, or PII 'shared'. An offer the server refuses or cannot receive stays here, with the item's sync state saying where it stands. WITH `filing.lineage_urn` SET THIS DEFAULTS TO 'shared': what a table, column or dashboard MEANS is the team's, and a note about it that only you can see helps nobody who opens that asset next. Pass 'private' only for something personal or sensitive. In read-only and plan mode a note is kept private whatever it is filed on (sharing is an egress those modes do not make); 'private' is accepted in every mode.
 */
export type Visibility2 = ("private" | "shared") | null;
/**
 * Maximum results to return in each knowledge lane.
 */
export type K = number;
/**
 * Optional item kind: schema_card, reference_sql, learned_fact, concept, or note.
 */
export type Kind10 = string | null;
/**
 * Concrete names and natural intent to search for.
 */
export type Query3 = string;
/**
 * Which knowledge to search. 'all' (default) is everything that reaches this project — what is about this codebase PLUS the org-wide facts that hold everywhere. 'this' narrows to entries about this codebase only, for when a general fact would be a distraction.
 */
export type Repo = "all" | "this";
/**
 * Optional: only items attached to these lineage asset URNs (from lineage_find / a lineage result). Scopes the query to one neighborhood; returns nothing when no knowledge is attached to them.
 */
export type Urns2 = string[] | null;
export type Catalog = ContextCard[];
export type Note3 = string;
export type TeamKnowledge = ContextCard[];
export type ItemId4 = string;
export type SharedWithTeam = boolean;
export type SharingDetail = string;
export type Title5 = string;
export type Trust2 = string;
export type TrustSource1 = string;
export type Visibility3 = string;
/**
 * The Unity Catalog catalog to create the schema in.
 */
export type Catalog1 = string;
/**
 * Optional description for the schema.
 */
export type Comment1 = string;
/**
 * Databricks connection handle.
 */
export type Connection15 = string;
/**
 * The new schema's name (unqualified).
 */
export type Name13 = string;
export type Catalog2 = string;
export type Comment2 = string;
export type FullName = string;
export type Name14 = string;
/**
 * AWS connection handle.
 */
export type Connection16 = string;
/**
 * The instance/cluster to snapshot.
 */
export type Identifier2 = string;
/**
 * AWS region; defaults to the connection's.
 */
export type Region3 = string;
/**
 * Name for the new manual snapshot.
 */
export type SnapshotIdentifier = string;
/**
 * Snapshot a DB instance or a DB cluster.
 */
export type Target2 = "instance" | "cluster";
export type SnapshotIdentifier1 = string;
export type SourceIdentifier = string;
export type Status3 = string;
export type Target3 = string;
/**
 * Databricks connection handle.
 */
export type Connection17 = string;
export type Active = boolean;
export type DisplayName = string;
export type Id2 = string;
export type UserName = string;
/**
 * A read-only SELECT over the source names, e.g. `SELECT ... FROM a JOIN b ...`.
 */
export type Join = string;
/**
 * A SHORT, human-friendly name for the result you expect (5 words or fewer, e.g. 'Q3 revenue by region', 'failed login counts'). Always provide it: when the result is large it's stored and shown under this name in the editor, so a good label makes it findable. Describe the result, not the query.
 */
export type ResultName4 = string;
export type Connection18 = string;
export type Limit6 = number;
/**
 * Identifier to reference this source by in the join SQL.
 */
export type Name15 = string;
/**
 * A read-only SELECT against `connection` producing this source.
 */
export type Sql1 = string;
/**
 * The per-connection reads to combine.
 */
export type Sources = JoinSource[];
export type Comment3 = string;
export type Name16 = string;
export type Nullable2 = boolean;
export type Position = number | null;
export type Type3 = string;
/**
 * Databricks connection handle.
 */
export type Connection19 = string;
export type Id3 = string;
export type Name17 = string;
export type State6 = string;
export type Warehouses = DbxWarehouseCard[];
export type Requested4 = string;
export type WarehouseId = string;
/**
 * Databricks connection handle.
 */
export type Connection20 = string;
/**
 * The warehouse id (from databricks.list_warehouses).
 */
export type WarehouseId1 = string;
export type CellId1 = string;
export type Op = "delete";
/**
 * Bucket; defaults to the connection's bucket.
 */
export type Bucket2 = string;
/**
 * AWS connection handle.
 */
export type Connection21 = string;
/**
 * Object key to delete.
 */
export type Key = string;
/**
 * Delete a specific version, not the latest.
 */
export type VersionId = string;
export type Bucket3 = string;
export type DeleteMarker = boolean;
export type Key1 = string;
export type VersionId1 = string;
/**
 * AWS connection handle.
 */
export type Connection22 = string;
/**
 * The DB cluster identifier (from list_clusters).
 */
export type Identifier3 = string;
/**
 * AWS region; defaults to the connection's.
 */
export type Region4 = string;
export type AvailabilityZones = string[];
export type BackupRetentionDays = number;
export type CreatedAt = string;
export type Database2 = string;
export type DeletionProtection = boolean;
export type Engine3 = string;
export type EngineMode = string;
export type EngineVersion1 = string;
export type Host1 = string;
export type IamAuthEnabled = boolean;
export type Identifier4 = string;
export type KmsKeyId1 = string;
export type LogExports = string[];
export type MasterUsername1 = string;
export type Members = string[];
export type MultiAz = boolean;
export type ParameterGroup = string;
export type PerformanceInsightsEnabled = boolean;
export type Port1 = number;
export type PreferredBackupWindow = string;
export type PreferredMaintenanceWindow = string;
export type ReaderHost = string;
export type SecurityGroups1 = string[];
export type ServerlessMaxCapacity = number;
export type ServerlessMinCapacity = number;
export type Status4 = string;
export type StorageEncrypted = boolean;
export type SubnetGroup = string;
export type VpcId1 = string;
/**
 * AWS connection handle.
 */
export type Connection23 = string;
/**
 * The DB instance identifier (from list_instances).
 */
export type Identifier5 = string;
/**
 * AWS region; defaults to the connection's.
 */
export type Region5 = string;
export type AllocatedStorageGb = number;
export type AutoMinorVersionUpgrade = boolean;
export type AvailabilityZone = string;
export type BackupRetentionDays1 = number;
export type CaCertificateIdentifier1 = string;
export type ClusterIdentifier = string;
export type CreatedAt1 = string;
export type Database3 = string;
export type DeletionProtection1 = boolean;
export type Engine4 = string;
export type EngineVersion2 = string;
export type Host2 = string;
export type IamAuthEnabled1 = boolean;
export type Identifier6 = string;
export type InstanceClass = string;
export type Iops = number;
export type KmsKeyId2 = string;
export type LogExports1 = string[];
export type MasterUsername2 = string;
export type MaxAllocatedStorageGb = number;
export type MonitoringIntervalSeconds = number;
export type MultiAz1 = boolean;
export type OptionGroup = string;
export type ParameterGroups = string[];
export type PerformanceInsightsEnabled1 = boolean;
export type PerformanceInsightsRetentionDays = number;
export type Port2 = number;
export type PreferredBackupWindow1 = string;
export type PreferredMaintenanceWindow1 = string;
export type PubliclyAccessible1 = boolean;
export type SecondaryAvailabilityZone = string;
export type SecurityGroups2 = string[];
export type Status5 = string;
export type StorageEncrypted1 = boolean;
export type StorageType = string;
export type SubnetGroup1 = string;
export type Subnets = string[];
export type VpcId2 = string;
/**
 * AWS connection handle.
 */
export type Connection24 = string;
/**
 * Max parameters to return.
 */
export type Limit7 = number;
/**
 * Case-insensitive substring filter on the parameter name.
 */
export type NameContains = string;
/**
 * The parameter-group name (from aws.rds.describe_instance).
 */
export type ParameterGroup1 = string;
/**
 * AWS region; defaults to the connection's.
 */
export type Region6 = string;
/**
 * Only parameters from this source; empty for all.
 */
export type Source2 = "" | "user" | "system" | "engine-default";
/**
 * A DB parameter group or a DB CLUSTER parameter group.
 */
export type Target4 = "instance" | "cluster";
export type ParameterGroup2 = string;
export type AllowedValues = string;
export type ApplyMethod = string;
export type ApplyType = string;
export type DataType2 = string;
export type Description7 = string;
export type Modifiable = boolean;
export type Name18 = string;
export type Source3 = string;
export type Value1 = string;
export type Parameters = ParameterCard[];
export type Target5 = string;
export type Connection25 = string;
export type DocumentId = string;
export type Plugin = string;
export type Connection26 = string;
export type DocumentId1 = string;
export type Plugin1 = string;
export type SectionId = string;
export type Text2 = string;
export type Title6 = string;
export type Sections = DocumentSectionCard[];
export type Limit8 = number;
export type Query4 = string;
export type Connection27 = string;
export type Error1 = string;
export type Plugin2 = string;
export type Searched = string;
export type Truncated9 = boolean;
export type Catalogue = SourceLedgerRow[];
export type Connection28 = string;
export type DocumentId2 = string;
export type Excerpt = string;
export type Plugin3 = string;
export type Title7 = string;
export type Url = string;
export type Hits = DocumentHitCard[];
export type Connection29 = string;
export type Plugin4 = string;
export type Sources1 = KnowledgeConnectionCard[];
/**
 * AWS connection handle.
 */
export type Connection30 = string;
/**
 * The DB instance identifier.
 */
export type Identifier7 = string;
/**
 * The log file (from aws.rds.list_log_files).
 */
export type LogFileName = string;
/**
 * Pagination marker; '0' starts at the beginning, or pass the next_marker from a previous call. Use 'most recent' style markers RDS returns.
 */
export type Marker = string;
/**
 * Max lines to read in this portion.
 */
export type NumberOfLines = number;
/**
 * AWS region; defaults to the connection's.
 */
export type Region7 = string;
export type Clipped = boolean;
export type Data1 = string;
export type LogFileName1 = string;
export type MorePending = boolean;
export type NextMarker = string;
/**
 * Druid connection handle
 */
export type Connection31 = string;
export type QueryId3 = string;
/**
 * Druid connection handle
 */
export type Connection32 = string;
export type Items = unknown[];
/**
 * Druid connection handle
 */
export type Connection33 = string;
export type LogTailBytes = number;
export type TaskId = string;
export type Certainty = number;
export type Dst = string;
export type Plugin5 = string;
export type Provenance = string;
export type Relation = string;
export type Src = string;
export type Transformation1 = string;
export type AgentNote1 = string | null;
export type Edges1 = EdgeCard[];
export type Knowledge1 = KnowledgeAnnotation[];
export type Missing = boolean;
export type Name19 = string;
export type NodeType1 = string;
export type Urn3 = string;
export type Nodes1 = NodeCard[];
export type CreateMissing = boolean;
export type Mode2 = "apply";
export type AttrsJson = string;
export type Description8 = string;
export type Directed1 = boolean;
export type Id4 = string;
export type Kind11 = string;
export type Label2 = string;
export type Name20 = string;
export type Op1 = "add_node" | "upsert_node" | "remove_node" | "add_edge" | "upsert_edge" | "remove_edge" | "set_graph";
export type Source4 = string;
export type Target6 = string;
export type Operations = GraphOperation[];
export type Path3 = string;
export type CellId2 = string;
/**
 * @minItems 1
 * @maxItems 100
 */
export type Edits = [NotebookTextEdit, ...NotebookTextEdit[]];
export type New = string;
export type Occurrence = number | null;
export type Old = string;
export type Op2 = "edit";
export type AttrsJson1 = string;
export type Description9 = string;
export type Mode3 = "create";
export type Name21 = string;
export type Overwrite = boolean;
export type Path4 = string;
export type Connection34 = string;
export type Direction = "out" | "in" | "both";
export type MaxHops = number;
export type Merge = boolean;
export type Mode4 = "import_lineage";
export type Path5 = string;
export type Seed = string;
export type GraphJson = string;
export type Mode5 = "save";
export type Overwrite1 = boolean;
export type Path6 = string;
/**
 * What an action does to the world.
 *
 * The single load-bearing axis: a SQL ``DROP``, a bash ``rm -rf`` and a
 * dbt-Cloud "delete job" all reduce to ``DESTROY`` and hit the same rule.
 * Persisted via ``native_enum=False`` (VARCHAR) when it ever reaches the
 * ORM — Pydantic models just serialize the ``.value`` string.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "Effect".
 */
export type Effect = "read" | "write" | "destroy" | "egress" | "exec" | "memory";
export type Connection35 = string;
export type Index1 = string;
export type TerminateAfter = number;
export type Count1 = number;
export type TerminatedEarly = boolean;
/**
 * Elasticsearch connection handle
 */
export type Connection36 = string;
/**
 * ES|QL parameters as positional values or single-entry named parameter objects
 */
export type Params = unknown[];
/**
 * ES|QL query; the server enforces its result ceiling
 */
export type Query6 = string;
export type TimeoutSeconds = number | null;
export type Columns4 = string[];
export type DocumentsFound = number | null;
export type IsPartial = boolean;
export type Rows1 = unknown[][];
export type TookMs = number;
export type ValuesLoaded = number | null;
/**
 * Elasticsearch connection handle
 */
export type Connection37 = string;
/**
 * Selected data stream, write alias, or concrete index
 */
export type Index2 = string;
export type Limit9 = number;
export type Hits1 = {
  [k: string]: unknown;
}[];
export type TimedOut = boolean;
export type TookMs1 = number;
export type Total2 = number | null;
/**
 * The environment to capture. Leave empty for the active one (in a cloud chat, the environment `python` runs in).
 */
export type EnvPath = string;
export type Editable = string[];
export type Detail = string;
export type Kind12 = string;
export type Name22 = string;
export type NotPortable = NotPortable1[];
export type Packages1 = number;
export type Path7 = string;
export type Summary = string;
/**
 * Only plan: list the commands without running any.
 */
export type DryRun = boolean;
/**
 * The environment to build or update. Leave empty for the active one (in a cloud chat) or the project's `.venv` (locally).
 */
export type EnvPath1 = string;
export type AlreadySatisfied = boolean;
export type DryRun1 = boolean;
export type Detail1 = string;
export type Kind13 =
  | "python_mismatch"
  | "target_not_conda"
  | "conda_unavailable"
  | "no_installer"
  | "local_source_missing"
  | "not_installed"
  | "deadline"
  | "step_failed"
  | "unknown_source"
  | "unpinned_input";
export type Name23 = string;
export type NotRecreated = Gap[];
export type Notes1 = string[];
export type Command1 = string;
export type ExitCode3 = number | null;
export type Label3 = string;
export type OutputTail = string;
export type Purpose =
  | "create_env"
  | "conda_install"
  | "uv_sync"
  | "install_requirements"
  | "install_packages"
  | "install_local"
  | "path_entries";
export type Ran = boolean;
export type Steps = StepReport[];
export type TargetEnv = string;
export type Handle7 = string;
export type Limit10 = number | null;
export type Offset = number;
export type Columns5 = string[];
export type HasMore = boolean;
export type Kind14 = string;
export type Limit11 = number;
export type NextOffset = number | null;
export type Offset1 = number;
export type Returned = number;
export type Rows2 = unknown[][];
export type Text3 = string;
export type Total3 = number;
/**
 * Bucket; defaults to the connection's bucket.
 */
export type Bucket4 = string;
/**
 * AWS connection handle.
 */
export type Connection38 = string;
/**
 * Max matches to return.
 */
export type Limit12 = number;
/**
 * Keys to scan before stopping.
 */
export type MaxKeys = number;
/**
 * Glob (e.g. '** /*.parquet') or regex matched on the key.
 */
export type Pattern = string;
/**
 * Restrict the scan to keys under this prefix.
 */
export type Prefix = string;
/**
 * Treat 'pattern' as a regex, not a glob.
 */
export type UseRegex = boolean;
export type Bucket5 = string;
export type Matches = {
  [k: string]: unknown;
}[];
export type Prefix1 = string;
export type Scanned = number;
export type Truncated10 = boolean;
export type AgentNote2 = string | null;
export type Count2 = number;
export type Titles = string[];
export type Urn4 = string;
export type FiledKnowledge = NodeKnowledgeBrief[];
export type Knowledge2 = KnowledgeAnnotation[];
export type Matches1 = NodeCard[];
export type Enabled = boolean;
export type Hashed = boolean;
export type Name24 = string;
export type NameInDestination = string;
export type Id5 = string;
export type LastFailed = string;
export type LastSynced = string;
export type Name25 = string;
export type Paused = boolean;
export type Service = string;
export type SetupState = string;
export type SyncState = string;
export type Tasks = string[];
export type UpdateState = string;
export type Urn5 = string;
export type Warnings = string[];
/**
 * The Fivetran connection handle to read pipelines from.
 */
export type Connection39 = string;
/**
 * Optional case-insensitive filter on a pipeline's name, source service, or id.
 */
export type Search = string;
export type Note4 = string;
export type Pipelines = FivetranPipelineCard[];
export type Total4 = number;
/**
 * The Fivetran connection handle.
 */
export type Connection40 = string;
/**
 * The pipeline id (from fivetran.pipeline_status or a fivetran:// lineage urn).
 */
export type ConnectorId = string;
/**
 * The SOURCE schema name, exactly as the source spells it (case-sensitive).
 */
export type SchemaName = string;
/**
 * The SOURCE table name, exactly as the source spells it (case-sensitive).
 */
export type Table2 = string;
export type Columns6 = FivetranColumnCard[];
export type Note5 = string;
/**
 * AWS connection handle.
 */
export type Connection41 = string;
/**
 * The RDS endpoint host (from aws.rds.connection_info).
 */
export type Host3 = string;
/**
 * The RDS endpoint port.
 */
export type Port3 = number;
/**
 * AWS region; defaults to the connection's.
 */
export type Region8 = string;
/**
 * The database user the IAM token authenticates as.
 */
export type Username = string;
export type ExpiresInSeconds = number;
export type Host4 = string;
export type Note6 = string;
export type Port4 = number;
export type Token = string;
export type Username1 = string;
/**
 * Databricks connection handle.
 */
export type Connection42 = string;
/**
 * The securable's full name (e.g. 'main' for a catalog, 'main.default' for a schema, 'main.default.orders' for a table).
 */
export type FullName1 = string;
/**
 * The kind of Unity Catalog securable to read grants on.
 */
export type SecurableType =
  | "catalog"
  | "schema"
  | "table"
  | "function"
  | "volume"
  | "external_location"
  | "storage_credential";
export type FullName2 = string;
export type Principal = string;
export type Privileges = string[];
export type Grants = GrantCard[];
export type SecurableType1 = string;
/**
 * Databricks connection handle.
 */
export type Connection43 = string;
/**
 * The job id (from databricks.list_jobs).
 */
export type JobId11 = number;
export type Creator = string;
export type Format2 = string;
export type JobId12 = number;
export type Name26 = string;
export type Schedule = string;
export type SchedulePaused = boolean;
export type TaskKeys = string[];
/**
 * Databricks connection handle.
 */
export type Connection44 = string;
/**
 * The pipeline id (from databricks.list_pipelines).
 */
export type PipelineId = string;
export type ClusterId3 = string;
export type Creator1 = string;
export type Health = string;
export type LatestUpdateId = string;
export type LatestUpdateState = string;
export type Name27 = string;
export type PipelineId1 = string;
export type State7 = string;
/**
 * Databricks connection handle.
 */
export type Connection45 = string;
/**
 * The run id to inspect (from databricks.run_job).
 */
export type RunId2 = number;
/**
 * Databricks connection handle.
 */
export type Connection46 = string;
/**
 * A TASK run id — from databricks.get_run's `tasks[].run_id`. For a multi-task job the PARENT run id has no output; you must pass a task's run id.
 */
export type RunId3 = number;
export type Clipped1 = boolean;
export type Error2 = string;
export type ErrorTrace = string;
export type Logs = string;
export type LogsTruncated = boolean;
export type RunId4 = number;
export type LifeCycleState = string;
export type ResultState = string;
export type RunId5 = number;
export type StateMessage = string;
export type RunId6 = number;
export type State8 = string;
export type TaskKey = string;
export type Tasks1 = TaskRunCard[];
/**
 * Databricks connection handle.
 */
export type Connection47 = string;
/**
 * The table's 3-level full name, e.g. 'main.default.orders'.
 */
export type FullName3 = string;
export type Columns7 = DbxColumnCard[];
export type Comment4 = string;
export type DataSourceFormat = string;
export type FullName4 = string;
export type Owner1 = string;
export type StorageLocation = string;
export type TableType = string;
export type Defs = string[];
export type Cells = string[];
export type Code2 = string;
export type Name28 = string | null;
export type Errors = GraphErrorInfo[];
export type Refs = string[];
/**
 * Four call signatures, switched by ``mode``.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphEditInput".
 */
export type GraphEditInput = EditCreate | EditSave | EditApply | EditImportLineage;
export type Applied = number;
export type BytesWritten = number;
export type Created = boolean;
export type ImportedEdges = number;
export type ImportedNodes = number;
export type Issues = string[];
export type Mode6 = string;
export type Note7 = string;
export type Path8 = string;
export type EdgeCount1 = number;
export type Error3 = string;
export type Name29 = string;
export type NodeCount1 = number;
export type Path9 = string;
/**
 * Seven read signatures, switched by ``mode``.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphQueryInput".
 */
export type GraphQueryInput =
  | QueryRead
  | QueryList
  | QueryTraverse
  | QuerySearch
  | QueryFilter
  | QueryPaths
  | QueryStats;
export type MaxEdges = number;
export type MaxNodes = number;
export type Mode7 = "read";
export type Path10 = string;
export type Style = "summary" | "text" | "mermaid";
export type Directory = string;
export type Limit13 = number;
export type Mode8 = "list";
export type Algorithm = "bfs" | "dfs";
export type Direction1 = "out" | "in" | "both";
export type AttrExact = boolean;
export type AttrKey = string;
export type AttrValue = string;
export type EdgeKinds = string[];
export type MaxDegree = number;
export type MinDegree = number;
export type NodeKinds = string[];
export type MaxHops1 = number;
export type MaxNodes1 = number;
export type Mode9 = "traverse";
export type Path11 = string;
export type Seeds = string[];
export type Limit14 = number;
export type Mode10 = "search";
export type Path12 = string;
export type Query8 = string;
export type Regex = boolean;
export type Target7 = "nodes" | "edges" | "both";
export type MaxNodes2 = number;
export type Mode11 = "filter";
export type Path13 = string;
export type EnumerateAll = boolean;
export type MaxHops2 = number;
export type MaxPaths = number;
export type Mode12 = "paths";
export type Path14 = string;
export type Source5 = string;
export type Target8 = string;
export type MaxCycles = number;
export type Mode13 = "stats";
export type Path15 = string;
export type Field = string;
export type Id6 = string;
export type Kind15 = string;
export type Label4 = string;
export type Snippet = string;
export type EdgeMatches = SearchHit[];
export type Files = GraphFileCard[];
export type Missing1 = string[];
export type Mode14 = string;
export type NodeMatches = SearchHit[];
export type Note8 = string;
export type Path16 = string;
export type Paths = string[][];
export type Rendered = string;
export type Acyclic = boolean;
export type AvgDegree = number;
export type Busiest = string[];
export type Components = number;
export type Cycles = string[][];
export type DirectedEdges = number;
export type EdgeCount2 = number;
export type Isolated = string[];
export type LargestComponent = number;
export type Leaves = string[];
export type MaxInDegree = number;
export type MaxOutDegree = number;
export type NodeCount2 = number;
export type ParallelEdges = number;
export type Roots = string[];
export type SelfLoops = number;
export type TopologicalOrder = string[];
export type UndirectedEdges = number;
export type Truncated11 = boolean;
export type Computed = boolean;
export type Edges2 = [unknown, unknown][];
/**
 * Bucket; defaults to the connection's bucket.
 */
export type Bucket6 = string;
/**
 * AWS connection handle.
 */
export type Connection48 = string;
/**
 * Object key to describe.
 */
export type Key2 = string;
/**
 * Describe a specific object version.
 */
export type VersionId2 = string;
export type Bucket7 = string;
export type ContentType1 = string;
export type Etag = string;
export type Key3 = string;
export type KmsKeyId3 = string;
export type LastModified = string;
export type ServerSideEncryption = string;
export type Size1 = number;
export type StorageClass = string;
export type VersionId3 = string;
/**
 * The Hex connection handle to list projects from.
 */
export type Connection49 = string;
/**
 * Optional case-insensitive filter on a project's title, description, or id.
 */
export type Search1 = string;
export type Note9 = string;
export type Id7 = string;
export type LastPublishedAt = string;
export type ProjectType = string;
export type Status6 = string;
export type Title8 = string;
export type Urn6 = string;
export type Projects = HexProjectCard[];
export type Total5 = number;
/**
 * The Hex connection handle.
 */
export type Connection50 = string;
/**
 * How many recent runs to return (newest first).
 */
export type Limit15 = number;
/**
 * The project to inspect (an id from hex.list_projects).
 */
export type ProjectId = string;
export type Note10 = string;
export type ProjectId1 = string;
export type ElapsedMs = number | null;
export type EndedAt = string;
export type RunId7 = string;
export type RunUrl = string;
export type StartedAt = string;
export type Status7 = string;
export type Trigger = string;
export type Runs = HexRunCard[];
export type After = string | null;
export type Before = string | null;
export type Kind16 = string;
export type Name30 = string;
export type Op3 = "insert";
export type Source6 = string;
export type JobId13 = number;
export type Name31 = string;
export type Diagnostics = string[];
export type Items1 = {
  [k: string]: unknown;
}[];
/**
 * Kafka connection handle
 */
export type Connection51 = string;
export type Connection52 = string;
export type IncludeValues = boolean;
export type MaxMessages = number;
export type Partition = number;
export type Topic = string;
export type Agents = AgentTypeInfo[];
/**
 * AWS connection handle (call sql.connections).
 */
export type Connection53 = string;
export type Buckets = BucketCard[];
/**
 * Databricks connection handle.
 */
export type Connection54 = string;
/**
 * Max catalogs to return.
 */
export type Limit16 = number;
export type Catalogs = CatalogCard[];
/**
 * Databricks connection handle.
 */
export type Connection55 = string;
/**
 * Max clusters to return.
 */
export type Limit17 = number;
export type Clusters = ClusterCard[];
/**
 * AWS connection handle (call sql.connections).
 */
export type Connection56 = string;
/**
 * Max instances to return.
 */
export type Limit18 = number;
/**
 * AWS region; defaults to the connection's.
 */
export type Region9 = string;
export type AllocatedStorageGb1 = number;
export type AvailabilityZone1 = string;
export type ClusterIdentifier1 = string;
export type Engine5 = string;
export type EngineVersion3 = string;
export type Host5 = string;
export type IamAuthEnabled2 = boolean;
export type Identifier8 = string;
export type InstanceClass1 = string;
export type MultiAz2 = boolean;
export type Port5 = number;
export type Status8 = string;
export type StorageType1 = string;
export type Instances = RdsInstanceCard[];
export type Region10 = string;
/**
 * Databricks connection handle (call sql.connections).
 */
export type Connection57 = string;
/**
 * Max jobs to return.
 */
export type Limit19 = number;
export type Jobs2 = JobCard[];
/**
 * AWS connection handle.
 */
export type Connection58 = string;
/**
 * The DB instance identifier (logs are per-instance).
 */
export type Identifier9 = string;
/**
 * Max log files to return.
 */
export type Limit20 = number;
/**
 * Only log files whose name contains this (e.g. 'error').
 */
export type NameContains1 = string;
/**
 * AWS region; defaults to the connection's.
 */
export type Region11 = string;
export type Identifier10 = string;
export type LastWrittenAt = string;
export type Name32 = string;
export type SizeBytes1 = number;
export type LogFiles = LogFileCard[];
/**
 * Bucket; defaults to the connection's bucket.
 */
export type Bucket8 = string;
/**
 * AWS connection handle.
 */
export type Connection59 = string;
/**
 * Group keys by this separator ('/' lists one folder level, not recursively).
 */
export type Delimiter = string;
/**
 * Max keys to return.
 */
export type MaxKeys1 = number;
/**
 * Key prefix to list under.
 */
export type Prefix2 = string;
/**
 * Resume listing after this key.
 */
export type StartAfter = string;
export type Bucket9 = string;
export type CommonPrefixes = string[];
export type Objects = {
  [k: string]: unknown;
}[];
export type Prefix3 = string;
export type Returned1 = number;
export type Truncated12 = boolean;
/**
 * Databricks connection handle.
 */
export type Connection60 = string;
/**
 * Max pipelines to return.
 */
export type Limit21 = number;
export type Creator2 = string;
export type Name33 = string;
export type PipelineId2 = string;
export type State9 = string;
export type Pipelines1 = PipelineCard[];
export type Active1 = boolean;
export type Added = boolean;
export type Environment1 = string;
export type Handle8 = string;
export type Plugin6 = string;
export type Connections = PluginConnectionInfo[];
export type Description10 = string;
export type Enabled1 = boolean;
export type Name34 = string;
export type Surfaces = string[];
export type UrnFormat = string;
export type Plugins = PluginInfo[];
/**
 * Only runs that are pending or running (not completed).
 */
export type ActiveOnly = boolean;
/**
 * Databricks connection handle.
 */
export type Connection61 = string;
/**
 * Filter to one job's runs; omit for all recent runs.
 */
export type JobId14 = number | null;
/**
 * Max runs to return.
 */
export type Limit22 = number;
export type JobId15 = number | null;
export type LifeCycleState1 = string;
export type ResultState1 = string;
export type RunId8 = number;
export type RunName = string;
export type StartTimeMs1 = number;
export type Url1 = string;
export type Runs1 = RunCard[];
/**
 * The catalog name (from databricks.list_catalogs).
 */
export type Catalog3 = string;
/**
 * Databricks connection handle.
 */
export type Connection62 = string;
/**
 * Max schemas to return.
 */
export type Limit23 = number;
export type Comment5 = string;
export type FullName5 = string;
export type Name35 = string;
export type Owner2 = string;
export type Schemas = SchemaCard[];
/**
 * AWS connection handle.
 */
export type Connection63 = string;
/**
 * Only snapshots of this instance/cluster; omit for all.
 */
export type Identifier11 = string;
/**
 * Max snapshots to return.
 */
export type Limit24 = number;
/**
 * AWS region; defaults to the connection's.
 */
export type Region12 = string;
/**
 * Restrict to one snapshot type; empty for all.
 */
export type SnapshotType = "" | "automated" | "manual" | "shared" | "public";
/**
 * Instance snapshots or cluster snapshots.
 */
export type Target9 = "instance" | "cluster";
export type Region13 = string;
export type AllocatedStorageGb2 = number;
export type CreatedAt2 = string;
export type Encrypted = boolean;
export type Engine6 = string;
export type EngineVersion4 = string;
export type Identifier12 = string;
export type SnapshotType1 = string;
export type SourceIdentifier1 = string;
export type Status9 = string;
export type Snapshots = SnapshotCard[];
export type Target10 = string;
/**
 * The catalog name.
 */
export type Catalog4 = string;
/**
 * Databricks connection handle.
 */
export type Connection64 = string;
/**
 * Max tables to return.
 */
export type Limit25 = number;
/**
 * The schema name (from databricks.list_schemas).
 */
export type SchemaName1 = string;
export type Comment6 = string;
export type DataSourceFormat1 = string;
export type FullName6 = string;
export type Name36 = string;
export type Owner3 = string;
export type TableType1 = string;
export type Tables2 = TableCard[];
export type Id8 = string;
export type Title9 = string;
export type Urn7 = string;
/**
 * The Looker connection handle.
 */
export type Connection65 = string;
/**
 * The dashboard to inspect (a numeric id, or a LookML id like model::name).
 */
export type DashboardId = string;
export type DashboardId1 = string;
export type Tables3 = string[];
export type Explore = string;
export type Model1 = string;
export type Tables4 = string[];
export type Title10 = string;
export type Tiles = LookerTileCard[];
export type Title11 = string;
export type Urn8 = string;
/**
 * The Looker connection handle to list dashboards from.
 */
export type Connection66 = string;
/**
 * Optional case-insensitive filter on a dashboard's title or id.
 */
export type Search2 = string;
export type Dashboards = LookerDashboardCard[];
export type Note11 = string;
export type Total6 = number;
/**
 * Wipe the whole list (work finished).
 */
export type Clear = boolean;
export type Delete = string[];
/**
 * Ids of prerequisite tasks (replaces the current list).
 */
export type DependsOn = string[] | null;
export type Description11 = string | null;
/**
 * Stable slug; new id = create, existing id = edit.
 */
export type Id9 = string;
/**
 * pending | in_progress | completed | cancelled.
 */
export type Status10 = ("pending" | "in_progress" | "completed" | "cancelled") | null;
/**
 * Required when creating a task.
 */
export type Title12 = string | null;
export type Upsert = TaskUpsert[];
export type Ready = string[];
export type Total7 = number;
export type Blocked = boolean;
export type BlockedBy = string[];
export type DependsOn1 = string[];
export type Description12 = string;
export type Id10 = string;
export type Status11 = "pending" | "in_progress" | "completed" | "cancelled";
export type Title13 = string;
export type UpdatedAgo1 = string;
export type Tasks2 = TaskView[];
export type Average = number | null;
export type Datapoints = number;
export type Latest = number | null;
export type Maximum = number | null;
export type Metric = string;
export type Minimum = number | null;
export type Unit = string;
/**
 * AWS connection handle.
 */
export type Connection67 = string;
/**
 * Length of the look-back window, in hours.
 */
export type Hours = number;
/**
 * The DB instance/cluster identifier.
 */
export type Identifier13 = string;
/**
 * CloudWatch AWS/RDS metric names; empty uses CPU, connections, freeable memory, free storage and read/write IOPS.
 */
export type Metrics = string[];
/**
 * AWS region; defaults to the connection's.
 */
export type Region14 = string;
/**
 * Dimension the metrics by instance or by cluster.
 */
export type Target11 = "instance" | "cluster";
export type End = string;
export type Identifier14 = string;
export type PeriodSeconds = number;
export type Series = MetricSeries[];
export type Start = string;
export type Target12 = string;
/**
 * Source collection
 */
export type Collection = string;
/**
 * MongoDB connection handle
 */
export type Connection68 = string;
/**
 * Source database
 */
export type Database4 = string;
/**
 * Return queryPlanner output without execution
 */
export type Explain = boolean;
export type Limit26 = number;
export type Pipeline = {
  [k: string]: unknown;
}[];
export type CostWarnings = string[];
export type Documents = {
  [k: string]: unknown;
}[];
/**
 * What an action does to the world.
 *
 * The single load-bearing axis: a SQL ``DROP``, a bash ``rm -rf`` and a
 * dbt-Cloud "delete job" all reduce to ``DESTROY`` and hit the same rule.
 * Persisted via ``native_enum=False`` (VARCHAR) when it ever reaches the
 * ORM — Pydantic models just serialize the ``.value`` string.
 */
export type Effect1 = "read" | "write" | "destroy" | "egress" | "exec" | "memory";
export type Explain1 = {
  [k: string]: unknown;
} | null;
export type Truncated13 = boolean;
export type After1 = string | null;
export type Before1 = string | null;
export type CellId3 = string;
export type Op4 = "move";
export type DefaultCharacterSetName = string;
export type DefaultCollationName = string;
export type SchemaName2 = string;
/**
 * MySQL connection handle.
 */
export type Connection69 = string;
/**
 * The processlist id to kill (from mysql.processlist).
 */
export type Id11 = number;
export type Id12 = number;
export type Requested5 = string;
/**
 * MySQL connection handle (call sql.connections).
 */
export type Connection70 = string;
export type Databases1 = MyDatabaseRow[];
export type Truncated14 = boolean;
export type Command2 = string;
export type Db = string;
export type Host6 = string;
export type Id13 = number;
export type Info = string;
export type State10 = string;
export type Time = number;
export type User1 = string;
/**
 * MySQL connection handle.
 */
export type Connection71 = string;
/**
 * Max sessions to return.
 */
export type Limit27 = number;
export type Processes1 = MyProcessRow[];
export type Truncated15 = boolean;
export type Engine7 = string;
export type TableName = string;
export type TableRows = number | null;
export type TableSchema = string;
export type TotalBytes1 = number | null;
/**
 * MySQL connection handle.
 */
export type Connection72 = string;
/**
 * Max tables to return.
 */
export type Limit28 = number;
export type Tables5 = MyTableSizeRow[];
export type Truncated16 = boolean;
export type Direction2 = string;
export type Items2 = ContextCard[];
export type Urn9 = string;
export type AgentNote3 = string | null;
export type Count3 = number;
export type Nodes2 = NodeKnowledgeCard[];
export type Urn10 = string;
export type Id14 = string;
export type Index3 = number;
export type Kind17 = string;
export type Name37 = string;
export type Defs1 = string[];
export type Downstream = string[];
export type GraphErrors = string[];
export type Author = string;
export type Untrusted1 = true;
export type Id15 = string;
export type Index4 = number;
export type Kind18 = string;
export type By1 = string;
export type FinishedAt = string | null;
export type RunId9 = string;
export type Trigger1 = string;
export type Lines = number;
export type LinksOmitted = number;
export type Matches2 = number[];
export type Name38 = string;
export type NamesOmitted = number;
export type Chars = number;
export type Ename = string;
export type HasChart = boolean;
export type HasImage = boolean;
export type HasTable = boolean;
export type HasWidget = boolean;
export type Kinds = string[];
export type Truncated17 = boolean;
export type OutputOutdated = boolean;
export type Refs1 = string[];
export type Blob = {
  [k: string]: unknown;
} | null;
export type CutByBudget = boolean;
export type Note12 = string;
export type Tool = string;
export type NextOffset1 = number | null;
export type Offset2 = number;
export type Returned2 = number;
export type Total8 = number;
export type Unit1 = "line" | "char";
export type Status12 =
  | "fresh"
  | "edited"
  | "stale"
  | "not_run"
  | "queued"
  | "running"
  | "error"
  | "interrupted"
  | "skipped"
  | "stopped"
  | "disabled";
export type Upstream = string[];
export type Action = "clear_outputs" | "enable" | "disable" | "duplicate" | "move" | "set_kind";
/**
 * The cells to act on, each its id, name, position (Cell 3), first or last. Omit with clear_outputs to clear every cell.
 */
export type Cells2 = [string, ...string[]] | null;
/**
 * For set_kind: the new kind.
 */
export type Kind19 = ("python" | "sql" | "markdown") | null;
export type Path17 = string;
/**
 * For move: up, down, top or bottom.
 */
export type To = ("up" | "down" | "top" | "bottom") | null;
export type Action1 = "clear_outputs" | "enable" | "disable" | "duplicate" | "move" | "set_kind";
export type Blob1 = {
  [k: string]: unknown;
} | null;
export type Changed = string[];
export type Text4 = string;
export type Version = string;
export type Path18 = string;
export type RefType1 = string;
export type ResultName5 = string;
export type Stale = string[];
export type Token1 = string | null;
export type Kind20 = string;
export type Name39 = string | null;
export type Source7 = string;
/**
 * @maxItems 500
 */
export type Cells3 = NotebookNewCell[];
export type Path19 = string;
export type Blob2 = {
  [k: string]: unknown;
} | null;
export type Cells4 = NotebookCellBrief[];
export type Path20 = string;
export type RefType2 = string;
export type ResultName6 = string;
export type Token2 = string;
export type TotalCells = number;
export type BaseToken = string | null;
/**
 * @minItems 1
 * @maxItems 200
 */
export type Ops = [
  (
    | InsertCellOp
    | EditCellOp
    | ReplaceCellOp
    | DeleteCellOp
    | RestoreCellOp
    | MoveCellOp
    | RenameCellOp
    | SetCellKindOp
    | SetCellConfigOp
    | SetCellMetaOp
    | SetSettingOp
  ),
  ...(
    | InsertCellOp
    | EditCellOp
    | ReplaceCellOp
    | DeleteCellOp
    | RestoreCellOp
    | MoveCellOp
    | RenameCellOp
    | SetCellKindOp
    | SetCellConfigOp
    | SetCellMetaOp
    | SetSettingOp
  )[]
];
export type CellId4 = string;
export type Op5 = "replace";
export type Source8 = string;
export type After2 = string | null;
export type CellId5 = string;
export type Op6 = "restore";
export type CellId6 = string;
export type Name40 = string;
export type Op7 = "rename";
export type CellId7 = string;
export type Kind21 = string;
export type Op8 = "set_kind";
export type CellId8 = string;
export type Op9 = "set_config";
export type CellId9 = string;
export type Op10 = "set_meta";
export type Key4 = string;
export type Op11 = "set_setting";
export type Path21 = string;
export type Blob3 = {
  [k: string]: unknown;
} | null;
export type Cells5 = CellAfterOp[];
export type Created1 = string[];
export type Notices = CellNotice[];
export type Path22 = string;
export type RefType3 = string;
export type Repeat = boolean;
export type ResultName7 = string;
export type Stale1 = string[];
export type StaleTotal = number;
export type Token3 = string;
export type EnvId = string;
export type Kind22 = string;
export type Python = string;
export type RecordedInFile = boolean;
export type SpecRoot = string;
export type State11 = string;
export type Action2 = "info" | "list" | "packages" | "install" | "remove" | "materialize" | "cancel" | "switch";
export type Env = string | null;
export type Limit29 = number;
export type Offset3 = number;
/**
 * @maxItems 50
 */
export type Packages2 = string[];
export type Path23 = string;
export type Search3 = string | null;
export type Blob4 = {
  [k: string]: unknown;
} | null;
export type Envs = NotebookEnvInfo[];
export type Name41 = string;
export type Version1 = string;
export type Packages3 = NotebookPackage[];
export type Blob5 = {
  [k: string]: unknown;
} | null;
export type CutByBudget1 = boolean;
export type Limit30 = number;
export type NextOffset2 = number | null;
export type Offset4 = number;
export type Returned3 = number;
export type Total9 = number;
export type Path24 = string;
export type RefType4 = string;
export type ResultName8 = string;
export type SpecChanged = string[];
export type ColumnOffset = number;
export type Columns8 = string[];
export type Name42 = string;
export type Offset5 = number;
export type TotalColumns = number;
export type TotalRows1 = number;
export type Defs2 = string[];
export type Distance = number | null;
export type Name43 = string;
export type Refs2 = string[];
export type Status13 =
  | "fresh"
  | "edited"
  | "stale"
  | "not_run"
  | "queued"
  | "running"
  | "error"
  | "interrupted"
  | "skipped"
  | "stopped"
  | "disabled";
export type CellId10 = string;
export type Kind23 = string;
export type Names = string[];
export type Cell = string | null;
export type Depth = number | "all";
export type Direction3 = "both" | "up" | "down";
export type Limit31 = number;
export type Offset6 = number;
export type Path25 = string;
export type Summary1 = boolean | null;
export type Blob6 = {
  [k: string]: unknown;
} | null;
export type Complete = boolean;
export type Downstream1 = string[];
export type DownstreamDirect = string[];
export type DownstreamTotal = number;
export type DownstreamTransitive = string[];
export type Edges3 = [unknown, unknown, unknown][];
export type Errors1 = NotebookGraphError[];
export type ErrorsTotal = number;
export type Path26 = string;
export type RefType5 = string;
export type ResultName9 = string;
export type Cells7 = number;
export type Edges4 = number;
export type Errors2 = number;
export type FirstRoots = string[];
export type Leaves1 = number;
export type Roots1 = number;
export type Upstream1 = string[];
export type UpstreamDirect = string[];
export type UpstreamTotal = number;
export type UpstreamTransitive = string[];
export type Bytes1 = number;
export type DataBase64 = string | null;
export type Index5 = number;
export type Mime = string;
export type Sha2561 = string;
export type ColumnLimit = number;
export type ColumnOffset1 = number;
export type FilterSql = string | null;
export type Limit32 = number;
export type Name44 = string | null;
export type Offset7 = number;
export type Path27 = string;
export type Sort = string | null;
export type What = "variables" | "frame" | "value";
export type Blob7 = {
  [k: string]: unknown;
} | null;
export type Path28 = string;
export type RefType6 = string;
export type ResultName10 = string;
export type Variables = NotebookVariable[] | null;
export type CellId11 = string | null;
export type Columns9 = string[] | null;
export type ColumnsTotal = number | null;
export type Name45 = string;
export type Shape = number[] | null;
export type SizeBytes2 = number | null;
export type Type4 = string;
export type What1 = "variables" | "frame" | "value";
export type MemoryBytes = number | null;
export type By2 = string;
export type RunId10 = string;
export type Status14 = string;
export type Trigger2 = string;
export type Queue = NotebookQueuedRun[];
export type QueueTotal = number;
export type Reactivity = "autorun" | "lazy";
export type StartedAt1 = string | null;
export type State12 = "absent" | "starting" | "idle" | "busy" | "restarting" | "stopped";
/**
 * status; interrupt (the running cell); interrupt_all (the running cell, and drop every queued run); restart (clears every value); shutdown (stops the kernel).
 */
export type Action3 = "status" | "interrupt" | "interrupt_all" | "restart" | "shutdown";
export type Path29 = string;
export type Blob8 = {
  [k: string]: unknown;
} | null;
export type Path30 = string;
export type RefType7 = string;
export type ResultName11 = string;
export type By3 = string;
export type RunId11 = string;
export type Status15 = string;
export type Runs2 = NotebookRunBrief[];
/**
 * A cell: its id, its name, its position (Cell 3), first or last.
 */
export type Cell1 = string;
export type ItemLimit = number;
export type ItemOffset = number;
export type LineLimit = number;
export type LineOffset = number | null;
export type MaxChars = number;
export type Part = "all" | "text" | "error" | "image" | "chart" | "table" | "widget";
export type Path31 = string;
export type RowLimit = number;
export type RowOffset = number;
export type TextOffset = number;
export type Blob9 = {
  [k: string]: unknown;
} | null;
export type CellId12 = string;
export type Images = NotebookImage[];
export type Path32 = string;
export type RefType8 = string;
export type ResultName12 = string;
export type Truncated18 = boolean;
export type CellId13 = string | null;
export type ModelId = string;
export type Type5 = string;
export type Widgets = NotebookWidgetState[];
export type CellId14 = string;
export type Name46 = string;
export type Reason2 = "target" | "upstream" | "descendant";
export type CellId15 = string | null;
export type Who = string;
export type Cells8 = string[] | null;
export type IncludeOutputs = boolean;
export type IncludeSource = boolean | null;
export type Limit33 = number;
export type Offset8 = number;
export type Path33 = string;
export type Regex1 = boolean;
export type Search4 = string | null;
export type SourceLimit = number;
export type SourceOffset = number;
export type Status16 =
  | []
  | [
      | "fresh"
      | "edited"
      | "stale"
      | "not_run"
      | "queued"
      | "running"
      | "error"
      | "interrupted"
      | "skipped"
      | "stopped"
      | "disabled"
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | [
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      ),
      (
        | "fresh"
        | "edited"
        | "stale"
        | "not_run"
        | "queued"
        | "running"
        | "error"
        | "interrupted"
        | "skipped"
        | "stopped"
        | "disabled"
      )
    ]
  | null;
export type Blob10 = {
  [k: string]: unknown;
} | null;
export type Cells9 = NotebookCellState[];
export type Matched1 = number;
export type Path34 = string;
export type Presence = NotebookPresence[];
export type RefType9 = string;
export type ResultName13 = string;
export type Token4 = string;
export type TotalCells1 = number;
export type ConfirmExpensive = boolean;
export type Path35 = string;
export type Target13 = RunCells | RunAll | RunStale | RunAbove | RunBelow | RunUpstream | RunDownstream;
/**
 * A cell: its id, its name, its position (Cell 3), first or last.
 *
 * @minItems 1
 * @maxItems 500
 */
export type Ids = [string, ...string[]];
export type Kind24 = "cells";
export type Kind25 = "all";
/**
 * Restart the kernel first, so every cell runs from a clean state.
 */
export type Restart = boolean;
export type Kind26 = "stale";
/**
 * A cell: its id, its name, its position (Cell 3), first or last.
 */
export type Id16 = string;
export type Kind27 = "above";
/**
 * A cell: its id, its name, its position (Cell 3), first or last.
 */
export type Id17 = string;
export type Kind28 = "below";
/**
 * A cell: its id, its name, its position (Cell 3), first or last.
 */
export type Id18 = string;
export type Kind29 = "upstream";
/**
 * A cell: its id, its name, its position (Cell 3), first or last.
 */
export type Id19 = string;
export type Kind30 = "downstream";
export type TimeoutS = number;
export type Wait = boolean;
export type Blob11 = {
  [k: string]: unknown;
} | null;
export type Cells10 = NotebookCellState[];
export type EstimateS = number | null;
export type Failures = NotebookCellState[];
export type Path36 = string;
export type Plan = NotebookPlanStep[];
export type PlanTotal = number;
export type QueuedBehind = string[];
export type Reactivity1 = "autorun" | "lazy";
export type RefType10 = string;
export type ResultName14 = string;
export type RunId12 = string | null;
export type See = NotebookMore[];
export type Status17 = "finished" | "running" | "needs_confirmation";
export type Summary2 = string;
export type Autoreload = ("off" | "on") | null;
export type Dataframe = ("polars" | "pandas" | "auto") | null;
export type Env1 = string | null;
export type OutputsInGit = boolean | null;
export type Path37 = string;
export type Reactivity2 = ("autorun" | "lazy") | null;
/**
 * The most rows kept from a SQL query that has no LIMIT of its own.
 */
export type SqlRowLimit = number | null;
export type Blob12 = {
  [k: string]: unknown;
} | null;
export type Path38 = string;
export type RefType11 = string;
export type ResultName15 = string;
/**
 * A cell: its id, its name, its position (Cell 3), first or last.
 */
export type Cell2 = string;
export type Image = number;
export type Part1 = "auto" | "table" | "chart" | "image" | "markdown" | "text";
export type Path39 = string;
export type RowLimit1 = number;
export type Available = ("table" | "chart" | "image" | "markdown" | "text" | "error")[];
export type Blob13 = {
  [k: string]: unknown;
} | null;
export type CellId16 = string;
export type CellName = string;
export type Bytes2 = number;
export type Sha2562 = string;
export type Bytes3 = number;
export type Index6 = number;
export type Mime1 = string;
export type Sha2563 = string;
export type Total10 = number;
export type Kind31 = "table" | "chart" | "image" | "markdown" | "text" | "error";
export type Note13 = string;
export type Path40 = string;
export type RefType12 = string;
export type ResultName16 = string;
export type Columns10 = string[];
export type ShownRows = number;
export type TotalColumns1 = number;
export type TotalRows2 = number;
export type Action4 = "list" | "get" | "set";
export type Limit34 = number;
export type ModelId1 = string | null;
export type Offset9 = number;
export type Path41 = string;
export type State13 = {
  [k: string]: unknown;
} | null;
export type Blob14 = {
  [k: string]: unknown;
} | null;
export type Path42 = string;
export type RefType13 = string;
export type ResultName17 = string;
export type Widgets1 = NotebookWidgetState[];
/**
 * Postgres connection handle.
 */
export type Connection73 = string;
/**
 * Max sessions to return.
 */
export type Limit35 = number;
export type ApplicationName = string;
export type BackendType = string;
export type Datname = string;
export type Pid = number;
export type Query9 = string;
export type QueryStart = string;
export type State14 = string;
export type Usename = string;
export type WaitEventType = string;
export type Sessions = PgActivityRow[];
export type Truncated19 = boolean;
/**
 * Postgres connection handle.
 */
export type Connection74 = string;
/**
 * The backend pid to cancel (from postgres.activity).
 */
export type Pid1 = number;
/**
 * pg_cancel_backend's verdict: true when the cancel signal was sent; false when no backend has that pid or this connection's role is not permitted to cancel it.
 */
export type Cancelled1 = boolean;
export type Pid2 = number;
export type Requested6 = string;
export type Datallowconn = boolean;
export type Datname1 = string;
export type Encoding = string;
export type Owner4 = string;
/**
 * Postgres connection handle (call sql.connections).
 */
export type Connection75 = string;
export type Databases2 = PgDatabaseRow[];
export type Truncated20 = boolean;
export type ApproxRows = number;
export type Kind32 = string;
export type Name47 = string;
export type SchemaName3 = string;
export type TotalBytes2 = number;
/**
 * Postgres connection handle.
 */
export type Connection76 = string;
/**
 * Max relations to return.
 */
export type Limit36 = number;
export type Relations = PgTableSizeRow[];
export type Truncated21 = boolean;
/**
 * Bucket; defaults to the connection's bucket.
 */
export type Bucket10 = string;
/**
 * AWS connection handle.
 */
export type Connection77 = string;
/**
 * Lifetime in seconds; capped at 3600.
 */
export type ExpiresIn = number;
/**
 * Object key to presign.
 */
export type Key5 = string;
export type Bucket11 = string;
export type ExpiresIn1 = number;
export type Key6 = string;
export type Url2 = string;
/**
 * Bucket; defaults to the connection's bucket.
 */
export type Bucket12 = string;
/**
 * AWS connection handle.
 */
export type Connection78 = string;
/**
 * Force the format instead of inferring it from the key.
 */
export type FileFormat = "auto" | "csv" | "jsonl" | "json" | "parquet";
/**
 * Object key of the CSV / JSONL / JSON / Parquet file.
 */
export type Key7 = string;
/**
 * Sample rows to return (0 = schema).
 */
export type MaxRows = number;
export type Bucket13 = string;
export type Columns11 = ColumnInfo[];
export type Format3 = string;
export type Key8 = string;
export type Note14 = string;
export type Rows3 = unknown[][];
export type SampledRows = number;
export type Truncated22 = boolean;
/**
 * Bucket; defaults to the connection's bucket.
 */
export type Bucket14 = string;
/**
 * AWS connection handle.
 */
export type Connection79 = string;
/**
 * UTF-8 text to write as the object body.
 */
export type Content1 = string;
/**
 * Content-Type to store.
 */
export type ContentType2 = string;
/**
 * Destination object key.
 */
export type Key9 = string;
export type Bucket15 = string;
export type Etag1 = string;
export type Key10 = string;
export type Size2 = number;
export type VersionId4 = string;
export type Connection80 = string;
/**
 * The SQL boolean the qualifier stands for.
 */
export type Predicate = string;
/**
 * A table name, or a read-only SELECT in parentheses, naming the population.
 */
export type Source9 = string;
export type Note15 = string;
export type RowsAfter = number;
export type RowsBefore = number;
export type RowsRemoved = number;
/**
 * Run this query in the BACKGROUND: return a job id immediately and notify you with the rows when it completes. Use for a heavy READ query whose result you don't need right away. Only read-only queries can be backgrounded (run a write in the foreground). NOTE: a backgrounded query is cost-checked up front with no prompt — if it would exceed a cap it is REFUSED, not queued; run it in the foreground to be prompted to raise the cap. Don't poll; you'll be notified.
 */
export type Background2 = boolean;
export type Connection81 = string;
export type Limit37 = number;
export type Mode15 = "sql";
/**
 * Values for placeholders in sql, bound by the driver and never written into the statement: %(name)s on Postgres/Redshift, {name:Type} on ClickHouse, {{String(name)}} (or Int64/Float64/Date/Boolean) on Tinybird — whose SQL API reads a ClickHouse {name:Type} as a workspace secret and refuses it — $name on DuckDB, :name on SQLite. One scalar per placeholder — a string, number, boolean, date or null; a list is spelled as one placeholder per item. Prefer a placeholder over quoting a value into the SQL when the value comes from data or from the user.
 */
export type Params1 = {
  [k: string]: string | number | boolean | null;
} | null;
/**
 * A SHORT, human-friendly name for the result you expect (5 words or fewer, e.g. 'Q3 revenue by region', 'failed login counts'). Always provide it: when the result is large it's stored and shown under this name in the editor, so a good label makes it findable. Describe the result, not the query.
 */
export type ResultName18 = string;
export type Sql2 = string;
/**
 * Run this query in the BACKGROUND: return a job id immediately and notify you with the rows when it completes. Use for a heavy READ query whose result you don't need right away. Only read-only queries can be backgrounded (run a write in the foreground). NOTE: a backgrounded query is cost-checked up front with no prompt — if it would exceed a cap it is REFUSED, not queued; run it in the foreground to be prompted to raise the cap. Don't poll; you'll be notified.
 */
export type Background3 = boolean;
export type Columns12 = string[] | null;
export type Connection82 = string;
export type Limit38 = number;
export type Mode16 = "table";
/**
 * A SHORT, human-friendly name for the result you expect (5 words or fewer, e.g. 'Q3 revenue by region', 'failed login counts'). Always provide it: when the result is large it's stored and shown under this name in the editor, so a good label makes it findable. Describe the result, not the query.
 */
export type ResultName19 = string;
export type Table3 = string;
export type Database5 = string;
export type Engine8 = string;
export type EngineMode1 = string;
export type EngineVersion5 = string;
export type Host7 = string;
export type IamAuthEnabled3 = boolean;
export type Identifier15 = string;
export type Members1 = string[];
export type MultiAz3 = boolean;
export type Port6 = number;
export type ReaderHost1 = string;
export type Status18 = string;
export type StorageEncrypted2 = boolean;
/**
 * AWS connection handle.
 */
export type Connection83 = string;
/**
 * Max clusters to return.
 */
export type Limit39 = number;
/**
 * AWS region; defaults to the connection's.
 */
export type Region15 = string;
export type Clusters1 = RdsClusterCard[];
export type Region16 = string;
/**
 * Bucket; defaults to the connection's bucket.
 */
export type Bucket16 = string;
/**
 * AWS connection handle.
 */
export type Connection84 = string;
/**
 * Object key to read.
 */
export type Key11 = string;
/**
 * Bytes to read; hard-capped at 1000000.
 */
export type Length = number;
/**
 * Byte offset to start reading from.
 */
export type Offset10 = number;
export type Binary = boolean;
export type Bucket17 = string;
export type Key12 = string;
export type NextOffset3 = number | null;
export type Offset11 = number;
export type ReturnedBytes = number;
export type Text5 = string;
export type TotalSize = number;
export type Truncated23 = boolean;
export type Kind33 = string;
export type Name48 = string;
export type Urn11 = string | null;
/**
 * Redshift connection handle (call sql.connections).
 */
export type Connection85 = string;
/**
 * The process id of the session running the query — sys_query_history's session_id (also stv_recents' pid). NOT the query_id.
 */
export type Pid3 = number;
export type Pid4 = number;
export type Requested7 = string;
export type AllowConnections = boolean;
export type Name49 = string;
/**
 * Redshift connection handle (call sql.connections).
 */
export type Connection86 = string;
export type Databases3 = RsDatabaseRow[];
/**
 * Redshift connection handle (call sql.connections).
 */
export type Connection87 = string;
/**
 * When true, include finished queries too — recent history newest first, not just running/queued.
 */
export type IncludeRecent = boolean;
/**
 * Max queries to return.
 */
export type Limit40 = number;
export type QueryId4 = number;
export type QueryText = string;
export type SessionId = number;
export type StartTime = string;
export type Status19 = string;
export type UserId = number;
export type Queries = RsRunningQueryRow[];
export type Truncated24 = boolean;
/**
 * Redshift connection handle (call sql.connections).
 */
export type Connection88 = string;
/**
 * Max tables to return.
 */
export type Limit41 = number;
/**
 * Only tables in this exact schema (default: every non-internal schema).
 */
export type SchemaFilter = string | null;
export type Diststyle = string;
export type PctUsed = number;
export type Rows4 = number;
export type SchemaName4 = string;
export type SizeMb = number;
export type Sortkey1 = string;
export type TableName1 = string;
export type Tables6 = RsTableInfoRow[];
export type Truncated25 = boolean;
/**
 * Databricks connection handle.
 */
export type Connection89 = string;
/**
 * The id of the job to trigger (from databricks.list_jobs).
 */
export type JobId16 = number;
export type RunId13 = number;
/**
 * The connection handle from sql.connections, e.g. 'pg'.
 */
export type Connection90 = string;
export type Mode17 = "describe";
/**
 * The table or view to describe, as a top-level string: bare ('orders') or schema-qualified ('public.orders'). Required when mode='describe'.
 */
export type Table4 = string;
/**
 * The connection handle from sql.connections, e.g. 'pg'.
 */
export type Connection91 = string;
export type Mode18 = "list";
/**
 * Include each relation's URN and the engine's own metadata schemas. Off by default: a listing is read by a model that pays for every row, and a URN restates the name beside it.
 */
export type Verbose = boolean;
export type Snippet1 = string;
export type Title14 = string;
export type Url3 = string;
export type App = string | null;
export type K1 = number;
export type Query10 = string;
export type App1 = string | null;
export type Description13 = string;
export type Name50 = string;
export type Tools = ToolCard[];
/**
 * The new comment/description (empty string clears it).
 */
export type Comment7 = string;
/**
 * Databricks connection handle.
 */
export type Connection92 = string;
/**
 * The securable's name — the catalog name, or 'catalog.schema' for a schema.
 */
export type FullName7 = string;
/**
 * The kind of securable to comment on.
 */
export type SecurableType2 = "catalog" | "schema";
export type Comment8 = string;
export type FullName8 = string;
export type SecurableType3 = string;
/**
 * Snowflake connection handle.
 */
export type Connection93 = string;
/**
 * The query id to cancel (from snowflake.running_queries).
 */
export type QueryId5 = string;
export type QueryId6 = string;
export type Result1 = string;
/**
 * Snowflake connection handle.
 */
export type Connection94 = string;
/**
 * Max refresh runs to return.
 */
export type Limit42 = number;
/**
 * Optional dynamic table to filter to — DB.SCHEMA.NAME (from snowflake.list_dynamic_tables). Empty = every dynamic table in the session database.
 */
export type Name51 = string;
export type FullName9 = string;
export type Name52 = string;
export type RefreshAction = string;
export type RefreshEndTime = string;
export type RefreshStartTime = string;
export type RefreshTrigger = string;
export type State15 = string;
export type StateCode = string;
export type StateMessage1 = string;
export type TargetLagSec = string;
export type Runs3 = SfDtRefreshRun[];
export type Truncated26 = boolean;
export type Name53 = string;
export type Requested8 = string;
export type Bytes4 = string;
export type FullName10 = string;
export type Name54 = string;
export type RefreshMode = string;
export type Rows5 = string;
export type SchedulingState = string;
export type TargetLag = string;
export type Warehouse = string;
/**
 * Snowflake connection handle.
 */
export type Connection95 = string;
/**
 * The dynamic table — DB.SCHEMA.NAME (from snowflake.list_dynamic_tables).
 */
export type Name55 = string;
/**
 * Snowflake connection handle (call sql.connections).
 */
export type Connection96 = string;
/**
 * Max dynamic tables to return.
 */
export type Limit43 = number;
export type DynamicTables = SfDynamicTableCard[];
export type Truncated27 = boolean;
/**
 * Snowflake connection handle (call sql.connections).
 */
export type Connection97 = string;
/**
 * Max materialized views to return.
 */
export type Limit44 = number;
export type BehindBy = string;
export type Comment9 = string;
export type FullName11 = string;
export type Invalid = string;
export type InvalidReason = string;
export type IsSecure = string;
export type Name56 = string;
export type RefreshedOn = string;
export type MaterializedViews = SfMaterializedViewCard[];
export type Truncated28 = boolean;
/**
 * Snowflake connection handle (call sql.connections).
 */
export type Connection98 = string;
export type Name57 = string;
export type Size3 = string;
export type State16 = string;
export type Warehouses1 = SfWarehouseCard[];
/**
 * Snowflake connection handle.
 */
export type Connection99 = string;
/**
 * Max queries to return.
 */
export type Limit45 = number;
export type ExecutionStatus = string;
export type QueryId7 = string;
export type QueryText1 = string;
export type StartTime1 = string;
export type UserName1 = string;
export type WarehouseName = string;
export type Queries1 = SfRunningQueryRow[];
export type Truncated29 = boolean;
export type Requested9 = string;
export type Warehouse1 = string;
/**
 * Snowflake connection handle.
 */
export type Connection100 = string;
/**
 * The warehouse name (from snowflake.list_warehouses).
 */
export type Warehouse2 = string;
export type ElementId = string;
export type Error4 = string;
export type Name58 = string;
export type Sql3 = string;
export type Urn12 = string;
/**
 * The Sigma connection handle to list workbooks from.
 */
export type Connection101 = string;
/**
 * Optional case-insensitive filter on a workbook's name, folder path, or id.
 */
export type Search5 = string;
export type Note16 = string;
export type Total11 = number;
export type Folder = string;
export type Name59 = string;
export type Urn13 = string;
export type WorkbookId = string;
export type Workbooks = SigmaWorkbookCard[];
/**
 * The Sigma connection handle.
 */
export type Connection102 = string;
/**
 * The workbook to inspect (an id from sigma.list_workbooks).
 */
export type WorkbookId1 = string;
export type Elements = SigmaElementSqlCard[];
export type Urn14 = string;
export type WorkbookId2 = string;
export type Description14 = string;
export type Name60 = string;
export type Agent = string;
export type Background4 = boolean;
export type Description15 = string | null;
export type Prompt = string;
export type ChildSessionId1 = string;
export type Summary3 = string;
export type Connections1 = ConnectionCard[];
export type ConnectionId = string;
export type ConnectionName = string;
export type DurationMs = number | null;
export type Engine9 = string;
export type ExecutedAt = string;
export type JoinColumns = string[];
export type Role1 = string;
export type RowCount2 = number;
export type Sources2 = SqlProvenance[];
export type Sql4 = string;
/**
 * Two legal call signatures: raw ``sql`` or a structured ``table`` read.
 * The discriminator ``mode`` makes both shapes visible to the model and the
 * ``match`` exhaustive to the type-checker. A call that omits ``mode`` (the
 * shape the flattened wire schema invites) is accepted, the tag inferred
 * from which cue field it carries.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SqlQueryInput".
 */
export type SqlQueryInput = QueryBySql | QueryByTable;
export type Columns13 = string[];
export type CostWarnings1 = string[];
export type JobId17 = string;
export type Note17 = string;
export type PreviewRows = unknown[][];
export type RefType14 = string;
export type ResultName20 = string;
export type RowCount3 = number;
export type Truncated30 = boolean;
/**
 * List relations on a connection, or describe one table's columns. A call
 * that omits ``mode`` describes when it names a ``table`` and lists otherwise.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SqlSchemaInput".
 */
export type SqlSchemaInput = SchemaList | SchemaDescribe;
export type Columns14 = ColumnCard[];
export type Relations1 = RelationCard[];
export type Table5 = string | null;
/**
 * AWS connection handle to authenticate (call sql.connections to list).
 */
export type Connection103 = string;
/**
 * Sign in again even if the cached SSO session is still valid. Leave false so an existing `aws sso login` is reused.
 */
export type Force = boolean;
export type Accounts = string[];
export type AlreadyValid = boolean;
export type Connection104 = string;
export type ExpiresAt = string;
export type SecondsRemaining = number;
export type SsoStartUrl = string;
export type UserCode = string;
export type VerificationUri = string;
/**
 * Databricks connection handle.
 */
export type Connection105 = string;
/**
 * Recompute the pipeline from scratch (reprocess all data) instead of an incremental update.
 */
export type FullRefresh = boolean;
/**
 * The pipeline id to update (from list_pipelines).
 */
export type PipelineId3 = string;
export type FullRefresh1 = boolean;
export type PipelineId4 = string;
export type UpdateId = string;
export type ObjectCount = number;
export type Prefix4 = string;
export type TotalBytes3 = number;
/**
 * Bucket; defaults to the connection's bucket.
 */
export type Bucket18 = string;
/**
 * AWS connection handle.
 */
export type Connection106 = string;
/**
 * Prefix path depth to group totals by (0 = no grouping).
 */
export type GroupDepth = number;
/**
 * Scan bound when metrics fail.
 */
export type MaxKeys2 = number;
/**
 * Only summarize keys under this prefix.
 */
export type Prefix5 = string;
export type Bucket19 = string;
export type ByPrefix = StorageGroup[];
export type ObjectCount1 = number;
export type Prefix6 = string;
export type Source10 = string;
export type TotalBytes4 = number;
export type Truncated31 = boolean;
/**
 * Trino connection handle.
 */
export type Connection107 = string;
/**
 * The query id to kill (from trino.running_queries).
 */
export type QueryId8 = string;
export type QueryId9 = string;
export type Requested10 = string;
/**
 * Trino connection handle (call sql.connections).
 */
export type Connection108 = string;
export type Catalogs1 = string[];
/**
 * The catalog to list schemas in (from trino.list_catalogs).
 */
export type Catalog5 = string;
/**
 * Trino connection handle.
 */
export type Connection109 = string;
/**
 * Max schemas to return.
 */
export type Limit46 = number;
export type Schemas1 = string[];
export type Truncated32 = boolean;
export type Query11 = string;
export type QueryId10 = string;
export type Source11 = string;
export type Started = string;
export type State17 = string;
export type User2 = string;
/**
 * Trino connection handle.
 */
export type Connection110 = string;
/**
 * Max queries to return.
 */
export type Limit47 = number;
export type Queries2 = TrinoQueryCard[];
export type Truncated33 = boolean;
/**
 * Databricks connection handle.
 */
export type Connection111 = string;
/**
 * The securable's full name (e.g. 'main' for a catalog, 'main.default' for a schema, 'main.default.orders' for a table).
 */
export type FullName12 = string;
/**
 * Privileges to GRANT, e.g. ['SELECT', 'USE_SCHEMA']. Empty to only revoke.
 */
export type Grant = string[];
/**
 * The user email, service-principal id, or account group name to change privileges for. Must be a REAL principal — Unity Catalog rejects system-generated groups (e.g. names starting with an underscore like '_workspace_users_…').
 */
export type Principal1 = string;
/**
 * Privileges to REVOKE, e.g. ['MODIFY']. Empty to only grant.
 */
export type Revoke = string[];
/**
 * The kind of Unity Catalog securable to change grants on.
 */
export type SecurableType4 =
  | "catalog"
  | "schema"
  | "table"
  | "function"
  | "volume"
  | "external_location"
  | "storage_credential";
export type FullName13 = string;
export type Granted = string[];
export type Principal2 = string;
export type Revoked = string[];
export type SecurableType5 = string;
export type Name61 = string | null;
export type Body3 = string | null;
export type Name62 = string | null;
export type Skills = SkillCard[];
export type MaxChars1 = number;
export type Url4 = string;
export type Content2 = string;
export type Status20 = number;
export type Title15 = string;
export type TotalChars = number;
export type Truncated34 = boolean;
export type Url5 = string;
export type Backend = string;
export type MaxResults = number;
export type Query12 = string;
export type Region17 = string;
export type Timelimit = string | null;
export type Query13 = string;
export type Results = SearchResult[];
/**
 * The schema change to classify.
 */
export type Kind34 =
  | "drop_column"
  | "add_column"
  | "rename_column"
  | "narrow_type"
  | "widen_type"
  | "change_type"
  | "make_not_null"
  | "make_nullable"
  | "drop_table";
/**
 * The replacement column name for rename_column.
 */
export type NewName = string;
/**
 * The proposed type for a type change.
 */
export type NewType = string;
/**
 * The current type for a type change.
 */
export type OldType = string;
/**
 * The column URN to change, or the table URN for drop_table or add_column.
 */
export type Target14 = string;
/**
 * Optional asset kind, such as table, view, model, job, or dashboard.
 */
export type Kind35 = string | null;
/**
 * The plain asset name to find in the lineage graph.
 */
export type Query14 = string;
/**
 * Collection for collection operations
 */
export type Collection1 = string;
/**
 * MongoDB connection handle
 */
export type Connection112 = string;
/**
 * Database for this operation
 */
export type Database6 = string;
/**
 * Also read what is filed on the nodes ONE hop up and down. Worth it before you explain or change an asset: the reason a mart's numbers look wrong is usually filed on the staging table feeding it, not on the mart.
 */
export type IncludeNeighbors = boolean;
/**
 * The lineage node whose knowledge to read, from a lineage_find match or any lineage result.
 */
export type Urn15 = string;
/**
 * Elasticsearch connection handle
 */
export type Connection113 = string;
/**
 * Optional index, stream, or transform pattern
 */
export type Target15 = string;
/**
 * Which way to walk: up for what feeds this asset, down for what reads it, both for the surrounding neighborhood.
 */
export type Direction4 = "up" | "down" | "both";
/**
 * relation walks asset to asset. column follows one column's flow, which exists only where the model or view SQL was parsed.
 */
export type Grain = "relation" | "column";
/**
 * How many hops to walk, at most 10. Each hop is one dependency step.
 */
export type MaxHops3 = number;
/**
 * The asset to walk from. A column URN (warehouse://db.schema.table#column) is walked at column grain whatever grain says, because only the column graph holds that node.
 */
export type Urn16 = string;
/**
 * How many hops to walk, at most 10. Each hop is one dependency step.
 */
export type MaxHops4 = number;
/**
 * The asset URN whose downstream impact to inspect.
 */
export type Urn17 = string;

export interface AlkeraToolManifest {}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "AffectedCard".
 */
export interface AffectedCard {
  category: Category;
  node_type?: NodeType;
  reason?: Reason;
  transformation?: Transformation;
  urn: Urn;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "AgentTypeInfo".
 */
export interface AgentTypeInfo {
  description: Description;
  input_schema: InputSchema;
  name: Name;
}
export interface InputSchema {
  [k: string]: unknown;
}
/**
 * The tool/usage counts a spawn result carries back to the parent.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "AgentUsageStats".
 */
export interface AgentUsageStats {
  by_tool?: ByTool;
  duration_seconds?: DurationSeconds;
  files_read?: FilesRead;
  model?: Model;
  tool_calls?: ToolCalls;
  truncated?: Truncated;
}
export interface ByTool {
  [k: string]: number;
}
/**
 * One asset the item governs, with what that asset is to the meaning.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "AttachmentInput".
 */
export interface AttachmentInput {
  note?: Note;
  role?: Role;
  urn: Urn1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BackgroundCancelInput".
 */
export interface BackgroundCancelInput {
  job_id: JobId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BackgroundCancelResult".
 */
export interface BackgroundCancelResult {
  cancelled: Cancelled;
  job_id: JobId1;
  state: State;
}
/**
 * One job's state for ``background_status`` — relative times only (the repo
 * convention, via ``format_age_ago``), never raw timestamps.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BackgroundJobView".
 */
export interface BackgroundJobView {
  child_session_id?: ChildSessionId;
  completed_ago?: CompletedAgo;
  error?: Error;
  job_id: JobId2;
  kind: Kind;
  output_preview?: OutputPreview;
  started_ago?: StartedAgo;
  state: State1;
  title?: Title;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BackgroundStatusInput".
 */
export interface BackgroundStatusInput {
  job_id?: JobId3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BackgroundStatusResult".
 */
export interface BackgroundStatusResult {
  jobs?: Jobs;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BashInput".
 */
export interface BashInput {
  background?: Background;
  command: Command;
  description: Description1;
  timeout?: Timeout;
  workdir?: Workdir;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BashResult".
 */
export interface BashResult {
  exit_code?: ExitCode;
  job_id?: JobId4;
  note?: Note1;
  output?: Output;
  output_path?: OutputPath;
  truncated?: Truncated1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobCreateInput".
 */
export interface BlobCreateInput {
  columns?: Columns;
  result_name?: ResultName;
  rows?: Rows;
  text?: Text;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobCreateOutput".
 */
export interface BlobCreateOutput {
  blob: BlobHandle;
  kind: Kind1;
  result_name?: ResultName1;
  total?: Total;
}
/**
 * A pointer to a spilled result in the content-addressed blob store. Handed
 * to the model / editor in place of an oversized inline payload; dereferenced
 * via the ``fetch_result`` tool or the ``blob.fetch`` RPC.
 *
 * Persisted ⇒ ``VersionedModel``: the handle rides a promoted result's spec
 * into the cloud, so a field a newer daemon adds beside the digest has to
 * survive an older reader instead of being dropped on the way through.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobHandle".
 */
export interface BlobHandle {
  media_type?: MediaType;
  metadata?: Metadata;
  schema_version?: SchemaVersion;
  sha256: Sha256;
  size: Size;
  [k: string]: unknown;
}
export interface Metadata {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobDeleteInput".
 */
export interface BlobDeleteInput {
  handle: Handle;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobDeleteOutput".
 */
export interface BlobDeleteOutput {
  deleted: Deleted;
  freed_bytes?: FreedBytes;
  message?: Message;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobDeriveInput".
 */
export interface BlobDeriveInput {
  descending?: Descending;
  distinct?: Distinct;
  handle: Handle1;
  limit?: Limit;
  order_by?: OrderBy;
  result_name?: ResultName2;
  select_columns?: SelectColumns;
  where?: Where;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobInfoInput".
 */
export interface BlobInfoInput {
  handle: Handle2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobInfoOutput".
 */
export interface BlobInfoOutput {
  columns?: Columns1;
  content_type?: ContentType;
  kind: Kind2;
  size_bytes?: SizeBytes;
  total?: Total1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobMaterializeInput".
 */
export interface BlobMaterializeInput {
  filename?: Filename;
  format?: Format;
  handle: Handle3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobMaterializeOutput".
 */
export interface BlobMaterializeOutput {
  bytes?: Bytes;
  char_count?: CharCount;
  columns?: Columns2;
  format: Format1;
  path: Path;
  row_count?: RowCount;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobProfileInput".
 */
export interface BlobProfileInput {
  handle: Handle4;
  top_k?: TopK;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobProfileOutput".
 */
export interface BlobProfileOutput {
  columns?: Columns3;
  row_count?: RowCount1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ColumnProfile".
 */
export interface ColumnProfile {
  distinct_capped?: DistinctCapped;
  distinct_count?: DistinctCount;
  dtype: Dtype;
  max?: Max;
  min?: Min;
  name: Name1;
  null_count?: NullCount;
  top_k?: TopK1;
}
export interface Max {
  [k: string]: unknown;
}
export interface Min {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TopValue".
 */
export interface TopValue {
  count?: Count;
  value?: Value;
}
export interface Value {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BlobQueryInput".
 */
export interface BlobQueryInput {
  handle: Handle5;
  result_name?: ResultName3;
  sql: Sql;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqCancelJobInput".
 */
export interface BqCancelJobInput {
  connection: Connection;
  job_id: JobId5;
  location?: Location;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqCancelJobResult".
 */
export interface BqCancelJobResult {
  job_id: JobId6;
  requested?: Requested;
  state?: State2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqDatasetCard".
 */
export interface BqDatasetCard {
  dataset_id: DatasetId;
  location?: Location1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqFieldCard".
 */
export interface BqFieldCard {
  description?: Description2;
  mode?: Mode;
  name: Name2;
  type?: Type;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqGetJobInput".
 */
export interface BqGetJobInput {
  connection: Connection1;
  job_id: JobId7;
  location?: Location2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqGetJobResult".
 */
export interface BqGetJobResult {
  cache_hit?: CacheHit;
  creation_time_ms?: CreationTimeMs;
  end_time_ms?: EndTimeMs;
  error_message?: ErrorMessage;
  job_id: JobId8;
  query?: Query;
  start_time_ms?: StartTimeMs;
  state?: State3;
  total_bytes_processed?: TotalBytesProcessed;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqGetTableInput".
 */
export interface BqGetTableInput {
  connection: Connection2;
  dataset: Dataset;
  table: Table;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqGetTableResult".
 */
export interface BqGetTableResult {
  dataset: Dataset1;
  description?: Description3;
  fields?: Fields;
  num_bytes?: NumBytes;
  num_rows?: NumRows;
  table: Table1;
  type?: Type1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqJobCard".
 */
export interface BqJobCard {
  creation_time_ms?: CreationTimeMs1;
  job_id: JobId9;
  location?: Location3;
  query?: Query1;
  state?: State4;
  total_bytes_processed?: TotalBytesProcessed1;
  user_email?: UserEmail;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqListDatasetsInput".
 */
export interface BqListDatasetsInput {
  connection: Connection3;
  limit?: Limit1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqListDatasetsResult".
 */
export interface BqListDatasetsResult {
  datasets?: Datasets;
  truncated?: Truncated2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqListJobsInput".
 */
export interface BqListJobsInput {
  all_users?: AllUsers;
  connection: Connection4;
  limit?: Limit2;
  state_filter?: StateFilter;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqListJobsResult".
 */
export interface BqListJobsResult {
  jobs?: Jobs1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqListTablesInput".
 */
export interface BqListTablesInput {
  connection: Connection5;
  dataset: Dataset2;
  limit?: Limit3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqListTablesResult".
 */
export interface BqListTablesResult {
  tables?: Tables;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BqTableCard".
 */
export interface BqTableCard {
  creation_time_ms?: CreationTimeMs2;
  table_id: TableId;
  type?: Type2;
}
/**
 * A declaration that this write's damage is wanted.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BreakIntent".
 */
export interface BreakIntent {
  assets?: Assets;
  reason?: Reason1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BucketCard".
 */
export interface BucketCard {
  creation_date?: CreationDate;
  name: Name3;
  region?: Region;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BucketInfoInput".
 */
export interface BucketInfoInput {
  bucket?: Bucket;
  connection: Connection6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "BucketInfoResult".
 */
export interface BucketInfoResult {
  bucket: Bucket1;
  encryption?: Encryption;
  kms_key_id?: KmsKeyId;
  lifecycle_rules?: LifecycleRules;
  public_access_blocked?: PublicAccessBlocked;
  region?: Region1;
  replication_rules?: ReplicationRules;
  tags?: Tags;
  unavailable?: Unavailable;
  versioning?: Versioning;
}
export interface Tags {
  [k: string]: string;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CallGraphPythonInput".
 */
export interface CallGraphPythonInput {
  code: Code;
  description?: Description4;
  path: Path1;
  save_path?: SavePath;
  timeout_ms?: TimeoutMs;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CallGraphPythonResult".
 */
export interface CallGraphPythonResult {
  exit_code?: ExitCode1;
  graph?: GraphView | null;
  output?: Output1;
  output_path?: OutputPath1;
  saved_path?: SavedPath;
  truncated?: Truncated4;
}
/**
 * A drawable graph payload: the document shape plus where it lives.
 *
 * ``nodes``/``edges`` are capped at :data:`INLINE_NODE_CAP` /
 * :data:`INLINE_EDGE_CAP`; ``node_count``/``edge_count`` are the REAL totals and
 * ``blob`` points at the complete ``.alkgraph`` document when it didn't fit.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphView".
 */
export interface GraphView {
  attrs?: Attrs;
  blob?: BlobHandle | null;
  description?: Description5;
  edge_count?: EdgeCount;
  edges?: Edges;
  name?: Name4;
  node_count?: NodeCount;
  nodes?: Nodes;
  path?: Path2;
  ref_type?: RefType;
  truncated?: Truncated3;
}
export interface Attrs {
  [k: string]: unknown;
}
/**
 * An edge as the renderer sees it — identical keys to the persisted doc.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphEdgeCard".
 */
export interface GraphEdgeCard {
  attrs?: Attrs1;
  directed?: Directed;
  kind?: Kind3;
  label?: Label;
  source?: Source;
  target?: Target;
}
export interface Attrs1 {
  [k: string]: unknown;
}
/**
 * A node as the renderer sees it — identical keys to the persisted doc.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphNodeCard".
 */
export interface GraphNodeCard {
  attrs?: Attrs2;
  id?: Id;
  kind?: Kind4;
  label?: Label1;
}
export interface Attrs2 {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CallIntegrationSdkInput".
 */
export interface CallIntegrationSdkInput {
  background?: Background1;
  code?: Code1;
  connection: Connection7;
  description?: Description6;
  file?: File;
  mode?: Mode1;
  timeout_ms?: TimeoutMs1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CallIntegrationSdkResult".
 */
export interface CallIntegrationSdkResult {
  client_label?: ClientLabel;
  docs?: Docs;
  exit_code?: ExitCode2;
  job_id?: JobId10;
  note?: Note2;
  output?: Output2;
  output_path?: OutputPath2;
  packages?: Packages;
  sdk_modules?: SdkModules;
  truncated?: Truncated5;
}
export interface Docs {
  [k: string]: string;
}
export interface Packages {
  [k: string]: string;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CallToolInput".
 */
export interface CallToolInput {
  args?: Args;
  name: Name5;
}
export interface Args {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CallToolOutput".
 */
export interface CallToolOutput {
  result?: Result;
}
export interface Result {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CancelRunInput".
 */
export interface CancelRunInput {
  connection: Connection8;
  run_id: RunId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CancelRunResult".
 */
export interface CancelRunResult {
  requested?: Requested1;
  run_id: RunId1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CatalogCard".
 */
export interface CatalogCard {
  catalog_type?: CatalogType;
  comment?: Comment;
  name: Name6;
  owner?: Owner;
}
/**
 * A cell a batch touched, as it is after the batch.
 *
 * ``index`` is its place among the live cells, ``None`` once it is deleted.
 * ``status`` is its run status when the writer knows it: the engine's client
 * fills it; a document store, which holds no run state, leaves it ``None``.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CellAfterOp".
 */
export interface CellAfterOp {
  deleted?: Deleted1;
  id: Id1;
  index: Index;
  kind: Kind5;
  name: Name7;
  status?: Status;
}
/**
 * Something a reader of the document should know about a cell.
 *
 * ``kind`` is an open string. Core kinds: ``cell_running``,
 * ``edited_deleted_cell``, ``external_conflict``, ``kind_changed_by_other``,
 * ``upstream_being_edited``. The engine also emits ``stale_base``,
 * ``duplicate_name``, ``invalid_file``,
 * ``file_deleted``, ``corrupt_snapshot``, ``memory_warning``.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CellNotice".
 */
export interface CellNotice {
  by?: By;
  cell_id?: CellId;
  data?: Data;
  kind: Kind6;
  message?: Message1;
}
export interface Data {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChDatabaseCard".
 */
export interface ChDatabaseCard {
  engine?: Engine;
  name: Name8;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChKillQueryInput".
 */
export interface ChKillQueryInput {
  connection: Connection9;
  query_id: QueryId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChKillQueryResult".
 */
export interface ChKillQueryResult {
  matched?: Matched;
  query_id: QueryId1;
  requested?: Requested2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChListDatabasesInput".
 */
export interface ChListDatabasesInput {
  connection: Connection10;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChListDatabasesResult".
 */
export interface ChListDatabasesResult {
  databases?: Databases;
  truncated?: Truncated6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChProcessCard".
 */
export interface ChProcessCard {
  elapsed?: Elapsed;
  memory_usage?: MemoryUsage;
  query?: Query2;
  query_id: QueryId2;
  read_rows?: ReadRows;
  user?: User;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChProcessesInput".
 */
export interface ChProcessesInput {
  connection: Connection11;
  limit?: Limit4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChProcessesResult".
 */
export interface ChProcessesResult {
  processes?: Processes;
  truncated?: Truncated7;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChTableSizeCard".
 */
export interface ChTableSizeCard {
  database: Database;
  engine?: Engine1;
  name: Name9;
  total_bytes?: TotalBytes;
  total_rows?: TotalRows;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChTableSizesInput".
 */
export interface ChTableSizesInput {
  connection: Connection12;
  limit?: Limit5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ChTableSizesResult".
 */
export interface ChTableSizesResult {
  tables?: Tables1;
  truncated?: Truncated8;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ClassifyResult".
 */
export interface ClassifyResult {
  affected?: Affected;
  agent_note?: AgentNote;
  category: Category1;
  direct_category: DirectCategory;
  knowledge?: Knowledge;
  knowledge_overflow?: KnowledgeOverflow;
  model_rollup?: ModelRollup;
}
/**
 * One knowledge item as it appears on a lineage result -- the item's body,
 * plus what makes it about THIS asset.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "KnowledgeAnnotation".
 */
export interface KnowledgeAnnotation {
  body?: Body;
  column?: Column;
  item_id: ItemId;
  kind?: Kind7;
  origin?: Origin;
  qualifier?: Qualifier;
  title?: Title1;
  trust?: Trust;
  urn: Urn2;
}
export interface Qualifier {
  [k: string]: unknown;
}
/**
 * Knowledge item-to-asset relationships omitted from a lineage result.
 *
 * The bounded ``item_ids`` and ``urns`` detail projections are emitted together or
 * both empty.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "KnowledgeOverflow".
 */
export interface KnowledgeOverflow {
  item_ids?: ItemIds;
  omitted?: Omitted;
  urns?: Urns;
}
export interface ModelRollup {
  [k: string]: string;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ClusterActionResult".
 */
export interface ClusterActionResult {
  cluster_id: ClusterId;
  requested: Requested3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ClusterCard".
 */
export interface ClusterCard {
  cluster_id: ClusterId1;
  name?: Name10;
  node_type_id?: NodeTypeId;
  num_workers?: NumWorkers;
  source?: Source1;
  spark_version?: SparkVersion;
  state?: State5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ClusterInput".
 */
export interface ClusterInput {
  cluster_id: ClusterId2;
  connection: Connection13;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ColumnCard".
 */
export interface ColumnCard {
  data_type?: DataType;
  name: Name11;
  nullable?: Nullable;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ColumnInfo".
 */
export interface ColumnInfo {
  data_type?: DataType1;
  name: Name12;
  nullable?: Nullable1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ConnectionCard".
 */
export interface ConnectionCard {
  dialect?: Dialect;
  environment?: Environment;
  handle: Handle6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ConnectionInfoInput".
 */
export interface ConnectionInfoInput {
  connection: Connection14;
  identifier: Identifier;
  region?: Region2;
  target?: Target1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ConnectionInfoResult".
 */
export interface ConnectionInfoResult {
  ca_certificate_identifier?: CaCertificateIdentifier;
  database?: Database1;
  engine?: Engine2;
  engine_version?: EngineVersion;
  host?: Host;
  iam_auth_available?: IamAuthAvailable;
  identifier: Identifier1;
  master_username?: MasterUsername;
  notes?: Notes;
  port?: Port;
  publicly_accessible?: PubliclyAccessible;
  security_groups?: SecurityGroups;
  sql_supported?: SqlSupported;
  sql_system?: SqlSystem;
  tls_available?: TlsAvailable;
  vpc_id?: VpcId;
}
/**
 * The wire shape of a KB item — provenance + freshness travel so the reader
 * weighs each item itself (results are RELEVANCE-ranked, not trust-ranked).
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ContextCard".
 */
export interface ContextCard {
  body?: Body1;
  confirmations?: Confirmations;
  domain?: Domain;
  file_sources?: FileSources;
  is_mine?: IsMine;
  item_id: ItemId1;
  kind: Kind8;
  lineage_urn?: LineageUrn;
  matched_by?: MatchedBy;
  origin?: Origin1;
  owner_teams?: OwnerTeams;
  score?: Score;
  shared_by?: SharedBy;
  shared_by_email?: SharedByEmail;
  source_class?: SourceClass;
  stale_sources?: StaleSources;
  status?: Status1;
  synonyms?: Synonyms;
  title?: Title2;
  trust?: Trust1;
  trust_source?: TrustSource;
  updated_ago?: UpdatedAgo;
  updated_at?: UpdatedAt;
  urns?: Urns1;
  visibility?: Visibility;
  visibility_scope?: VisibilityScope;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ContextEditInput".
 */
export interface ContextEditInput {
  attachments?: Attachments;
  body?: Body2;
  file_sources?: FileSources1;
  /**
   * Re-file the meaning: the lineage node it sits on, its domain, its other names, its lifecycle status, or its accountable teams. Fields you leave unset keep their stored value.
   */
  filing?: FilingInput | null;
  item_id: ItemId2;
  repo_specific?: RepoSpecific;
  title?: Title3;
  trusted?: Trusted;
  visibility?: Visibility1;
}
/**
 * Where a meaning is filed: the lineage node it sits on, its domain, its other
 * names, its lifecycle, its accountable teams. On an edit an unset field keeps the
 * stored value.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FilingInput".
 */
export interface FilingInput {
  domain?: Domain1;
  lineage_urn?: LineageUrn1;
  owner_teams?: OwnerTeams1;
  status?: Status2;
  synonyms?: Synonyms1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ContextGetInput".
 */
export interface ContextGetInput {
  item_id: ItemId3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ContextNoteInput".
 */
export interface ContextNoteInput {
  attachments?: Attachments1;
  file_sources?: FileSources2;
  /**
   * How this meaning is filed: the lineage node it sits ON (`lineage_urn` — read its description, it is the highest-value field here), its business domain, the other names people use for it, its lifecycle status, and the teams accountable for it.
   */
  filing?: FilingInput | null;
  kind?: Kind9;
  repo_specific?: RepoSpecific1;
  text: Text1;
  title?: Title4;
  trusted?: Trusted1;
  visibility?: Visibility2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ContextSearchInput".
 */
export interface ContextSearchInput {
  k?: K;
  kind?: Kind10;
  query: Query3;
  repo?: Repo;
  urns?: Urns2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ContextSearchResult".
 */
export interface ContextSearchResult {
  catalog?: Catalog;
  note?: Note3;
  team_knowledge?: TeamKnowledge;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ContextWriteResult".
 */
export interface ContextWriteResult {
  item_id: ItemId4;
  shared_with_team?: SharedWithTeam;
  sharing_detail?: SharingDetail;
  title?: Title5;
  trust: Trust2;
  trust_source: TrustSource1;
  visibility?: Visibility3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CreateSchemaInput".
 */
export interface CreateSchemaInput {
  catalog: Catalog1;
  comment?: Comment1;
  connection: Connection15;
  name: Name13;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CreateSchemaResult".
 */
export interface CreateSchemaResult {
  catalog: Catalog2;
  comment?: Comment2;
  full_name: FullName;
  name: Name14;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CreateSnapshotInput".
 */
export interface CreateSnapshotInput {
  connection: Connection16;
  identifier: Identifier2;
  region?: Region3;
  snapshot_identifier: SnapshotIdentifier;
  target?: Target2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CreateSnapshotResult".
 */
export interface CreateSnapshotResult {
  snapshot_identifier: SnapshotIdentifier1;
  source_identifier: SourceIdentifier;
  status?: Status3;
  target: Target3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CurrentUserInput".
 */
export interface CurrentUserInput {
  connection: Connection17;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "CurrentUserResult".
 */
export interface CurrentUserResult {
  active?: Active;
  display_name?: DisplayName;
  id?: Id2;
  user_name?: UserName;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DataJoinInput".
 */
export interface DataJoinInput {
  join: Join;
  result_name?: ResultName4;
  sources: Sources;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "JoinSource".
 */
export interface JoinSource {
  connection: Connection18;
  limit?: Limit6;
  name: Name15;
  sql: Sql1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DbxColumnCard".
 */
export interface DbxColumnCard {
  comment?: Comment3;
  name: Name16;
  nullable?: Nullable2;
  position?: Position;
  type?: Type3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DbxListWarehousesInput".
 */
export interface DbxListWarehousesInput {
  connection: Connection19;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DbxListWarehousesResult".
 */
export interface DbxListWarehousesResult {
  warehouses?: Warehouses;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DbxWarehouseCard".
 */
export interface DbxWarehouseCard {
  id: Id3;
  name?: Name17;
  state?: State6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DbxWarehouseActionResult".
 */
export interface DbxWarehouseActionResult {
  requested: Requested4;
  warehouse_id: WarehouseId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DbxWarehouseInput".
 */
export interface DbxWarehouseInput {
  connection: Connection20;
  warehouse_id: WarehouseId1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DeleteCellOp".
 */
export interface DeleteCellOp {
  cell_id: CellId1;
  op?: Op;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DeleteObjectInput".
 */
export interface DeleteObjectInput {
  bucket?: Bucket2;
  connection: Connection21;
  key: Key;
  version_id?: VersionId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DeleteObjectResult".
 */
export interface DeleteObjectResult {
  bucket: Bucket3;
  delete_marker?: DeleteMarker;
  key: Key1;
  version_id?: VersionId1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DescribeClusterInput".
 */
export interface DescribeClusterInput {
  connection: Connection22;
  identifier: Identifier3;
  region?: Region4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DescribeClusterResult".
 */
export interface DescribeClusterResult {
  availability_zones?: AvailabilityZones;
  backup_retention_days?: BackupRetentionDays;
  created_at?: CreatedAt;
  database?: Database2;
  deletion_protection?: DeletionProtection;
  engine?: Engine3;
  engine_mode?: EngineMode;
  engine_version?: EngineVersion1;
  host?: Host1;
  iam_auth_enabled?: IamAuthEnabled;
  identifier: Identifier4;
  kms_key_id?: KmsKeyId1;
  log_exports?: LogExports;
  master_username?: MasterUsername1;
  members?: Members;
  multi_az?: MultiAz;
  parameter_group?: ParameterGroup;
  performance_insights_enabled?: PerformanceInsightsEnabled;
  port?: Port1;
  preferred_backup_window?: PreferredBackupWindow;
  preferred_maintenance_window?: PreferredMaintenanceWindow;
  reader_host?: ReaderHost;
  security_groups?: SecurityGroups1;
  serverless_max_capacity?: ServerlessMaxCapacity;
  serverless_min_capacity?: ServerlessMinCapacity;
  status?: Status4;
  storage_encrypted?: StorageEncrypted;
  subnet_group?: SubnetGroup;
  tags?: Tags1;
  vpc_id?: VpcId1;
}
export interface Tags1 {
  [k: string]: string;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DescribeInstanceInput".
 */
export interface DescribeInstanceInput {
  connection: Connection23;
  identifier: Identifier5;
  region?: Region5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DescribeInstanceResult".
 */
export interface DescribeInstanceResult {
  allocated_storage_gb?: AllocatedStorageGb;
  auto_minor_version_upgrade?: AutoMinorVersionUpgrade;
  availability_zone?: AvailabilityZone;
  backup_retention_days?: BackupRetentionDays1;
  ca_certificate_identifier?: CaCertificateIdentifier1;
  cluster_identifier?: ClusterIdentifier;
  created_at?: CreatedAt1;
  database?: Database3;
  deletion_protection?: DeletionProtection1;
  engine?: Engine4;
  engine_version?: EngineVersion2;
  host?: Host2;
  iam_auth_enabled?: IamAuthEnabled1;
  identifier: Identifier6;
  instance_class?: InstanceClass;
  iops?: Iops;
  kms_key_id?: KmsKeyId2;
  log_exports?: LogExports1;
  master_username?: MasterUsername2;
  max_allocated_storage_gb?: MaxAllocatedStorageGb;
  monitoring_interval_seconds?: MonitoringIntervalSeconds;
  multi_az?: MultiAz1;
  option_group?: OptionGroup;
  parameter_groups?: ParameterGroups;
  performance_insights_enabled?: PerformanceInsightsEnabled1;
  performance_insights_retention_days?: PerformanceInsightsRetentionDays;
  port?: Port2;
  preferred_backup_window?: PreferredBackupWindow1;
  preferred_maintenance_window?: PreferredMaintenanceWindow1;
  publicly_accessible?: PubliclyAccessible1;
  secondary_availability_zone?: SecondaryAvailabilityZone;
  security_groups?: SecurityGroups2;
  status?: Status5;
  storage_encrypted?: StorageEncrypted1;
  storage_type?: StorageType;
  subnet_group?: SubnetGroup1;
  subnets?: Subnets;
  tags?: Tags2;
  vpc_id?: VpcId2;
}
export interface Tags2 {
  [k: string]: string;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DescribeParametersInput".
 */
export interface DescribeParametersInput {
  connection: Connection24;
  limit?: Limit7;
  name_contains?: NameContains;
  parameter_group: ParameterGroup1;
  region?: Region6;
  source?: Source2;
  target?: Target4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DescribeParametersResult".
 */
export interface DescribeParametersResult {
  parameter_group: ParameterGroup2;
  parameters?: Parameters;
  target?: Target5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ParameterCard".
 */
export interface ParameterCard {
  allowed_values?: AllowedValues;
  apply_method?: ApplyMethod;
  apply_type?: ApplyType;
  data_type?: DataType2;
  description?: Description7;
  modifiable?: Modifiable;
  name: Name18;
  source?: Source3;
  value?: Value1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DocsGetInput".
 */
export interface DocsGetInput {
  connection: Connection25;
  document_id: DocumentId;
  plugin: Plugin;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DocsGetResult".
 */
export interface DocsGetResult {
  connection: Connection26;
  document_id: DocumentId1;
  plugin: Plugin1;
  sections?: Sections;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DocumentSectionCard".
 */
export interface DocumentSectionCard {
  section_id: SectionId;
  text?: Text2;
  title?: Title6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DocsSearchInput".
 */
export interface DocsSearchInput {
  limit?: Limit8;
  query: Query4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DocsSearchResult".
 */
export interface DocsSearchResult {
  catalogue?: Catalogue;
  hits?: Hits;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SourceLedgerRow".
 */
export interface SourceLedgerRow {
  connection: Connection27;
  error?: Error1;
  plugin: Plugin2;
  searched?: Searched;
  truncated?: Truncated9;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DocumentHitCard".
 */
export interface DocumentHitCard {
  connection: Connection28;
  document_id: DocumentId2;
  excerpt?: Excerpt;
  plugin: Plugin3;
  title?: Title7;
  url?: Url;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DocsSourcesInput".
 */
export interface DocsSourcesInput {}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DocsSourcesResult".
 */
export interface DocsSourcesResult {
  sources?: Sources1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "KnowledgeConnectionCard".
 */
export interface KnowledgeConnectionCard {
  connection: Connection29;
  plugin: Plugin4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DownloadLogPortionInput".
 */
export interface DownloadLogPortionInput {
  connection: Connection30;
  identifier: Identifier7;
  log_file_name: LogFileName;
  marker?: Marker;
  number_of_lines?: NumberOfLines;
  region?: Region7;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DownloadLogPortionResult".
 */
export interface DownloadLogPortionResult {
  clipped?: Clipped;
  data?: Data1;
  log_file_name: LogFileName1;
  more_pending?: MorePending;
  next_marker?: NextMarker;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DruidCancelQueryInput".
 */
export interface DruidCancelQueryInput {
  connection: Connection31;
  query_id: QueryId3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DruidConnectionInput".
 */
export interface DruidConnectionInput {
  connection: Connection32;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DruidItemsResult".
 */
export interface DruidItemsResult {
  items?: Items;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "DruidTaskInput".
 */
export interface DruidTaskInput {
  connection: Connection33;
  log_tail_bytes?: LogTailBytes;
  task_id: TaskId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EdgeCard".
 */
export interface EdgeCard {
  certainty: Certainty;
  dst: Dst;
  plugin?: Plugin5;
  provenance: Provenance;
  relation: Relation;
  src: Src;
  transformation?: Transformation1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EdgesResult".
 */
export interface EdgesResult {
  agent_note?: AgentNote1;
  edges?: Edges1;
  knowledge?: Knowledge1;
  knowledge_overflow?: KnowledgeOverflow;
  nodes?: Nodes1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NodeCard".
 */
export interface NodeCard {
  missing?: Missing;
  name?: Name19;
  node_type?: NodeType1;
  urn: Urn3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EditApply".
 */
export interface EditApply {
  create_missing?: CreateMissing;
  mode?: Mode2;
  operations?: Operations;
  path: Path3;
}
/**
 * One mutation in a ``mode="apply"`` batch. Only the fields the ``op`` needs
 * are read; the rest are ignored.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphOperation".
 */
export interface GraphOperation {
  attrs_json?: AttrsJson;
  description?: Description8;
  directed?: Directed1;
  id?: Id4;
  kind?: Kind11;
  label?: Label2;
  name?: Name20;
  op: Op1;
  source?: Source4;
  target?: Target6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EditCellOp".
 */
export interface EditCellOp {
  cell_id: CellId2;
  edits: Edits;
  op?: Op2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookTextEdit".
 */
export interface NotebookTextEdit {
  new: New;
  occurrence?: Occurrence;
  old: Old;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EditCreate".
 */
export interface EditCreate {
  attrs_json?: AttrsJson1;
  description?: Description9;
  mode?: Mode3;
  name?: Name21;
  overwrite?: Overwrite;
  path: Path4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EditImportLineage".
 */
export interface EditImportLineage {
  connection?: Connection34;
  direction?: Direction;
  max_hops?: MaxHops;
  merge?: Merge;
  mode?: Mode4;
  path: Path5;
  seed?: Seed;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EditSave".
 */
export interface EditSave {
  graph_json: GraphJson;
  mode?: Mode5;
  overwrite?: Overwrite1;
  path: Path6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ElasticsearchCountInput".
 */
export interface ElasticsearchCountInput {
  connection: Connection35;
  index: Index1;
  query?: Query5;
  terminate_after?: TerminateAfter;
}
export interface Query5 {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ElasticsearchCountResult".
 */
export interface ElasticsearchCountResult {
  count?: Count1;
  terminated_early?: TerminatedEarly;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ElasticsearchEsqlInput".
 */
export interface ElasticsearchEsqlInput {
  connection: Connection36;
  params?: Params;
  query: Query6;
  timeout_seconds?: TimeoutSeconds;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ElasticsearchEsqlResult".
 */
export interface ElasticsearchEsqlResult {
  columns?: Columns4;
  documents_found?: DocumentsFound;
  is_partial?: IsPartial;
  rows?: Rows1;
  took_ms?: TookMs;
  values_loaded?: ValuesLoaded;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ElasticsearchSearchInput".
 */
export interface ElasticsearchSearchInput {
  connection: Connection37;
  index: Index2;
  limit?: Limit9;
  query?: Query7;
}
export interface Query7 {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ElasticsearchSearchResult".
 */
export interface ElasticsearchSearchResult {
  hits?: Hits1;
  timed_out?: TimedOut;
  took_ms?: TookMs1;
  total?: Total2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EnvironmentCaptureInput".
 */
export interface EnvironmentCaptureInput {
  env_path?: EnvPath;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EnvironmentCaptureResult".
 */
export interface EnvironmentCaptureResult {
  editable?: Editable;
  not_portable?: NotPortable;
  packages?: Packages1;
  path?: Path7;
  summary?: Summary;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotPortable".
 */
export interface NotPortable1 {
  detail?: Detail;
  kind: Kind12;
  name?: Name22;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EnvironmentRecreateInput".
 */
export interface EnvironmentRecreateInput {
  dry_run?: DryRun;
  env_path?: EnvPath1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "EnvironmentRecreateResult".
 */
export interface EnvironmentRecreateResult {
  already_satisfied?: AlreadySatisfied;
  dry_run?: DryRun1;
  not_recreated?: NotRecreated;
  notes?: Notes1;
  steps?: Steps;
  target_env?: TargetEnv;
}
/**
 * Something the recreate cannot (or did not) make match the spec.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "Gap".
 */
export interface Gap {
  detail?: Detail1;
  kind: Kind13;
  name?: Name23;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "StepReport".
 */
export interface StepReport {
  command: Command1;
  exit_code?: ExitCode3;
  inputs?: Inputs;
  label?: Label3;
  output_tail?: OutputTail;
  purpose: Purpose;
  ran?: Ran;
}
export interface Inputs {
  [k: string]: string;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FetchResultInput".
 */
export interface FetchResultInput {
  handle: Handle7;
  limit?: Limit10;
  offset?: Offset;
}
/**
 * Cursor fields FIRST: when an oversized page rides the generic dispatch
 * spill, only the head of the serialized result survives as the preview, and
 * the cursor (``next_offset``/``has_more``) must be in it or paging dead-ends.
 * Serialization follows this declaration order.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FetchResultOutput".
 */
export interface FetchResultOutput {
  columns?: Columns5;
  has_more?: HasMore;
  kind: Kind14;
  limit?: Limit11;
  next_offset?: NextOffset;
  offset?: Offset1;
  returned?: Returned;
  rows?: Rows2;
  text?: Text3;
  total?: Total3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FindObjectsInput".
 */
export interface FindObjectsInput {
  bucket?: Bucket4;
  connection: Connection38;
  limit?: Limit12;
  max_keys?: MaxKeys;
  pattern: Pattern;
  prefix?: Prefix;
  use_regex?: UseRegex;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FindObjectsResult".
 */
export interface FindObjectsResult {
  bucket: Bucket5;
  matches?: Matches;
  prefix?: Prefix1;
  result_blob?: BlobHandle | null;
  scanned?: Scanned;
  truncated?: Truncated10;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FindResult".
 */
export interface FindResult {
  agent_note?: AgentNote2;
  filed_knowledge?: FiledKnowledge;
  knowledge?: Knowledge2;
  knowledge_overflow?: KnowledgeOverflow;
  matches?: Matches1;
}
/**
 * That a node HAS knowledge filed on it, and roughly what about.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NodeKnowledgeBrief".
 */
export interface NodeKnowledgeBrief {
  count?: Count2;
  titles?: Titles;
  urn: Urn4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FivetranColumnCard".
 */
export interface FivetranColumnCard {
  enabled?: Enabled;
  hashed?: Hashed;
  name: Name24;
  name_in_destination?: NameInDestination;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FivetranPipelineCard".
 */
export interface FivetranPipelineCard {
  id: Id5;
  last_failed?: LastFailed;
  last_synced?: LastSynced;
  name?: Name25;
  paused?: Paused;
  service?: Service;
  setup_state?: SetupState;
  sync_state?: SyncState;
  tasks?: Tasks;
  update_state?: UpdateState;
  urn?: Urn5;
  warnings?: Warnings;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FivetranPipelineStatusInput".
 */
export interface FivetranPipelineStatusInput {
  connection: Connection39;
  search?: Search;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FivetranPipelineStatusResult".
 */
export interface FivetranPipelineStatusResult {
  note?: Note4;
  pipelines?: Pipelines;
  total?: Total4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FivetranTableColumnsInput".
 */
export interface FivetranTableColumnsInput {
  connection: Connection40;
  connector_id: ConnectorId;
  schema_name: SchemaName;
  table: Table2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "FivetranTableColumnsResult".
 */
export interface FivetranTableColumnsResult {
  columns?: Columns6;
  note?: Note5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GenerateAuthTokenInput".
 */
export interface GenerateAuthTokenInput {
  connection: Connection41;
  host: Host3;
  port?: Port3;
  region?: Region8;
  username: Username;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GenerateAuthTokenResult".
 */
export interface GenerateAuthTokenResult {
  expires_in_seconds?: ExpiresInSeconds;
  host: Host4;
  note?: Note6;
  port: Port4;
  token: Token;
  username: Username1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetGrantsInput".
 */
export interface GetGrantsInput {
  connection: Connection42;
  full_name: FullName1;
  securable_type: SecurableType;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetGrantsResult".
 */
export interface GetGrantsResult {
  full_name: FullName2;
  grants?: Grants;
  securable_type: SecurableType1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GrantCard".
 */
export interface GrantCard {
  principal: Principal;
  privileges?: Privileges;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetJobInput".
 */
export interface GetJobInput {
  connection: Connection43;
  job_id: JobId11;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetJobResult".
 */
export interface GetJobResult {
  creator?: Creator;
  format?: Format2;
  job_id: JobId12;
  name?: Name26;
  schedule?: Schedule;
  schedule_paused?: SchedulePaused;
  task_keys?: TaskKeys;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetPipelineInput".
 */
export interface GetPipelineInput {
  connection: Connection44;
  pipeline_id: PipelineId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetPipelineResult".
 */
export interface GetPipelineResult {
  cluster_id?: ClusterId3;
  creator?: Creator1;
  health?: Health;
  latest_update_id?: LatestUpdateId;
  latest_update_state?: LatestUpdateState;
  name?: Name27;
  pipeline_id: PipelineId1;
  state?: State7;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetRunInput".
 */
export interface GetRunInput {
  connection: Connection45;
  run_id: RunId2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetRunOutputInput".
 */
export interface GetRunOutputInput {
  connection: Connection46;
  run_id: RunId3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetRunOutputResult".
 */
export interface GetRunOutputResult {
  clipped?: Clipped1;
  error?: Error2;
  error_trace?: ErrorTrace;
  logs?: Logs;
  logs_truncated?: LogsTruncated;
  run_id: RunId4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetRunResult".
 */
export interface GetRunResult {
  life_cycle_state?: LifeCycleState;
  result_state?: ResultState;
  run_id: RunId5;
  state_message?: StateMessage;
  tasks?: Tasks1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TaskRunCard".
 */
export interface TaskRunCard {
  run_id: RunId6;
  state?: State8;
  task_key?: TaskKey;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetTableInput".
 */
export interface GetTableInput {
  connection: Connection47;
  full_name: FullName3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GetTableResult".
 */
export interface GetTableResult {
  columns?: Columns7;
  comment?: Comment4;
  data_source_format?: DataSourceFormat;
  full_name: FullName4;
  owner?: Owner1;
  storage_location?: StorageLocation;
  table_type?: TableType;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphCellSummary".
 */
export interface GraphCellSummary {
  defs?: Defs;
  errors?: Errors;
  refs?: Refs;
}
/**
 * A problem in the dependency graph: its code (``multiple_definitions``,
 * ``cycle``, ``syntax``, ``delete_nonlocal``, ...), the name it is about,
 * and the cells it involves. The one graph error model: ``GraphSummary``,
 * ``GraphView``, the cells of a view and the ``graph`` event all carry it.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphErrorInfo".
 */
export interface GraphErrorInfo {
  cells?: Cells;
  code: Code2;
  name?: Name28;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphEditResult".
 */
export interface GraphEditResult {
  applied?: Applied;
  bytes_written?: BytesWritten;
  created?: Created;
  graph?: GraphView | null;
  imported_edges?: ImportedEdges;
  imported_nodes?: ImportedNodes;
  issues?: Issues;
  mode?: Mode6;
  note?: Note7;
  path?: Path8;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphFileCard".
 */
export interface GraphFileCard {
  edge_count?: EdgeCount1;
  error?: Error3;
  name?: Name29;
  node_count?: NodeCount1;
  path?: Path9;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QueryRead".
 */
export interface QueryRead {
  max_edges?: MaxEdges;
  max_nodes?: MaxNodes;
  mode?: Mode7;
  path: Path10;
  style?: Style;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QueryList".
 */
export interface QueryList {
  directory?: Directory;
  limit?: Limit13;
  mode?: Mode8;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QueryTraverse".
 */
export interface QueryTraverse {
  algorithm?: Algorithm;
  direction?: Direction1;
  filters?: NodeFilter | null;
  max_hops?: MaxHops1;
  max_nodes?: MaxNodes1;
  mode?: Mode9;
  path: Path11;
  seeds?: Seeds;
}
/**
 * A predicate over nodes/edges, used by traversal, filter and search.
 *
 * Every field is optional and they AND together. ``attr_equals`` matches a
 * node's ``attrs`` by exact value; ``attr_contains`` matches a substring of the
 * stringified value, which is what you want for free-form imported metadata.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NodeFilter".
 */
export interface NodeFilter {
  attr_exact?: AttrExact;
  attr_key?: AttrKey;
  attr_value?: AttrValue;
  edge_kinds?: EdgeKinds;
  max_degree?: MaxDegree;
  min_degree?: MinDegree;
  node_kinds?: NodeKinds;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QuerySearch".
 */
export interface QuerySearch {
  limit?: Limit14;
  mode?: Mode10;
  path: Path12;
  query: Query8;
  regex?: Regex;
  target?: Target7;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QueryFilter".
 */
export interface QueryFilter {
  filters?: NodeFilter;
  max_nodes?: MaxNodes2;
  mode?: Mode11;
  path: Path13;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QueryPaths".
 */
export interface QueryPaths {
  enumerate_all?: EnumerateAll;
  max_hops?: MaxHops2;
  max_paths?: MaxPaths;
  mode?: Mode12;
  path: Path14;
  source: Source5;
  target: Target8;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QueryStats".
 */
export interface QueryStats {
  max_cycles?: MaxCycles;
  mode?: Mode13;
  path: Path15;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphQueryResult".
 */
export interface GraphQueryResult {
  edge_matches?: EdgeMatches;
  files?: Files;
  graph?: GraphView | null;
  hops?: Hops;
  missing?: Missing1;
  mode?: Mode14;
  node_matches?: NodeMatches;
  note?: Note8;
  path?: Path16;
  paths?: Paths;
  rendered?: Rendered;
  stats?: GraphStatsCard | null;
  truncated?: Truncated11;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SearchHit".
 */
export interface SearchHit {
  field?: Field;
  id?: Id6;
  kind?: Kind15;
  label?: Label4;
  snippet?: Snippet;
}
export interface Hops {
  [k: string]: number;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphStatsCard".
 */
export interface GraphStatsCard {
  acyclic?: Acyclic;
  avg_degree?: AvgDegree;
  busiest?: Busiest;
  components?: Components;
  cycles?: Cycles;
  directed_edges?: DirectedEdges;
  edge_count?: EdgeCount2;
  isolated?: Isolated;
  largest_component?: LargestComponent;
  leaves?: Leaves;
  max_in_degree?: MaxInDegree;
  max_out_degree?: MaxOutDegree;
  node_count?: NodeCount2;
  parallel_edges?: ParallelEdges;
  roots?: Roots;
  self_loops?: SelfLoops;
  topological_order?: TopologicalOrder;
  undirected_edges?: UndirectedEdges;
}
/**
 * The document graph (current text): for diagnostics only.
 *
 * ``computed`` is false when the writer had no analysis of exactly the state
 * it answers for (the platform analyses off the hot path, and the ``graph``
 * event on the notebook channel carries the analysis once it is ready);
 * ``cells`` and ``edges`` are empty then.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "GraphSummary".
 */
export interface GraphSummary {
  cells?: Cells1;
  computed?: Computed;
  edges?: Edges2;
}
export interface Cells1 {
  [k: string]: GraphCellSummary;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "HeadObjectInput".
 */
export interface HeadObjectInput {
  bucket?: Bucket6;
  connection: Connection48;
  key: Key2;
  version_id?: VersionId2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "HeadObjectResult".
 */
export interface HeadObjectResult {
  bucket: Bucket7;
  content_type?: ContentType1;
  etag?: Etag;
  key: Key3;
  kms_key_id?: KmsKeyId3;
  last_modified?: LastModified;
  metadata?: Metadata1;
  server_side_encryption?: ServerSideEncryption;
  size?: Size1;
  storage_class?: StorageClass;
  version_id?: VersionId3;
}
export interface Metadata1 {
  [k: string]: string;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "HexListProjectsInput".
 */
export interface HexListProjectsInput {
  connection: Connection49;
  search?: Search1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "HexListProjectsResult".
 */
export interface HexListProjectsResult {
  note?: Note9;
  projects?: Projects;
  total?: Total5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "HexProjectCard".
 */
export interface HexProjectCard {
  id: Id7;
  last_published_at?: LastPublishedAt;
  project_type?: ProjectType;
  status?: Status6;
  title?: Title8;
  urn?: Urn6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "HexProjectRunsInput".
 */
export interface HexProjectRunsInput {
  connection: Connection50;
  limit?: Limit15;
  project_id: ProjectId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "HexProjectRunsResult".
 */
export interface HexProjectRunsResult {
  note?: Note10;
  project_id: ProjectId1;
  runs?: Runs;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "HexRunCard".
 */
export interface HexRunCard {
  elapsed_ms?: ElapsedMs;
  ended_at?: EndedAt;
  run_id: RunId7;
  run_url?: RunUrl;
  started_at?: StartedAt;
  status?: Status7;
  trigger?: Trigger;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "InsertCellOp".
 */
export interface InsertCellOp {
  after?: After;
  before?: Before;
  config?: Config;
  kind?: Kind16;
  meta?: Meta;
  name?: Name30;
  op?: Op3;
  source?: Source6;
}
export interface Config {
  [k: string]: unknown;
}
export interface Meta {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "JobCard".
 */
export interface JobCard {
  job_id: JobId13;
  name?: Name31;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "KafkaCardsResult".
 */
export interface KafkaCardsResult {
  diagnostics?: Diagnostics;
  items?: Items1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "KafkaConnectionInput".
 */
export interface KafkaConnectionInput {
  connection: Connection51;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "KafkaPeekInput".
 */
export interface KafkaPeekInput {
  connection: Connection52;
  include_values?: IncludeValues;
  max_messages?: MaxMessages;
  partition?: Partition;
  topic: Topic;
}
/**
 * No arguments — listing is unconditional.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListAgentTypesInput".
 */
export interface ListAgentTypesInput {}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListAgentTypesResult".
 */
export interface ListAgentTypesResult {
  agents: Agents;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListBucketsInput".
 */
export interface ListBucketsInput {
  connection: Connection53;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListBucketsResult".
 */
export interface ListBucketsResult {
  buckets?: Buckets;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListCatalogsInput".
 */
export interface ListCatalogsInput {
  connection: Connection54;
  limit?: Limit16;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListCatalogsResult".
 */
export interface ListCatalogsResult {
  catalogs?: Catalogs;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListClustersInput".
 */
export interface ListClustersInput {
  connection: Connection55;
  limit?: Limit17;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListClustersResult".
 */
export interface ListClustersResult {
  clusters?: Clusters;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListInstancesInput".
 */
export interface ListInstancesInput {
  connection: Connection56;
  limit?: Limit18;
  region?: Region9;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListInstancesResult".
 */
export interface ListInstancesResult {
  instances?: Instances;
  region?: Region10;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RdsInstanceCard".
 */
export interface RdsInstanceCard {
  allocated_storage_gb?: AllocatedStorageGb1;
  availability_zone?: AvailabilityZone1;
  cluster_identifier?: ClusterIdentifier1;
  engine?: Engine5;
  engine_version?: EngineVersion3;
  host?: Host5;
  iam_auth_enabled?: IamAuthEnabled2;
  identifier: Identifier8;
  instance_class?: InstanceClass1;
  multi_az?: MultiAz2;
  port?: Port5;
  status?: Status8;
  storage_type?: StorageType1;
  tags?: Tags3;
}
export interface Tags3 {
  [k: string]: string;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListJobsInput".
 */
export interface ListJobsInput {
  connection: Connection57;
  limit?: Limit19;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListJobsResult".
 */
export interface ListJobsResult {
  jobs?: Jobs2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListLogFilesInput".
 */
export interface ListLogFilesInput {
  connection: Connection58;
  identifier: Identifier9;
  limit?: Limit20;
  name_contains?: NameContains1;
  region?: Region11;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListLogFilesResult".
 */
export interface ListLogFilesResult {
  identifier: Identifier10;
  log_files?: LogFiles;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "LogFileCard".
 */
export interface LogFileCard {
  last_written_at?: LastWrittenAt;
  name: Name32;
  size_bytes?: SizeBytes1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListObjectsInput".
 */
export interface ListObjectsInput {
  bucket?: Bucket8;
  connection: Connection59;
  delimiter?: Delimiter;
  max_keys?: MaxKeys1;
  prefix?: Prefix2;
  start_after?: StartAfter;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListObjectsResult".
 */
export interface ListObjectsResult {
  bucket: Bucket9;
  common_prefixes?: CommonPrefixes;
  objects?: Objects;
  prefix?: Prefix3;
  result_blob?: BlobHandle | null;
  returned?: Returned1;
  truncated?: Truncated12;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListPipelinesInput".
 */
export interface ListPipelinesInput {
  connection: Connection60;
  limit?: Limit21;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListPipelinesResult".
 */
export interface ListPipelinesResult {
  pipelines?: Pipelines1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PipelineCard".
 */
export interface PipelineCard {
  creator?: Creator2;
  name?: Name33;
  pipeline_id: PipelineId2;
  state?: State9;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListPluginsInput".
 */
export interface ListPluginsInput {}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListPluginsOutput".
 */
export interface ListPluginsOutput {
  plugins?: Plugins;
}
/**
 * One plugin's status for the agent's ``list_plugins`` discovery tool.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PluginInfo".
 */
export interface PluginInfo {
  active?: Active1;
  connections?: Connections;
  description?: Description10;
  enabled?: Enabled1;
  name: Name34;
  surfaces?: Surfaces;
  urn_format?: UrnFormat;
}
/**
 * A connection a plugin manages, for the agent's discovery tool — live
 * (``added``) or merely detected (a candidate the user can enable).
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PluginConnectionInfo".
 */
export interface PluginConnectionInfo {
  added?: Added;
  environment?: Environment1;
  handle: Handle8;
  plugin: Plugin6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListRunsInput".
 */
export interface ListRunsInput {
  active_only?: ActiveOnly;
  connection: Connection61;
  job_id?: JobId14;
  limit?: Limit22;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListRunsResult".
 */
export interface ListRunsResult {
  runs?: Runs1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RunCard".
 */
export interface RunCard {
  job_id?: JobId15;
  life_cycle_state?: LifeCycleState1;
  result_state?: ResultState1;
  run_id: RunId8;
  run_name?: RunName;
  start_time_ms?: StartTimeMs1;
  url?: Url1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListSchemasInput".
 */
export interface ListSchemasInput {
  catalog: Catalog3;
  connection: Connection62;
  limit?: Limit23;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListSchemasResult".
 */
export interface ListSchemasResult {
  schemas?: Schemas;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SchemaCard".
 */
export interface SchemaCard {
  comment?: Comment5;
  full_name?: FullName5;
  name: Name35;
  owner?: Owner2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListSnapshotsInput".
 */
export interface ListSnapshotsInput {
  connection: Connection63;
  identifier?: Identifier11;
  limit?: Limit24;
  region?: Region12;
  snapshot_type?: SnapshotType;
  target?: Target9;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListSnapshotsResult".
 */
export interface ListSnapshotsResult {
  region?: Region13;
  snapshots?: Snapshots;
  target?: Target10;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SnapshotCard".
 */
export interface SnapshotCard {
  allocated_storage_gb?: AllocatedStorageGb2;
  created_at?: CreatedAt2;
  encrypted?: Encrypted;
  engine?: Engine6;
  engine_version?: EngineVersion4;
  identifier: Identifier12;
  snapshot_type?: SnapshotType1;
  source_identifier?: SourceIdentifier1;
  status?: Status9;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListTablesInput".
 */
export interface ListTablesInput {
  catalog: Catalog4;
  connection: Connection64;
  limit?: Limit25;
  schema_name: SchemaName1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ListTablesResult".
 */
export interface ListTablesResult {
  tables?: Tables2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TableCard".
 */
export interface TableCard {
  comment?: Comment6;
  data_source_format?: DataSourceFormat1;
  full_name?: FullName6;
  name: Name36;
  owner?: Owner3;
  table_type?: TableType1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "LookerDashboardCard".
 */
export interface LookerDashboardCard {
  id: Id8;
  title?: Title9;
  urn?: Urn7;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "LookerDashboardTilesInput".
 */
export interface LookerDashboardTilesInput {
  connection: Connection65;
  dashboard_id: DashboardId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "LookerDashboardTilesResult".
 */
export interface LookerDashboardTilesResult {
  dashboard_id: DashboardId1;
  tables?: Tables3;
  tiles?: Tiles;
  title?: Title11;
  urn?: Urn8;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "LookerTileCard".
 */
export interface LookerTileCard {
  explore?: Explore;
  model?: Model1;
  tables?: Tables4;
  title?: Title10;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "LookerListDashboardsInput".
 */
export interface LookerListDashboardsInput {
  connection: Connection66;
  search?: Search2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "LookerListDashboardsResult".
 */
export interface LookerListDashboardsResult {
  dashboards?: Dashboards;
  note?: Note11;
  total?: Total6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ManageTasksInput".
 */
export interface ManageTasksInput {
  clear?: Clear;
  delete?: Delete;
  upsert?: Upsert;
}
/**
 * One add-or-edit. A new ``id`` creates (``title`` required); an existing ``id``
 * edits only the fields provided here.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TaskUpsert".
 */
export interface TaskUpsert {
  depends_on?: DependsOn;
  description?: Description11;
  id: Id9;
  status?: Status10;
  title?: Title12;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ManageTasksOutput".
 */
export interface ManageTasksOutput {
  summary?: TaskSummary;
  tasks?: Tasks2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TaskSummary".
 */
export interface TaskSummary {
  counts?: Counts;
  ready?: Ready;
  total?: Total7;
}
export interface Counts {
  [k: string]: number;
}
/**
 * A task as the model sees it — full detail + derived blocked state +
 * RELATIVE updated time (never a raw timestamp).
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TaskView".
 */
export interface TaskView {
  blocked?: Blocked;
  blocked_by?: BlockedBy;
  depends_on?: DependsOn1;
  description?: Description12;
  id: Id10;
  status: Status11;
  title: Title13;
  updated_ago?: UpdatedAgo1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MetricSeries".
 */
export interface MetricSeries {
  average?: Average;
  datapoints?: Datapoints;
  latest?: Latest;
  maximum?: Maximum;
  metric: Metric;
  minimum?: Minimum;
  unit?: Unit;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MetricsInput".
 */
export interface MetricsInput {
  connection: Connection67;
  hours?: Hours;
  identifier: Identifier13;
  metrics?: Metrics;
  region?: Region14;
  target?: Target11;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MetricsResult".
 */
export interface MetricsResult {
  end?: End;
  identifier: Identifier14;
  period_seconds?: PeriodSeconds;
  series?: Series;
  start?: Start;
  target?: Target12;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MongoAggregateInput".
 */
export interface MongoAggregateInput {
  collection: Collection;
  connection: Connection68;
  database: Database4;
  explain?: Explain;
  limit?: Limit26;
  pipeline?: Pipeline;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MongoAggregateResult".
 */
export interface MongoAggregateResult {
  cost_warnings?: CostWarnings;
  documents?: Documents;
  effect?: Effect1;
  explain?: Explain1;
  truncated?: Truncated13;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MoveCellOp".
 */
export interface MoveCellOp {
  after?: After1;
  before?: Before1;
  cell_id: CellId3;
  op?: Op4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyDatabaseRow".
 */
export interface MyDatabaseRow {
  default_character_set_name?: DefaultCharacterSetName;
  default_collation_name?: DefaultCollationName;
  schema_name: SchemaName2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyKillQueryInput".
 */
export interface MyKillQueryInput {
  connection: Connection69;
  id: Id11;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyKillQueryResult".
 */
export interface MyKillQueryResult {
  id: Id12;
  requested?: Requested5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyListDatabasesInput".
 */
export interface MyListDatabasesInput {
  connection: Connection70;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyListDatabasesResult".
 */
export interface MyListDatabasesResult {
  databases?: Databases1;
  truncated?: Truncated14;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyProcessRow".
 */
export interface MyProcessRow {
  command?: Command2;
  db?: Db;
  host?: Host6;
  id?: Id13;
  info?: Info;
  state?: State10;
  time?: Time;
  user?: User1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyProcesslistInput".
 */
export interface MyProcesslistInput {
  connection: Connection71;
  limit?: Limit27;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyProcesslistResult".
 */
export interface MyProcesslistResult {
  processes?: Processes1;
  truncated?: Truncated15;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyTableSizeRow".
 */
export interface MyTableSizeRow {
  engine?: Engine7;
  table_name: TableName;
  table_rows?: TableRows;
  table_schema: TableSchema;
  total_bytes?: TotalBytes1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyTableSizesInput".
 */
export interface MyTableSizesInput {
  connection: Connection72;
  limit?: Limit28;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "MyTableSizesResult".
 */
export interface MyTableSizesResult {
  tables?: Tables5;
  truncated?: Truncated16;
}
/**
 * Everything filed on ONE node.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NodeKnowledgeCard".
 */
export interface NodeKnowledgeCard {
  direction?: Direction2;
  items?: Items2;
  urn: Urn9;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NodeKnowledgeResult".
 */
export interface NodeKnowledgeResult {
  agent_note?: AgentNote3;
  count?: Count3;
  nodes?: Nodes2;
  urn?: Urn10;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookCellBrief".
 */
export interface NotebookCellBrief {
  id: Id14;
  index: Index3;
  kind: Kind17;
  name: Name37;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookCellState".
 */
export interface NotebookCellState {
  defs?: Defs1;
  downstream?: Downstream;
  graph_errors?: GraphErrors;
  head?: Untrusted | null;
  id: Id15;
  index: Index4;
  kind: Kind18;
  last_run?: NotebookRunAttribution | null;
  lines?: Lines;
  links_omitted?: LinksOmitted;
  matches?: Matches2;
  meta?: Meta1;
  name: Name38;
  names_omitted?: NamesOmitted;
  output?: NotebookOutputSummary | null;
  output_outdated?: OutputOutdated;
  refs?: Refs1;
  source?: Untrusted | null;
  source_page?: NotebookTextPage | null;
  status: Status12;
  upstream?: Upstream;
}
/**
 * Text a notebook produced, carried as data.
 *
 * ``author`` names who caused the text to exist (the person or agent whose run
 * printed it, or whose edit wrote the source) so the reader can weigh it;
 * ``content`` is the text itself, or for structured outputs (a chart spec, a
 * table page) the JSON value.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "Untrusted".
 */
export interface Untrusted {
  author: Author;
  content: Content;
  untrusted?: Untrusted1;
}
export interface Content {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookRunAttribution".
 */
export interface NotebookRunAttribution {
  by: By1;
  finished_at?: FinishedAt;
  run_id: RunId9;
  trigger: Trigger1;
}
export interface Meta1 {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookOutputSummary".
 */
export interface NotebookOutputSummary {
  chars?: Chars;
  error?: NotebookErrorInfo | null;
  has_chart?: HasChart;
  has_image?: HasImage;
  has_table?: HasTable;
  has_widget?: HasWidget;
  kinds?: Kinds;
  text?: Untrusted | null;
  truncated?: Truncated17;
}
/**
 * A cell's error. ``ename`` is the exception class, reduced to a dotted
 * identifier; the message and traceback are the notebook's own words.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookErrorInfo".
 */
export interface NotebookErrorInfo {
  ename: Ename;
  evalue: Untrusted;
  traceback?: Untrusted | null;
}
/**
 * Where a window of text sits in the whole text.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookTextPage".
 */
export interface NotebookTextPage {
  blob?: Blob;
  cut_by_budget?: CutByBudget;
  more?: NotebookMore | null;
  next_offset?: NextOffset1;
  offset: Offset2;
  returned: Returned2;
  total: Total8;
  unit: Unit1;
}
/**
 * The call that returns what a result left out, ready to send as is.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookMore".
 */
export interface NotebookMore {
  args: Args1;
  note?: Note12;
  tool: Tool;
}
export interface Args1 {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookCellsInput".
 */
export interface NotebookCellsInput {
  action: Action;
  cells?: Cells2;
  kind?: Kind19;
  path: Path17;
  to?: To;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookCellsOutput".
 */
export interface NotebookCellsOutput {
  action: Action1;
  blob?: Blob1;
  changed?: Changed;
  notebook_guide?: NotebookGuide | null;
  path: Path18;
  ref_type?: RefType1;
  result_name?: ResultName5;
  stale?: Stale;
  token?: Token1;
}
/**
 * The essentials of the ``notebooks`` skill, carried by the first notebook
 * tool result in a conversation so the agent has them without loading the
 * skill. ``version`` changes whenever the text does.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookGuide".
 */
export interface NotebookGuide {
  text: Text4;
  version: Version;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookCreateInput".
 */
export interface NotebookCreateInput {
  cells?: Cells3;
  path: Path19;
  settings?: Settings;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookNewCell".
 */
export interface NotebookNewCell {
  kind?: Kind20;
  meta?: Meta2;
  name?: Name39;
  source?: Source7;
}
/**
 * Cell settings the file records; for a SQL cell: output_var, connection, engine, show_output.
 */
export interface Meta2 {
  [k: string]: unknown;
}
export interface Settings {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookCreateOutput".
 */
export interface NotebookCreateOutput {
  blob?: Blob2;
  cells: Cells4;
  more?: NotebookMore | null;
  notebook_guide?: NotebookGuide | null;
  path: Path20;
  ref_type?: RefType2;
  result_name?: ResultName6;
  token: Token2;
  total_cells?: TotalCells;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookEditInput".
 */
export interface NotebookEditInput {
  base_token?: BaseToken;
  ops: Ops;
  path: Path21;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ReplaceCellOp".
 */
export interface ReplaceCellOp {
  cell_id: CellId4;
  op?: Op5;
  source: Source8;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RestoreCellOp".
 */
export interface RestoreCellOp {
  after?: After2;
  cell_id: CellId5;
  op?: Op6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RenameCellOp".
 */
export interface RenameCellOp {
  cell_id: CellId6;
  name: Name40;
  op?: Op7;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SetCellKindOp".
 */
export interface SetCellKindOp {
  cell_id: CellId7;
  kind: Kind21;
  op?: Op8;
}
/**
 * marimo's cell options, for any cell: ``disabled``, ``hide_code``,
 * ``expand_output``, ``column``. A SQL cell's ``show_output``,
 * ``output_var`` and ``connection`` are not here: they are ``set_meta``.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SetCellConfigOp".
 */
export interface SetCellConfigOp {
  cell_id: CellId8;
  config: Config1;
  op?: Op9;
}
/**
 * Only disabled, hide_code, expand_output or column; SQL settings are set_meta.
 */
export interface Config1 {
  [k: string]: unknown;
}
/**
 * A SQL or Markdown cell's settings: for SQL ``connection`` (the name of
 * one of the workspace's connections; ``None`` runs it in the notebook's
 * DuckDB), ``output_var`` and ``show_output``; for Markdown ``quote``.
 * Keys not named keep their value.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SetCellMetaOp".
 */
export interface SetCellMetaOp {
  cell_id: CellId9;
  meta: Meta3;
  op?: Op10;
}
/**
 * SQL: connection, output_var, show_output, engine. Markdown: quote.
 */
export interface Meta3 {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SetSettingOp".
 */
export interface SetSettingOp {
  key: Key4;
  op?: Op11;
  value: Value2;
}
export interface Value2 {
  [k: string]: unknown;
}
/**
 * The engine's result of the batch (each touched cell with its run
 * status, notices), for ``path``. ``graph`` covers the touched cells and
 * the edges into and out of them, not the whole notebook.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookEditOutput".
 */
export interface NotebookEditOutput {
  blob?: Blob3;
  cells: Cells5;
  created: Created1;
  graph: GraphSummary;
  notebook_guide?: NotebookGuide | null;
  notices: Notices;
  path: Path22;
  ref_type?: RefType3;
  repeat: Repeat;
  result_name?: ResultName7;
  stale?: Stale1;
  stale_total?: StaleTotal;
  token: Token3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookEnvInfo".
 */
export interface NotebookEnvInfo {
  env_id: EnvId;
  kind: Kind22;
  python: Python;
  recorded_in_file: RecordedInFile;
  spec_root: SpecRoot;
  state: State11;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookEnvInput".
 */
export interface NotebookEnvInput {
  action?: Action2;
  env?: Env;
  limit?: Limit29;
  offset?: Offset3;
  packages?: Packages2;
  path: Path23;
  search?: Search3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookEnvOutput".
 */
export interface NotebookEnvOutput {
  blob?: Blob4;
  env: NotebookEnvInfo;
  envs?: Envs;
  log?: Untrusted | null;
  notebook_guide?: NotebookGuide | null;
  packages?: Packages3;
  page?: NotebookPage | null;
  path: Path24;
  ref_type?: RefType4;
  result_name?: ResultName8;
  spec_changed?: SpecChanged;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookPackage".
 */
export interface NotebookPackage {
  name: Name41;
  version: Version1;
}
/**
 * Where a page sits in its list.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookPage".
 */
export interface NotebookPage {
  blob?: Blob5;
  cut_by_budget?: CutByBudget1;
  limit: Limit30;
  more?: NotebookMore | null;
  next_offset?: NextOffset2;
  offset: Offset4;
  returned: Returned3;
  total: Total9;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookFramePage".
 */
export interface NotebookFramePage {
  column_offset?: ColumnOffset;
  columns: Columns8;
  name: Name42;
  offset: Offset5;
  rows: Untrusted;
  total_columns?: TotalColumns;
  total_rows: TotalRows1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookGraphCell".
 */
export interface NotebookGraphCell {
  defs: Defs2;
  distance?: Distance;
  name: Name43;
  refs: Refs2;
  status: Status13;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookGraphError".
 */
export interface NotebookGraphError {
  cell_id: CellId10;
  kind: Kind23;
  names?: Names;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookGraphInput".
 */
export interface NotebookGraphInput {
  cell?: Cell;
  depth?: Depth;
  direction?: Direction3;
  limit?: Limit31;
  offset?: Offset6;
  path: Path25;
  summary?: Summary1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookGraphOutput".
 */
export interface NotebookGraphOutput {
  blob?: Blob6;
  cells: Cells6;
  complete?: Complete;
  downstream?: Downstream1;
  downstream_direct?: DownstreamDirect;
  downstream_total?: DownstreamTotal;
  downstream_transitive?: DownstreamTransitive;
  edges: Edges3;
  errors?: Errors1;
  errors_total?: ErrorsTotal;
  notebook_guide?: NotebookGuide | null;
  page?: NotebookPage | null;
  path: Path26;
  ref_type?: RefType5;
  result_name?: ResultName9;
  summary?: NotebookGraphSummary | null;
  upstream?: Upstream1;
  upstream_direct?: UpstreamDirect;
  upstream_total?: UpstreamTotal;
  upstream_transitive?: UpstreamTransitive;
}
export interface Cells6 {
  [k: string]: NotebookGraphCell;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookGraphSummary".
 */
export interface NotebookGraphSummary {
  by_status?: ByStatus;
  cells: Cells7;
  edges: Edges4;
  errors: Errors2;
  first_roots?: FirstRoots;
  leaves: Leaves1;
  roots: Roots1;
}
export interface ByStatus {
  [k: string]: number;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookImage".
 */
export interface NotebookImage {
  bytes: Bytes1;
  data_base64?: DataBase64;
  index?: Index5;
  mime: Mime;
  sha256: Sha2561;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookInspectInput".
 */
export interface NotebookInspectInput {
  column_limit?: ColumnLimit;
  column_offset?: ColumnOffset1;
  filter_sql?: FilterSql;
  limit?: Limit32;
  name?: Name44;
  offset?: Offset7;
  path: Path27;
  sort?: Sort;
  what?: What;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookInspectOutput".
 */
export interface NotebookInspectOutput {
  blob?: Blob7;
  frame?: NotebookFramePage | null;
  notebook_guide?: NotebookGuide | null;
  page?: NotebookPage | null;
  path: Path28;
  ref_type?: RefType6;
  result_name?: ResultName10;
  value?: Untrusted | null;
  variables?: Variables;
  what: What1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookVariable".
 */
export interface NotebookVariable {
  cell_id?: CellId11;
  columns?: Columns9;
  columns_total?: ColumnsTotal;
  name: Name45;
  repr: Untrusted;
  shape?: Shape;
  size_bytes?: SizeBytes2;
  type: Type4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookKernelInfo".
 */
export interface NotebookKernelInfo {
  env: NotebookEnvInfo;
  memory_bytes?: MemoryBytes;
  queue?: Queue;
  queue_total?: QueueTotal;
  reactivity: Reactivity;
  started_at?: StartedAt1;
  state: State12;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookQueuedRun".
 */
export interface NotebookQueuedRun {
  by: By2;
  run_id: RunId10;
  status: Status14;
  trigger: Trigger2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookKernelInput".
 */
export interface NotebookKernelInput {
  action?: Action3;
  path: Path29;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookKernelOutput".
 */
export interface NotebookKernelOutput {
  blob?: Blob8;
  kernel: NotebookKernelInfo;
  notebook_guide?: NotebookGuide | null;
  path: Path30;
  ref_type?: RefType7;
  result_name?: ResultName11;
  runs?: Runs2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookRunBrief".
 */
export interface NotebookRunBrief {
  by: By3;
  run_id: RunId11;
  status: Status15;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookOutputInput".
 */
export interface NotebookOutputInput {
  cell: Cell1;
  item_limit?: ItemLimit;
  item_offset?: ItemOffset;
  line_limit?: LineLimit;
  line_offset?: LineOffset;
  max_chars?: MaxChars;
  part?: Part;
  path: Path31;
  row_limit?: RowLimit;
  row_offset?: RowOffset;
  text_offset?: TextOffset;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookOutputOutput".
 */
export interface NotebookOutputOutput {
  blob?: Blob9;
  cell_id: CellId12;
  chart_page?: NotebookTextPage | null;
  chart_spec?: Untrusted | null;
  error?: NotebookErrorInfo | null;
  images?: Images;
  images_page?: NotebookPage | null;
  notebook_guide?: NotebookGuide | null;
  path: Path32;
  ref_type?: RefType8;
  result_name?: ResultName12;
  run?: NotebookRunAttribution | null;
  table?: Untrusted | null;
  table_page?: NotebookPage | null;
  text?: Untrusted | null;
  text_page?: NotebookTextPage | null;
  truncated?: Truncated18;
  widgets?: Widgets;
  widgets_page?: NotebookPage | null;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookWidgetState".
 */
export interface NotebookWidgetState {
  cell_id?: CellId13;
  model_id: ModelId;
  type: Type5;
  value?: Untrusted | null;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookPlanStep".
 */
export interface NotebookPlanStep {
  cell_id: CellId14;
  name: Name46;
  reason: Reason2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookPresence".
 */
export interface NotebookPresence {
  cell_id?: CellId15;
  who: Who;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookReadInput".
 */
export interface NotebookReadInput {
  cells?: Cells8;
  include_outputs?: IncludeOutputs;
  include_source?: IncludeSource;
  limit?: Limit33;
  offset?: Offset8;
  path: Path33;
  regex?: Regex1;
  search?: Search4;
  source_limit?: SourceLimit;
  source_offset?: SourceOffset;
  status?: Status16;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookReadOutput".
 */
export interface NotebookReadOutput {
  blob?: Blob10;
  cells: Cells9;
  kernel: NotebookKernelInfo;
  matched?: Matched1;
  notebook_guide?: NotebookGuide | null;
  page?: NotebookPage | null;
  path: Path34;
  presence?: Presence;
  ref_type?: RefType9;
  result_name?: ResultName13;
  settings: Settings1;
  token: Token4;
  total_cells?: TotalCells1;
}
export interface Settings1 {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookRunInput".
 */
export interface NotebookRunInput {
  confirm_expensive?: ConfirmExpensive;
  path: Path35;
  target: Target13;
  timeout_s?: TimeoutS;
  wait?: Wait;
}
/**
 * These cells, after any cell they need that has not run yet.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RunCells".
 */
export interface RunCells {
  ids: Ids;
  kind?: Kind24;
}
/**
 * Every cell, in dependency order.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RunAll".
 */
export interface RunAll {
  kind?: Kind25;
  restart?: Restart;
}
/**
 * Every cell whose result no longer matches its code or inputs.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RunStale".
 */
export interface RunStale {
  kind?: Kind26;
}
/**
 * Every cell above this one in notebook order (not the cell itself).
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RunAbove".
 */
export interface RunAbove {
  id: Id16;
  kind?: Kind27;
}
/**
 * This cell and every cell below it in notebook order.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RunBelow".
 */
export interface RunBelow {
  id: Id17;
  kind?: Kind28;
}
/**
 * This cell and every cell it depends on, run again even when they hold values.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RunUpstream".
 */
export interface RunUpstream {
  id: Id18;
  kind?: Kind29;
}
/**
 * This cell and every cell that reads it, in either reactivity mode.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RunDownstream".
 */
export interface RunDownstream {
  id: Id19;
  kind?: Kind30;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookRunOutput".
 */
export interface NotebookRunOutput {
  blob?: Blob11;
  cells?: Cells10;
  counts?: Counts1;
  env: NotebookEnvInfo;
  estimate_s?: EstimateS;
  failures?: Failures;
  notebook_guide?: NotebookGuide | null;
  path: Path36;
  plan: Plan;
  plan_total?: PlanTotal;
  queued_behind?: QueuedBehind;
  reactivity: Reactivity1;
  ref_type?: RefType10;
  result_name?: ResultName14;
  run_id: RunId12;
  see?: See;
  status: Status17;
  summary?: Summary2;
}
export interface Counts1 {
  [k: string]: number;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookSettingsInput".
 */
export interface NotebookSettingsInput {
  autoreload?: Autoreload;
  dataframe?: Dataframe;
  env?: Env1;
  outputs_in_git?: OutputsInGit;
  path: Path37;
  reactivity?: Reactivity2;
  sql_row_limit?: SqlRowLimit;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookSettingsOutput".
 */
export interface NotebookSettingsOutput {
  blob?: Blob12;
  notebook_guide?: NotebookGuide | null;
  path: Path38;
  ref_type?: RefType11;
  result_name?: ResultName15;
  settings: Settings2;
}
export interface Settings2 {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookShowOutputInput".
 */
export interface NotebookShowOutputInput {
  cell?: Cell2;
  image?: Image;
  part?: Part1;
  path: Path39;
  row_limit?: RowLimit1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookShowOutputOutput".
 */
export interface NotebookShowOutputOutput {
  available?: Available;
  blob?: Blob13;
  cell_error?: NotebookErrorInfo | null;
  cell_id: CellId16;
  cell_name: CellName;
  chart_ref?: NotebookStoredChart | null;
  chart_spec?: Untrusted | null;
  chart_summary?: Untrusted | null;
  image?: NotebookShownImage | null;
  kind: Kind31;
  markdown?: Untrusted | null;
  note?: Note13;
  notebook_guide?: NotebookGuide | null;
  path: Path40;
  ref_type?: RefType12;
  result_name?: ResultName16;
  table?: NotebookShownTable | null;
  text?: Untrusted | null;
}
/**
 * A chart too large to carry in the reply, by the file the notebook keeps
 * its spec in beside itself (``<sha256>.json`` in the notebook's output
 * folder): where a chat or a thread reads the spec to draw it.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookStoredChart".
 */
export interface NotebookStoredChart {
  bytes: Bytes2;
  sha256: Sha2562;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookShownImage".
 */
export interface NotebookShownImage {
  bytes: Bytes3;
  index: Index6;
  mime: Mime1;
  sha256: Sha2563;
  total: Total10;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookShownTable".
 */
export interface NotebookShownTable {
  columns: Columns10;
  rows: Untrusted;
  shown_rows: ShownRows;
  total_columns: TotalColumns1;
  total_rows: TotalRows2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookWidgetInput".
 */
export interface NotebookWidgetInput {
  action?: Action4;
  limit?: Limit34;
  model_id?: ModelId1;
  offset?: Offset9;
  path: Path41;
  state?: State13;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "NotebookWidgetOutput".
 */
export interface NotebookWidgetOutput {
  blob?: Blob14;
  notebook_guide?: NotebookGuide | null;
  page?: NotebookPage | null;
  path: Path42;
  ref_type?: RefType13;
  result_name?: ResultName17;
  run?: NotebookRunOutput | null;
  widgets?: Widgets1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgActivityInput".
 */
export interface PgActivityInput {
  connection: Connection73;
  limit?: Limit35;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgActivityResult".
 */
export interface PgActivityResult {
  sessions?: Sessions;
  truncated?: Truncated19;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgActivityRow".
 */
export interface PgActivityRow {
  application_name?: ApplicationName;
  backend_type?: BackendType;
  datname?: Datname;
  pid: Pid;
  query?: Query9;
  query_start?: QueryStart;
  state?: State14;
  usename?: Usename;
  wait_event_type?: WaitEventType;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgCancelQueryInput".
 */
export interface PgCancelQueryInput {
  connection: Connection74;
  pid: Pid1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgCancelQueryResult".
 */
export interface PgCancelQueryResult {
  cancelled?: Cancelled1;
  pid: Pid2;
  requested?: Requested6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgDatabaseRow".
 */
export interface PgDatabaseRow {
  datallowconn?: Datallowconn;
  datname: Datname1;
  encoding?: Encoding;
  owner?: Owner4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgListDatabasesInput".
 */
export interface PgListDatabasesInput {
  connection: Connection75;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgListDatabasesResult".
 */
export interface PgListDatabasesResult {
  databases?: Databases2;
  truncated?: Truncated20;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgTableSizeRow".
 */
export interface PgTableSizeRow {
  approx_rows?: ApproxRows;
  kind?: Kind32;
  name: Name47;
  schema_name: SchemaName3;
  total_bytes?: TotalBytes2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgTableSizesInput".
 */
export interface PgTableSizesInput {
  connection: Connection76;
  limit?: Limit36;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PgTableSizesResult".
 */
export interface PgTableSizesResult {
  relations?: Relations;
  truncated?: Truncated21;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PresignUrlInput".
 */
export interface PresignUrlInput {
  bucket?: Bucket10;
  connection: Connection77;
  expires_in?: ExpiresIn;
  key: Key5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PresignUrlResult".
 */
export interface PresignUrlResult {
  bucket: Bucket11;
  expires_in?: ExpiresIn1;
  key: Key6;
  url?: Url2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PreviewTabularInput".
 */
export interface PreviewTabularInput {
  bucket?: Bucket12;
  connection: Connection78;
  file_format?: FileFormat;
  key: Key7;
  max_rows?: MaxRows;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PreviewTabularResult".
 */
export interface PreviewTabularResult {
  bucket: Bucket13;
  columns?: Columns11;
  format?: Format3;
  key: Key8;
  note?: Note14;
  rows?: Rows3;
  sampled_rows?: SampledRows;
  truncated?: Truncated22;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PutObjectInput".
 */
export interface PutObjectInput {
  bucket?: Bucket14;
  connection: Connection79;
  content: Content1;
  content_type?: ContentType2;
  key: Key9;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "PutObjectResult".
 */
export interface PutObjectResult {
  bucket: Bucket15;
  etag?: Etag1;
  key: Key10;
  size?: Size2;
  version_id?: VersionId4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QualifierBiteInput".
 */
export interface QualifierBiteInput {
  connection: Connection80;
  predicate: Predicate;
  source: Source9;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QualifierBiteResult".
 */
export interface QualifierBiteResult {
  note: Note15;
  rows_after: RowsAfter;
  rows_before: RowsBefore;
  rows_removed: RowsRemoved;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QueryBySql".
 */
export interface QueryBySql {
  background?: Background2;
  connection: Connection81;
  /**
   * Set this only after a write was refused for downstream impact. It records what you are deliberately breaking and why, and lets the rest of the migration run without challenging every step.
   */
  intent?: BreakIntent | null;
  limit?: Limit37;
  mode?: Mode15;
  params?: Params1;
  result_name?: ResultName18;
  sql: Sql2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "QueryByTable".
 */
export interface QueryByTable {
  background?: Background3;
  columns?: Columns12;
  connection: Connection82;
  limit?: Limit38;
  mode?: Mode16;
  result_name?: ResultName19;
  table: Table3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RdsClusterCard".
 */
export interface RdsClusterCard {
  database?: Database5;
  engine?: Engine8;
  engine_mode?: EngineMode1;
  engine_version?: EngineVersion5;
  host?: Host7;
  iam_auth_enabled?: IamAuthEnabled3;
  identifier: Identifier15;
  members?: Members1;
  multi_az?: MultiAz3;
  port?: Port6;
  reader_host?: ReaderHost1;
  status?: Status18;
  storage_encrypted?: StorageEncrypted2;
  tags?: Tags4;
}
export interface Tags4 {
  [k: string]: string;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RdsListClustersInput".
 */
export interface RdsListClustersInput {
  connection: Connection83;
  limit?: Limit39;
  region?: Region15;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RdsListClustersResult".
 */
export interface RdsListClustersResult {
  clusters?: Clusters1;
  region?: Region16;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ReadObjectInput".
 */
export interface ReadObjectInput {
  bucket?: Bucket16;
  connection: Connection84;
  key: Key11;
  length?: Length;
  offset?: Offset10;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ReadObjectResult".
 */
export interface ReadObjectResult {
  binary?: Binary;
  bucket: Bucket17;
  key: Key12;
  next_offset?: NextOffset3;
  offset?: Offset11;
  returned_bytes?: ReturnedBytes;
  text?: Text5;
  total_size?: TotalSize;
  truncated?: Truncated23;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RelationCard".
 */
export interface RelationCard {
  kind?: Kind33;
  name: Name48;
  urn?: Urn11;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsCancelQueryInput".
 */
export interface RsCancelQueryInput {
  connection: Connection85;
  pid: Pid3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsCancelQueryResult".
 */
export interface RsCancelQueryResult {
  pid: Pid4;
  requested?: Requested7;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsDatabaseRow".
 */
export interface RsDatabaseRow {
  allow_connections?: AllowConnections;
  name: Name49;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsListDatabasesInput".
 */
export interface RsListDatabasesInput {
  connection: Connection86;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsListDatabasesResult".
 */
export interface RsListDatabasesResult {
  databases?: Databases3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsRunningQueriesInput".
 */
export interface RsRunningQueriesInput {
  connection: Connection87;
  include_recent?: IncludeRecent;
  limit?: Limit40;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsRunningQueriesResult".
 */
export interface RsRunningQueriesResult {
  queries?: Queries;
  truncated?: Truncated24;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsRunningQueryRow".
 */
export interface RsRunningQueryRow {
  query_id: QueryId4;
  query_text?: QueryText;
  session_id: SessionId;
  start_time?: StartTime;
  status?: Status19;
  user_id?: UserId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsTableInfoInput".
 */
export interface RsTableInfoInput {
  connection: Connection88;
  limit?: Limit41;
  schema_filter?: SchemaFilter;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsTableInfoResult".
 */
export interface RsTableInfoResult {
  tables?: Tables6;
  truncated?: Truncated25;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RsTableInfoRow".
 */
export interface RsTableInfoRow {
  diststyle?: Diststyle;
  pct_used?: PctUsed;
  rows?: Rows4;
  schema_name: SchemaName4;
  size_mb?: SizeMb;
  sortkey1?: Sortkey1;
  table_name: TableName1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RunJobInput".
 */
export interface RunJobInput {
  connection: Connection89;
  job_id: JobId16;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "RunJobResult".
 */
export interface RunJobResult {
  run_id?: RunId13;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SchemaDescribe".
 */
export interface SchemaDescribe {
  connection: Connection90;
  mode?: Mode17;
  table: Table4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SchemaList".
 */
export interface SchemaList {
  connection: Connection91;
  mode?: Mode18;
  verbose?: Verbose;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SearchResult".
 */
export interface SearchResult {
  snippet?: Snippet1;
  title?: Title14;
  url?: Url3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SearchToolsInput".
 */
export interface SearchToolsInput {
  app?: App;
  k?: K1;
  query: Query10;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SearchToolsOutput".
 */
export interface SearchToolsOutput {
  tools?: Tools;
}
/**
 * A discovered tool's advertised shape (what ``search_tools`` returns).
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "ToolCard".
 */
export interface ToolCard {
  app?: App1;
  description?: Description13;
  input_schema?: InputSchema1;
  name: Name50;
}
export interface InputSchema1 {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SetCommentInput".
 */
export interface SetCommentInput {
  comment: Comment7;
  connection: Connection92;
  full_name: FullName7;
  securable_type: SecurableType2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SetCommentResult".
 */
export interface SetCommentResult {
  comment: Comment8;
  full_name: FullName8;
  securable_type: SecurableType3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfCancelQueryInput".
 */
export interface SfCancelQueryInput {
  connection: Connection93;
  query_id: QueryId5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfCancelQueryResult".
 */
export interface SfCancelQueryResult {
  query_id: QueryId6;
  result?: Result1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfDtRefreshHistoryInput".
 */
export interface SfDtRefreshHistoryInput {
  connection: Connection94;
  limit?: Limit42;
  name?: Name51;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfDtRefreshHistoryResult".
 */
export interface SfDtRefreshHistoryResult {
  runs?: Runs3;
  truncated?: Truncated26;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfDtRefreshRun".
 */
export interface SfDtRefreshRun {
  full_name?: FullName9;
  name: Name52;
  refresh_action?: RefreshAction;
  refresh_end_time?: RefreshEndTime;
  refresh_start_time?: RefreshStartTime;
  refresh_trigger?: RefreshTrigger;
  state?: State15;
  state_code?: StateCode;
  state_message?: StateMessage1;
  target_lag_sec?: TargetLagSec;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfDynamicTableActionResult".
 */
export interface SfDynamicTableActionResult {
  name: Name53;
  requested: Requested8;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfDynamicTableCard".
 */
export interface SfDynamicTableCard {
  bytes?: Bytes4;
  full_name?: FullName10;
  name: Name54;
  refresh_mode?: RefreshMode;
  rows?: Rows5;
  scheduling_state?: SchedulingState;
  target_lag?: TargetLag;
  warehouse?: Warehouse;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfDynamicTableInput".
 */
export interface SfDynamicTableInput {
  connection: Connection95;
  name: Name55;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfListDynamicTablesInput".
 */
export interface SfListDynamicTablesInput {
  connection: Connection96;
  limit?: Limit43;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfListDynamicTablesResult".
 */
export interface SfListDynamicTablesResult {
  dynamic_tables?: DynamicTables;
  truncated?: Truncated27;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfListMaterializedViewsInput".
 */
export interface SfListMaterializedViewsInput {
  connection: Connection97;
  limit?: Limit44;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfListMaterializedViewsResult".
 */
export interface SfListMaterializedViewsResult {
  materialized_views?: MaterializedViews;
  truncated?: Truncated28;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfMaterializedViewCard".
 */
export interface SfMaterializedViewCard {
  behind_by?: BehindBy;
  comment?: Comment9;
  full_name?: FullName11;
  invalid?: Invalid;
  invalid_reason?: InvalidReason;
  is_secure?: IsSecure;
  name: Name56;
  refreshed_on?: RefreshedOn;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfListWarehousesInput".
 */
export interface SfListWarehousesInput {
  connection: Connection98;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfListWarehousesResult".
 */
export interface SfListWarehousesResult {
  warehouses?: Warehouses1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfWarehouseCard".
 */
export interface SfWarehouseCard {
  name: Name57;
  size?: Size3;
  state?: State16;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfRunningQueriesInput".
 */
export interface SfRunningQueriesInput {
  connection: Connection99;
  limit?: Limit45;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfRunningQueriesResult".
 */
export interface SfRunningQueriesResult {
  queries?: Queries1;
  truncated?: Truncated29;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfRunningQueryRow".
 */
export interface SfRunningQueryRow {
  execution_status?: ExecutionStatus;
  query_id: QueryId7;
  query_text?: QueryText1;
  start_time?: StartTime1;
  user_name?: UserName1;
  warehouse_name?: WarehouseName;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfWarehouseActionResult".
 */
export interface SfWarehouseActionResult {
  requested: Requested9;
  warehouse: Warehouse1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SfWarehouseInput".
 */
export interface SfWarehouseInput {
  connection: Connection100;
  warehouse: Warehouse2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SigmaElementSqlCard".
 */
export interface SigmaElementSqlCard {
  element_id: ElementId;
  error?: Error4;
  name?: Name58;
  sql?: Sql3;
  urn?: Urn12;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SigmaListWorkbooksInput".
 */
export interface SigmaListWorkbooksInput {
  connection: Connection101;
  search?: Search5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SigmaListWorkbooksResult".
 */
export interface SigmaListWorkbooksResult {
  note?: Note16;
  total?: Total11;
  workbooks?: Workbooks;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SigmaWorkbookCard".
 */
export interface SigmaWorkbookCard {
  folder?: Folder;
  name?: Name59;
  urn?: Urn13;
  workbook_id: WorkbookId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SigmaWorkbookSqlInput".
 */
export interface SigmaWorkbookSqlInput {
  connection: Connection102;
  workbook_id: WorkbookId1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SigmaWorkbookSqlResult".
 */
export interface SigmaWorkbookSqlResult {
  elements?: Elements;
  urn?: Urn14;
  workbook_id: WorkbookId2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SkillCard".
 */
export interface SkillCard {
  description?: Description14;
  name: Name60;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SpawnAgentInput".
 */
export interface SpawnAgentInput {
  agent?: Agent;
  background?: Background4;
  description?: Description15;
  prompt: Prompt;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SpawnAgentResult".
 */
export interface SpawnAgentResult {
  child_session_id?: ChildSessionId1;
  stats: AgentUsageStats;
  summary: Summary3;
}
/**
 * No arguments — lists every connection you can target.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SqlConnectionsInput".
 */
export interface SqlConnectionsInput {}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SqlConnectionsResult".
 */
export interface SqlConnectionsResult {
  connections?: Connections1;
}
/**
 * Where a SQL result came from, beside the rows it describes.
 *
 * Rendered inline on the result card — never behind a click — and copied onto
 * the receipt when the result is promoted. Names only: the connection, the
 * credential role the statement ran under, the engine. A secret never rides
 * here, and the connector that fills it never sees one.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SqlProvenance".
 */
export interface SqlProvenance {
  connection_id?: ConnectionId;
  connection_name?: ConnectionName;
  duration_ms?: DurationMs;
  engine?: Engine9;
  executed_at?: ExecutedAt;
  join_columns?: JoinColumns;
  role?: Role1;
  row_count?: RowCount2;
  sources?: Sources2;
  sql?: Sql4;
}
/**
 * The shared tabular result shape every rows producer returns, so the
 * editor chip and ``fetch_result`` paging are identical for every producer.
 *
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SqlQueryResult".
 */
export interface SqlQueryResult {
  blob?: BlobHandle | null;
  columns?: Columns13;
  cost_warnings?: CostWarnings1;
  job_id?: JobId17;
  note?: Note17;
  preview_rows?: PreviewRows;
  provenance?: SqlProvenance | null;
  ref_type?: RefType14;
  result_name?: ResultName20;
  row_count?: RowCount3;
  truncated?: Truncated30;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SqlSchemaResult".
 */
export interface SqlSchemaResult {
  columns?: Columns14;
  relations?: Relations1;
  table?: Table5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SsoLoginInput".
 */
export interface SsoLoginInput {
  connection: Connection103;
  force?: Force;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "SsoLoginResultModel".
 */
export interface SsoLoginResultModel {
  accounts?: Accounts;
  already_valid?: AlreadyValid;
  connection: Connection104;
  expires_at?: ExpiresAt;
  seconds_remaining?: SecondsRemaining;
  sso_start_url: SsoStartUrl;
  user_code?: UserCode;
  verification_uri?: VerificationUri;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "StartPipelineUpdateInput".
 */
export interface StartPipelineUpdateInput {
  connection: Connection105;
  full_refresh?: FullRefresh;
  pipeline_id: PipelineId3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "StartPipelineUpdateResult".
 */
export interface StartPipelineUpdateResult {
  full_refresh?: FullRefresh1;
  pipeline_id: PipelineId4;
  update_id?: UpdateId;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "StorageGroup".
 */
export interface StorageGroup {
  object_count?: ObjectCount;
  prefix: Prefix4;
  total_bytes?: TotalBytes3;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "StorageSummaryInput".
 */
export interface StorageSummaryInput {
  bucket?: Bucket18;
  connection: Connection106;
  group_depth?: GroupDepth;
  max_keys?: MaxKeys2;
  prefix?: Prefix5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "StorageSummaryResult".
 */
export interface StorageSummaryResult {
  bucket: Bucket19;
  by_prefix?: ByPrefix;
  by_storage_class?: ByStorageClass;
  object_count?: ObjectCount1;
  prefix?: Prefix6;
  source?: Source10;
  total_bytes?: TotalBytes4;
  truncated?: Truncated31;
}
export interface ByStorageClass {
  [k: string]: StorageGroup;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TrinoKillQueryInput".
 */
export interface TrinoKillQueryInput {
  connection: Connection107;
  query_id: QueryId8;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TrinoKillQueryResult".
 */
export interface TrinoKillQueryResult {
  query_id: QueryId9;
  requested?: Requested10;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TrinoListCatalogsInput".
 */
export interface TrinoListCatalogsInput {
  connection: Connection108;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TrinoListCatalogsResult".
 */
export interface TrinoListCatalogsResult {
  catalogs?: Catalogs1;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TrinoListSchemasInput".
 */
export interface TrinoListSchemasInput {
  catalog: Catalog5;
  connection: Connection109;
  limit?: Limit46;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TrinoListSchemasResult".
 */
export interface TrinoListSchemasResult {
  schemas?: Schemas1;
  truncated?: Truncated32;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TrinoQueryCard".
 */
export interface TrinoQueryCard {
  query?: Query11;
  query_id: QueryId10;
  source?: Source11;
  started?: Started;
  state?: State17;
  user?: User2;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TrinoRunningQueriesInput".
 */
export interface TrinoRunningQueriesInput {
  connection: Connection110;
  limit?: Limit47;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "TrinoRunningQueriesResult".
 */
export interface TrinoRunningQueriesResult {
  queries?: Queries2;
  truncated?: Truncated33;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "UpdateGrantsInput".
 */
export interface UpdateGrantsInput {
  connection: Connection111;
  full_name: FullName12;
  grant?: Grant;
  principal: Principal1;
  revoke?: Revoke;
  securable_type: SecurableType4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "UpdateGrantsResult".
 */
export interface UpdateGrantsResult {
  full_name: FullName13;
  granted?: Granted;
  principal: Principal2;
  revoked?: Revoked;
  securable_type: SecurableType5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "UseSkillInput".
 */
export interface UseSkillInput {
  name?: Name61;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "UseSkillResult".
 */
export interface UseSkillResult {
  body?: Body3;
  name?: Name62;
  skills?: Skills;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "WebFetchInput".
 */
export interface WebFetchInput {
  max_chars?: MaxChars1;
  url: Url4;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "WebFetchResult".
 */
export interface WebFetchResult {
  content?: Content2;
  status?: Status20;
  title?: Title15;
  total_chars?: TotalChars;
  truncated?: Truncated34;
  url?: Url5;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "WebSearchInput".
 */
export interface WebSearchInput {
  backend?: Backend;
  max_results?: MaxResults;
  query: Query12;
  region?: Region17;
  timelimit?: Timelimit;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "WebSearchResult".
 */
export interface WebSearchResult {
  query?: Query13;
  results?: Results;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "_ClassifyQuery".
 */
export interface _ClassifyQuery {
  kind: Kind34;
  new_name?: NewName;
  new_type?: NewType;
  old_type?: OldType;
  target: Target14;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "_FindQuery".
 */
export interface _FindQuery {
  kind?: Kind35;
  query: Query14;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "_MongoReadInput".
 */
export interface _MongoReadInput {
  collection?: Collection1;
  connection: Connection112;
  database?: Database6;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "_MongoReadResult".
 */
export interface _MongoReadResult {
  payload?: Payload;
}
export interface Payload {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "_NodeKnowledgeQuery".
 */
export interface _NodeKnowledgeQuery {
  include_neighbors?: IncludeNeighbors;
  urn: Urn15;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "_ReadInput".
 */
export interface _ReadInput {
  connection: Connection113;
  target?: Target15;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "_ReadResult".
 */
export interface _ReadResult {
  payload?: Payload1;
}
export interface Payload1 {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "_TraverseQuery".
 */
export interface _TraverseQuery {
  direction: Direction4;
  grain?: Grain;
  max_hops?: MaxHops3;
  urn: Urn16;
}
/**
 * This interface was referenced by `AlkeraToolManifest`'s JSON-Schema
 * via the `definition` "_UrnQuery".
 */
export interface _UrnQuery {
  max_hops?: MaxHops4;
  urn: Urn17;
}
