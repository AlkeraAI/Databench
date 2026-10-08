// The widget manager (`@alkera/widgets`) is a bundle that places nothing on
// `window`: it hands its exports to `window.__alkRegister` under the name
// `alkera-widgets`. The frame's bootstrap knows only render modules, so the
// host puts this prelude before the bundle's text: it wraps the bootstrap's
// `__alkRegister`, takes the `alkera-widgets` registration (the bundle's own
// registry chains to whatever function it finds), and registers a render
// module for widget views that starts the manager once and hands it every
// later message, `module` messages included, since the manager requests,
// verifies and loads its own dependencies (scripts and anywidget assets alike).

import { WIDGET_VIEW_MIME } from "./protocol";

export const WIDGETS_MODULE = "@alkera/widgets";

/** The name the manager bundle registers its exports under. */
export const WIDGETS_BUNDLE_REGISTRATION = "alkera-widgets";

export const WIDGETS_PRELUDE = `
;(function () {
  var bootstrap = window.__alkRegister;
  if (typeof bootstrap !== "function") return;
  window.__alkRegister = function (name, version, exports) {
    if (name !== ${JSON.stringify(WIDGETS_BUNDLE_REGISTRATION)}) return bootstrap(name, version, exports);
    if (!exports || typeof exports.start !== "function") return;
    bootstrap(${JSON.stringify(WIDGETS_MODULE)}, String(version || "1"), {
      mimes: [${JSON.stringify(WIDGET_VIEW_MIME)}],
      handlesModules: true,
      render: function (init, api) {
        var manager = exports.start({
          root: api.root,
          post: function (message, transfer) { api.post(message.type, message, transfer); }
        });
        api.onMessage(function (message) { manager.handle(message); });
        manager.handle(init);
      }
    });
  };
})();
`;

/** The manager bundle's text with the prelude before it. */
export function withWidgetsAdapter(bundle: string): string {
  return `${WIDGETS_PRELUDE}\n${bundle}`;
}
