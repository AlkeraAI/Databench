// Extension points: how private code extends the open portal, editor and chat by registration.
//
// Open code never imports private code. Where the open app needs something only a
// private distribution supplies (a page, a sidebar leaf, an editor surface, a tool card),
// it declares an ExtensionPoint and reads it; the private distribution ships a WebExtension
// whose `install` registers into the points it extends. This is the browser twin of
// `alkera_core.extensions`, with the same rules. It lives in @alkera/ui (as
// `@alkera/ui/extensions`) because the chat's own points sit in this package.
//
// Composition is explicit. A product entry point (`src/main.tsx`, `src/vscode-main.tsx`)
// passes its list of extensions to `installExtensions` before it boots the open app.
// With nothing installed, every point is empty and the open app runs on its own.
//
// A point freezes the first time it is read. Registering after that throws instead of
// leaving a reader that already rendered without the late item, so every installation
// has to happen during composition, before the first render.

export class ExtensionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ExtensionError";
  }
}

/** One seam: an ordered set of items the open app reads and extensions register into.
 *  Every item carries a `key`, unique within the point. */
export class ExtensionPoint<T extends { readonly key: string }> {
  readonly name: string;
  #items: T[] = [];
  #frozen = false;

  constructor(name: string) {
    this.name = name;
  }

  get frozen(): boolean {
    return this.#frozen;
  }

  /** Add `item` after everything registered before it. */
  register(item: T): void {
    if (this.#frozen) {
      throw new ExtensionError(
        `extension point ${this.name} was already read; register during composition, before the app renders`,
      );
    }
    if (this.#items.some((existing) => existing.key === item.key)) {
      throw new ExtensionError(`${item.key} is already registered on ${this.name}`);
    }
    this.#items.push(item);
  }

  /** Everything registered, in registration order. Freezes the point. */
  items(): readonly T[] {
    this.#frozen = true;
    return [...this.#items];
  }
}

/** A private distribution's contribution: `install` registers into the points it
 *  extends. `name` identifies it across installs. */
export interface WebExtension {
  readonly name: string;
  install(): void;
}

const installed = new Map<string, WebExtension>();

/** Install each extension once, in order. Installing the same extension again is a
 *  no-op; a different extension under a name already installed throws. */
export function installExtensions(extensions: readonly WebExtension[]): void {
  for (const extension of extensions) {
    const current = installed.get(extension.name);
    if (current === extension) continue;
    if (current) throw new ExtensionError(`a different extension named ${extension.name} is installed`);
    extension.install();
    installed.set(extension.name, extension);
  }
}

/** The names of every installed extension, in installation order. */
export function installedExtensions(): readonly string[] {
  return [...installed.keys()];
}
