// layout — flex composition primitives (Stack column, Inline row) so pages compose spacing through a
// `gap` prop instead of a bespoke display:flex class. The shared gap resolver lives beside them.
export { Stack, type StackProps } from "./Stack";
export { Inline, type InlineProps } from "./Inline";
export { Toolbar, type ToolbarProps } from "./Toolbar";
export { resolveGap, type SpaceScale } from "./gap";
