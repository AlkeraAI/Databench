// The portal entry. The boot module is imported first, so its GitHub callback strip runs
// before any extension module loads. A product build supplies its extensions through
// virtual:alkera-web-product (src/vite/sourceOverlay.ts); the open app installs none.
import { startPortal } from "./app/boot/startPortal";
import { PORTAL_EXTENSIONS } from "virtual:alkera-web-product";

startPortal(PORTAL_EXTENSIONS);
