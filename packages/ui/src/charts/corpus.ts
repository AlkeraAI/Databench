// The shared chart corpus (`packages/api-core/tests/fixtures/charts`), read for
// the renderer's tests. The Python validator walks the same files, so a case
// added on either side is a case on both.

import { readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";

export const CORPUS = resolve(__dirname, "../../../api-core/tests/fixtures/charts");

export interface CorpusCase<T> {
  name: string;
  value: T;
}

export function corpus<T = Record<string, unknown>>(kind: string): CorpusCase<T>[] {
  const dir = join(CORPUS, kind);
  return readdirSync(dir)
    .filter((file) => file.endsWith(".json"))
    .sort()
    .map((file) => ({ name: file.replace(/\.json$/, ""), value: JSON.parse(readFileSync(join(dir, file), "utf8")) as T }));
}

export interface InvalidCase {
  spec: Record<string, unknown>;
  path: string;
  policy: string;
  unsafe: boolean;
}
