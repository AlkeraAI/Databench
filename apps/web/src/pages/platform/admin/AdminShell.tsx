// The admin page frame — every register renders inside it. It marks the dense product surface (so
// the hero/scroll gates skip while the craft laws hold) and portals the page subtitle into the
// masthead. Navigation between registers lives in the sidebar (Admin's subtabs), not here, so a
// register page is just its stacked content.

import type { ReactNode } from "react";

import { TopbarSubtitle } from "../../../app/Topbar";

// The admin page frame — one content column so every register's plates/tables stack on the scale from
// a uniform top. The page's single layout container (a page-root flex column, not an inner grid).
const shell: React.CSSProperties = {
  color: "var(--alkPrimaryText)",
  display: "flex",
  flexDirection: "column",
  gap: "var(--alkSpace7)",
  minWidth: 0,
};

export function AdminShell({ subtitle, children }: { subtitle?: ReactNode; children: ReactNode }) {
  return (
    <div style={shell} data-measure-surface="product">
      {subtitle != null ? <TopbarSubtitle>{subtitle}</TopbarSubtitle> : null}
      {children}
    </div>
  );
}
