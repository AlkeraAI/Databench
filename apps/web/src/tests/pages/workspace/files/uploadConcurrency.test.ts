// How many files a drop keeps in flight, and who decides.
//
// Every part answer carries `X-Upload-Concurrency`: the share of the server's
// upload budget this person holds while other people are uploading too
// (`apps/backend/backend/api/body_limit.py`). A queue that opens more lanes
// than that is asking for the 503 it then has to wait out; one that reads the
// guidance sends what will be admitted. The guidance can only ever narrow the
// queue — it is a ceiling from the server, not permission to flood.

import { beforeEach, describe, expect, it } from "vitest";

import { uploadConcurrency } from "@/api/filesUpload";
import { UPLOAD_CONCURRENCY } from "@/pages/workspace/files/dragDrop";
import { lanesAllowed } from "@/pages/workspace/files/useUploads";

function answered(concurrency: string | null): Response {
  return new Response(null, {
    headers: concurrency === null ? {} : { "x-upload-concurrency": concurrency },
  });
}

beforeEach(() => {
  uploadConcurrency.hint = null;
});

describe("the share the server names on a part answer", () => {
  it("is what a drop keeps in flight once it has been said", () => {
    expect(lanesAllowed()).toBe(UPLOAD_CONCURRENCY);
    uploadConcurrency.observe(answered("1"));
    expect(lanesAllowed()).toBe(1);
  });

  it("never opens more lanes than the queue's own default, however large the share", () => {
    uploadConcurrency.observe(answered(String(UPLOAD_CONCURRENCY + 47)));
    expect(lanesAllowed()).toBe(UPLOAD_CONCURRENCY);
  });

  it.each([
    ["0", "a share of none is not a lane count"],
    ["-2", "a negative share"],
    ["2.5", "half a part in flight"],
    ["plenty", "prose"],
    ["", "an empty header"],
  ])("ignores %s (%s) rather than acting on it", (raw) => {
    uploadConcurrency.observe(answered(raw));
    expect(uploadConcurrency.hint).toBeNull();
    expect(lanesAllowed()).toBe(UPLOAD_CONCURRENCY);
  });

  it("keeps the last guidance when a response carries none", () => {
    uploadConcurrency.observe(answered("2"));
    uploadConcurrency.observe(answered(null));
    expect(lanesAllowed()).toBe(2);
  });

  it("widens again when the share does, so a drop is not held down by an old crowd", () => {
    uploadConcurrency.observe(answered("1"));
    expect(lanesAllowed()).toBe(1);
    uploadConcurrency.observe(answered("2"));
    expect(lanesAllowed()).toBe(2);
  });
});
