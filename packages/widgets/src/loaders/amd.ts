// Classic AMD widget bundles (a library's own nbextension `index.js`) as text.
// The file is injected as an inline classic script with a `define` shim in
// place; the shim captures the module's dependencies and factory, and the
// manager resolves the dependencies before calling the factory.

export interface AmdDefinition {
  name: string | null;
  deps: string[];
  factory: unknown;
}

/** An unfetchable URL handed to webpack's automatic publicPath, which reads
 *  `document.currentScript.src` and throws on an inline script. Anything the
 *  bundle then loads relative to it is blocked by the frame's CSP. */
export function publicPathFor(name: string): string {
  return `https://widget-assets.invalid/${encodeURIComponent(name)}/index.js`;
}

type Define = ((...args: unknown[]) => void) & { amd: object };

/** Runs classic script text synchronously. The frame's injector appends an
 *  inline <script>; the publicPath stand-in becomes that element's `src`. */
export type ScriptInjector = (code: string, publicPath: string) => void;

export const injectClassicScript: ScriptInjector = (code, publicPath) => {
  const script = document.createElement("script");
  Object.defineProperty(script, "src", { get: () => publicPath });
  script.textContent = code;
  document.head.appendChild(script); // an inline classic script runs synchronously
};

function makeDefine(captured: AmdDefinition[]): Define {
  const define = ((...args: unknown[]) => {
    let name: string | null = null;
    let deps: string[] = [];
    if (typeof args[0] === "string") name = args.shift() as string;
    if (Array.isArray(args[0])) deps = (args.shift() as unknown[]).map(String);
    const factory = args[0];
    captured.push({ name, deps, factory });
  }) as Define;
  define.amd = { jQuery: false };
  return define;
}

/**
 * Runs `code` with the shim installed as `window.define` and returns the
 * definitions it made. A named definition for `moduleName` wins over anonymous
 * ones; a bundle that defines nothing is an error.
 */
export function evaluateAmd(code: string, moduleName: string, inject: ScriptInjector = injectClassicScript): AmdDefinition {
  const captured: AmdDefinition[] = [];
  const g = globalThis as unknown as Record<string, unknown>;
  const previous = g.define;
  g.define = makeDefine(captured);
  try {
    inject(code, publicPathFor(moduleName));
  } finally {
    g.define = previous;
  }
  const chosen = captured.find((d) => d.name === moduleName) ?? captured.find((d) => d.name === null) ?? captured[0];
  if (!chosen) throw new Error(`${moduleName} did not define a widget module`);
  return chosen;
}

/** Calls the factory with resolved dependencies (`require`, `exports` and
 *  `module` get their CommonJS-style stand-ins). */
export function instantiateAmd(definition: AmdDefinition, resolved: Map<string, unknown>): Record<string, unknown> {
  const exportsObject: Record<string, unknown> = {};
  const moduleObject = { exports: exportsObject };
  const args = definition.deps.map((dep) => {
    if (dep === "exports") return exportsObject;
    if (dep === "module") return moduleObject;
    if (dep === "require") {
      return (name: string) => {
        if (!resolved.has(name)) throw new Error(`${name} is not available to this widget`);
        return resolved.get(name);
      };
    }
    return resolved.get(dep);
  });
  const { factory } = definition;
  const value = typeof factory === "function" ? (factory as (...a: unknown[]) => unknown)(...args) : factory;
  const result = value === undefined ? moduleObject.exports : value;
  return (result ?? {}) as Record<string, unknown>;
}

/** Dependencies the manager must supply before instantiating. */
export function externalDeps(definition: AmdDefinition): string[] {
  return definition.deps.filter((d) => d !== "exports" && d !== "module" && d !== "require");
}
