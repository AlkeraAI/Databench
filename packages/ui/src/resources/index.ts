// resources — the blob-reference system of the chat shell: the injected host
// actions (ReferenceStoreProvider), the inline chips / artifact rows with a
// hover preview (ReferenceList), the renderer registry keyed by refType / page
// kind, the shared paginated data grid (DataTable), and the full read-only
// paginated blob view (BlobView). The React-free data contract (BlobReference,
// BlobPage, BlobFetcher, decodeNonFinite, formatCell) lives in @alkera/chat-model.
export { BlobView } from "./BlobView";
export { DataTable, DataTablePager, type DataTableProps, type DataTablePagerProps } from "./DataTable";
export {
  ReferenceChip,
  ReferenceRow,
  ReferenceList,
  type ReferenceActions,
  type ReferenceListProps,
} from "./ReferenceList";
export {
  ReferenceStoreProvider,
  useReferenceActions,
  type ReferenceActionsState,
} from "./ReferenceStoreProvider";
export { registerReferenceRenderer, resolveReferenceRenderer, BlobTable, type ReferenceRenderer } from "./registry";
