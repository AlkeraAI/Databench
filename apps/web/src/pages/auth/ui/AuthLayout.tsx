import type { ReactNode } from "react";

import { useSpecificTitle } from "../../../app/documentTitle";
import { AuthFrame } from "./AuthFrame";

export interface AuthLayoutProps {
  title: ReactNode;
  /** One line under the title naming the value — not a generic greeting. */
  lede?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}

/** The carded auth page: the shared {@link AuthFrame} wrapping a single centered
 *  card that holds the page's title, lede, form, and footer. The cardless device-grant page
 *  drops its own body into AuthFrame instead. */
export function AuthLayout({ title, lede, children, footer }: AuthLayoutProps) {
  // An auth page sits outside the nav, so its own heading is what names the tab —
  // and it follows the page through its states ("Sign in", then "You're signed in").
  useSpecificTitle(typeof title === "string" ? title : null);
  return (
    <AuthFrame>
      <main className="pa-auth__card">
        <h1 className="pa-auth__title">{title}</h1>
        {lede ? <p className="pa-auth__sub">{lede}</p> : null}
        {children}
        {footer ? <p className="pa-auth__foot">{footer}</p> : null}
      </main>
    </AuthFrame>
  );
}
