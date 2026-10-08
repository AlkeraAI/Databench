// The entry of the built bundle (dist/alkera-widgets.js): a classic script the
// output frame injects, which registers its exports as `alkera-widgets`
// through `window.__alkRegister` and places nothing else on `window`.
import * as widgets from "./index";
import { register } from "./registry";

declare const __ALK_WIDGETS_VERSION__: string;

/** The name the frame host reads this bundle's exports under. */
export const BUNDLE_REGISTRATION = "alkera-widgets";

register(BUNDLE_REGISTRATION, __ALK_WIDGETS_VERSION__, { ...widgets });
