// One address, one surviving object.
//
// `/objects/:objectId` is the link a chat hands out after "Save as result". This
// route reads the object once and lets its TYPE decide: a result renders, a
// retired kind (a saved query or a report, now filed as chat templates) says its
// kind went, and a live kind with a page of its own (a chat, a workspace, a
// template) is sent to the address the server names for it.
//
// Deciding here rather than inside the result page matters. A
// query has no rows and no CSV (both routes are result-only and answer 404), so
// a page that fires those requests anyway leaves 404s in the log of a screen
// that looks fine. The read is one request either way — the result page asks
// for the same object under the same query key and gets the cache.

import type { ReactElement } from "react";
import { Link, Navigate, useParams } from "react-router-dom";

import { useObject } from "../../../api/objects";

import { ObjectPage } from "./ObjectPage";
import { ObjectUnavailable } from "./ObjectUnavailable";

export function ObjectRoute(): ReactElement {
  const { objectId } = useParams<{ objectId: string }>();
  const object = useObject(objectId);

  if (object.isError) {
    return <ObjectUnavailable error={object.error} onRetry={() => void object.refetch()} />;
  }
  if (!object.data) return <p className="alk-caption">Loading…</p>;
  // A server older than the field sends none, and the page then answers as it did.
  const page = object.data.web_url ?? "";
  if (object.data.type !== "result" && page.startsWith("/") && !page.startsWith("/objects/")) {
    return <Navigate to={page} replace />;
  }
  if (object.data.type !== "result") {
    // Where the thing went is the only part a reader cannot work out from the
    // sentence: the retirement moved every saved query and report into the
    // owner's Chat Templates folder.
    return (
      <p className="alk-caption">
        This kind of object was retired. Saved queries and reports are chat templates now, under
        Chat Templates in <Link to="/files">Files</Link>.
      </p>
    );
  }
  return <ObjectPage />;
}
