// The chat chrome with its host wiring attached: the signed-in account, the
// settings menu's actions, and the door to the web app. One module, because the
// chat surface and the home surface carry the same header, and a settings
// action that means one thing in one of them and something else in the other is
// a bug waiting.
//
// What the menu offers comes from the shell's own table (`chatRoutes()`), not
// from a constant: the editor's rows are commands dispatched through its host,
// which a browser tab cannot run. A page lists the one action it can honour,
// and signs out through the portal's own logout.
//
// The caller owns the `.chat-root` scope this renders into.

import type { ReactElement } from "react";
import { useNavigate } from "react-router-dom";

import { Chrome, type ChromeProps } from "@alkera/ui";

import { useLogout } from "../../../api/auth";

import { chatRoutes, performChromeAction } from "./chatRoutes";
import { chatHost } from "./data";
import { useChatAccount } from "./useChatAccount";

/** Where a signed-out reader lands. The portal's guards send an unauthenticated
 *  visitor here anyway; naming it keeps the redirect from racing them. */
const SIGNED_OUT = "/login";

export type ChatChromeProps = Omit<
  ChromeProps,
  "account" | "settings" | "onSettingsAction" | "onOpenWebApp"
>;

export function ChatChrome(props: ChatChromeProps): ReactElement {
  const account = useChatAccount();
  const frontendUrl = account.webAppUrl;
  const navigate = useNavigate();
  const logout = useLogout();
  const settings = chatRoutes().settings;

  const signOut = (): void => {
    // The portal's own logout: the cookie dies server-side and the cached
    // identity with it, so the guards take the reader to the door. `onSettled`
    // rather than `onSuccess` — a session that would not clean up server-side
    // must still not leave the reader looking at a signed-in screen.
    logout.mutate(undefined, {
      onSettled: () => navigate(SIGNED_OUT, { replace: true }),
    });
  };

  return (
    <Chrome
      {...props}
      account={account.email ? { email: account.email } : undefined}
      settings={settings.map((entry) => entry.id)}
      // The web app is a door only once the host knows where it is; until then
      // the key simply doesn't render.
      onOpenWebApp={frontendUrl ? () => void chatHost().auth.openBrowser(frontendUrl) : undefined}
      onSettingsAction={(id) => {
        const action = settings.find((entry) => entry.id === id)?.action;
        // Only a row this shell listed can arrive here, and every listed row
        // has an action; `performChromeAction` answers false for the one it
        // cannot do on its own, which is ending the session.
        if (action && !performChromeAction(action, navigate)) signOut();
      }}
    />
  );
}
