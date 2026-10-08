// Served by the productExtensions Vite plugin (sourceOverlay.ts): the
// extensions the portal installs, empty for the open app.
declare module "virtual:alkera-web-product" {
  import type { WebExtension } from "@alkera/ui/extensions";

  export const PORTAL_EXTENSIONS: readonly WebExtension[];
}
