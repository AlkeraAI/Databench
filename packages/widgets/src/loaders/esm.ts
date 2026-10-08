// anywidget ESM as text. An inline <script type="module"> cannot hand its
// exports to anyone, so the source is rewritten before injection: the module
// ends by registering its exports under a name the loader chose
// (`window.__alkRegister`, see ../registry). The lexer is es-module-lexer's
// asm.js build, because the frame's CSP blocks WebAssembly.
import { parse } from "es-module-lexer/js";

import { REGISTER, whenRegistered, type ModuleExports } from "../registry";

export class EsmImportError extends Error {
  constructor(public readonly specifier: string) {
    super(
      `this widget imports ${specifier}; notebook outputs cannot load code from elsewhere, so a widget must bundle its dependencies`,
    );
    this.name = "EsmImportError";
  }
}

/** The rewritten source and the export names it hands over. */
export interface RewrittenEsm {
  code: string;
  exports: string[];
}

/** The version anywidget ESM registers under. */
export const ESM_VERSION = "esm";

const DEFAULT_LOCAL = "__alk_default";

/**
 * Rewrites `code` so that, evaluated as a module, it calls
 * `globalThis.__alkRegister(name, version, {<export>: <local>, ...})`.
 * Refuses a module that statically imports or re-exports another module: in
 * the frame that is a fetch, which the CSP blocks, before any of it runs.
 */
export function rewriteEsm(code: string, name: string, version = ESM_VERSION): RewrittenEsm {
  const [imports, exports] = parse(code);
  for (const imp of imports) {
    // A dynamic import is code that may never run (a lazy path); if it does,
    // the CSP refuses the fetch and the widget reports it then.
    if (imp.type === "import-meta" || imp.type === "dynamic") continue;
    throw new EsmImportError(imp.specifier);
  }
  let out = code;
  const binds: [string, string][] = [];
  // Edit from the end so earlier offsets stay valid.
  const sorted = [...exports].sort((a, b) => b.start - a.start);
  for (const exp of sorted) {
    if (exp.type !== "direct") continue; // re-exports were refused above
    const local = exp.localStart >= 0 ? code.slice(exp.localStart, exp.localEnd) : null;
    if (local !== null) {
      binds.push([exp.name, local]);
    } else if (exp.name === "default") {
      // `export default <expression>`: give the expression a local name.
      out = `${out.slice(0, exp.exportStart)}const ${DEFAULT_LOCAL} =${out.slice(exp.end)}`;
      binds.push(["default", DEFAULT_LOCAL]);
    }
  }
  binds.reverse();
  const members = binds.map(([exportName, local]) => `${JSON.stringify(exportName)}: ${local}`).join(", ");
  const trailer = `\n;globalThis.${REGISTER}(${JSON.stringify(name)}, ${JSON.stringify(version)}, {${members}});\n`;
  return { code: out + trailer, exports: binds.map(([exportName]) => exportName) };
}

const loading = new Map<string, (err: Error) => void>();
let sequence = 0;
let watching = false;

function watchErrors(): void {
  if (watching) return;
  watching = true;
  // A module that throws while evaluating never reaches its trailer; the
  // error surfaces on the window, and fails every load still in flight.
  addEventListener("error", (event: ErrorEvent) => {
    for (const [name, fail] of loading) {
      loading.delete(name);
      fail(new Error(event.message || "the widget's module threw while loading"));
    }
  });
}

/** Evaluates ESM source as an inline module and resolves with the exports it
 *  registered. */
export function loadEsm(code: string, timeoutMs = 20_000): Promise<ModuleExports> {
  watchErrors();
  const name = `esm:${++sequence}`;
  let rewritten: RewrittenEsm;
  try {
    rewritten = rewriteEsm(code, name);
  } catch (err) {
    return Promise.reject(err instanceof Error ? err : new Error(String(err)));
  }
  const failed = new Promise<never>((_resolve, reject) => loading.set(name, reject));
  const registered = whenRegistered(name, timeoutMs).then(
    (r) => r.exports,
    () => {
      throw new Error("the widget's module did not finish loading");
    },
  );
  const script = document.createElement("script");
  script.type = "module";
  script.addEventListener("error", () => loading.get(name)?.(new Error("the widget's module failed to load")));
  script.textContent = rewritten.code;
  document.head.appendChild(script);
  return Promise.race([registered, failed]).finally(() => loading.delete(name));
}
