// How an environment is named to a person.
//
// An environment's id is `<kind>:<spec root relative to the workspace>`, so
// its name comes from there. Its `spec_root` is where the spec lives on the
// machine running the kernel: an absolute path on a box means nothing to the
// person and is never shown. The Python version is the listing's; when the
// listing does not know it yet, no version is shown rather than an empty one.

import type { EnvInfo } from "./types";

type Named = Pick<EnvInfo, "env_id" | "kind" | "spec_root">;

const KIND_LABEL: Record<string, string> = {
  default: "Default",
  uv_project: "uv project",
  venv: "Virtual environment",
  script: "Script dependencies",
  requirements: "requirements.txt",
  conda: "Conda",
};

/** A path as it sits on a machine (`/opt/...`, `C:\...`, `~/...`), not one
 *  relative to the workspace. */
function isMachinePath(path: string): boolean {
  return path.startsWith("/") || path.startsWith("~") || /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("\\\\");
}

/** The kind as words: "uv project", "Virtual environment", or the kind itself. */
export function envKindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind;
}

/** Where the environment's spec is, relative to the workspace: `.` for the
 *  workspace itself, `analysis/` for a folder. Null when nothing relative is
 *  known. */
export function envSpecPath(env: Named): string | null {
  const colon = env.env_id.indexOf(":");
  const fromId = colon >= 0 ? env.env_id.slice(colon + 1) : "";
  if (fromId !== "" && !isMachinePath(fromId)) return fromId;
  if (env.spec_root !== "" && !isMachinePath(env.spec_root)) return env.spec_root;
  return null;
}

/** The environment's name: "Default", or the kind with the folder its spec is
 *  in when that is not the workspace itself ("uv project in analysis"). */
export function envName(env: Named): string {
  if (env.kind === "default") return KIND_LABEL.default!;
  const kind = envKindLabel(env.kind || "environment");
  const path = envSpecPath(env);
  if (path === null || path === "." || path === "./") return kind;
  return `${kind} in ${path.replace(/\/+$/, "")}`;
}

/** "Python 3.12.13", or null when the version is not known. */
export function envPython(env: Pick<EnvInfo, "python">): string | null {
  const version = env.python.trim();
  return version === "" ? null : `Python ${version}`;
}

/** The name with its Python version when known: "uv project · Python 3.12". */
export function envLabel(env: Named & Pick<EnvInfo, "python">): string {
  const python = envPython(env);
  return python === null ? envName(env) : `${envName(env)} · ${python}`;
}
