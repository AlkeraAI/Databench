// The build check that keeps the live draft's runtime out of what a page loads
// up front, driven over hand-made manifests: it must catch a leak through any
// static import, and must not mistake the lazy chunk for one.

import { describe, expect, it } from "vitest";

// @ts-expect-error -- a plain ESM build script, typed by its use here
import { chartProblems, editorProblems, portalProblems } from "../../../scripts/checkBundle.mjs";

type Manifest = Record<string, { file: string; isEntry?: boolean; imports?: string[]; dynamicImports?: string[] }>;

const lazy: Manifest = {
  "index.html": { file: "assets/index.js", isEntry: true, imports: ["_vendor.js"], dynamicImports: ["src/api/realtime/crdt/loroRuntime.ts"] },
  "_vendor.js": { file: "assets/vendor.js" },
  "src/api/realtime/crdt/loroRuntime.ts": { file: "assets/loroRuntime.js", imports: ["node_modules/loro-crdt/web/loro_wasm_bg.wasm"] },
  "node_modules/loro-crdt/web/loro_wasm_bg.wasm": { file: "assets/loro_wasm_bg.wasm" },
};

describe("portalProblems", () => {
  it("passes a build that loads Loro only on demand", () => {
    expect(portalProblems(lazy)).toEqual([]);
  });

  it("catches Loro pulled in through a chain of static imports", () => {
    const leaked: Manifest = {
      ...lazy,
      "_vendor.js": { file: "assets/vendor.js", imports: ["src/api/realtime/crdt/loroRuntime.ts"] },
    };
    expect(portalProblems(leaked)).toEqual([
      "index.html loads src/api/realtime/crdt/loroRuntime.ts up front",
      "index.html loads node_modules/loro-crdt/web/loro_wasm_bg.wasm up front",
    ]);
  });

  it("catches the WebAssembly loaded up front under any name", () => {
    const wasm: Manifest = {
      "vscode.html": { file: "assets/vscode.js", isEntry: true, imports: ["x.wasm"] },
      "x.wasm": { file: "assets/x.wasm" },
    };
    expect(portalProblems(wasm)).toEqual(["vscode.html loads x.wasm up front"]);
  });
});

describe("editorProblems", () => {
  it("passes an editor build with no Loro in it, and names every file that has it", () => {
    expect(editorProblems(["index.js", "font.woff2"])).toEqual([]);
    expect(editorProblems(["index.js", "loro_wasm_bg-abc.wasm", "loroRuntime-x.js"])).toEqual([
      "the editor build ships loro_wasm_bg-abc.wasm",
      "the editor build ships loroRuntime-x.js",
    ]);
  });
});

describe("the live file editor", () => {
  const editor: Manifest = {
    "index.html": {
      file: "assets/index.js",
      isEntry: true,
      imports: ["_vendor.js"],
      dynamicImports: ["src/pages/workspace/chat/workspace/LiveFileEditor.tsx"],
    },
    "_vendor.js": { file: "assets/vendor.js" },
    "src/pages/workspace/chat/workspace/LiveFileEditor.tsx": { file: "assets/LiveFileEditor.js", imports: ["_dist.js"] },
    "_dist.js": { file: "assets/dist.js" },
  };
  const contents: Record<string, string> = {
    "assets/index.js": "render()",
    "assets/vendor.js": "react()",
    "assets/LiveFileEditor.js": "editor()",
    "assets/dist.js": 'theme({".cm-editor":{}})',
  };
  const read = (file: string): string => contents[file] ?? "";

  it("passes a build that loads CodeMirror only with the editor", () => {
    expect(portalProblems(editor, read)).toEqual([]);
  });

  it("catches CodeMirror pulled up front through a shared chunk, whatever it is called", () => {
    const leaked: Manifest = { ...editor, "_vendor.js": { file: "assets/vendor.js", imports: ["_dist.js"] } };
    expect(portalProblems(leaked, read)).toEqual(["index.html loads CodeMirror up front in assets/dist.js"]);
  });

  it("catches the editor module itself loaded up front", () => {
    const leaked: Manifest = {
      ...editor,
      "_vendor.js": { file: "assets/vendor.js", imports: ["src/pages/workspace/chat/workspace/LiveFileEditor.tsx"] },
    };
    expect(portalProblems(leaked, read)).toContain(
      "index.html loads src/pages/workspace/chat/workspace/LiveFileEditor.tsx up front",
    );
  });

  it("fails an editor (webview) build that ships CodeMirror at all", () => {
    expect(editorProblems(["index.js", "dist.js"], (f: string) => (f === "dist.js" ? ".cm-content{}" : ""))).toEqual([
      "the editor build ships CodeMirror in dist.js",
    ]);
  });
});

