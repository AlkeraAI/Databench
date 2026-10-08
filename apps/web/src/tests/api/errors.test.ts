import { describe, expect, it } from "vitest";

import { ApiError, GENERIC_FAILURE, refusalCopy, refusalSentence } from "@/api/errors";

describe("ApiError", () => {
  it("reads the canonical envelope", () => {
    const err = new ApiError(403, {
      error: { code: "forbidden", message: "You do not have permission.", trace_id: "t" },
    });
    expect(err.status).toBe(403);
    expect(err.code).toBe("forbidden");
    expect(err.message).toBe("You do not have permission.");
  });

  it("surfaces the field-level reason on a validation failure", () => {
    // The envelope's own message on a 422 is the generic "The request failed
    // validation." — useless to whoever typed the value. The reason they need is
    // one level down, in the per-field list.
    const err = new ApiError(422, {
      error: {
        code: "validation_error",
        message: "The request failed validation.",
        trace_id: "t",
        details: {
          errors: [
            {
              type: "value_error",
              loc: ["body", "org_name"],
              msg: "Value error, Organization name cannot contain a web address.",
            },
          ],
        },
      },
    });
    expect(err.code).toBe("validation_error");
    expect(err.message).toBe("Organization name cannot contain a web address.");
  });

  it("never surfaces a built-in constraint message a validator did not write", () => {
    // "String should have at least 1 character" reached the profile page's toast verbatim: it
    // names no field and is written for a developer. Only a validator's own sentence is shown.
    const err = new ApiError(422, {
      error: {
        code: "validation_error",
        message: "The request failed validation.",
        trace_id: "t",
        details: {
          errors: [
            { type: "string_too_short", loc: ["body", "first_name"], msg: "String should have at least 1 character" },
          ],
        },
      },
    });
    expect(err.message).toBe("The request failed validation.");
  });

  it("shows the sentence behind a custom error type our own validator named", () => {
    // A route's bounded figure is refused with `PydanticCustomError("budget_too_large", …)`:
    // the type is ours, not one of Pydantic's built-ins, so its message was written for the
    // reader and stands in for the envelope's placeholder.
    const err = new ApiError(422, {
      error: {
        code: "validation_error",
        message: "The request failed validation.",
        trace_id: "t",
        details: {
          errors: [
            { type: "budget_too_large", loc: ["body", "limit_nanos"], msg: "A budget can be at most $1,000,000,000 per cycle." },
          ],
        },
      },
    });
    expect(err.message).toBe("A budget can be at most $1,000,000,000 per cycle.");
  });

  it("falls back to the envelope message when no field detail is present", () => {
    const err = new ApiError(422, {
      error: { code: "validation_error", message: "The request failed validation.", trace_id: "t" },
    });
    expect(err.message).toBe("The request failed validation.");
  });

  it("only unwraps details for a validation error", () => {
    // A non-validation code already carries a written-for-humans message; the
    // details bag on those is structured context (a blocked domain, a plan tier),
    // not a better sentence.
    const err = new ApiError(400, {
      error: {
        code: "personal_email_blocked",
        message: "Please sign up with your work email.",
        details: { errors: [{ msg: "should not win" }] },
      },
    });
    expect(err.message).toBe("Please sign up with your work email.");
  });

  it("reads the flat envelope every Files route answers", () => {
    // The Files routes do not use the platform's nested `{error:{…}}` body —
    // `apps/backend/backend/api/deps/files_errors.py` answers `{code, message}`
    // at the top level. Read flat, or `ApiError.code` is null and no Files screen
    // can branch on a `files.*` code.
    const err = new ApiError(422, {
      code: "files.part_checksum_mismatch",
      message: "part checksum mismatch: declared 0xaa, computed 0xbb",
    });
    expect(err.status).toBe(422);
    expect(err.code).toBe("files.part_checksum_mismatch");
    expect(err.message).toBe("part checksum mismatch: declared 0xaa, computed 0xbb");
  });

  it("keeps the code of a flat refusal that carries no message", () => {
    // The opaque "not yours" body is `{code:"not_found"}` and nothing else, so the
    // code still has to survive while the sentence falls back to the status.
    const err = new ApiError(404, { code: "not_found" }, "Request failed");
    expect(err.code).toBe("not_found");
    expect(err.message).toBe("Request failed (404)");
  });

  it("prefers the nested envelope when a body somehow carries both", () => {
    // The platform handler is the one that writes `error.code`; a body carrying
    // both shapes came from it, so the nested code is the authoritative one.
    const err = new ApiError(403, {
      error: { code: "forbidden", message: "You do not have permission." },
      code: "files.held",
      message: "on hold",
    });
    expect(err.code).toBe("forbidden");
    expect(err.message).toBe("You do not have permission.");
  });

  it("keeps a Files refusal's detail bag so a caller can read the fields its code names", () => {
    const err = new ApiError(409, {
      code: "files.live_pending",
      message: "not written back yet",
      detail: { holder: "demo-box", outcome: "busy", landing: null },
    });
    expect(err.detail).toEqual({ holder: "demo-box", outcome: "busy", landing: null });
  });

  it("reads a Files refusal's fields from the envelope before the flat keys beside it", () => {
    const err = new ApiError(409, {
      error: {
        type: "urn:alkera:error:files.live_pending",
        code: "files.live_pending",
        status: 409,
        message: "not written back yet",
        trace_id: "t",
        details: { holder: "demo-box", outcome: "busy" },
      },
      code: "files.stale",
      message: "stale",
      detail: { holder: "an-older-box" },
    });
    expect(err.code).toBe("files.live_pending");
    expect(err.message).toBe("not written back yet");
    expect(err.detail).toEqual({ holder: "demo-box", outcome: "busy" });
  });

  it("has no detail bag for an envelope that names none, whatever the flat keys say", () => {
    const err = new ApiError(404, {
      error: { type: "urn:alkera:error:not_found", code: "not_found", status: 404, message: "Not found.", trace_id: "t" },
      code: "not_found",
      detail: { node_id: "n" },
    });
    expect(err.detail).toBeNull();
  });

  it.each([
    ["no detail at all", { code: "files.live_pending", message: "m" }],
    ["the legacy sentence", { detail: "Invalid email or password" }],
    ["a list", { code: "files.x", detail: ["a"] }],
    ["null", { code: "files.x", detail: null }],
  ])("has no detail bag for %s", (_name, body) => {
    expect(new ApiError(409, body).detail).toBeNull();
  });

  it("ignores a non-string flat code", () => {
    expect(new ApiError(500, { code: 7, message: "boom" }, "Request failed").code).toBeNull();
  });

  it("still parses the legacy detail shape", () => {
    expect(new ApiError(400, { detail: "Invalid email or password" }).message).toBe(
      "Invalid email or password",
    );
  });

  it("falls back when the body carries nothing parseable", () => {
    expect(new ApiError(502, null, "Upstream failed").message).toBe("Upstream failed (502)");
  });
});


