/* A process-wide stack of dismissable overlays, so Escape closes exactly ONE surface — the
 * topmost — even when several are open at once (a Modal over a modal SidePanel, a Modal over an
 * inline SidePanel). A single shared `document` keydown listener owns Escape and routes it to the
 * top of the stack; this is why each overlay must NOT bind its own Escape listener (two listeners
 * on `document` both fire on one press — `stopPropagation` doesn't stop a sibling listener on the
 * same node, so both would close).
 */

type EscHandler = () => void;

const stack: EscHandler[] = [];
let bound = false;

function onKey(e: KeyboardEvent): void {
  if (e.key !== "Escape") return;
  const top = stack[stack.length - 1];
  if (!top) return;
  e.stopPropagation();
  top();
}

/** Register `onEscape` as the current top overlay; returns an unregister to call on close. */
export function pushEsc(onEscape: EscHandler): () => void {
  stack.push(onEscape);
  if (!bound) {
    document.addEventListener("keydown", onKey);
    bound = true;
  }
  return () => {
    const i = stack.lastIndexOf(onEscape);
    if (i >= 0) stack.splice(i, 1);
  };
}
