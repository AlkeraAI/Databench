/**
 * Auto-generated from packages/shared-openapi/tool-manifest.json.
 * Do not edit by hand. Run `make gen-tool-manifest` after changing
 * an alkera tool's Input/Output Pydantic models.
 *
 * The SHARED SOURCE OF TRUTH for the alkera tool surface. `AlkeraToolName`
 * is the exhaustive set of tool names the backend exposes; the tool-card
 * registry keys off it, so a backend rename becomes a `tsc` error here.
 */

import type * as M from "./toolSchemas";

/** Maps every generated `$def` name to its concrete TS type. */
export interface ToolTypeMap {
  AffectedCard: M.AffectedCard;
  AgentTypeInfo: M.AgentTypeInfo;
  AgentUsageStats: M.AgentUsageStats;
  AttachmentInput: M.AttachmentInput;
  BackgroundCancelInput: M.BackgroundCancelInput;
  BackgroundCancelResult: M.BackgroundCancelResult;
  BackgroundJobView: M.BackgroundJobView;
  BackgroundStatusInput: M.BackgroundStatusInput;
  BackgroundStatusResult: M.BackgroundStatusResult;
  BashInput: M.BashInput;
  BashResult: M.BashResult;
  BlobCreateInput: M.BlobCreateInput;
  BlobCreateOutput: M.BlobCreateOutput;
  BlobDeleteInput: M.BlobDeleteInput;
  BlobDeleteOutput: M.BlobDeleteOutput;
  BlobDeriveInput: M.BlobDeriveInput;
  BlobHandle: M.BlobHandle;
  BlobInfoInput: M.BlobInfoInput;
  BlobInfoOutput: M.BlobInfoOutput;
  BlobMaterializeInput: M.BlobMaterializeInput;
  BlobMaterializeOutput: M.BlobMaterializeOutput;
  BlobProfileInput: M.BlobProfileInput;
  BlobProfileOutput: M.BlobProfileOutput;
  BlobQueryInput: M.BlobQueryInput;
  BqCancelJobInput: M.BqCancelJobInput;
  BqCancelJobResult: M.BqCancelJobResult;
  BqDatasetCard: M.BqDatasetCard;
  BqFieldCard: M.BqFieldCard;
  BqGetJobInput: M.BqGetJobInput;
  BqGetJobResult: M.BqGetJobResult;
  BqGetTableInput: M.BqGetTableInput;
  BqGetTableResult: M.BqGetTableResult;
  BqJobCard: M.BqJobCard;
  BqListDatasetsInput: M.BqListDatasetsInput;
  BqListDatasetsResult: M.BqListDatasetsResult;
  BqListJobsInput: M.BqListJobsInput;
  BqListJobsResult: M.BqListJobsResult;
  BqListTablesInput: M.BqListTablesInput;
  BqListTablesResult: M.BqListTablesResult;
  BqTableCard: M.BqTableCard;
  BreakIntent: M.BreakIntent;
  BucketCard: M.BucketCard;
  BucketInfoInput: M.BucketInfoInput;
  BucketInfoResult: M.BucketInfoResult;
  CallGraphPythonInput: M.CallGraphPythonInput;
  CallGraphPythonResult: M.CallGraphPythonResult;
  CallIntegrationSdkInput: M.CallIntegrationSdkInput;
  CallIntegrationSdkResult: M.CallIntegrationSdkResult;
  CallToolInput: M.CallToolInput;
  CallToolOutput: M.CallToolOutput;
  CancelRunInput: M.CancelRunInput;
  CancelRunResult: M.CancelRunResult;
  CatalogCard: M.CatalogCard;
  CellAfterOp: M.CellAfterOp;
  CellNotice: M.CellNotice;
  ChDatabaseCard: M.ChDatabaseCard;
  ChKillQueryInput: M.ChKillQueryInput;
  ChKillQueryResult: M.ChKillQueryResult;
  ChListDatabasesInput: M.ChListDatabasesInput;
  ChListDatabasesResult: M.ChListDatabasesResult;
  ChProcessCard: M.ChProcessCard;
  ChProcessesInput: M.ChProcessesInput;
  ChProcessesResult: M.ChProcessesResult;
  ChTableSizeCard: M.ChTableSizeCard;
  ChTableSizesInput: M.ChTableSizesInput;
  ChTableSizesResult: M.ChTableSizesResult;
  ClassifyResult: M.ClassifyResult;
  ClusterActionResult: M.ClusterActionResult;
  ClusterCard: M.ClusterCard;
  ClusterInput: M.ClusterInput;
  ColumnCard: M.ColumnCard;
  ColumnInfo: M.ColumnInfo;
  ColumnProfile: M.ColumnProfile;
  ConnectionCard: M.ConnectionCard;
  ConnectionInfoInput: M.ConnectionInfoInput;
  ConnectionInfoResult: M.ConnectionInfoResult;
  ContextCard: M.ContextCard;
  ContextEditInput: M.ContextEditInput;
  ContextGetInput: M.ContextGetInput;
  ContextNoteInput: M.ContextNoteInput;
  ContextSearchInput: M.ContextSearchInput;
  ContextSearchResult: M.ContextSearchResult;
  ContextWriteResult: M.ContextWriteResult;
  CreateSchemaInput: M.CreateSchemaInput;
  CreateSchemaResult: M.CreateSchemaResult;
  CreateSnapshotInput: M.CreateSnapshotInput;
  CreateSnapshotResult: M.CreateSnapshotResult;
  CurrentUserInput: M.CurrentUserInput;
  CurrentUserResult: M.CurrentUserResult;
  DataJoinInput: M.DataJoinInput;
  DbxColumnCard: M.DbxColumnCard;
  DbxListWarehousesInput: M.DbxListWarehousesInput;
  DbxListWarehousesResult: M.DbxListWarehousesResult;
  DbxWarehouseActionResult: M.DbxWarehouseActionResult;
  DbxWarehouseCard: M.DbxWarehouseCard;
  DbxWarehouseInput: M.DbxWarehouseInput;
  DeleteCellOp: M.DeleteCellOp;
  DeleteObjectInput: M.DeleteObjectInput;
  DeleteObjectResult: M.DeleteObjectResult;
  DescribeClusterInput: M.DescribeClusterInput;
  DescribeClusterResult: M.DescribeClusterResult;
  DescribeInstanceInput: M.DescribeInstanceInput;
  DescribeInstanceResult: M.DescribeInstanceResult;
  DescribeParametersInput: M.DescribeParametersInput;
  DescribeParametersResult: M.DescribeParametersResult;
  DocsGetInput: M.DocsGetInput;
  DocsGetResult: M.DocsGetResult;
  DocsSearchInput: M.DocsSearchInput;
  DocsSearchResult: M.DocsSearchResult;
  DocsSourcesInput: M.DocsSourcesInput;
  DocsSourcesResult: M.DocsSourcesResult;
  DocumentHitCard: M.DocumentHitCard;
  DocumentSectionCard: M.DocumentSectionCard;
  DownloadLogPortionInput: M.DownloadLogPortionInput;
  DownloadLogPortionResult: M.DownloadLogPortionResult;
  DruidCancelQueryInput: M.DruidCancelQueryInput;
  DruidConnectionInput: M.DruidConnectionInput;
  DruidItemsResult: M.DruidItemsResult;
  DruidTaskInput: M.DruidTaskInput;
  EdgeCard: M.EdgeCard;
  EdgesResult: M.EdgesResult;
  EditApply: M.EditApply;
  EditCellOp: M.EditCellOp;
  EditCreate: M.EditCreate;
  EditImportLineage: M.EditImportLineage;
  EditSave: M.EditSave;
  Effect: M.Effect;
  ElasticsearchCountInput: M.ElasticsearchCountInput;
  ElasticsearchCountResult: M.ElasticsearchCountResult;
  ElasticsearchEsqlInput: M.ElasticsearchEsqlInput;
  ElasticsearchEsqlResult: M.ElasticsearchEsqlResult;
  ElasticsearchSearchInput: M.ElasticsearchSearchInput;
  ElasticsearchSearchResult: M.ElasticsearchSearchResult;
  EnvironmentCaptureInput: M.EnvironmentCaptureInput;
  EnvironmentCaptureResult: M.EnvironmentCaptureResult;
  EnvironmentRecreateInput: M.EnvironmentRecreateInput;
  EnvironmentRecreateResult: M.EnvironmentRecreateResult;
  FetchResultInput: M.FetchResultInput;
  FetchResultOutput: M.FetchResultOutput;
  FilingInput: M.FilingInput;
  FindObjectsInput: M.FindObjectsInput;
  FindObjectsResult: M.FindObjectsResult;
  FindResult: M.FindResult;
  FivetranColumnCard: M.FivetranColumnCard;
  FivetranPipelineCard: M.FivetranPipelineCard;
  FivetranPipelineStatusInput: M.FivetranPipelineStatusInput;
  FivetranPipelineStatusResult: M.FivetranPipelineStatusResult;
  FivetranTableColumnsInput: M.FivetranTableColumnsInput;
  FivetranTableColumnsResult: M.FivetranTableColumnsResult;
  Gap: M.Gap;
  GenerateAuthTokenInput: M.GenerateAuthTokenInput;
  GenerateAuthTokenResult: M.GenerateAuthTokenResult;
  GetGrantsInput: M.GetGrantsInput;
  GetGrantsResult: M.GetGrantsResult;
  GetJobInput: M.GetJobInput;
  GetJobResult: M.GetJobResult;
  GetPipelineInput: M.GetPipelineInput;
  GetPipelineResult: M.GetPipelineResult;
  GetRunInput: M.GetRunInput;
  GetRunOutputInput: M.GetRunOutputInput;
  GetRunOutputResult: M.GetRunOutputResult;
  GetRunResult: M.GetRunResult;
  GetTableInput: M.GetTableInput;
  GetTableResult: M.GetTableResult;
  GrantCard: M.GrantCard;
  GraphCellSummary: M.GraphCellSummary;
  GraphEdgeCard: M.GraphEdgeCard;
  GraphEditInput: M.GraphEditInput;
  GraphEditResult: M.GraphEditResult;
  GraphErrorInfo: M.GraphErrorInfo;
  GraphFileCard: M.GraphFileCard;
  GraphNodeCard: M.GraphNodeCard;
  GraphOperation: M.GraphOperation;
  GraphQueryInput: M.GraphQueryInput;
  GraphQueryResult: M.GraphQueryResult;
  GraphStatsCard: M.GraphStatsCard;
  GraphSummary: M.GraphSummary;
  GraphView: M.GraphView;
  HeadObjectInput: M.HeadObjectInput;
  HeadObjectResult: M.HeadObjectResult;
  HexListProjectsInput: M.HexListProjectsInput;
  HexListProjectsResult: M.HexListProjectsResult;
  HexProjectCard: M.HexProjectCard;
  HexProjectRunsInput: M.HexProjectRunsInput;
  HexProjectRunsResult: M.HexProjectRunsResult;
  HexRunCard: M.HexRunCard;
  InsertCellOp: M.InsertCellOp;
  JobCard: M.JobCard;
  JoinSource: M.JoinSource;
  KafkaCardsResult: M.KafkaCardsResult;
  KafkaConnectionInput: M.KafkaConnectionInput;
  KafkaPeekInput: M.KafkaPeekInput;
  KnowledgeAnnotation: M.KnowledgeAnnotation;
  KnowledgeConnectionCard: M.KnowledgeConnectionCard;
  KnowledgeOverflow: M.KnowledgeOverflow;
  ListAgentTypesInput: M.ListAgentTypesInput;
  ListAgentTypesResult: M.ListAgentTypesResult;
  ListBucketsInput: M.ListBucketsInput;
  ListBucketsResult: M.ListBucketsResult;
  ListCatalogsInput: M.ListCatalogsInput;
  ListCatalogsResult: M.ListCatalogsResult;
  ListClustersInput: M.ListClustersInput;
  ListClustersResult: M.ListClustersResult;
  ListInstancesInput: M.ListInstancesInput;
  ListInstancesResult: M.ListInstancesResult;
  ListJobsInput: M.ListJobsInput;
  ListJobsResult: M.ListJobsResult;
  ListLogFilesInput: M.ListLogFilesInput;
  ListLogFilesResult: M.ListLogFilesResult;
  ListObjectsInput: M.ListObjectsInput;
  ListObjectsResult: M.ListObjectsResult;
  ListPipelinesInput: M.ListPipelinesInput;
  ListPipelinesResult: M.ListPipelinesResult;
  ListPluginsInput: M.ListPluginsInput;
  ListPluginsOutput: M.ListPluginsOutput;
  ListRunsInput: M.ListRunsInput;
  ListRunsResult: M.ListRunsResult;
  ListSchemasInput: M.ListSchemasInput;
  ListSchemasResult: M.ListSchemasResult;
  ListSnapshotsInput: M.ListSnapshotsInput;
  ListSnapshotsResult: M.ListSnapshotsResult;
  ListTablesInput: M.ListTablesInput;
  ListTablesResult: M.ListTablesResult;
  LogFileCard: M.LogFileCard;
  LookerDashboardCard: M.LookerDashboardCard;
  LookerDashboardTilesInput: M.LookerDashboardTilesInput;
  LookerDashboardTilesResult: M.LookerDashboardTilesResult;
  LookerListDashboardsInput: M.LookerListDashboardsInput;
  LookerListDashboardsResult: M.LookerListDashboardsResult;
  LookerTileCard: M.LookerTileCard;
  ManageTasksInput: M.ManageTasksInput;
  ManageTasksOutput: M.ManageTasksOutput;
  MetricSeries: M.MetricSeries;
  MetricsInput: M.MetricsInput;
  MetricsResult: M.MetricsResult;
  MongoAggregateInput: M.MongoAggregateInput;
  MongoAggregateResult: M.MongoAggregateResult;
  MoveCellOp: M.MoveCellOp;
  MyDatabaseRow: M.MyDatabaseRow;
  MyKillQueryInput: M.MyKillQueryInput;
  MyKillQueryResult: M.MyKillQueryResult;
  MyListDatabasesInput: M.MyListDatabasesInput;
  MyListDatabasesResult: M.MyListDatabasesResult;
  MyProcessRow: M.MyProcessRow;
  MyProcesslistInput: M.MyProcesslistInput;
  MyProcesslistResult: M.MyProcesslistResult;
  MyTableSizeRow: M.MyTableSizeRow;
  MyTableSizesInput: M.MyTableSizesInput;
  MyTableSizesResult: M.MyTableSizesResult;
  NodeCard: M.NodeCard;
  NodeFilter: M.NodeFilter;
  NodeKnowledgeBrief: M.NodeKnowledgeBrief;
  NodeKnowledgeCard: M.NodeKnowledgeCard;
  NodeKnowledgeResult: M.NodeKnowledgeResult;
  NotPortable: M.NotPortable;
  NotebookCellBrief: M.NotebookCellBrief;
  NotebookCellState: M.NotebookCellState;
  NotebookCellsInput: M.NotebookCellsInput;
  NotebookCellsOutput: M.NotebookCellsOutput;
  NotebookCreateInput: M.NotebookCreateInput;
  NotebookCreateOutput: M.NotebookCreateOutput;
  NotebookEditInput: M.NotebookEditInput;
  NotebookEditOutput: M.NotebookEditOutput;
  NotebookEnvInfo: M.NotebookEnvInfo;
  NotebookEnvInput: M.NotebookEnvInput;
  NotebookEnvOutput: M.NotebookEnvOutput;
  NotebookErrorInfo: M.NotebookErrorInfo;
  NotebookFramePage: M.NotebookFramePage;
  NotebookGraphCell: M.NotebookGraphCell;
  NotebookGraphError: M.NotebookGraphError;
  NotebookGraphInput: M.NotebookGraphInput;
  NotebookGraphOutput: M.NotebookGraphOutput;
  NotebookGraphSummary: M.NotebookGraphSummary;
  NotebookGuide: M.NotebookGuide;
  NotebookImage: M.NotebookImage;
  NotebookInspectInput: M.NotebookInspectInput;
  NotebookInspectOutput: M.NotebookInspectOutput;
  NotebookKernelInfo: M.NotebookKernelInfo;
  NotebookKernelInput: M.NotebookKernelInput;
  NotebookKernelOutput: M.NotebookKernelOutput;
  NotebookMore: M.NotebookMore;
  NotebookNewCell: M.NotebookNewCell;
  NotebookOutputInput: M.NotebookOutputInput;
  NotebookOutputOutput: M.NotebookOutputOutput;
  NotebookOutputSummary: M.NotebookOutputSummary;
  NotebookPackage: M.NotebookPackage;
  NotebookPage: M.NotebookPage;
  NotebookPlanStep: M.NotebookPlanStep;
  NotebookPresence: M.NotebookPresence;
  NotebookQueuedRun: M.NotebookQueuedRun;
  NotebookReadInput: M.NotebookReadInput;
  NotebookReadOutput: M.NotebookReadOutput;
  NotebookRunAttribution: M.NotebookRunAttribution;
  NotebookRunBrief: M.NotebookRunBrief;
  NotebookRunInput: M.NotebookRunInput;
  NotebookRunOutput: M.NotebookRunOutput;
  NotebookSettingsInput: M.NotebookSettingsInput;
  NotebookSettingsOutput: M.NotebookSettingsOutput;
  NotebookShowOutputInput: M.NotebookShowOutputInput;
  NotebookShowOutputOutput: M.NotebookShowOutputOutput;
  NotebookShownImage: M.NotebookShownImage;
  NotebookShownTable: M.NotebookShownTable;
  NotebookStoredChart: M.NotebookStoredChart;
  NotebookTextEdit: M.NotebookTextEdit;
  NotebookTextPage: M.NotebookTextPage;
  NotebookVariable: M.NotebookVariable;
  NotebookWidgetInput: M.NotebookWidgetInput;
  NotebookWidgetOutput: M.NotebookWidgetOutput;
  NotebookWidgetState: M.NotebookWidgetState;
  ParameterCard: M.ParameterCard;
  PgActivityInput: M.PgActivityInput;
  PgActivityResult: M.PgActivityResult;
  PgActivityRow: M.PgActivityRow;
  PgCancelQueryInput: M.PgCancelQueryInput;
  PgCancelQueryResult: M.PgCancelQueryResult;
  PgDatabaseRow: M.PgDatabaseRow;
  PgListDatabasesInput: M.PgListDatabasesInput;
  PgListDatabasesResult: M.PgListDatabasesResult;
  PgTableSizeRow: M.PgTableSizeRow;
  PgTableSizesInput: M.PgTableSizesInput;
  PgTableSizesResult: M.PgTableSizesResult;
  PipelineCard: M.PipelineCard;
  PluginConnectionInfo: M.PluginConnectionInfo;
  PluginInfo: M.PluginInfo;
  PresignUrlInput: M.PresignUrlInput;
  PresignUrlResult: M.PresignUrlResult;
  PreviewTabularInput: M.PreviewTabularInput;
  PreviewTabularResult: M.PreviewTabularResult;
  PutObjectInput: M.PutObjectInput;
  PutObjectResult: M.PutObjectResult;
  QualifierBiteInput: M.QualifierBiteInput;
  QualifierBiteResult: M.QualifierBiteResult;
  QueryBySql: M.QueryBySql;
  QueryByTable: M.QueryByTable;
  QueryFilter: M.QueryFilter;
  QueryList: M.QueryList;
  QueryPaths: M.QueryPaths;
  QueryRead: M.QueryRead;
  QuerySearch: M.QuerySearch;
  QueryStats: M.QueryStats;
  QueryTraverse: M.QueryTraverse;
  RdsClusterCard: M.RdsClusterCard;
  RdsInstanceCard: M.RdsInstanceCard;
  RdsListClustersInput: M.RdsListClustersInput;
  RdsListClustersResult: M.RdsListClustersResult;
  ReadObjectInput: M.ReadObjectInput;
  ReadObjectResult: M.ReadObjectResult;
  RelationCard: M.RelationCard;
  RenameCellOp: M.RenameCellOp;
  ReplaceCellOp: M.ReplaceCellOp;
  RestoreCellOp: M.RestoreCellOp;
  RsCancelQueryInput: M.RsCancelQueryInput;
  RsCancelQueryResult: M.RsCancelQueryResult;
  RsDatabaseRow: M.RsDatabaseRow;
  RsListDatabasesInput: M.RsListDatabasesInput;
  RsListDatabasesResult: M.RsListDatabasesResult;
  RsRunningQueriesInput: M.RsRunningQueriesInput;
  RsRunningQueriesResult: M.RsRunningQueriesResult;
  RsRunningQueryRow: M.RsRunningQueryRow;
  RsTableInfoInput: M.RsTableInfoInput;
  RsTableInfoResult: M.RsTableInfoResult;
  RsTableInfoRow: M.RsTableInfoRow;
  RunAbove: M.RunAbove;
  RunAll: M.RunAll;
  RunBelow: M.RunBelow;
  RunCard: M.RunCard;
  RunCells: M.RunCells;
  RunDownstream: M.RunDownstream;
  RunJobInput: M.RunJobInput;
  RunJobResult: M.RunJobResult;
  RunStale: M.RunStale;
  RunUpstream: M.RunUpstream;
  SchemaCard: M.SchemaCard;
  SchemaDescribe: M.SchemaDescribe;
  SchemaList: M.SchemaList;
  SearchHit: M.SearchHit;
  SearchResult: M.SearchResult;
  SearchToolsInput: M.SearchToolsInput;
  SearchToolsOutput: M.SearchToolsOutput;
  SetCellConfigOp: M.SetCellConfigOp;
  SetCellKindOp: M.SetCellKindOp;
  SetCellMetaOp: M.SetCellMetaOp;
  SetCommentInput: M.SetCommentInput;
  SetCommentResult: M.SetCommentResult;
  SetSettingOp: M.SetSettingOp;
  SfCancelQueryInput: M.SfCancelQueryInput;
  SfCancelQueryResult: M.SfCancelQueryResult;
  SfDtRefreshHistoryInput: M.SfDtRefreshHistoryInput;
  SfDtRefreshHistoryResult: M.SfDtRefreshHistoryResult;
  SfDtRefreshRun: M.SfDtRefreshRun;
  SfDynamicTableActionResult: M.SfDynamicTableActionResult;
  SfDynamicTableCard: M.SfDynamicTableCard;
  SfDynamicTableInput: M.SfDynamicTableInput;
  SfListDynamicTablesInput: M.SfListDynamicTablesInput;
  SfListDynamicTablesResult: M.SfListDynamicTablesResult;
  SfListMaterializedViewsInput: M.SfListMaterializedViewsInput;
  SfListMaterializedViewsResult: M.SfListMaterializedViewsResult;
  SfListWarehousesInput: M.SfListWarehousesInput;
  SfListWarehousesResult: M.SfListWarehousesResult;
  SfMaterializedViewCard: M.SfMaterializedViewCard;
  SfRunningQueriesInput: M.SfRunningQueriesInput;
  SfRunningQueriesResult: M.SfRunningQueriesResult;
  SfRunningQueryRow: M.SfRunningQueryRow;
  SfWarehouseActionResult: M.SfWarehouseActionResult;
  SfWarehouseCard: M.SfWarehouseCard;
  SfWarehouseInput: M.SfWarehouseInput;
  SigmaElementSqlCard: M.SigmaElementSqlCard;
  SigmaListWorkbooksInput: M.SigmaListWorkbooksInput;
  SigmaListWorkbooksResult: M.SigmaListWorkbooksResult;
  SigmaWorkbookCard: M.SigmaWorkbookCard;
  SigmaWorkbookSqlInput: M.SigmaWorkbookSqlInput;
  SigmaWorkbookSqlResult: M.SigmaWorkbookSqlResult;
  SkillCard: M.SkillCard;
  SnapshotCard: M.SnapshotCard;
  SourceLedgerRow: M.SourceLedgerRow;
  SpawnAgentInput: M.SpawnAgentInput;
  SpawnAgentResult: M.SpawnAgentResult;
  SqlConnectionsInput: M.SqlConnectionsInput;
  SqlConnectionsResult: M.SqlConnectionsResult;
  SqlProvenance: M.SqlProvenance;
  SqlQueryInput: M.SqlQueryInput;
  SqlQueryResult: M.SqlQueryResult;
  SqlSchemaInput: M.SqlSchemaInput;
  SqlSchemaResult: M.SqlSchemaResult;
  SsoLoginInput: M.SsoLoginInput;
  SsoLoginResultModel: M.SsoLoginResultModel;
  StartPipelineUpdateInput: M.StartPipelineUpdateInput;
  StartPipelineUpdateResult: M.StartPipelineUpdateResult;
  StepReport: M.StepReport;
  StorageGroup: M.StorageGroup;
  StorageSummaryInput: M.StorageSummaryInput;
  StorageSummaryResult: M.StorageSummaryResult;
  TableCard: M.TableCard;
  TaskRunCard: M.TaskRunCard;
  TaskSummary: M.TaskSummary;
  TaskUpsert: M.TaskUpsert;
  TaskView: M.TaskView;
  ToolCard: M.ToolCard;
  TopValue: M.TopValue;
  TrinoKillQueryInput: M.TrinoKillQueryInput;
  TrinoKillQueryResult: M.TrinoKillQueryResult;
  TrinoListCatalogsInput: M.TrinoListCatalogsInput;
  TrinoListCatalogsResult: M.TrinoListCatalogsResult;
  TrinoListSchemasInput: M.TrinoListSchemasInput;
  TrinoListSchemasResult: M.TrinoListSchemasResult;
  TrinoQueryCard: M.TrinoQueryCard;
  TrinoRunningQueriesInput: M.TrinoRunningQueriesInput;
  TrinoRunningQueriesResult: M.TrinoRunningQueriesResult;
  Untrusted: M.Untrusted;
  UpdateGrantsInput: M.UpdateGrantsInput;
  UpdateGrantsResult: M.UpdateGrantsResult;
  UseSkillInput: M.UseSkillInput;
  UseSkillResult: M.UseSkillResult;
  WebFetchInput: M.WebFetchInput;
  WebFetchResult: M.WebFetchResult;
  WebSearchInput: M.WebSearchInput;
  WebSearchResult: M.WebSearchResult;
  _ClassifyQuery: M._ClassifyQuery;
  _FindQuery: M._FindQuery;
  _MongoReadInput: M._MongoReadInput;
  _MongoReadResult: M._MongoReadResult;
  _NodeKnowledgeQuery: M._NodeKnowledgeQuery;
  _ReadInput: M._ReadInput;
  _ReadResult: M._ReadResult;
  _TraverseQuery: M._TraverseQuery;
  _UrnQuery: M._UrnQuery;
}