describe("refusalCopy", () => {
  const copy = { conflict: "moved", forbidden: "not yours", fallback: "could not" };

  it.each([
    ["a version conflict, by status", new ApiError(409, { detail: "anything" }), "moved"],
    ["a version conflict, by code", new ApiError(400, { detail: { code: "version_conflict", message: "x" } }), "moved"],
    ["the server's own sentence", new ApiError(422, { code: "files.name_taken", message: "That name is taken" }), "That name is taken"],
    ["a bare forbidden", new ApiError(403, { code: "forbidden" }), "not yours"],
    ["nothing said: the fallback, never a status code", new ApiError(500, null), "could not"],
    ["not a refusal at all", new Error("network"), "could not"],
  ])("%s", (_why, error, expected) => {
    expect(refusalCopy(error, copy)).toBe(expected);
  });

  it("falls back where no forbidden copy was given", () => {
    expect(refusalCopy(new ApiError(403, { code: "forbidden" }), { conflict: "c", fallback: "f" })).toBe("f");
  });
});

describe("refusalSentence", () => {
  const envelope = (code: string, message: string) => ({ error: { code, message, trace_id: "t" } });
  const wording = {
    known: { last_admin: "Make someone else an admin first.", 404: "Not offered here." },
    unexplained: { 403: "Not yours." },
  };

  it.each([
    ["a caller's copy for the code beats the server's sentence", new ApiError(409, envelope("last_admin", "server words")), "Make someone else an admin first."],
    ["a caller's copy for the status beats the server's sentence", new ApiError(404, envelope("not_found", "Not found")), "Not offered here."],
    ["the code is read before the status", new ApiError(404, envelope("last_admin", "x")), "Make someone else an admin first."],
    ["an explained refusal keeps the server's sentence", new ApiError(429, envelope("org_creation_limited", "Try again later.")), "Try again later."],
    ["an unknown code with a sentence keeps the sentence", new ApiError(409, envelope("brand_new_code", "Said by the server.")), "Said by the server."],
    ["an unexplained refusal takes the caller's copy for its status", new ApiError(403, { code: "forbidden" }), "Not yours."],
    ["an unexplained refusal with no copy for it is generic", new ApiError(400, null, "could not join the organization"), GENERIC_FAILURE],
    ["a 5xx is generic even when the server wrote a sentence", new ApiError(500, envelope("internal_error", "An unexpected error occurred.")), GENERIC_FAILURE],
    ["a 503 the server explained is still generic", new ApiError(503, envelope("db_unavailable", "database down")), GENERIC_FAILURE],
    ["a transport failure is generic", new TypeError("Failed to fetch"), GENERIC_FAILURE],
    ["something that is not an error at all is generic", "boom", GENERIC_FAILURE],
  ])("%s", (_why, error, expected) => {
    expect(refusalSentence(error, wording)).toBe(expected);
  });

  it.each([
    ["a 500", 500],
    ["a 502", 502],
    ["an unexplained 409", 409],
    ["an unexplained 400", 400],
  ])("never shows the client's own diagnostic for %s", (_why, status) => {
    const error = new ApiError(status, null, "could not join the organization");
    expect(error.message).toBe(`could not join the organization (${status})`);
    expect(refusalSentence(error)).toBe(GENERIC_FAILURE);
  });

  it("uses the caller's fallback in place of the generic sentence", () => {
    expect(refusalSentence(new ApiError(500, null), { fallback: "This chat could not be renamed." })).toBe(
      "This chat could not be renamed.",
    );
  });

  it("does not let the copy for an unexplained status override a sentence the server wrote", () => {
    expect(refusalSentence(new ApiError(403, envelope("forbidden", "Only admins can do that.")), wording)).toBe(
      "Only admins can do that.",
    );
  });
});
