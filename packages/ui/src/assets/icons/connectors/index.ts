// The connector brand registry, keyed by connector/plugin id (the CLI plugin catalog
// vocabulary). Each entry uses vendor artwork copied verbatim from alkera-public-site
// public/assets/data and inlined at build time by vite-plugin-svgr. A dark file is declared
// only when the vendor ships a different drawing. One file otherwise serves both schemes.
// Never recolor a mark. Fix a wrong logo by replacing the SVG. An id absent here takes the
// plug fallback, generic_sql among today's connectors.

import type { ComponentType, SVGProps } from "react";

import AirflowDark from "./airflow/airflow-mark-dark.svg?react";
import Airflow from "./airflow/airflow-mark.svg?react";
import AwsDark from "./aws/aws-mark-dark.svg?react";
import Aws from "./aws/aws-mark.svg?react";
import Bigquery from "./bigquery/bigquery-mark.svg?react";
import Clickhouse from "./clickhouse/clickhouse-mark.svg?react";
import Confluence from "./confluence/confluence-mark.svg?react";
import Databricks from "./databricks/databricks-mark.svg?react";
import Dbt from "./dbt/dbt-mark.svg?react";
import DruidDark from "./druid/druid-mark-dark.svg?react";
import Druid from "./druid/druid-mark.svg?react";
import DuckdbDark from "./duckdb/duckdb-mark-dark.svg?react";
import Duckdb from "./duckdb/duckdb-mark.svg?react";
import ElasticsearchDark from "./elasticsearch/elasticsearch-mark-dark.svg?react";
import Elasticsearch from "./elasticsearch/elasticsearch-mark.svg?react";
import Fivetran from "./fivetran/fivetran-mark.svg?react";
import Gdocs from "./gdocs/gdocs-mark.svg?react";
import Hex from "./hex/hex-mark.svg?react";
import KafkaDark from "./kafka/kafka-mark-dark.svg?react";
import Kafka from "./kafka/kafka-mark.svg?react";
import Looker from "./looker/looker-mark.svg?react";
import MongodbDark from "./mongodb/mongodb-mark-dark.svg?react";
import Mongodb from "./mongodb/mongodb-mark.svg?react";
import Mysql from "./mysql/mysql-mark.svg?react";
import Notion from "./notion/notion-mark.svg?react";
import PlanetscaleDark from "./planetscale/planetscale-mark-dark.svg?react";
import Planetscale from "./planetscale/planetscale-mark.svg?react";
import Postgresql from "./postgresql/postgresql-mark.svg?react";
import Redshift from "./redshift/redshift-mark.svg?react";
import SigmaDark from "./sigma/sigma-mark-dark.svg?react";
import Sigma from "./sigma/sigma-mark.svg?react";
import Snowflake from "./snowflake/snowflake-mark.svg?react";
import Sqlite from "./sqlite/sqlite-mark.svg?react";
import Tableau from "./tableau/tableau-mark.svg?react";
import Tinybird from "./tinybird/tinybird-mark.svg?react";
import TinybirdDark from "./tinybird/tinybird-mark-dark.svg?react";
import Trino from "./trino/trino-mark.svg?react";

export type MarkComponent = ComponentType<SVGProps<SVGSVGElement>>;

/** A connector brand uses one vendor mark unless the vendor supplies a distinct dark mark. */
export interface ConnectorBrand {
  title: string;
  Light: MarkComponent;
  Dark?: MarkComponent;
}

export const CONNECTOR_BRANDS: Record<string, ConnectorBrand> = {
  airflow: { title: "Apache Airflow", Light: Airflow, Dark: AirflowDark },
  aws: { title: "AWS", Light: Aws, Dark: AwsDark },
  bigquery: { title: "Google BigQuery", Light: Bigquery },
  clickhouse: { title: "ClickHouse", Light: Clickhouse },
  confluence: { title: "Atlassian Confluence", Light: Confluence },
  databricks: { title: "Databricks", Light: Databricks },
  dbt: { title: "dbt", Light: Dbt },
  druid: { title: "Apache Druid", Light: Druid, Dark: DruidDark },
  duckdb_local: { title: "DuckDB", Light: Duckdb, Dark: DuckdbDark },
  elasticsearch: { title: "Elasticsearch", Light: Elasticsearch, Dark: ElasticsearchDark },
  fivetran: { title: "Fivetran", Light: Fivetran },
  gdocs: { title: "Google Docs", Light: Gdocs },
  hex: { title: "Hex", Light: Hex },
  kafka: { title: "Apache Kafka", Light: Kafka, Dark: KafkaDark },
  looker: { title: "Looker", Light: Looker },
  mongodb: { title: "MongoDB", Light: Mongodb, Dark: MongodbDark },
  mysql: { title: "MySQL", Light: Mysql },
  notion: { title: "Notion", Light: Notion },
  planetscale: { title: "PlanetScale", Light: Planetscale, Dark: PlanetscaleDark },
  postgres: { title: "PostgreSQL", Light: Postgresql },
  redshift: { title: "Amazon Redshift", Light: Redshift },
  sigma: { title: "Sigma", Light: Sigma, Dark: SigmaDark },
  snowflake: { title: "Snowflake", Light: Snowflake },
  sqlite: { title: "SQLite", Light: Sqlite },
  tableau: { title: "Tableau", Light: Tableau },
  tinybird: { title: "Tinybird", Light: Tinybird, Dark: TinybirdDark },
  trino: { title: "Trino", Light: Trino },
};
