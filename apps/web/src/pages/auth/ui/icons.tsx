import type { ReactNode } from "react";

// Portal-local glyphs — the brand mark and chrome icons, drawn in the bench's single-weight rounded
// line. The OAuth provider marks (Google, GitHub) live in @alkera/ui brand (ProviderLogo) so every
// surface renders one canonical mark.

interface GlyphProps {
  size?: number;
  className?: string;
}

function Line({ size = 16, className, children }: GlyphProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={className}
    >
      {children}
    </svg>
  );
}

/** The brand mark: a distillation flask, round-bottomed, drawn in the bench line. */
export function FlaskMark({ size = 16, className }: GlyphProps) {
  return (
    <Line size={size} className={className}>
      <path d="M10 3h4" />
      <path d="M11 3v6" />
      <path d="M13 3v6" />
      <circle cx="12" cy="15" r="6" />
      <path d="M7.5 17.2h9" />
    </Line>
  );
}

export function ArrowLeftIcon({ size = 16, className }: GlyphProps) {
  return (
    <Line size={size} className={className}>
      <line x1="19" y1="12" x2="5" y2="12" />
      <polyline points="12 19 5 12 12 5" />
    </Line>
  );
}

export function CheckIcon({ size = 16, className }: GlyphProps) {
  return (
    <Line size={size} className={className}>
      <polyline points="20 6 9 17 4 12" />
    </Line>
  );
}

export function CrossIcon({ size = 16, className }: GlyphProps) {
  return (
    <Line size={size} className={className}>
      <path d="M7.5 7.5 16.5 16.5" />
      <path d="M16.5 7.5 7.5 16.5" />
    </Line>
  );
}

export function ClockIcon({ size = 16, className }: GlyphProps) {
  return (
    <Line size={size} className={className}>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 7.5V12L15 14" />
    </Line>
  );
}

export function AlertIcon({ size = 16, className }: GlyphProps) {
  return (
    <Line size={size} className={className}>
      <path d="M12 4 20.5 19.5H3.5z" />
      <path d="M12 10v4" />
      <path d="M12 16.9h.01" />
    </Line>
  );
}

export function MailIcon({ size = 16, className }: GlyphProps) {
  return (
    <Line size={size} className={className}>
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="m22 6-10 7L2 6" />
    </Line>
  );
}

export function MoonIcon({ size = 16, className }: GlyphProps) {
  return (
    <Line size={size} className={className}>
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </Line>
  );
}

export function SunIcon({ size = 16, className }: GlyphProps) {
  return (
    <Line size={size} className={className}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </Line>
  );
}

