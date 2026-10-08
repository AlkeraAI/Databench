// The device user_code, set in hairline-ruled ledger cells — one character per cell, in
// Newsreader (no mono), with a brass rule beneath. The same code reads identically here and
// in the CLI / extension that issued it, so confirming a match is trivial by eye. In
// `skeleton` mode the cells render as pulsing placeholders for the loading state.

/** Split a user_code into display groups, one cell per character. Segments split on the
 *  hyphen the backend formats codes with (e.g. "WXYZ-1234" → "WXYZ" + "1234"); a code
 *  without one renders as a single group. */
function toGroups(code: string): string[][] {
  return code
    .split("-")
    .filter(Boolean)
    .map((segment) => Array.from(segment));
}

interface CodeCellsProps {
  /** The raw device user_code, e.g. "WXYZ-1234". */
  code: string;
  /** Render empty pulsing cells instead of the characters (the loading state). */
  skeleton?: boolean;
}

export function CodeCells({ code, skeleton }: CodeCellsProps) {
  // Skeleton has no real code yet — show a fixed two-group placeholder shape.
  const groups = skeleton ? [["", "", "", ""], ["", "", "", ""]] : toGroups(code);
  return (
    <div
      className={`pa-code${skeleton ? " pa-code--skeleton" : ""}`}
      role="img"
      aria-label={skeleton ? "Loading the device code" : `Device code ${code}`}
      aria-busy={skeleton || undefined}
    >
      {groups.map((group, gi) => (
        <div className="pa-code__group" key={`g${gi}`}>
          {group.map((char, ci) => (
            <span className="pa-code__cell" key={`c${gi}-${ci}`} aria-hidden="true">
              {skeleton ? <span className="pa-code__skel" /> : char}
            </span>
          ))}
        </div>
      ))}
    </div>
  );
}
