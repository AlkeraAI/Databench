// How a frame module announces itself. The bootstrap page defines
// `window.__alkRegister` before any module arrives; a module's code calls it
// once, with its name, its version and its exports (the MIME types it draws
// and how it draws them).

import type { FrameModule } from "../protocol";

/** The version every platform frame module registers under. */
export const FRAME_MODULE_VERSION = "1";

export function registerFrameModule(name: string, module: FrameModule, version = FRAME_MODULE_VERSION): void {
  if (typeof window !== "undefined") window.__alkRegister?.(name, version, module);
}

/** Notebook outputs store text as a string or as a list of lines. */
export function outputText(data: unknown): string {
  if (typeof data === "string") return data;
  if (Array.isArray(data)) return data.map((line) => String(line)).join("");
  return "";
}

/** A JSON output: an object, or (from some writers) its text. */
export function outputJson(data: unknown): Record<string, unknown> {
  const value = typeof data === "string" || Array.isArray(data) ? JSON.parse(outputText(data)) : data;
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("This output is not a chart specification.");
  }
  return value as Record<string, unknown>;
}
