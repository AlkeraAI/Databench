// How an integration is named wherever one appears. Lives in lib/ because the
// browser portal and the editor webview both name these integrations, and the
// portal may not import the webview.

// Private on purpose: exporting the map would let a call site read it directly
// and skip the unknown-plugin fallback below.
const PLUGIN_LABELS: Record<string, string> = {
  aws: "AWS",
  snowflake: "Snowflake",
  bigquery: "BigQuery",
  duckdb_local: "DuckDB",
  postgres: "Postgres",
  redshift: "Redshift",
  mysql: "MySQL",
  clickhouse: "ClickHouse",
  databricks: "Databricks",
  druid: "Druid",
  elasticsearch: "Elasticsearch",
  mongodb: "MongoDB",
  kafka: "Kafka",
  sqlite: "SQLite",
  trino: "Trino",
  generic_sql: "Generic SQL",
  dbt: "dbt",
  airflow: "Airflow",
  tableau: "Tableau",
  looker: "Looker",
  hex: "Hex",
  sigma: "Sigma",
  fivetran: "Fivetran",
  gdocs: "Google Docs",
  confluence: "Confluence",
  notion: "Notion",
};

/** An integration's display name, falling back to its raw id so a plugin added
 *  after this map still reads as something. */
export function pluginLabel(name: string): string {
  return PLUGIN_LABELS[name] ?? name;
}