/** Maps each tool name to its Input/Output type names. */
export interface ToolIOTable {
  "aws.rds.connection_info": { input: "ConnectionInfoInput"; output: "ConnectionInfoResult" };
  "aws.rds.create_snapshot": { input: "CreateSnapshotInput"; output: "CreateSnapshotResult" };
  "aws.rds.describe_cluster": { input: "DescribeClusterInput"; output: "DescribeClusterResult" };
  "aws.rds.describe_instance": { input: "DescribeInstanceInput"; output: "DescribeInstanceResult" };
  "aws.rds.describe_parameters": { input: "DescribeParametersInput"; output: "DescribeParametersResult" };
  "aws.rds.download_log_portion": { input: "DownloadLogPortionInput"; output: "DownloadLogPortionResult" };
  "aws.rds.generate_auth_token": { input: "GenerateAuthTokenInput"; output: "GenerateAuthTokenResult" };
  "aws.rds.list_clusters": { input: "RdsListClustersInput"; output: "RdsListClustersResult" };
  "aws.rds.list_instances": { input: "ListInstancesInput"; output: "ListInstancesResult" };
  "aws.rds.list_log_files": { input: "ListLogFilesInput"; output: "ListLogFilesResult" };
  "aws.rds.list_snapshots": { input: "ListSnapshotsInput"; output: "ListSnapshotsResult" };
  "aws.rds.metrics": { input: "MetricsInput"; output: "MetricsResult" };
  "aws.s3.bucket_info": { input: "BucketInfoInput"; output: "BucketInfoResult" };
  "aws.s3.delete_object": { input: "DeleteObjectInput"; output: "DeleteObjectResult" };
  "aws.s3.find_objects": { input: "FindObjectsInput"; output: "FindObjectsResult" };
  "aws.s3.head_object": { input: "HeadObjectInput"; output: "HeadObjectResult" };
  "aws.s3.list_buckets": { input: "ListBucketsInput"; output: "ListBucketsResult" };
  "aws.s3.list_objects": { input: "ListObjectsInput"; output: "ListObjectsResult" };
  "aws.s3.presign_url": { input: "PresignUrlInput"; output: "PresignUrlResult" };
  "aws.s3.preview_tabular": { input: "PreviewTabularInput"; output: "PreviewTabularResult" };
  "aws.s3.put_object": { input: "PutObjectInput"; output: "PutObjectResult" };
  "aws.s3.read_object": { input: "ReadObjectInput"; output: "ReadObjectResult" };
  "aws.s3.storage_summary": { input: "StorageSummaryInput"; output: "StorageSummaryResult" };
  "aws.sso_login": { input: "SsoLoginInput"; output: "SsoLoginResultModel" };
  "background_cancel": { input: "BackgroundCancelInput"; output: "BackgroundCancelResult" };
  "background_status": { input: "BackgroundStatusInput"; output: "BackgroundStatusResult" };
  "bash": { input: "BashInput"; output: "BashResult" };
  "bigquery.cancel_job": { input: "BqCancelJobInput"; output: "BqCancelJobResult" };
  "bigquery.get_job": { input: "BqGetJobInput"; output: "BqGetJobResult" };
  "bigquery.get_table": { input: "BqGetTableInput"; output: "BqGetTableResult" };
  "bigquery.list_datasets": { input: "BqListDatasetsInput"; output: "BqListDatasetsResult" };
  "bigquery.list_jobs": { input: "BqListJobsInput"; output: "BqListJobsResult" };
  "bigquery.list_tables": { input: "BqListTablesInput"; output: "BqListTablesResult" };
  "blob.create": { input: "BlobCreateInput"; output: "BlobCreateOutput" };
  "blob.delete": { input: "BlobDeleteInput"; output: "BlobDeleteOutput" };
  "blob.derive": { input: "BlobDeriveInput"; output: "SqlQueryResult" };
  "blob.info": { input: "BlobInfoInput"; output: "BlobInfoOutput" };
  "blob.materialize": { input: "BlobMaterializeInput"; output: "BlobMaterializeOutput" };
  "blob.profile": { input: "BlobProfileInput"; output: "BlobProfileOutput" };
  "blob.query": { input: "BlobQueryInput"; output: "SqlQueryResult" };
  "call_graph_python": { input: "CallGraphPythonInput"; output: "CallGraphPythonResult" };
  "call_integration_sdk": { input: "CallIntegrationSdkInput"; output: "CallIntegrationSdkResult" };
  "call_tool": { input: "CallToolInput"; output: "CallToolOutput" };
  "clickhouse.kill_query": { input: "ChKillQueryInput"; output: "ChKillQueryResult" };
  "clickhouse.list_databases": { input: "ChListDatabasesInput"; output: "ChListDatabasesResult" };
  "clickhouse.running_queries": { input: "ChProcessesInput"; output: "ChProcessesResult" };
  "clickhouse.table_sizes": { input: "ChTableSizesInput"; output: "ChTableSizesResult" };
  "context_edit": { input: "ContextEditInput"; output: "ContextWriteResult" };
  "context_get": { input: "ContextGetInput"; output: "ContextCard" };
  "context_note": { input: "ContextNoteInput"; output: "ContextWriteResult" };
  "context_search": { input: "ContextSearchInput"; output: "ContextSearchResult" };
  "data.join": { input: "DataJoinInput"; output: "SqlQueryResult" };
  "databricks.cancel_run": { input: "CancelRunInput"; output: "CancelRunResult" };
  "databricks.create_schema": { input: "CreateSchemaInput"; output: "CreateSchemaResult" };
  "databricks.current_user": { input: "CurrentUserInput"; output: "CurrentUserResult" };
  "databricks.get_grants": { input: "GetGrantsInput"; output: "GetGrantsResult" };
  "databricks.get_job": { input: "GetJobInput"; output: "GetJobResult" };
  "databricks.get_pipeline": { input: "GetPipelineInput"; output: "GetPipelineResult" };
  "databricks.get_run": { input: "GetRunInput"; output: "GetRunResult" };
  "databricks.get_run_output": { input: "GetRunOutputInput"; output: "GetRunOutputResult" };
  "databricks.get_table": { input: "GetTableInput"; output: "GetTableResult" };
  "databricks.list_catalogs": { input: "ListCatalogsInput"; output: "ListCatalogsResult" };
  "databricks.list_clusters": { input: "ListClustersInput"; output: "ListClustersResult" };
  "databricks.list_jobs": { input: "ListJobsInput"; output: "ListJobsResult" };
  "databricks.list_pipelines": { input: "ListPipelinesInput"; output: "ListPipelinesResult" };
  "databricks.list_runs": { input: "ListRunsInput"; output: "ListRunsResult" };
  "databricks.list_schemas": { input: "ListSchemasInput"; output: "ListSchemasResult" };
  "databricks.list_tables": { input: "ListTablesInput"; output: "ListTablesResult" };
  "databricks.list_warehouses": { input: "DbxListWarehousesInput"; output: "DbxListWarehousesResult" };
  "databricks.restart_cluster": { input: "ClusterInput"; output: "ClusterActionResult" };
  "databricks.run_job": { input: "RunJobInput"; output: "RunJobResult" };
  "databricks.set_comment": { input: "SetCommentInput"; output: "SetCommentResult" };
  "databricks.start_cluster": { input: "ClusterInput"; output: "ClusterActionResult" };
  "databricks.start_pipeline_update": { input: "StartPipelineUpdateInput"; output: "StartPipelineUpdateResult" };
  "databricks.start_warehouse": { input: "DbxWarehouseInput"; output: "DbxWarehouseActionResult" };
  "databricks.stop_warehouse": { input: "DbxWarehouseInput"; output: "DbxWarehouseActionResult" };
  "databricks.update_grants": { input: "UpdateGrantsInput"; output: "UpdateGrantsResult" };
  "docs.get": { input: "DocsGetInput"; output: "DocsGetResult" };
  "docs.search": { input: "DocsSearchInput"; output: "DocsSearchResult" };
  "docs.sources": { input: "DocsSourcesInput"; output: "DocsSourcesResult" };
  "druid.cancel_query": { input: "DruidCancelQueryInput"; output: "DruidItemsResult" };
  "druid.compaction_status": { input: "DruidConnectionInput"; output: "DruidItemsResult" };
  "druid.list_supervisors": { input: "DruidConnectionInput"; output: "DruidItemsResult" };
  "druid.load_status": { input: "DruidConnectionInput"; output: "DruidItemsResult" };
  "druid.segments_summary": { input: "DruidConnectionInput"; output: "DruidItemsResult" };
  "druid.task_status": { input: "DruidTaskInput"; output: "DruidItemsResult" };
  "elasticsearch.cluster_health": { input: "_ReadInput"; output: "_ReadResult" };
  "elasticsearch.count": { input: "ElasticsearchCountInput"; output: "ElasticsearchCountResult" };
  "elasticsearch.data_stream_stats": { input: "_ReadInput"; output: "_ReadResult" };
  "elasticsearch.esql_query": { input: "ElasticsearchEsqlInput"; output: "ElasticsearchEsqlResult" };
  "elasticsearch.index_stats": { input: "_ReadInput"; output: "_ReadResult" };
  "elasticsearch.list_pipelines": { input: "_ReadInput"; output: "_ReadResult" };
  "elasticsearch.search": { input: "ElasticsearchSearchInput"; output: "ElasticsearchSearchResult" };
  "elasticsearch.transform_stats": { input: "_ReadInput"; output: "_ReadResult" };
  "environment.capture": { input: "EnvironmentCaptureInput"; output: "EnvironmentCaptureResult" };
  "environment.recreate": { input: "EnvironmentRecreateInput"; output: "EnvironmentRecreateResult" };
  "fetch_result": { input: "FetchResultInput"; output: "FetchResultOutput" };
  "fivetran.pipeline_status": { input: "FivetranPipelineStatusInput"; output: "FivetranPipelineStatusResult" };
  "fivetran.table_columns": { input: "FivetranTableColumnsInput"; output: "FivetranTableColumnsResult" };
  "graph.edit": { input: "GraphEditInput"; output: "GraphEditResult" };
  "graph.query": { input: "GraphQueryInput"; output: "GraphQueryResult" };
  "hex.list_projects": { input: "HexListProjectsInput"; output: "HexListProjectsResult" };
  "hex.project_runs": { input: "HexProjectRunsInput"; output: "HexProjectRunsResult" };
  "kafka.connectors": { input: "KafkaConnectionInput"; output: "KafkaCardsResult" };
  "kafka.consumer_groups": { input: "KafkaConnectionInput"; output: "KafkaCardsResult" };
  "kafka.describe_cluster": { input: "KafkaConnectionInput"; output: "KafkaCardsResult" };
  "kafka.list_topics": { input: "KafkaConnectionInput"; output: "KafkaCardsResult" };
  "kafka.peek": { input: "KafkaPeekInput"; output: "KafkaCardsResult" };
  "kafka.schema_subjects": { input: "KafkaConnectionInput"; output: "KafkaCardsResult" };
  "kafka.topic_configs": { input: "KafkaConnectionInput"; output: "KafkaCardsResult" };
  "lineage_classify_change": { input: "_ClassifyQuery"; output: "ClassifyResult" };
  "lineage_find": { input: "_FindQuery"; output: "FindResult" };
  "lineage_impact": { input: "_UrnQuery"; output: "EdgesResult" };
  "lineage_knowledge": { input: "_NodeKnowledgeQuery"; output: "NodeKnowledgeResult" };
  "lineage_traverse": { input: "_TraverseQuery"; output: "EdgesResult" };
  "list_agent_types": { input: "ListAgentTypesInput"; output: "ListAgentTypesResult" };
  "list_plugins": { input: "ListPluginsInput"; output: "ListPluginsOutput" };
  "looker.dashboard_tiles": { input: "LookerDashboardTilesInput"; output: "LookerDashboardTilesResult" };
  "looker.list_dashboards": { input: "LookerListDashboardsInput"; output: "LookerListDashboardsResult" };
  "manage_tasks": { input: "ManageTasksInput"; output: "ManageTasksOutput" };
  "mongodb.aggregate": { input: "MongoAggregateInput"; output: "MongoAggregateResult" };
  "mongodb.coll_stats": { input: "_MongoReadInput"; output: "_MongoReadResult" };
  "mongodb.current_op": { input: "_MongoReadInput"; output: "_MongoReadResult" };
  "mongodb.db_stats": { input: "_MongoReadInput"; output: "_MongoReadResult" };
  "mongodb.list_indexes": { input: "_MongoReadInput"; output: "_MongoReadResult" };
  "mongodb.server_status": { input: "_MongoReadInput"; output: "_MongoReadResult" };
  "mysql.kill_query": { input: "MyKillQueryInput"; output: "MyKillQueryResult" };
  "mysql.list_databases": { input: "MyListDatabasesInput"; output: "MyListDatabasesResult" };
  "mysql.processlist": { input: "MyProcesslistInput"; output: "MyProcesslistResult" };
  "mysql.table_sizes": { input: "MyTableSizesInput"; output: "MyTableSizesResult" };
  "notebook.cells": { input: "NotebookCellsInput"; output: "NotebookCellsOutput" };
  "notebook.create": { input: "NotebookCreateInput"; output: "NotebookCreateOutput" };
  "notebook.edit": { input: "NotebookEditInput"; output: "NotebookEditOutput" };
  "notebook.env": { input: "NotebookEnvInput"; output: "NotebookEnvOutput" };
  "notebook.graph": { input: "NotebookGraphInput"; output: "NotebookGraphOutput" };
  "notebook.inspect": { input: "NotebookInspectInput"; output: "NotebookInspectOutput" };
  "notebook.kernel": { input: "NotebookKernelInput"; output: "NotebookKernelOutput" };
  "notebook.output": { input: "NotebookOutputInput"; output: "NotebookOutputOutput" };
  "notebook.read": { input: "NotebookReadInput"; output: "NotebookReadOutput" };
  "notebook.run": { input: "NotebookRunInput"; output: "NotebookRunOutput" };
  "notebook.settings": { input: "NotebookSettingsInput"; output: "NotebookSettingsOutput" };
  "notebook.show_output": { input: "NotebookShowOutputInput"; output: "NotebookShowOutputOutput" };
  "notebook.widget": { input: "NotebookWidgetInput"; output: "NotebookWidgetOutput" };
  "postgres.activity": { input: "PgActivityInput"; output: "PgActivityResult" };
  "postgres.cancel_query": { input: "PgCancelQueryInput"; output: "PgCancelQueryResult" };
  "postgres.list_databases": { input: "PgListDatabasesInput"; output: "PgListDatabasesResult" };
  "postgres.table_sizes": { input: "PgTableSizesInput"; output: "PgTableSizesResult" };
  "redshift.cancel_query": { input: "RsCancelQueryInput"; output: "RsCancelQueryResult" };
  "redshift.list_databases": { input: "RsListDatabasesInput"; output: "RsListDatabasesResult" };
  "redshift.running_queries": { input: "RsRunningQueriesInput"; output: "RsRunningQueriesResult" };
  "redshift.table_info": { input: "RsTableInfoInput"; output: "RsTableInfoResult" };
  "search_tools": { input: "SearchToolsInput"; output: "SearchToolsOutput" };
  "sigma.list_workbooks": { input: "SigmaListWorkbooksInput"; output: "SigmaListWorkbooksResult" };
  "sigma.workbook_sql": { input: "SigmaWorkbookSqlInput"; output: "SigmaWorkbookSqlResult" };
  "snowflake.cancel_query": { input: "SfCancelQueryInput"; output: "SfCancelQueryResult" };
  "snowflake.dynamic_table_refresh_history": { input: "SfDtRefreshHistoryInput"; output: "SfDtRefreshHistoryResult" };
  "snowflake.list_dynamic_tables": { input: "SfListDynamicTablesInput"; output: "SfListDynamicTablesResult" };
  "snowflake.list_materialized_views": { input: "SfListMaterializedViewsInput"; output: "SfListMaterializedViewsResult" };
  "snowflake.list_warehouses": { input: "SfListWarehousesInput"; output: "SfListWarehousesResult" };
  "snowflake.refresh_dynamic_table": { input: "SfDynamicTableInput"; output: "SfDynamicTableActionResult" };
  "snowflake.resume_dynamic_table": { input: "SfDynamicTableInput"; output: "SfDynamicTableActionResult" };
  "snowflake.resume_warehouse": { input: "SfWarehouseInput"; output: "SfWarehouseActionResult" };
  "snowflake.running_queries": { input: "SfRunningQueriesInput"; output: "SfRunningQueriesResult" };
  "snowflake.suspend_dynamic_table": { input: "SfDynamicTableInput"; output: "SfDynamicTableActionResult" };
  "snowflake.suspend_warehouse": { input: "SfWarehouseInput"; output: "SfWarehouseActionResult" };
  "spawn_agent": { input: "SpawnAgentInput"; output: "SpawnAgentResult" };
  "sql.connections": { input: "SqlConnectionsInput"; output: "SqlConnectionsResult" };
  "sql.qualifier_bite": { input: "QualifierBiteInput"; output: "QualifierBiteResult" };
  "sql.query": { input: "SqlQueryInput"; output: "SqlQueryResult" };
  "sql.schema": { input: "SqlSchemaInput"; output: "SqlSchemaResult" };
  "trino.kill_query": { input: "TrinoKillQueryInput"; output: "TrinoKillQueryResult" };
  "trino.list_catalogs": { input: "TrinoListCatalogsInput"; output: "TrinoListCatalogsResult" };
  "trino.list_schemas": { input: "TrinoListSchemasInput"; output: "TrinoListSchemasResult" };
  "trino.running_queries": { input: "TrinoRunningQueriesInput"; output: "TrinoRunningQueriesResult" };
  "use_skill": { input: "UseSkillInput"; output: "UseSkillResult" };
  "web.fetch": { input: "WebFetchInput"; output: "WebFetchResult" };
  "web.search": { input: "WebSearchInput"; output: "WebSearchResult" };
}

