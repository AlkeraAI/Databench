/**
 * The shared control-size ladder — one source of truth so every interactive control speaks the same
 * size vocabulary. `sm` 28 / `md` 32 / `lg` 38 map to the `--alkControlSm/Md/Lg` height tokens.
 *
 * Two invariants the ladder is chosen to keep:
 *  1. Controls of the SAME size line up in a row (a `lg` Button, TextInput, Select, SearchBar, and
 *     SegmentedControl are all 38px tall — the toolbar/field height and the default).
 *  2. A control nests inside a one-step-larger BORDERED field with even, on-scale padding: an `sm`
 *     (28) glyph button inside an `lg` (38) field whose 1px border leaves a 36px content box sits
 *     with (36 − 28) / 2 = 4px (`--alkSpace1`) above and below.
 *
 * Decorative scales (IconChip's 24/28/34, the chip pills) are deliberately their own thing — they
 * are not controls and do not line up in a control row.
 *
 * Horizontal padding scales with the height from ONE source of truth — the `--alkFieldPad{Sm,Md,Lg}`
 * and `--alkControlPad{Sm,Md,Lg}` token triples in theme/tokens.css. There are deliberately TWO
 * registers: the tighter FIELD register for bordered text inputs (TextInput, Select), and the roomier
 * CONTROL register for buttons and toolbar controls (Button, SearchBar, SegmentedControl,
 * DropdownTrigger). A new control references the matching triple — it does NOT spell its own ladder.
 */
export type ControlSize = "sm" | "md" | "lg";
