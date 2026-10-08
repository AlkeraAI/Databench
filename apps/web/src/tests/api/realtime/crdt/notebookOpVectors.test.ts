// The shared notebook op vectors, run against the browser's editor.
//
// `packages/alkera-notebook/tests/vectors/notebook_ops.json` is the one
// statement of what an op means; the file store and the platform's live
// document run the same file. Each case is seeded as a live Loro notebook,
// applied through the editor's `apply` and read back from the document.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import type { NotebookOp } from "@alkera/notebook-ui";

import { NotebookDocument, NotebookOpRefused, type NotebookDocSource } from "@/api/realtime/crdt/notebookDoc";

import { loroNode } from "./liveServer";

interface VectorCell {
  id: string;
  kind: string;
  source: string;
  meta?: Record<string, unknown>;
}

interface VectorCase {
  name: string;
  cells: VectorCell[];
  ops: unknown[];
  expect: { cells: [string, string, string][] } | { error: string; index: number };
}

const VECTORS = resolve(process.cwd(), "../../packages/alkera-notebook/tests/vectors/notebook_ops.json");
const CASES = (JSON.parse(readFileSync(VECTORS, "utf8")) as { cases: VectorCase[] }).cases;

type LoroDoc = InstanceType<typeof loroNode.LoroDoc>;

function seeded(cells: VectorCell[]): LoroDoc {
  const doc = new loroNode.LoroDoc();
  doc.setPeerId(1n);
  const root = doc.getMap("cells");
  const order = doc.getMovableList("order");
  for (const cell of cells) {
    const map = root.setContainer(cell.id, new loroNode.LoroMap());
    map.set("kind", cell.kind);
    map.set("name", "_");
    map.setContainer("source", new loroNode.LoroText()).insert(0, cell.source);
    map.setContainer("config", new loroNode.LoroMap());
    const meta = map.setContainer("meta", new loroNode.LoroMap());
    for (const [key, value] of Object.entries(cell.meta ?? {})) meta.set(key, value as never);
    map.setContainer("extra", new loroNode.LoroMap());
    map.set("deleted", false);
    order.push(cell.id);
  }
  doc.commit();
  return doc;
}

function editor(doc: LoroDoc): NotebookDocument {
  doc.setPeerId(2n);
  const source: NotebookDocSource = { doc, canWrite: true, listen: () => () => {}, localCommitted: () => {} };
  let n = 0;
  return new NotebookDocument({ loro: loroNode, source, newId: () => `n${String(n++).padStart(9, "0")}` });
}

describe("the shared op vectors", () => {
  it("are there to run", () => {
    expect(CASES.length).toBeGreaterThanOrEqual(22);
  });

  it.each(CASES.map((c) => [c.name, c] as const))("%s", (_name, vector) => {
    const doc = seeded(vector.cells);
    const nb = editor(doc);
    const ops = vector.ops as NotebookOp[];
    if ("error" in vector.expect) {
      let refused: unknown = null;
      try {
        nb.apply(ops);
      } catch (error) {
        refused = error;
      }
      expect(refused).toBeInstanceOf(NotebookOpRefused);
      const { code, index } = refused as NotebookOpRefused;
      expect({ code, index }).toEqual({ code: vector.expect.error, index: vector.expect.index });
      return;
    }
    nb.apply(ops);
    const named = new Set(vector.cells.map((c) => c.id));
    const got = nb
      .snapshot()
      .cells.filter((c) => named.has(c.id))
      .map((c) => [c.id, c.kind, c.source]);
    expect(got).toEqual(vector.expect.cells);
  });
});