export type AlkeraToolName = keyof ToolIOTable;

type Lookup<T extends string> = T extends keyof ToolTypeMap ? ToolTypeMap[T] : never;

export type ToolInput<N extends AlkeraToolName> = Lookup<ToolIOTable[N]["input"]>;
export type ToolOutput<N extends AlkeraToolName> = Lookup<ToolIOTable[N]["output"]>;

/** Runtime list of every alkera tool name (for membership checks). */
export const ALKERA_TOOL_NAMES = [
  "aws.rds.connection_info",
  "aws.rds.create_snapshot",
  "aws.rds.describe_cluster",
  "aws.rds.describe_instance",
  "aws.rds.describe_parameters",
  "aws.rds.download_log_portion",
  "aws.rds.generate_auth_token",
  "aws.rds.list_clusters",
  "aws.rds.list_instances",
  "aws.rds.list_log_files",
  "aws.rds.list_snapshots",
  "aws.rds.metrics",
  "aws.s3.bucket_info",
  "aws.s3.delete_object",
  "aws.s3.find_objects",
  "aws.s3.head_object",
  "aws.s3.list_buckets",
  "aws.s3.list_objects",
  "aws.s3.presign_url",
  "aws.s3.preview_tabular",
  "aws.s3.put_object",
  "aws.s3.read_object",
  "aws.s3.storage_summary",
  "aws.sso_login",
  "background_cancel",
  "background_status",
  "bash",
  "bigquery.cancel_job",
  "bigquery.get_job",
  "bigquery.get_table",
  "bigquery.list_datasets",
  "bigquery.list_jobs",
  "bigquery.list_tables",
  "blob.create",
  "blob.delete",
  "blob.derive",
  "blob.info",
  "blob.materialize",
  "blob.profile",
  "blob.query",
  "call_graph_python",
  "call_integration_sdk",
  "call_tool",
  "clickhouse.kill_query",
  "clickhouse.list_databases",
  "clickhouse.running_queries",
  "clickhouse.table_sizes",
  "context_edit",
  "context_get",
  "context_note",
  "context_search",
  "data.join",
  "databricks.cancel_run",
  "databricks.create_schema",
  "databricks.current_user",
  "databricks.get_grants",
  "databricks.get_job",
  "databricks.get_pipeline",
  "databricks.get_run",
  "databricks.get_run_output",
  "databricks.get_table",
  "databricks.list_catalogs",
  "databricks.list_clusters",
  "databricks.list_jobs",
  "databricks.list_pipelines",
  "databricks.list_runs",
  "databricks.list_schemas",
  "databricks.list_tables",
  "databricks.list_warehouses",
  "databricks.restart_cluster",
  "databricks.run_job",
  "databricks.set_comment",
  "databricks.start_cluster",
  "databricks.start_pipeline_update",
  "databricks.start_warehouse",
  "databricks.stop_warehouse",
  "databricks.update_grants",
  "docs.get",
  "docs.search",
  "docs.sources",
  "druid.cancel_query",
  "druid.compaction_status",
  "druid.list_supervisors",
  "druid.load_status",
  "druid.segments_summary",
  "druid.task_status",
  "elasticsearch.cluster_health",
  "elasticsearch.count",
  "elasticsearch.data_stream_stats",
  "elasticsearch.esql_query",
  "elasticsearch.index_stats",
  "elasticsearch.list_pipelines",
  "elasticsearch.search",
  "elasticsearch.transform_stats",
  "environment.capture",
  "environment.recreate",
  "fetch_result",
  "fivetran.pipeline_status",
  "fivetran.table_columns",
  "graph.edit",
  "graph.query",
  "hex.list_projects",
  "hex.project_runs",
  "kafka.connectors",
  "kafka.consumer_groups",
  "kafka.describe_cluster",
  "kafka.list_topics",
  "kafka.peek",
  "kafka.schema_subjects",
  "kafka.topic_configs",
  "lineage_classify_change",
  "lineage_find",
  "lineage_impact",
  "lineage_knowledge",
  "lineage_traverse",
  "list_agent_types",
  "list_plugins",
  "looker.dashboard_tiles",
  "looker.list_dashboards",
  "manage_tasks",
  "mongodb.aggregate",
  "mongodb.coll_stats",
  "mongodb.current_op",
  "mongodb.db_stats",
  "mongodb.list_indexes",
  "mongodb.server_status",
  "mysql.kill_query",
  "mysql.list_databases",
  "mysql.processlist",
  "mysql.table_sizes",
  "notebook.cells",
  "notebook.create",
  "notebook.edit",
  "notebook.env",
  "notebook.graph",
  "notebook.inspect",
  "notebook.kernel",
  "notebook.output",
  "notebook.read",
  "notebook.run",
  "notebook.settings",
  "notebook.show_output",
  "notebook.widget",
  "postgres.activity",
  "postgres.cancel_query",
  "postgres.list_databases",
  "postgres.table_sizes",
  "redshift.cancel_query",
  "redshift.list_databases",
  "redshift.running_queries",
  "redshift.table_info",
  "search_tools",
  "sigma.list_workbooks",
  "sigma.workbook_sql",
  "snowflake.cancel_query",
  "snowflake.dynamic_table_refresh_history",
  "snowflake.list_dynamic_tables",
  "snowflake.list_materialized_views",
  "snowflake.list_warehouses",
  "snowflake.refresh_dynamic_table",
  "snowflake.resume_dynamic_table",
  "snowflake.resume_warehouse",
  "snowflake.running_queries",
  "snowflake.suspend_dynamic_table",
  "snowflake.suspend_warehouse",
  "spawn_agent",
  "sql.connections",
  "sql.qualifier_bite",
  "sql.query",
  "sql.schema",
  "trino.kill_query",
  "trino.list_catalogs",
  "trino.list_schemas",
  "trino.running_queries",
  "use_skill",
  "web.fetch",
  "web.search",
] as const satisfies readonly AlkeraToolName[];

