// Feature-local bench marks for the admin registry, drawn in the flask's single-
// weight rounded-cap line (no fills, no glows) — the same vernacular as the shared
// app/icons.tsx, kept here so the admin glyphs don't crowd the shell's icon set.
//
// The signature is the assay-fail SEAL: a registrar's rejection stamp struck over
// a model whose sell price sits below its provider cost. It pairs the danger
// pigment with a barred-triangle bench glyph so the meaning never rides hue alone.

const STROKE = 1.7;

function Bench({ size, children, className }: { size: number; children: React.ReactNode; className?: string }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={STROKE}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

/** The assay-fail mark — a barred triangle (caution struck through), the seal's glyph. Drawn
 *  symmetric about the viewBox so it sits true beside a label. Colour rides currentColor. */
export function AssayFailMark({ size = 14 }: { size?: number }) {
  return (
    <Bench size={size}>
      <path d="M12 4.4 19.4 18.6 4.6 18.6Z" />
      <path d="M8 13.6h8" />
    </Bench>
  );
}

/** The register-empty mark — an open ledger with a hairline rule and a margin tick, for a
 *  register with no entries yet. */
export function RegisterMark({ size = 48 }: { size?: number }) {
  return (
    <Bench size={size}>
      <path d="M4 5.5A1.5 1.5 0 015.5 4H11a1 1 0 011 1v14a1 1 0 00-1-1H5.5A1.5 1.5 0 014 16.5z" />
      <path d="M12 5a1 1 0 011-1h5.5A1.5 1.5 0 0120 5.5v11a1.5 1.5 0 00-1.5-1.5H13a1 1 0 00-1 1z" />
      <path d="M6.5 8h3M14.5 8h3M6.5 11h3M14.5 11h3" />
    </Bench>
  );
}

/** The load-failed mark — a broken vessel (the bench's spilled-assay glyph), for an error plate. */
export function SpilledMark({ size = 48 }: { size?: number }) {
  return (
    <Bench size={size}>
      <path d="M8 4.5h8M9 4.5v5.2L6.4 16a2 2 0 001.8 2.9h7.6a2 2 0 001.8-2.9L15 9.7V4.5" />
      <path d="M7 7.5l10 9.5" />
    </Bench>
  );
}

/** The catalog-empty mark — a graduated vessel awaiting a reagent. */
export function ReagentMark({ size = 48 }: { size?: number }) {
  return (
    <Bench size={size}>
      <path d="M8 3h8" />
      <path d="M9.2 3v15.5A2.5 2.5 0 0012 21a2.5 2.5 0 002.8-2.5V3" />
      <path d="M11 8.5h2M11 12h3M11 15.5h2" />
    </Bench>
  );
}
