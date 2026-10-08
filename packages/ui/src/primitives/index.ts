// primitives — the base UI toolkit, grouped by role. Controls (buttons, toggles), inputs (fields),
// overlays (modal, drawer, floating), display (cards, tables, readings), and code (highlighted plates).
// The shared internals (cx, sizes, icons, base.css) sit at this root; each group re-exports its own.
export { cx } from "./cx";
export type { ControlSize } from "./sizes";
// Public so any surface can compose an iconOnly Button with the overlay glyph set.
export * from "./icons";

// layout — flex composition primitives (Stack, Inline)
export * from "./layout";

// format — how a machine's facts are read by a person (counts, instants, spans,
// bound parameters). Shared so a tool card and the receipt of the result it was
// saved into never render the same fact two ways.
export * from "./format";

// controls
export * from "./controls/Caret";
export * from "./controls/Button";
export * from "./controls/CopyButton";
export * from "./controls/IconChip";
export * from "./controls/Checkbox";
export * from "./controls/Switch";
export * from "./controls/SegmentedControl";
export * from "./controls/Tabs";
export * from "./controls/TabStrip";

// inputs
export * from "./inputs/TextInput";
export * from "./inputs/NumberInput";
export * from "./inputs/Select";
export * from "./inputs/DateInput";
export * from "./inputs/Textarea";
export * from "./inputs/Field";

// overlays
export * from "./overlays/Modal";
export * from "./overlays/ConfirmDialog";
export * from "./overlays/SidePanel";
export * from "./overlays/ContextMenu";
export * from "./overlays/Dropdown";
export * from "./overlays/Popover";
export * from "./overlays/Submenu";
export * from "./overlays/Tooltip";
export * from "./overlays/Toast";

// display
export * from "./display/Avatar";
export * from "./display/Identity";
export * from "./display/NodeLabel";
export * from "./display/Table";
export * from "./display/Anchor";
export * from "./display/Callout";
export * from "./display/Tree";
export * from "./display/Pill";
export * from "./display/StatusPill";
export * from "./display/Skeleton";
export * from "./display/SortHeader";
export * from "./display/UrlLink";
export { Card, type CardProps, type CardSize } from "./display/Card";
export { Collapse, type CollapseProps } from "./display/Collapse";
export { CopyReading, type CopyReadingProps } from "./display/CopyReading";
export { DescList, DescRow, type DescListProps, type DescRowProps } from "./display/DescList";
export { EmptyState, type EmptyStateProps } from "./display/EmptyState";
export { PageError, type PageErrorProps } from "./display/PageError";
export {
  LoadingIndicator,
  type LoadingForm,
  type LoadingIndicatorProps,
  type LoadingSize,
} from "./display/LoadingIndicator";
export { Meter, type MeterProps, type MeterTone } from "./display/Meter";
export { StatCard, type StatCardProps } from "./display/StatCard";
export { Text, type TextProps, type TextVariant, type TextTone } from "./display/Text";

// render — code, terminal, markdown, resource previews (one highlight engine, one warm palette)
export * from "./render";
