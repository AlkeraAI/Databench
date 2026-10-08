// The open portal's extensions: every extension the open app's own pages
// define, installed by the open build. A product build installs these first
// and then its own (src/product/portal.ts), so an open extension is never
// installed only by the product. Within a point, entries keep registration
// order, so the order here is part of the sidebar and the tab strips.

import type { WebExtension } from "@alkera/ui/extensions";

import { CONNECTIONS_PORTAL } from "../pages/workspace/connections/extension";

export const PORTAL_EXTENSIONS: readonly WebExtension[] = [CONNECTIONS_PORTAL];
