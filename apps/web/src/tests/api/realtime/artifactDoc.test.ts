// The artifact reducer is the client half of the server's per-field last-writer-wins: the
// same `(ts, peer_id)` order, so both sides converge on the same value from any delivery
// order. Pinned: the tie-break, per-field independence, echoes as no-ops, the seconds unit of
// the writer clock, and the tolerance of the snapshot parser.

import { describe, expect, it } from "vitest";

import {
  ARTIFACT_FIELDS,
  applyArtifactOp,
  artifactValues,
  artifactWriteTs,
  isArtifactField,
  laterWrite,
  parseArtifactState,
  setFieldsOp,
  type ArtifactDocState,
} from "@/api/realtime/artifactDoc";

const state = (over: Partial<ArtifactDocState["fields"]> = {}, kb_version = 1): ArtifactDocState => ({
  kb_version,
  fields: {
    title: { value: "Orders fact", ts: 100, peer_id: "srv:0" },
    body: { value: "One row per order.", ts: 100, peer_id: "srv:0" },
    ...over,
  },
});

describe("parseArtifactState", () => {
  it("reads the wire shape", () => {
    const raw = {
      kb_version: 4,
      fields: {
        title: { value: "T", ts: 1757073660.5, peer_id: "p:1" },
        body: { value: "B", ts: 1757073600, peer_id: "srv:0" },
      },
    };
    expect(parseArtifactState(raw)).toEqual(raw);
  });

  it.each([
    ["null", null],
    ["a string", "state"],
    ["an empty object", {}],
    ["fields that are not an object", { kb_version: 1, fields: "x" }],
  ])("tolerates %s as an empty state", (_name, raw) => {
    expect(parseArtifactState(raw)).toEqual({ kb_version: typeof raw === "object" && raw && "kb_version" in raw ? 1 : 0, fields: {} });
  });

  it("drops unknown fields and malformed writes, defaults a missing peer id", () => {
    const parsed = parseArtifactState({
      kb_version: 2,
      fields: {
        title: { value: "T", ts: 5 },
        body: { value: 7, ts: 5, peer_id: "p" },
        summary: { value: "S", ts: 5, peer_id: "p" },
        extra: "junk",
      },
    });
    expect(parsed).toEqual({ kb_version: 2, fields: { title: { value: "T", ts: 5, peer_id: "" } } });
  });
});

describe("laterWrite — the (ts, peer_id) order", () => {
  it.each([
    ["a later clock wins", { ts: 2, peer_id: "a" }, { ts: 1, peer_id: "z" }, true],
    ["an earlier clock loses", { ts: 1, peer_id: "z" }, { ts: 2, peer_id: "a" }, false],
    ["an equal clock: the higher peer id wins", { ts: 1, peer_id: "p:b" }, { ts: 1, peer_id: "p:a" }, true],
    ["an equal clock: the lower peer id loses", { ts: 1, peer_id: "p:a" }, { ts: 1, peer_id: "p:b" }, false],
    ["the same write again is not later", { ts: 1, peer_id: "p:a" }, { ts: 1, peer_id: "p:a" }, false],
    ["the server's seed loses a tie to a client peer that sorts higher", { ts: 1, peer_id: "srv:0" }, { ts: 1, peer_id: "p:a" }, true],
    ["a fractional-second difference counts", { ts: 1.5, peer_id: "a" }, { ts: 1.25, peer_id: "z" }, true],
  ])("%s", (_name, candidate, current, expected) => {
    expect(laterWrite(candidate, current)).toBe(expected);
  });

  it("anything beats a field that was never written", () => {
    expect(laterWrite({ ts: 0, peer_id: "" }, undefined)).toBe(true);
  });
});

