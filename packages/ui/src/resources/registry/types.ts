// The extensible "reference" system: a tool can produce a result too big to
// inline (a wide table, a long text/JSON dump, a screenshot), stored as a blob
// and referenced by a stable handle. The chat shows a compact, named CHIP with
// a hover PREVIEW; the full, paginated content opens READ-ONLY in the editor
// page. New reference types register a renderer (see registry.tsx) without the
// chat transcript needing to know about them.
//
// The data contract (`BlobReference`, `BlobPage`, `BlobFetcher`) is React-free
// and lives in @alkera/chat-model; only the React-bearing renderer shape is
// declared here.

import type { ReactNode } from "react";

import type { BlobPage } from "@alkera/chat-model";

/** A pluggable renderer for one reference type. The registry maps a type key
 *  (declared `refType`, else the page `kind`) to one of these. */
export interface ReferenceRenderer {
  /** Registry key — matched against a reference's `refType` or a page `kind`. */
  type: string;
  /** Glyph shown on the inline chip. */
  icon: ReactNode;
  /** A one-line descriptor for the chip / preview header (e.g. "128 rows"). */
  describe(page: BlobPage): string;
  /** The compact hover preview (first page already fetched). */
  Preview: (props: { page: BlobPage }) => ReactNode;
  /** The full, paginated read-only view shown in the editor page. */
  FullView: (props: { page: BlobPage }) => ReactNode;
}
