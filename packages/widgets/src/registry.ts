// Module registration inside the output frame. Every script injected into the
// frame (this bundle, an environment library's AMD file once instantiated, an
// anywidget ESM after its export rewrite) hands over its exports by calling
// `window.__alkRegister(name, version, exports)`; nothing else is placed on
// `window`. The frame host may define the function first (to receive this
// bundle's exports); installing here chains to it, so both see every call.

export const REGISTER = "__alkRegister";

export type ModuleExports = Record<string, unknown>;

export type Register = (name: string, version: string, exports: ModuleExports) => void;

export interface Registration {
  name: string;
  version: string;
  exports: ModuleExports;
}

type Listener = (registration: Registration) => void;

const OURS = Symbol.for("alkera.widgets.register");

interface State {
  registrations: Map<string, Registration>;
  listeners: Set<Listener>;
}

type Marked = Register & { [OURS]?: State };

function host(): Record<string, unknown> {
  return globalThis as unknown as Record<string, unknown>;
}

/** Installs `window.__alkRegister` once, chaining to one the host defined.
 *  Returns the registry's state (the same object on every call). */
function install(): State {
  const g = host();
  const current = g[REGISTER] as Marked | undefined;
  const existing = current?.[OURS];
  if (existing) return existing;
  const state: State = { registrations: new Map(), listeners: new Set() };
  const previous = typeof current === "function" ? current : null;
  const register: Marked = (name, version, exports) => {
    if (typeof name !== "string" || !name) return;
    const registration: Registration = { name, version: String(version ?? ""), exports: exports ?? {} };
    state.registrations.set(name, registration);
    for (const listener of [...state.listeners]) listener(registration);
    previous?.(name, version, exports);
  };
  register[OURS] = state;
  g[REGISTER] = register;
  return state;
}

/** Registers a module, as an injected script would. */
export function register(name: string, version: string, exports: ModuleExports): void {
  install();
  (host()[REGISTER] as Register)(name, version, exports);
}

export function registration(name: string): Registration | undefined {
  return install().registrations.get(name);
}

/** Calls `listener` for every later registration; returns the unsubscribe. */
export function onRegister(listener: Listener): () => void {
  const state = install();
  state.listeners.add(listener);
  return () => state.listeners.delete(listener);
}

/** Resolves once `name` registers (now, if it already has). */
export function whenRegistered(name: string, timeoutMs: number): Promise<Registration> {
  const known = registration(name);
  if (known) return Promise.resolve(known);
  return new Promise<Registration>((resolve, reject) => {
    const stop = onRegister((r) => {
      if (r.name !== name) return;
      stop();
      clearTimeout(timer);
      resolve(r);
    });
    const timer = setTimeout(() => {
      stop();
      reject(new Error(`${name} did not register`));
    }, timeoutMs);
  });
}
