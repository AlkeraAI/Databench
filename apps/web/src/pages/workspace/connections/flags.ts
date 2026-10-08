/** Switches on the connection dialog that are off in the UI for now.
 *
 *  `MEMBER_PREFILL_ENABLED` is the per-field "members inherit this value"
 *  column: its boxes, the box over the column, and the sentence counting what
 *  members would still answer. While it is false none of that renders and a new
 *  team connection withholds nothing — the save is the same request the dialog
 *  sends today with every box left on, so the server contract is untouched. A
 *  stored row that already withholds fields keeps them: the dialog cannot show
 *  that choice, so it must not silently hand a team a credential the row was
 *  saved to keep back. Flip this to true to bring the column back.
 *
 *  Typed `boolean` rather than inferred as the literal `false` so both branches
 *  keep typechecking, and so a test can stand a different value in its place.
 */
export const MEMBER_PREFILL_ENABLED: boolean = false;