export interface ToolMeta {
  title: string;
  app: string | null;
  hot: boolean;
}

/** Per-tool advertised metadata (title/app/hot) from the backend spec. */
export const TOOL_META: Record<AlkeraToolName, ToolMeta> = {
  "aws.rds.connection_info": { title: "Resolve what is needed to connect to an RDS database", app: "aws", hot: false },
  "aws.rds.create_snapshot": { title: "Create an RDS snapshot", app: "aws", hot: false },
  "aws.rds.describe_cluster": { title: "Describe an RDS/Aurora DB cluster", app: "aws", hot: false },
  "aws.rds.describe_instance": { title: "Describe an RDS DB instance", app: "aws", hot: false },
  "aws.rds.describe_parameters": { title: "Describe RDS parameter-group settings", app: "aws", hot: false },
  "aws.rds.download_log_portion": { title: "Read a portion of an RDS log file", app: "aws", hot: false },
  "aws.rds.generate_auth_token": { title: "Mint an RDS IAM auth token", app: "aws", hot: false },
  "aws.rds.list_clusters": { title: "List RDS/Aurora DB clusters", app: "aws", hot: false },
  "aws.rds.list_instances": { title: "List RDS DB instances", app: "aws", hot: false },
  "aws.rds.list_log_files": { title: "List an RDS instance's log files", app: "aws", hot: false },
  "aws.rds.list_snapshots": { title: "List RDS snapshots", app: "aws", hot: false },
  "aws.rds.metrics": { title: "Get RDS CloudWatch metrics", app: "aws", hot: false },
  "aws.s3.bucket_info": { title: "Describe an S3 bucket", app: "aws", hot: false },
  "aws.s3.delete_object": { title: "Delete an S3 object", app: "aws", hot: false },
  "aws.s3.find_objects": { title: "Find S3 objects by pattern", app: "aws", hot: false },
  "aws.s3.head_object": { title: "Describe an S3 object", app: "aws", hot: false },
  "aws.s3.list_buckets": { title: "List S3 buckets", app: "aws", hot: false },
  "aws.s3.list_objects": { title: "List S3 objects", app: "aws", hot: false },
  "aws.s3.presign_url": { title: "Presign an S3 download URL", app: "aws", hot: false },
  "aws.s3.preview_tabular": { title: "Preview an S3 tabular file", app: "aws", hot: false },
  "aws.s3.put_object": { title: "Write an S3 object", app: "aws", hot: false },
  "aws.s3.read_object": { title: "Read S3 object bytes", app: "aws", hot: false },
  "aws.s3.storage_summary": { title: "Summarize S3 storage", app: "aws", hot: false },
  "aws.sso_login": { title: "Sign in to AWS IAM Identity Center", app: "aws", hot: false },
  "background_cancel": { title: "Cancel a background job", app: "background", hot: true },
  "background_status": { title: "Background job status", app: "background", hot: true },
  "bash": { title: "Run a bash command", app: "bash", hot: true },
  "bigquery.cancel_job": { title: "Cancel a BigQuery job", app: "bigquery", hot: false },
  "bigquery.get_job": { title: "Get a BigQuery job", app: "bigquery", hot: false },
  "bigquery.get_table": { title: "Describe a BigQuery table", app: "bigquery", hot: false },
  "bigquery.list_datasets": { title: "List BigQuery datasets", app: "bigquery", hot: false },
  "bigquery.list_jobs": { title: "List BigQuery jobs", app: "bigquery", hot: false },
  "bigquery.list_tables": { title: "List BigQuery tables", app: "bigquery", hot: false },
  "blob.create": { title: "Create a result blob", app: "blob", hot: true },
  "blob.delete": { title: "Delete a result blob", app: "blob", hot: false },
  "blob.derive": { title: "Reshape a result", app: "blob", hot: false },
  "blob.info": { title: "Inspect a result's shape", app: "blob", hot: true },
  "blob.materialize": { title: "Write a result to a file", app: "blob", hot: false },
  "blob.profile": { title: "Profile a tabular result", app: "blob", hot: true },
  "blob.query": { title: "Query a result with SQL", app: "blob", hot: true },
  "call_graph_python": { title: "Run Python over a planning graph", app: "graph", hot: true },
  "call_integration_sdk": { title: "Call an integration's SDK", app: "integration_sdk", hot: false },
  "call_tool": { title: "Call tool", app: null, hot: true },
  "clickhouse.kill_query": { title: "Kill a ClickHouse query", app: "clickhouse", hot: false },
  "clickhouse.list_databases": { title: "List ClickHouse databases", app: "clickhouse", hot: false },
  "clickhouse.running_queries": { title: "List running ClickHouse queries", app: "clickhouse", hot: false },
  "clickhouse.table_sizes": { title: "List ClickHouse table sizes", app: "clickhouse", hot: false },
  "context_edit": { title: "Edit project knowledge", app: "context", hot: true },
  "context_get": { title: "Get a knowledge item", app: "context", hot: true },
  "context_note": { title: "Write to project knowledge", app: "context", hot: true },
  "context_search": { title: "Search project knowledge", app: "context", hot: true },
  "data.join": { title: "Join across connections", app: "sql", hot: false },
  "databricks.cancel_run": { title: "Cancel a Databricks job run", app: "databricks", hot: false },
  "databricks.create_schema": { title: "Create a Unity Catalog schema", app: "databricks", hot: false },
  "databricks.current_user": { title: "Get the Databricks identity", app: "databricks", hot: false },
  "databricks.get_grants": { title: "Get Unity Catalog grants", app: "databricks", hot: false },
  "databricks.get_job": { title: "Get a Databricks job's definition", app: "databricks", hot: false },
  "databricks.get_pipeline": { title: "Get a Lakeflow / DLT pipeline", app: "databricks", hot: false },
  "databricks.get_run": { title: "Get a Databricks job run", app: "databricks", hot: false },
  "databricks.get_run_output": { title: "Get a Databricks job run's output", app: "databricks", hot: false },
  "databricks.get_table": { title: "Describe a Unity Catalog table", app: "databricks", hot: false },
  "databricks.list_catalogs": { title: "List Unity Catalog catalogs", app: "databricks", hot: false },
  "databricks.list_clusters": { title: "List Databricks clusters", app: "databricks", hot: false },
  "databricks.list_jobs": { title: "List Databricks jobs", app: "databricks", hot: false },
  "databricks.list_pipelines": { title: "List Lakeflow / DLT pipelines", app: "databricks", hot: false },
  "databricks.list_runs": { title: "List Databricks job runs", app: "databricks", hot: false },
  "databricks.list_schemas": { title: "List Unity Catalog schemas", app: "databricks", hot: false },
  "databricks.list_tables": { title: "List Unity Catalog tables", app: "databricks", hot: false },
  "databricks.list_warehouses": { title: "List Databricks SQL warehouses", app: "databricks", hot: false },
  "databricks.restart_cluster": { title: "Restart a Databricks cluster", app: "databricks", hot: false },
  "databricks.run_job": { title: "Run a Databricks job", app: "databricks", hot: false },
  "databricks.set_comment": { title: "Set a Unity Catalog comment", app: "databricks", hot: false },
  "databricks.start_cluster": { title: "Start a Databricks cluster", app: "databricks", hot: false },
  "databricks.start_pipeline_update": { title: "Trigger a Lakeflow / DLT pipeline update", app: "databricks", hot: false },
  "databricks.start_warehouse": { title: "Start a Databricks SQL warehouse", app: "databricks", hot: false },
  "databricks.stop_warehouse": { title: "Stop a Databricks SQL warehouse", app: "databricks", hot: false },
  "databricks.update_grants": { title: "Grant or revoke Unity Catalog privileges", app: "databricks", hot: false },
  "docs.get": { title: "Read a connected document", app: "docs", hot: false },
  "docs.search": { title: "Search connected documents", app: "docs", hot: false },
  "docs.sources": { title: "List knowledge sources", app: "docs", hot: true },
  "druid.cancel_query": { title: "Cancel Druid query", app: "druid", hot: false },
  "druid.compaction_status": { title: "Get Druid compaction status", app: "druid", hot: false },
  "druid.list_supervisors": { title: "List Druid supervisors", app: "druid", hot: false },
  "druid.load_status": { title: "Get Druid load status", app: "druid", hot: false },
  "druid.segments_summary": { title: "Summarize Druid segments", app: "druid", hot: false },
  "druid.task_status": { title: "Get Druid task status", app: "druid", hot: false },
  "elasticsearch.cluster_health": { title: "Elasticsearch cluster health", app: "elasticsearch", hot: false },
  "elasticsearch.count": { title: "Count Elasticsearch matches", app: "elasticsearch", hot: false },
  "elasticsearch.data_stream_stats": { title: "Elasticsearch data stream statistics", app: "elasticsearch", hot: false },
  "elasticsearch.esql_query": { title: "Run an ES|QL query", app: "elasticsearch", hot: false },
  "elasticsearch.index_stats": { title: "Elasticsearch index statistics", app: "elasticsearch", hot: false },
  "elasticsearch.list_pipelines": { title: "List Elasticsearch ingest pipelines", app: "elasticsearch", hot: false },
  "elasticsearch.search": { title: "Search Elasticsearch documents", app: "elasticsearch", hot: false },
  "elasticsearch.transform_stats": { title: "Elasticsearch transform statistics", app: "elasticsearch", hot: false },
  "environment.capture": { title: "Capture the environment", app: "environment", hot: false },
  "environment.recreate": { title: "Recreate the environment", app: "environment", hot: false },
  "fetch_result": { title: "Fetch a large result", app: null, hot: true },
  "fivetran.pipeline_status": { title: "Fivetran pipeline health", app: "fivetran", hot: false },
  "fivetran.table_columns": { title: "Fivetran live column mapping", app: "fivetran", hot: false },
  "graph.edit": { title: "Create or edit a planning graph", app: "graph", hot: true },
  "graph.query": { title: "Query a planning graph", app: "graph", hot: true },
  "hex.list_projects": { title: "List Hex projects", app: "hex", hot: false },
  "hex.project_runs": { title: "Hex project run history", app: "hex", hot: false },
  "kafka.connectors": { title: "List Kafka Connect connectors", app: "kafka", hot: false },
  "kafka.consumer_groups": { title: "List Kafka consumer groups", app: "kafka", hot: false },
  "kafka.describe_cluster": { title: "Describe Kafka cluster", app: "kafka", hot: false },
  "kafka.list_topics": { title: "List Kafka topics", app: "kafka", hot: false },
  "kafka.peek": { title: "Peek at Kafka messages", app: "kafka", hot: false },
  "kafka.schema_subjects": { title: "List Kafka schemas", app: "kafka", hot: false },
  "kafka.topic_configs": { title: "Get Kafka topic configs", app: "kafka", hot: false },
  "lineage_classify_change": { title: "Lineage classify change", app: "lineage", hot: true },
  "lineage_find": { title: "Find data asset", app: "lineage", hot: true },
  "lineage_impact": { title: "Lineage impact", app: "lineage", hot: true },
  "lineage_knowledge": { title: "Read an asset's knowledge", app: "lineage", hot: true },
  "lineage_traverse": { title: "Trace lineage", app: "lineage", hot: true },
  "list_agent_types": { title: "List agent types", app: "agent", hot: true },
  "list_plugins": { title: "List plugins & connections", app: null, hot: true },
  "looker.dashboard_tiles": { title: "Inspect a Looker dashboard's tiles", app: "looker", hot: false },
  "looker.list_dashboards": { title: "List Looker dashboards", app: "looker", hot: false },
  "manage_tasks": { title: "Manage tasks", app: "tasks", hot: true },
  "mongodb.aggregate": { title: "Run a MongoDB aggregate", app: "mongodb", hot: false },
  "mongodb.coll_stats": { title: "MongoDB collection statistics", app: "mongodb", hot: false },
  "mongodb.current_op": { title: "MongoDB current operations", app: "mongodb", hot: false },
  "mongodb.db_stats": { title: "MongoDB database statistics", app: "mongodb", hot: false },
  "mongodb.list_indexes": { title: "List MongoDB indexes", app: "mongodb", hot: false },
  "mongodb.server_status": { title: "MongoDB server status", app: "mongodb", hot: false },
  "mysql.kill_query": { title: "Kill a MySQL query", app: "mysql", hot: false },
  "mysql.list_databases": { title: "List MySQL databases", app: "mysql", hot: false },
  "mysql.processlist": { title: "Show the MySQL processlist", app: "mysql", hot: false },
  "mysql.table_sizes": { title: "List MySQL table sizes", app: "mysql", hot: false },
  "notebook.cells": { title: "Notebook cell actions", app: "notebook", hot: false },
  "notebook.create": { title: "Create a notebook", app: "notebook", hot: false },
  "notebook.edit": { title: "Edit notebook cells", app: "notebook", hot: false },
  "notebook.env": { title: "Notebook environment", app: "notebook", hot: false },
  "notebook.graph": { title: "Notebook dependency graph", app: "notebook", hot: false },
  "notebook.inspect": { title: "Inspect notebook values", app: "notebook", hot: false },
  "notebook.kernel": { title: "Notebook kernel", app: "notebook", hot: false },
  "notebook.output": { title: "Read a cell's output", app: "notebook", hot: false },
  "notebook.read": { title: "Read a notebook", app: "notebook", hot: false },
  "notebook.run": { title: "Run notebook cells", app: "notebook", hot: false },
  "notebook.settings": { title: "Notebook settings", app: "notebook", hot: false },
  "notebook.show_output": { title: "Show a cell's output", app: "notebook", hot: false },
  "notebook.widget": { title: "Notebook widgets", app: "notebook", hot: false },
  "postgres.activity": { title: "Show Postgres activity", app: "postgres", hot: false },
  "postgres.cancel_query": { title: "Cancel a Postgres query", app: "postgres", hot: false },
  "postgres.list_databases": { title: "List Postgres databases", app: "postgres", hot: false },
  "postgres.table_sizes": { title: "Show Postgres relation sizes", app: "postgres", hot: false },
  "redshift.cancel_query": { title: "Cancel a Redshift query", app: "redshift", hot: false },
  "redshift.list_databases": { title: "List Redshift databases", app: "redshift", hot: false },
  "redshift.running_queries": { title: "List running Redshift queries", app: "redshift", hot: false },
  "redshift.table_info": { title: "List Redshift tables by size", app: "redshift", hot: false },
  "search_tools": { title: "Search tools", app: null, hot: true },
  "sigma.list_workbooks": { title: "List Sigma workbooks", app: "sigma", hot: false },
  "sigma.workbook_sql": { title: "Sigma workbook compiled SQL", app: "sigma", hot: false },
  "snowflake.cancel_query": { title: "Cancel a Snowflake query", app: "snowflake", hot: false },
  "snowflake.dynamic_table_refresh_history": { title: "Snowflake dynamic-table refresh history", app: "snowflake", hot: false },
  "snowflake.list_dynamic_tables": { title: "List Snowflake dynamic tables", app: "snowflake", hot: false },
  "snowflake.list_materialized_views": { title: "List Snowflake materialized views", app: "snowflake", hot: false },
  "snowflake.list_warehouses": { title: "List Snowflake warehouses", app: "snowflake", hot: false },
  "snowflake.refresh_dynamic_table": { title: "Refresh a Snowflake dynamic table", app: "snowflake", hot: false },
  "snowflake.resume_dynamic_table": { title: "Resume a Snowflake dynamic table", app: "snowflake", hot: false },
  "snowflake.resume_warehouse": { title: "Resume a Snowflake warehouse", app: "snowflake", hot: false },
  "snowflake.running_queries": { title: "Show running Snowflake queries", app: "snowflake", hot: false },
  "snowflake.suspend_dynamic_table": { title: "Suspend a Snowflake dynamic table", app: "snowflake", hot: false },
  "snowflake.suspend_warehouse": { title: "Suspend a Snowflake warehouse", app: "snowflake", hot: false },
  "spawn_agent": { title: "Spawn agent", app: "agent", hot: true },
  "sql.connections": { title: "List data connections", app: "sql", hot: true },
  "sql.qualifier_bite": { title: "Count what a qualifier removes", app: "sql", hot: false },
  "sql.query": { title: "Run SQL query", app: "sql", hot: false },
  "sql.schema": { title: "Inspect schema", app: "sql", hot: false },
  "trino.kill_query": { title: "Kill a running Trino query", app: "trino", hot: false },
  "trino.list_catalogs": { title: "List Trino catalogs", app: "trino", hot: false },
  "trino.list_schemas": { title: "List Trino schemas in a catalog", app: "trino", hot: false },
  "trino.running_queries": { title: "List running Trino queries", app: "trino", hot: false },
  "use_skill": { title: "Load a skill", app: "skill", hot: false },
  "web.fetch": { title: "Web fetch", app: "web", hot: true },
  "web.search": { title: "Web search", app: "web", hot: true },
};
