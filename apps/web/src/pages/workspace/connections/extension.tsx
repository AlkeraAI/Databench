// Connections in the portal: the page, its sidebar leaf, and the plate on a team's page.

import type { WebExtension } from "@alkera/ui/extensions";
import { PORTAL_NAV, PORTAL_ROUTES, TEAM_PLATES } from "../../../app/extensions/portal";
import { ConnectionsPage } from "./ConnectionsPage";
import { ConnectionsPlate } from "./ConnectionsPlate";

export const CONNECTIONS_PORTAL: WebExtension = {
  name: "connections.portal",
  install() {
    // Every data source this person's chats can reach, their own and their teams'. Any
    // signed-in member reaches it: adding one for yourself needs no admin, and the page
    // itself gates what each row offers on the server's `can_manage`.
    PORTAL_ROUTES.register({ key: "connections", mount: "member", path: "/connections", element: <ConnectionsPage /> });
    // Before Chat: what a chat can reach is the thing a person sets up first, and the
    // answer to "why can't the agent see my warehouse?" is one leaf up rather than buried
    // in an org settings page.
    PORTAL_NAV.register({
      key: "connections",
      kind: "leaf",
      group: "Personal",
      after: "/",
      leaf: { label: "Connections", to: "/connections", icon: "plug" },
    });
    TEAM_PLATES.register({ key: "connections", after: "provenance", Plate: ConnectionsPlate });
  },
};
