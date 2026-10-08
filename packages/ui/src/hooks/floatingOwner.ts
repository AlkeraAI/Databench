import { createContext, useCallback, useRef, type RefObject } from "react";

/**
 * Lets a float own the floats opened from inside it. A child float (a submenu flyout) is
 * portaled to `<body>`, so it is not inside its owner's panel in the DOM. Without this, a press
 * in the flyout reads as an outside press and closes the owner before the row is chosen, and a
 * close while focus is in the flyout drops focus on `<body>`.
 *
 * The owner provides `register`; a child float registers its panel element while mounted and the
 * owner treats a node inside any registered panel as inside itself.
 */
export type RegisterOwnedFloat = (el: HTMLElement) => () => void;

export const FloatingOwnerContext = createContext<RegisterOwnedFloat | null>(null);

/** The owner side: a stable `register` for the context and `owns(node)` for its own checks. */
export function useOwnedFloats(panelRef: RefObject<HTMLElement | null>): {
  register: RegisterOwnedFloat;
  owns: (node: Node | null | undefined) => boolean;
} {
  const owned = useRef(new Set<HTMLElement>());
  const register = useCallback<RegisterOwnedFloat>((el) => {
    owned.current.add(el);
    return () => {
      owned.current.delete(el);
    };
  }, []);
  const owns = useCallback(
    (node: Node | null | undefined) => {
      if (!node) return false;
      if (panelRef.current?.contains(node)) return true;
      for (const el of owned.current) if (el.contains(node)) return true;
      return false;
    },
    [panelRef],
  );
  return { register, owns };
}
