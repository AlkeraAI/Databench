// Which palette the chat paints in: the one the document around it is already
// in. Any surface that renders a `.chat-root` needs it, so it lives apart from
// the surfaces.
//
// The rule is the one `theme/tokens.css` itself keys on: dark is the default, and light is
// declared — by the host's own class in the editor, or by the portal's
// `data-alkera-color-scheme` attribute.

import { useEffect, useState } from "react";

const LIGHT_HOST_CLASSES = ["vscode-light", "vscode-high-contrast-light"];
const SCHEME_ATTR = "data-alkera-color-scheme";

function documentIsLight(): boolean {
  const body = document.body;
  if (LIGHT_HOST_CLASSES.some((name) => body.classList.contains(name))) return true;
  return (
    document.documentElement.getAttribute(SCHEME_ATTR) === "light" ||
    body.getAttribute(SCHEME_ATTR) === "light"
  );
}

/** True while the surrounding document is in its dark palette. */
export function useHostDark(): boolean {
  const [dark, setDark] = useState(() => !documentIsLight());
  useEffect(() => {
    const observer = new MutationObserver(() => setDark(!documentIsLight()));
    const watch = { attributes: true, attributeFilter: ["class", SCHEME_ATTR] };
    observer.observe(document.body, watch);
    observer.observe(document.documentElement, watch);
    // The portal writes the attribute from an effect of its own, which may land
    // after this one; re-read once the observers are attached so a first paint
    // in the wrong palette cannot stick.
    setDark(!documentIsLight());
    return () => observer.disconnect();
  }, []);
  return dark;
}
