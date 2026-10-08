import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ToastViewport, useToasts } from "@alkera/ui";

import { activeOrgId, setOrgChangedHandler, takeSessionNotice } from "../../api/activeOrg";
import { endTabSession } from "../../api/auth";
import { NO_ORGANIZATION_PATH } from "../../api/orgs";
import { onSessionMessage } from "../../api/sessionChannel";

/**
 * Keeps every tab of this browser in the org the session is in. Mounted once at the app
 * root.
 *
 * - When the client learns the session moved under this tab (a 409 `org_changed`, a
 *   refresh answering for another org), the cached data is dropped before the reload, so
 *   nothing rendered for the old org is painted again.
 * - When another tab announces a switch or a sign-out, this tab reloads at the overview.
 *   It shows nothing first: the reload is the whole answer. A switch into the org this tab
 *   already shows changes nothing here, so it is ignored.
 * - When another tab left the org and is in none now, this tab stops renewing the session
 *   and goes to the no-organization landing with it.
 */
export function SessionBridge() {
  const queryClient = useQueryClient();
  useEffect(() => {
    // The tab leaves on its own once the handler returns.
    setOrgChangedHandler(() => endTabSession({ qc: queryClient, org: "moved" }));
    const unsubscribe = onSessionMessage((message) => {
      if (message.type === "org_switched" && message.org_team_id === activeOrgId()) return;
      if (message.type === "org_left") {
        endTabSession({ qc: queryClient, org: "none", to: NO_ORGANIZATION_PATH });
        return;
      }
      endTabSession({ qc: queryClient, org: "moved", to: "/" });
    });
    return () => {
      setOrgChangedHandler(null);
      unsubscribe();
    };
  }, [queryClient]);
  return null;
}

/**
 * The one line a tab left itself before a reload ("Switched to Acme in another window."),
 * shown once after it. Mounted in the signed-in shell.
 */
export function SessionNotice() {
  const { toasts, push, dismiss } = useToasts();
  useEffect(() => {
    const notice = takeSessionNotice();
    if (notice) push({ message: notice, tone: "info" });
  }, [push]);
  return <ToastViewport toasts={toasts} onDismiss={dismiss} position="bottom-center" />;
}
