// Load-time patches for widget modules, keyed by module name and version
// range, applied right after a module registers: to the platform's own
// modules and to environment assets alike, since an environment file arrives
// unmodified from the library's nbextension.
import { parseVersion, satisfies } from "./semver";

export interface ModulePatch {
  /** Shown in diagnostics, for example "VegaExprModel.updateFunction". */
  name: string;
  module: string;
  range: string;
  /** Applies the patch to the module's exports. Returns false when the
   *  exports do not have the shape the patch replaces (nothing changed). */
  apply(exports: Record<string, unknown>): boolean;
}

export class PatchRegistry {
  private readonly patches: ModulePatch[] = [];

  register(patch: ModulePatch): void {
    this.patches.push(patch);
  }

  /**
   * Applies every patch registered for `module` whose range admits `version`.
   * When the version is not a concrete version (an environment asset whose
   * version the engine could not read), every patch for the module is tried;
   * each one checks the shape it replaces, so a patch never applies blindly.
   * Returns the names of the patches that changed something.
   */
  apply(module: string, version: string, exports: Record<string, unknown>): string[] {
    const concrete = parseVersion(version) !== null;
    const applied: string[] = [];
    for (const patch of this.patches) {
      if (patch.module !== module) continue;
      if (concrete && !satisfies(version, patch.range)) continue;
      if (patch.apply(exports)) applied.push(patch.name);
    }
    return applied;
  }
}
