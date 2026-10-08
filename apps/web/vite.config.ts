import { fileURLToPath } from "node:url";

import { defineWebConfig } from "./webConfig";

// The open app: the portal entry with the open extensions (src/open/portal.ts)
// and no editor webview. A product package builds over it with its own config.
export default defineWebConfig({
  packageDir: fileURLToPath(new URL(".", import.meta.url)),
  portal: fileURLToPath(new URL("./src/open/portal.ts", import.meta.url)),
});