describe("applyArtifactOp", () => {
  const op = (fields: Record<string, { value: unknown; ts: number }>) => ({ intent: "set_fields" as const, fields });

  it("a later write replaces a field and reports it changed", () => {
    const next = applyArtifactOp(state(), op({ title: { value: "New", ts: 101 } }), "p:2");
    expect(next.changed).toEqual(["title"]);
    expect(next.state.fields.title).toEqual({ value: "New", ts: 101, peer_id: "p:2" });
    expect(next.state.fields.body).toEqual(state().fields.body);
  });

  it("an older write loses and changes nothing — the same state object comes back", () => {
    const before = state();
    const next = applyArtifactOp(before, op({ title: { value: "Old", ts: 99 } }), "p:2");
    expect(next.changed).toEqual([]);
    expect(next.state).toBe(before);
  });

  it("the later (ts, peer_id) wins per field independently: two peers each land their own field", () => {
    const afterA = applyArtifactOp(state(), op({ title: { value: "A's title", ts: 200 } }), "p:a");
    const afterB = applyArtifactOp(afterA.state, op({ body: { value: "B's body", ts: 150 } }), "p:b");
    expect(afterB.state.fields.title).toMatchObject({ value: "A's title", peer_id: "p:a" });
    expect(afterB.state.fields.body).toMatchObject({ value: "B's body", peer_id: "p:b" });
  });

  it("converges from either delivery order", () => {
    const a = op({ title: { value: "A", ts: 200 } });
    const b = op({ title: { value: "B", ts: 300 } });
    const ab = applyArtifactOp(applyArtifactOp(state(), a, "p:a").state, b, "p:b").state;
    const ba = applyArtifactOp(applyArtifactOp(state(), b, "p:b").state, a, "p:a").state;
    expect(ab).toEqual(ba);
    expect(ab.fields.title).toMatchObject({ value: "B" });
  });

  it("an equal clock breaks the tie on peer id, deterministically", () => {
    const tieA = applyArtifactOp(state(), op({ title: { value: "A", ts: 200 } }), "p:a").state;
    const tieB = applyArtifactOp(tieA, op({ title: { value: "B", ts: 200 } }), "p:b");
    expect(tieB.changed).toEqual(["title"]);
    const backA = applyArtifactOp(tieB.state, op({ title: { value: "A", ts: 200 } }), "p:a");
    expect(backA.changed).toEqual([]);
  });

  it("an echo of the same write is a no-op", () => {
    const first = applyArtifactOp(state(), op({ title: { value: "Same", ts: 250 } }), "p:me");
    const again = applyArtifactOp(first.state, op({ title: { value: "Same", ts: 250 } }), "p:me");
    expect(again.changed).toEqual([]);
    expect(again.state).toBe(first.state);
  });

  it("a mixed op applies the winning fields and skips the losing ones", () => {
    const next = applyArtifactOp(
      state(),
      op({ title: { value: "New", ts: 101 }, body: { value: "Stale", ts: 50 } }),
      "p:2",
    );
    expect(next.changed).toEqual(["title"]);
    expect(next.state.fields.body).toEqual(state().fields.body);
  });

  it("ignores unknown fields, non-string values and other intents", () => {
    expect(applyArtifactOp(state(), op({ summary: { value: "S", ts: 999 } }), "p:2").changed).toEqual([]);
    expect(applyArtifactOp(state(), op({ title: { value: 42, ts: 999 } }), "p:2").changed).toEqual([]);
    const before = state();
    expect(applyArtifactOp(before, { intent: "append", fields: { title: { value: "x", ts: 999 } } }, "p:2").state).toBe(before);
    expect(applyArtifactOp(before, { intent: "set_fields" }, "p:2").state).toBe(before);
  });
});

describe("the writer clock and the op shape", () => {
  it("artifactWriteTs is seconds since the epoch, as the server stamps content_updated_at", () => {
    expect(artifactWriteTs(1_757_073_660_500)).toBe(1_757_073_660.5);
    const now = artifactWriteTs();
    expect(now).toBeGreaterThan(1e9);
    expect(now).toBeLessThan(1e10);
  });

  it("setFieldsOp writes only the fields given, at one clock, and never an unknown one", () => {
    expect(setFieldsOp({ title: "T" }, 5)).toEqual({ intent: "set_fields", fields: { title: { value: "T", ts: 5 } } });
    expect(setFieldsOp({ title: "T", body: "B" }, 5).fields).toEqual({
      title: { value: "T", ts: 5 },
      body: { value: "B", ts: 5 },
    });
    expect(setFieldsOp({}, 5).fields).toEqual({});
    expect(setFieldsOp({ title: "" }, 5).fields).toEqual({ title: { value: "", ts: 5 } });
  });

  it("artifactValues flattens the state to plain strings per written field", () => {
    expect(artifactValues(state())).toEqual({ title: "Orders fact", body: "One row per order." });
    expect(artifactValues({ kb_version: 0, fields: {} })).toEqual({});
  });

  it("the field vocabulary is title and body", () => {
    expect([...ARTIFACT_FIELDS]).toEqual(["title", "body"]);
    expect(isArtifactField("title")).toBe(true);
    expect(isArtifactField("summary")).toBe(false);
  });
});
