// The single hooks home for @alkera/ui. Overlay/float infrastructure used internally by the
// base components plus the small reusable utilities (compact-number formatting, scroll-edge fades)
// shared across consumers. The public subset is re-exported from src/index.ts; pushEsc stays
// package-internal.
export { useFloating, type FloatingSide, type FloatingAlign, type UseFloatingOptions, type FloatingResult } from "./useFloating";
export { usePresence } from "./usePresence";
export { useDismiss } from "./useDismiss";
export { useEscLayer } from "./useEscLayer";
export { useFocusTrap, type FocusTrapOptions } from "./useFocusTrap";
export { pushEsc } from "./overlayStack";
export { abbreviate, useAbbreviate } from "./useAbbreviate";
export { useCopyToClipboard } from "./useCopyToClipboard";
export { SEARCH_DEBOUNCE_MS, useDebouncedValue } from "./useDebouncedValue";
export { useMediaQuery } from "./useMediaQuery";
export {
  usePanZoomStage,
  type PanZoomStage,
  type PanZoomStageOptions,
} from "./usePanZoomStage";
export { useScrollShadow } from "./useScrollShadow";
export { isOverflowing } from "./useOverflow";
export { useRowSelection, type RowSelection } from "./useRowSelection";
export { usePager, type UsePagerOptions, type UsePagerResult } from "./usePager";
export { required, isEmail, minLength, firstError, matches, type Validator } from "./validation";
export {
  useColorScheme,
  useColorSchemeChanges,
  applyColorScheme,
  watchColorScheme,
  type ColorScheme,
  type UseColorSchemeOptions,
} from "./useColorScheme";
