import { useCallback, useEffect, useState } from "react";

import type { BlobFetcher, BlobPage, BlobReference } from "@alkera/chat-model";

import { DataTablePager } from "../DataTable";
import { resolveReferenceRenderer } from "../registry";
import "./blobview.css";

type ViewState =
  | { status: "loading" }
  | { status: "ready"; page: BlobPage }
  | { status: "error"; message: string };

/** The full, READ-ONLY, paginated view of a blob — the editor-page surface a
 *  reference opens into. Rows paginate by row, text by character. The DAEMON owns
 *  the page size: the first fetch sends no limit so the daemon picks its per-kind
 *  default, then every page (incl. a typed jump) reuses `page.limit` — the size it
 *  actually returned — so the pager's offset math never disagrees with the server
 *  and skips rows. Content rendering comes from the registry (table for rows, text
 *  for text, …) so new reference types render here without changes. */
export function BlobView({
  reference,
  fetchBlob,
}: {
  reference: BlobReference;
  fetchBlob: BlobFetcher;
}) {
  const [state, setState] = useState<ViewState>({ status: "loading" });

  const load = useCallback(
    (nextOffset: number, limit?: number) => {
      setState({ status: "loading" });
      fetchBlob(reference.handle, nextOffset, limit).then(
        (page) => setState({ status: "ready", page }),
        (err: unknown) =>
          setState({ status: "error", message: err instanceof Error ? err.message : "Couldn't load this result." }),
      );
    },
    [fetchBlob, reference.handle],
  );

  // Initial page — no explicit limit, so the daemon picks the per-kind default
  // (we don't yet know rows vs text). `load` is keyed on the handle, so opening a
  // different reference reloads from the top.
  useEffect(() => {
    load(0);
  }, [load]);

  if (state.status === "loading") {
    return <div className="alk-blobv-status" role="status">Loading result…</div>;
  }
  if (state.status === "error") {
    return <div className="alk-blobv-status" role="alert">{state.message}</div>;
  }

  const { page } = state;
  const renderer = resolveReferenceRenderer(reference.refType, page.kind);
  // The daemon owns the page size — `page.limit` is what it actually paged by, so
  // the pager's offset math agrees with the server and never skips rows. (A client
  // guess that differs from the daemon default — rows page by 50, not 100 — would.)
  const limit = Math.max(1, page.limit);
  const unit = page.kind === "rows" ? "rows" : "characters";
  const from = page.total === 0 ? 0 : page.offset + 1;
  const to = page.offset + page.returned;
  // Jumping to page P refetches from offset (P-1)·limit, reusing the server's limit.
  const pageCount = Math.max(1, Math.ceil(page.total / limit));
  const current = Math.floor(page.offset / limit) + 1;

  return (
    <div className="alk-blobv">
      <div className="alk-blobv__body">{renderer.FullView({ page })}</div>
      <div className="alk-blobv__bar">
        <DataTablePager
          page={current}
          pageCount={pageCount}
          onPage={(target) => load((target - 1) * limit, limit)}
          count={
            <>
              {renderer.describe(page)} · showing {from.toLocaleString()}–{to.toLocaleString()} of{" "}
              {page.total.toLocaleString()} {unit}
            </>
          }
        />
      </div>
    </div>
  );
}
