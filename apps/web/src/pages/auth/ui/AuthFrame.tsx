import type { ReactNode } from "react";

import { BrandLogo } from "@alkera/ui";

import { useColorScheme } from "../../../app/useColorScheme";
import { MoonIcon, SunIcon } from "./icons";
import { VineBackground } from "./VineBackground";
import "./auth.css";

/** The shared auth frame: a top bar with the brand lockup left and a light/dark
 *  toggle right, over an optional self-drafting vine background. The frame centers its single
 *  child, so a page drops in either the auth card ({@link AuthLayout}) or a cardless body and
 *  gets the same chrome and theming. The vine is the expressive backdrop for the sign-in
 *  cards; the device-grant confirmation drops it (`vine={false}`) so the code stands alone. */
export function AuthFrame({ children, vine = true }: { children: ReactNode; vine?: boolean }) {
  return (
    <div className="pa-auth">
      {vine ? <VineBackground /> : null}

      <div className="pa-auth__topbar">
        <span className="pa-auth__logo">
          <BrandLogo size={28} wordmark />
        </span>
        <ThemeToggle />
      </div>

      {children}
    </div>
  );
}

/** Top-right light/dark control. Drives the portal's color scheme through useColorScheme,
 *  which writes `data-alkera-color-scheme` on the document root — the same control the rest
 *  of the app uses, so the choice persists and applies everywhere once the user signs in.
 *  The icon shows the scheme it switches TO (moon by day, sun at night). */
function ThemeToggle() {
  // A signed-out reader gets the same theme a signed-in one does — the machine's, until they say
  // otherwise. `resolved` is the scheme actually on screen, tracked live, so flipping the OS while
  // the sign-in page is open moves the icon with the page.
  const { resolved, setScheme } = useColorScheme();
  const dark = resolved === "dark";

  return (
    <button
      type="button"
      className="pa-auth__theme-toggle"
      onClick={() => setScheme(dark ? "light" : "dark")}
      aria-label={dark ? "Switch to light theme" : "Switch to dark theme"}
      aria-pressed={dark}
    >
      {dark ? <SunIcon size={17} /> : <MoonIcon size={17} />}
    </button>
  );
}
