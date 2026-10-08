import { Outlet, useLocation, useNavigate } from "react-router-dom";

import { Tabs } from "@alkera/ui";

import { useIdentityDashboard } from "../../../api/dashboard";
import { useSpecificTitle } from "../../../app/documentTitle";
import { ORG_SETTINGS_TABS as EXTENSION_TABS, placeAfter } from "../../../app/extensions/portal";
import { Icon, type IconName } from "../../../app/icons";
import styles from "./OrgSettingsTabs.module.css";

/**
 * Organization settings — one tabbed area rather than scattered destinations. General is the
 * settings document at the bare path (so every `#section` deep link an admin has bookmarked still
 * resolves); Single sign-on and Audit log are its siblings, each still its own route so the address
 * bar names the tab and the browser's back button steps between them. An installed extension adds
 * its own tabs (billing's) through `ORG_SETTINGS_TABS`.
 *
 * The sibling tabs are org-admin surfaces: the route guard is the enforcement, and the strip
 * simply doesn't offer a doorway a plain member can't pass (the same rule the sidebar follows).
 */

export const ORG_SETTINGS_ROOT = "/settings/organization";

export interface OrgSettingsTab {
  key: string;
  label: string;
  to: string;
  icon: IconName;
  /** Offered only to an org admin — the route behind it is guarded the same way. */
  orgAdmin?: boolean;
}

/** The open portal's own tabs. */
export const OPEN_ORG_SETTINGS_TABS: readonly OrgSettingsTab[] = [
  { key: "general", label: "General", to: ORG_SETTINGS_ROOT, icon: "settings" },
  { key: "sso", label: "Single sign-on", to: `${ORG_SETTINGS_ROOT}/sso`, icon: "shield", orgAdmin: true },
  { key: "audit", label: "Audit log", to: `${ORG_SETTINGS_ROOT}/audit`, icon: "books", orgAdmin: true },
];

/** Every tab in strip order: the open ones with each installed extension's placed after the
 *  tab it names. Reads the extension point, so call it while rendering. */
export function orgSettingsTabs(): OrgSettingsTab[] {
  return placeAfter(
    OPEN_ORG_SETTINGS_TABS,
    EXTENSION_TABS.items().map((tab) => ({
      item: { key: tab.key, label: tab.label, to: `${ORG_SETTINGS_ROOT}/${tab.path}`, icon: tab.icon, orgAdmin: tab.orgAdmin },
      after: tab.after,
      label: tab.key,
    })),
    (tab) => tab.key,
  );
}

/** Which tab a path is on — the longest tab path the URL sits under, General otherwise (its path
 *  is a prefix of every sibling, so the sort matters). */
export function activeOrgSettingsTab(pathname: string, tabs: readonly OrgSettingsTab[] = orgSettingsTabs()): string {
  const match = tabs.filter((t) => pathname === t.to || pathname.startsWith(`${t.to}/`)).sort(
    (a, b) => b.to.length - a.to.length,
  )[0];
  return (match ?? tabs[0]).key;
}

export function OrgSettingsTabs() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const isOrgAdmin = useIdentityDashboard().data?.is_org_admin ?? false;

  const tabs = orgSettingsTabs();
  const offered = tabs.filter((tab) => !tab.orgAdmin || isOrgAdmin);

  // The masthead holds still across the strip, but the browser tab names the tab in
  // view — except on the bare path, which IS the settings page rather than one of its
  // parts.
  const active = tabs.find((t) => t.key === activeOrgSettingsTab(pathname, tabs));
  useSpecificTitle(pathname === ORG_SETTINGS_ROOT ? null : (active?.label ?? null));

  return (
    <>
      <div className={styles.strip}>
        <Tabs
          label="Organization settings"
          value={activeOrgSettingsTab(pathname, tabs)}
          items={offered.map((tab) => ({
            key: tab.key,
            label: tab.label,
            icon: <Icon name={tab.icon} size={15} />,
          }))}
          onChange={(key) => {
            const tab = tabs.find((t) => t.key === key);
            if (tab) navigate(tab.to);
          }}
        />
      </div>
      <Outlet />
    </>
  );
}
