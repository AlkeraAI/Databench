// The virtual modules are declared in an ambient .d.ts, which has nothing to
// import; the reference carries it into any program that compiles this file
// (the host app's included), not only this package's.
// eslint-disable-next-line @typescript-eslint/triple-slash-reference -- an ambient .d.ts cannot be imported
/// <reference path="./virtual.d.ts" />
// The platform's own frame modules, loaded only when an output needs one: each
// is its own chunk (Plotly alone is several megabytes).

import type { FrameServices } from "../outputs/types";
import { PLATFORM_FRAME_MODULES, type PlatformFrameModule } from "./protocol";

const IMPORTS: Record<PlatformFrameModule, () => Promise<{ default: string }>> = {
  "nb-html": () => import("virtual:nb-frame-module/nb-html"),
  "nb-svg": () => import("virtual:nb-frame-module/nb-svg"),
  "nb-vega": () => import("virtual:nb-frame-module/nb-vega"),
  "nb-plotly": () => import("virtual:nb-frame-module/nb-plotly"),
};

export function isPlatformFrameModule(name: string): name is PlatformFrameModule {
  return (PLATFORM_FRAME_MODULES as readonly string[]).includes(name);
}

/** The code of one of the platform's frame modules. */
export async function loadPlatformFrameModule(name: PlatformFrameModule): Promise<string> {
  return (await IMPORTS[name]()).default;
}

/** A `FrameServices.loadModule` that answers the platform's modules (and the
 *  widget manager, which is a platform bundle too) itself and
 *  hands every other name (the widget manager, a notebook's widget assets) to
 *  `others`. A platform name is never handed on: no other source may supply
 *  code under it. */
export function platformModuleLoader(others?: FrameServices["loadModule"]): FrameServices["loadModule"] {
  return (name, version) => {
    if (isPlatformFrameModule(name)) return loadPlatformFrameModule(name);
    if (name === "@alkera/widgets") return import("virtual:nb-frame-module/alkera-widgets").then((m) => m.default);
    if (others) return others(name, version);
    return Promise.reject(new Error(`No source for the module ${name}.`));
  };
}
