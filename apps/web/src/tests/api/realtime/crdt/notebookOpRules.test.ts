// The browser's port of the SQL and Markdown cell templates, held to the
// vectors their owner (`alkera_notebook.format.templates`) runs:
// `packages/alkera-notebook/tests/vectors/notebook_templates.json`.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { classify, renderCell } from "@/api/realtime/crdt/notebookOpRules";

interface Reading {
  name: string;
  code: string;
  reads_as: [string, string, Record<string, unknown>];
}

interface Rendering extends Reading {
  kind: string;
  source: string;
  meta: Record<string, unknown>;
}

const VECTORS = JSON.parse(readFileSync(resolve(process.cwd(), "../../packages/alkera-notebook/tests/vectors/notebook_templates.json"), "utf8")) as {
  render: Rendering[];
  classify: Reading[];
};

const read = (code: string): [string, string, Record<string, unknown>] => {
  const { kind, source, meta } = classify(code);
  return [kind, source, meta];
};

describe("the cell templates", () => {
  it.each(VECTORS.render.map((c) => [c.name, c] as const))("render %s as the owner does", (_name, vector) => {
    const code = renderCell(vector.kind, vector.source, vector.meta);
    expect(code).toBe(vector.code);
    expect(read(code)).toEqual(vector.reads_as);
  });

  it.each(VECTORS.classify.map((c) => [c.name, c] as const))("read %s as the owner does", (_name, vector) => {
    expect(read(vector.code)).toEqual(vector.reads_as);
  });
});
