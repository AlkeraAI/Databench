// The syntax a file is highlighted with, by its extension. Each language loads
// on its own when a file of that kind is opened, so the editor's first load
// carries none of them.

import type { Extension } from "@codemirror/state";

import { extensionOf } from "@alkera/ui";

type Loader = () => Promise<Extension>;

/** Files that read as prose wrap their lines until the reader says otherwise;
 *  code keeps its own. */
const PROSE = /\.(md|markdown|mdx|txt|text|rst|adoc|org)$/i;

export function wrapsByDefault(name: string): boolean {
  return PROSE.test(name);
}

const LOADERS: Record<string, Loader> = {
  js: () => import("@codemirror/lang-javascript").then((m) => m.javascript()),
  mjs: () => import("@codemirror/lang-javascript").then((m) => m.javascript()),
  cjs: () => import("@codemirror/lang-javascript").then((m) => m.javascript()),
  jsx: () => import("@codemirror/lang-javascript").then((m) => m.javascript({ jsx: true })),
  ts: () => import("@codemirror/lang-javascript").then((m) => m.javascript({ typescript: true })),
  mts: () => import("@codemirror/lang-javascript").then((m) => m.javascript({ typescript: true })),
  tsx: () => import("@codemirror/lang-javascript").then((m) => m.javascript({ jsx: true, typescript: true })),
  py: () => import("@codemirror/lang-python").then((m) => m.python()),
  json: () => import("@codemirror/lang-json").then((m) => m.json()),
  md: () => import("@codemirror/lang-markdown").then((m) => m.markdown()),
  markdown: () => import("@codemirror/lang-markdown").then((m) => m.markdown()),
  css: () => import("@codemirror/lang-css").then((m) => m.css()),
  html: () => import("@codemirror/lang-html").then((m) => m.html()),
  htm: () => import("@codemirror/lang-html").then((m) => m.html()),
  sql: () => import("@codemirror/lang-sql").then((m) => m.sql()),
  yaml: () => import("@codemirror/lang-yaml").then((m) => m.yaml()),
  yml: () => import("@codemirror/lang-yaml").then((m) => m.yaml()),
};

/** The language extension for `name`, or `null` for a file drawn as plain text. */
export function languageLoaderFor(name: string): Loader | null {
  return LOADERS[extensionOf(name)] ?? null;
}