describe("charts", () => {
  const RUNTIME = "../../packages/ui/src/charts/vegaRuntime.ts";
  const VEGA = "../../node_modules/.pnpm/vega@6.4.0/node_modules/vega/build/vega.module.js";
  const lazy: Manifest = {
    "index.html": { file: "assets/index.js", isEntry: true, imports: ["_vendor.js"], dynamicImports: [RUNTIME] },
    "vscode.html": { file: "assets/vscode.js", isEntry: true, imports: ["_vendor.js"], dynamicImports: [RUNTIME] },
    "_vendor.js": { file: "assets/vendor.js" },
    [RUNTIME]: { file: "assets/vegaRuntime.js", imports: [VEGA] },
    [VEGA]: { file: "assets/vega.js" },
  };
  const contents: Record<string, string> = {
    "assets/index.js": "render()",
    "assets/vscode.js": "render()",
    "assets/vendor.js": "react()",
    "assets/vegaRuntime.js": "mount()",
    "assets/vega.js": 'error("Unrecognized data set: "+t)',
  };
  const read = (file: string): string => contents[file] ?? "";

  it("passes a build where both entries load Vega only with the first chart", () => {
    expect(chartProblems(lazy, read)).toEqual([]);
  });

  it("catches the runtime pulled up front through a static import", () => {
    const leaked: Manifest = { ...lazy, "_vendor.js": { file: "assets/vendor.js", imports: [RUNTIME] } };
    expect(chartProblems(leaked, read)).toEqual([
      `index.html loads ${RUNTIME} up front`,
      `index.html loads ${VEGA} up front`,
      `vscode.html loads ${RUNTIME} up front`,
      `vscode.html loads ${VEGA} up front`,
    ]);
  });

  it("catches Vega bundled into an up-front chunk under any name", () => {
    const merged: Record<string, string> = { ...contents, "assets/vendor.js": 'react();error("Unrecognized signal name")' };
    expect(chartProblems(lazy, (f: string) => merged[f] ?? "")).toEqual([
      "index.html loads Vega up front in assets/vendor.js",
      "vscode.html loads Vega up front in assets/vendor.js",
    ]);
  });

  it("fails a chart chunk that names a package CDN or loads code from a URL", () => {
    const cdn: Record<string, string> = { ...contents, "assets/vega.js": `${contents["assets/vega.js"]};import("https://cdn.jsdelivr.net/npm/vega")` };
    expect(chartProblems(lazy, (f: string) => cdn[f] ?? "")).toEqual([
      "assets/vega.js names the package CDN cdn.jsdelivr.net",
      "assets/vega.js loads code from a URL (import\\(\\s*[\"'`](https?:)?\\/\\/)",
    ]);
  });

  it("leaves a CDN name in a chunk that is not the chart runtime to the other checks", () => {
    const elsewhere: Record<string, string> = { ...contents, "assets/vendor.js": "docs('https://unpkg.com/x')" };
    expect(chartProblems(lazy, (f: string) => elsewhere[f] ?? "")).toEqual([]);
  });
});
