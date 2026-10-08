import { describe, expect, it } from "vitest";

import vectors from "./generated/permissionPresentationVectors.json";
import {
  PERMISSION_MODES,
  PRESENTER_KEYS,
  presentPermission,
  redact,
  resolveTool,
  type PermissionAsk,
  type PermissionPresentation,
} from "./permissionPresentation";

interface Vector {
  name: string;
  ask: PermissionAsk;
  presentation: PermissionPresentation;
}

const VECTORS = vectors as unknown as Vector[];

// The conformance contract with the server. Each vector's `presentation` is
// what `alkera_core.permission_presentation.present` returned for its `ask`;
// this side must return exactly the same, field for field, or the web and Slack
// would describe one ask two different ways.
describe("the TS presenter reproduces the server's presentation", () => {
  it.each(VECTORS.map((vector) => [vector.name, vector] as const))("%s", (_name, vector) => {
    expect(presentPermission(vector.ask)).toEqual(vector.presentation);
  });

  it("is exercised by a vector for every registered presenter a title can come from", () => {
    const covered = new Set(VECTORS.map((vector) => vector.presentation.presenter));
    const titled = PRESENTER_KEYS.filter((key) => !key.endsWith("_lane"));
    expect(titled.filter((key) => !covered.has(key))).toEqual([]);
  });
});

describe("redaction", () => {
  it.each([
    ["AWS_SECRET_ACCESS_KEY=abc123 aws s3 ls", "AWS_SECRET_ACCESS_KEY=[redacted] aws s3 ls"],
    ["curl -H 'Authorization: Bearer tok.en'", "curl -H 'Authorization: Bearer [redacted]'"],
    ["psql postgres://app:pw@db/x", "psql postgres://app:[redacted]@db/x"],
    ["mysql --password=hunter2 -u root", "mysql --password=[redacted] -u root"],
    ["echo sk-abcdefghijklmnopqrstu", "echo [redacted]"],
  ])("hides the secret in %s", (text, expected) => {
    expect(redact(text)).toBe(expected);
  });

  it("leaves a line with no secret byte-identical", () => {
    const line = "git log --oneline -20 && echo tokens are counted";
    expect(redact(line)).toBe(line);
  });
});

describe("tool resolution", () => {
  it.each([
    ["mcp__alkera__blob.query", "blob.query"],
    ["alkera_blob_query", "blob.query"],
    ["Bash", "bash"],
    ["terminal", "bash"],
    ["task", null],
    ["some_future_guard", null],
  ])("%s resolves to %s", (wire, tool) => {
    expect(resolveTool(wire)).toBe(tool);
  });
});

describe("permission modes", () => {
  it("offers every stance the server accepts, bypass included, in the menu order", () => {
    expect(PERMISSION_MODES.map((mode) => mode.value)).toEqual([
      "default",
      "plan",
      "auto",
      "read_only",
      "bypass",
    ]);
  });
});
