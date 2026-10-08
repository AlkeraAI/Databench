// @alkera/ui — the Alkera UI library, organized by domain. `primitives` is the base toolkit
// (controls, inputs, overlays, display, render); `brand`, `viz` and `chat` are the feature
// domains. The public surface is flat: everything re-exports from here. The data product's
// concerns (`connections`, `lineage`, `graph`) and the editor's sign-in gates (`auth`) are not
// part of it: they are reached through their own entry points (`@alkera/ui/connections`,
// `/lineage`, `/graph`, `/auth`) so the open library never names them.
// (Markdown, CodeBlock, Terminal, and the resource previews come through `primitives/render`.)
export * from "./theme/motion";
export * from "./primitives";
export * from "./compute";
export * from "./brand";
export * from "./viz";
export * from "./charts";
export * from "./types";
export * from "./chat";
export * from "./layout/StackedPage";
export * from "./layout/SplitPane";
export * from "./preview";
// resources: explicit re-exports — its ReferenceList (chips + hover previews +
// injected store actions, the chat-shell contract) takes the public name over the
// simpler internal primitives/render one (explicit exports beat the primitives star).
export {
  BlobView,
  BlobTable,
  DataTable,
  DataTablePager,
  ReferenceChip,
  ReferenceRow,
  ReferenceList,
  ReferenceStoreProvider,
  registerReferenceRenderer,
  resolveReferenceRenderer,
  useReferenceActions,
  type DataTableProps,
  type DataTablePagerProps,
  type ReferenceActions,
  type ReferenceActionsState,
  type ReferenceListProps,
  type ReferenceRenderer,
} from "./resources";
export * from "./log";

// Public hooks — the overlay/float primitives a host may need plus the small shared utilities.
// pushEsc stays package-internal; a host-owned transient joins the shared Escape stack through
// useEscLayer instead. useFocusTrap is public for a host-owned modal surface (the portal's
// narrow navigation drawer) that must honour the same contract as Modal and SidePanel.
export {
  useFloating,
  type FloatingSide,
  type FloatingAlign,
  type UseFloatingOptions,
  type FloatingResult,
  usePresence,
  useDismiss,
  useEscLayer,
  useFocusTrap,
  type FocusTrapOptions,
  useDebouncedValue,
  SEARCH_DEBOUNCE_MS,
  abbreviate,
  useAbbreviate,
  useMediaQuery,
  useScrollShadow,
  useRowSelection,
  type RowSelection,
  required,
  isEmail,
  minLength,
  firstError,
  matches,
  type Validator,
  useColorScheme,
  applyColorScheme,
  type ColorScheme,
  type UseColorSchemeOptions,
} from "./hooks";
