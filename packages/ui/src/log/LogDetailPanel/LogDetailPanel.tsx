// The one shared treatment for a log/record detail drawer, used by every log surface: the org audit
// log, the platform audit log, and the crash-report register. A record's row opens this panel; the
// panel shows a summary of the core fields, then the known structured fields as labeled blocks
// (a stack trace, logs, a request line), and finally any leftover detail as highlighted JSON.
//
// Columns and row content differ per page; the panel chrome, the summary treatment, the section
// label, and the code plate do not — so the three logs read as one design.

import type { ReactNode } from "react";

import { CodeBlock, DescList, DescRow, SidePanel, Terminal } from "../../primitives";

import "./log-panel.css";

export interface LogDetailPanelProps {
  open: boolean;
  onClose: () => void;
  /** Small-caps eyebrow over the title (the record kind, e.g. "Audit event"). */
  eyebrow: ReactNode;
  /** The record's headline — its action, error type, or component. */
  title: ReactNode;
  /** Header actions (e.g. a link to the actor), left of the close button. */
  headActions?: ReactNode;
  width?: number;
  children: ReactNode;
}

/** The drawer shell. Every log detail opens the same width, chrome, and motion. */
export function LogDetailPanel({ open, onClose, eyebrow, title, headActions, width = 520, children }: LogDetailPanelProps) {
  return (
    <SidePanel open={open} onClose={onClose} anchor="viewport" mode="modal" width={width} eyebrow={eyebrow} title={title} headActions={headActions}>
      <div className="alk-logdetail">{children}</div>
    </SidePanel>
  );
}

/** The summary of a record's core fields — the same definition-list treatment on every log. */
export function LogSummary({ children }: { children: ReactNode }) {
  return <DescList>{children}</DescList>;
}

export { DescRow as LogRow };

/** A labeled block for a known structured field. `code` renders highlighted source / JSON; `terminal`
 *  renders command output; otherwise the text is plain prose. One treatment for every named field. */
export function LogSection({
  label,
  children,
  render = "text",
  language,
}: {
  label: ReactNode;
  children: string;
  render?: "text" | "code" | "terminal";
  language?: string;
}) {
  return (
    <section className="alk-logdetail__section">
      <span className="alk-eyebrow">{label}</span>
      {render === "code" ? (
        <CodeBlock code={children} language={language} lineNumbers={false} wrap maxHeight={320} aria-label={String(label)} />
      ) : render === "terminal" ? (
        <Terminal output={children} maxHeight={320} aria-label={String(label)} />
      ) : (
        <p className="alk-logdetail__prose">{children}</p>
      )}
    </section>
  );
}

/** The catch-all: a record's leftover detail dict as highlighted JSON. Renders nothing when empty, so
 *  a record whose fields are all known never shows an empty "Detail" block. */
export function LogJson({ label = "Detail", data }: { label?: ReactNode; data: Record<string, unknown> | null | undefined }) {
  if (data == null || Object.keys(data).length === 0) return null;
  return <LogSection label={label} render="code" language="json">{JSON.stringify(data, null, 2)}</LogSection>;
}
