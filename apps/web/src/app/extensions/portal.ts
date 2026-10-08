// The browser portal's extension points. Each one is read where the open app composes
// that part of itself (the route table, the sidebar, the org settings strip, the admin
// org page, the team page), so a private page arrives the same way an open one is
// written, and with nothing installed the portal is the open product alone.

import { useState, type ComponentType, type ReactNode } from "react";

import type { ChromeMenuAction, MachineCardData } from "@alkera/ui";

import type { Stat } from "../../pages/workspace/dashboard/model";
import type { IconName } from "../icons";
import type { MachineQuote, OrgMachineDetail, OrgMachineRead, OrgMachineUpdate } from "../../api/machines";
import type { MachineViewer } from "../../pages/organization/machines/model";
import type { PinnedCopy } from "../../pages/workspace/chat/MachineBanner";
import type { Notify } from "../notify";
import type { CurrentUser } from "../../api/auth";
import type { ChatFailureNote, FailureFacts } from "../../pages/workspace/chat/useChatFailureNote";
import type { NavGroup, NavLeaf } from "../nav";
import { ExtensionError, ExtensionPoint } from "@alkera/ui/extensions";

/** Where in the route table a page mounts. Each mount is a guard chain the open
 *  table already has; a page picks the one its readers pass. */
export type RouteMount =
  /** Signed in, in an org, inside the app shell. Any member. */
  | "member"
  /** As `member`, behind the org-admin guard. */
  | "orgAdmin"
  /** Outside every guard: legacy redirects and public landing pages. */
  | "public"
  /** Inside the app shell, behind the platform staff guard (the admin console). */
  | "platformStaff"
  /** As `platformStaff`, behind the higher platform grade. */
  | "platformAdmin";

export interface PortalRoute {
  /** Unique within the point. */
  key: string;
  mount: RouteMount;
  /** A react-router path. */
  path: string;
  element: ReactNode;
  /** What the page is a page of: the masthead title and the tab's middle part.
   *  A path with a `:param` names every page under its static prefix. */
  title?: string;
}

export const PORTAL_ROUTES = new ExtensionPoint<PortalRoute>("portal.routes");

/** A sidebar entry. A leaf joins an open group after the leaf whose path is `after`
 *  (`null` puts it first); a child joins the parent labelled `parent` (the admin console)
 *  the same way; a group joins after the group named `after`. */
export type PortalNav =
  | { key: string; kind: "leaf"; group: string; after: string | null; leaf: NavLeaf }
  | { key: string; kind: "child"; parent: string; after: string | null; leaf: NavLeaf }
  | { key: string; kind: "group"; after: string; group: NavGroup };

export const PORTAL_NAV = new ExtensionPoint<PortalNav>("portal.nav");

/** A tab of the org settings area, mounted under `/settings/organization/<path>`. */
export interface OrgSettingsTabEntry {
  key: string;
  label: string;
  /** The path under the settings root, without a leading slash. */
  path: string;
  icon: IconName;
  /** Offered only to an org admin; the route behind it is guarded the same way. */
  orgAdmin: boolean;
  /** The key of the tab this one follows. */
  after: string;
  element: ReactNode;
}

export const ORG_SETTINGS_TABS = new ExtensionPoint<OrgSettingsTabEntry>("portal.org_settings_tabs");

/** What a section of the org settings General page is handed. */
export interface OrgSettingsSectionProps {
  isAdmin: boolean;
  notify: Notify;
}

/** A section of the org settings General page, after the open ones, in registration order. */
export interface OrgSettingsSection {
  key: string;
  Section: ComponentType<OrgSettingsSectionProps>;
}

export const ORG_SETTINGS_SECTIONS = new ExtensionPoint<OrgSettingsSection>("portal.org_settings_sections");

/** What a section of the person's own account settings is handed. */
export interface AccountSettingsSectionProps {
  user: CurrentUser;
  notify: Notify;
}

/** A section of the person's own account settings, before Delete account, in registration order. */
export interface AccountSettingsSection {
  key: string;
  Section: ComponentType<AccountSettingsSectionProps>;
}

export const ACCOUNT_SETTINGS_SECTIONS = new ExtensionPoint<AccountSettingsSection>("portal.account_settings_sections");

/** What a section of the platform console's org page is handed. */
export interface AdminOrgSectionProps {
  orgId: string;
  orgName: string;
  /** Whether the viewer holds the higher platform grade (writes are theirs). */
  isAdmin: boolean;
  onDone: (text: string, ok: boolean) => void;
}

/** A tab of the platform console's org page (`/admin/orgs/:orgId?tab=<key>`). */
export interface AdminOrgTab {
  key: string;
  label: string;
  /** The key of the tab this one follows. */
  after: string;
  Section: ComponentType<AdminOrgSectionProps>;
}

export const ADMIN_ORG_TABS = new ExtensionPoint<AdminOrgTab>("portal.admin_org_tabs");

/** A card on the overview tab of the platform console's org page, placed after the open card
 *  whose key is `after` (`null` puts it first). Open cards, in order: sandbox, compute,
 *  storage, live-editing, settings. A card that is for the higher grade only checks
 *  `isAdmin` itself. */
export interface AdminOrgCard {
  key: string;
  after: string | null;
  Card: ComponentType<AdminOrgSectionProps>;
}

export const ADMIN_ORG_CARDS = new ExtensionPoint<AdminOrgCard>("portal.admin_org_cards");

/** What a card on the platform console's user page is handed. */
export interface AdminUserSectionProps {
  userId: string;
  /** The account's email, or null while the register is still loading. */
  userEmail: string | null;
  isAdmin: boolean;
  onDone: (text: string, ok: boolean) => void;
}

/** A card on the platform console's user page, after the open ones, in registration order. */
export interface AdminUserCard {
  key: string;
  Card: ComponentType<AdminUserSectionProps>;
}

export const ADMIN_USER_CARDS = new ExtensionPoint<AdminUserCard>("portal.admin_user_cards");

/** A section of a team page beside Members, switched to by its tab. `?tab=<key>` opens on it. */
export interface TeamSection {
  key: string;
  label: string;
  Section: ComponentType<{ teamId: string }>;
}

export const TEAM_SECTIONS = new ExtensionPoint<TeamSection>("portal.team_sections");

/** A plate in a team page's context column, after the open plates named by `after`. */
export interface TeamPlate {
  key: string;
  /** The key of the open plate this one follows. */
  after: string;
  Plate: ComponentType<{ teamId: string; teamName: string }>;
}

export const TEAM_PLATES = new ExtensionPoint<TeamPlate>("portal.team_plates");

/** Insert each contribution after its anchor, in registration order. An anchor that
 *  names nothing is a composition bug, so it throws rather than guessing a place. */
export function placeAfter<T>(
  base: readonly T[],
  contributions: readonly { item: T; after: string | null; label: string }[],
  keyOf: (item: T) => string,
): T[] {
  const placed = [...base];
  for (const { item, after, label } of contributions) {
    if (after === null) {
      placed.unshift(item);
      continue;
    }
    const at = placed.findIndex((existing) => keyOf(existing) === after);
    if (at < 0) throw new ExtensionError(`${label} names ${after} to follow, and nothing has that name`);
    // After the anchor and after anything already placed behind it from the same anchor,
    // so two contributions to one anchor keep their registration order.
    let end = at + 1;
    while (end < placed.length && contributions.some((c) => c.item === placed[end] && c.after === after)) end += 1;
    placed.splice(end, 0, item);
  }
  return placed;
}

/** What an extension's chat menu row reads for the chat that is open. */
export interface ChatMenuRow {
  /** The row, or `null` when it has nothing to offer for this chat. */
  action: ChromeMenuAction | null;
  /** Anything the row opens over the chat (a dialog, a toast viewport). */
  dialog: ReactNode;
}

/** A row in the chat header's overflow menu, ahead of the open rows. `useRow` is a hook the
 *  chat calls on every render; `chatId` is `null` in a shell that offers no extension rows. */
export interface ChatMenuEntry {
  key: string;
  useRow: (chatId: string | null) => ChatMenuRow;
}

export const CHAT_MENU_ACTIONS = new ExtensionPoint<ChatMenuEntry>("portal.chat_menu_actions");

/** Every installed extension's row for the open chat, in registration order. */
export function useChatMenuRows(chatId: string | null): (ChatMenuRow & { key: string })[] {
  const [entries] = useState(() => CHAT_MENU_ACTIONS.items());
  const rows: (ChatMenuRow & { key: string })[] = [];
  // The list is frozen before the first render and held in state, so every render calls the
  // same hooks in the same order.
  for (const entry of entries) {
    rows.push({ ...entry.useRow(chatId), key: entry.key });
  }
  return rows;
}

/** A read an overview source waits on: the fields of a React Query result the overview folds
 *  into its own loading and error states. */
export interface OverviewQuery {
  isError: boolean;
  error: unknown;
  refetch: () => unknown;
}

/** What a source adds to the overview once its reads answered. A stat card is placed after
 *  the card whose `key` is `after` (`null` puts it first); panels follow the chats panel. */
export interface OverviewSlice {
  stats: readonly { stat: Stat; after: string | null }[];
  panels: readonly OverviewPanel[];
}

/** A panel on the overview grid. `node` carries its own grid placement class. */
export interface OverviewPanel {
  key: string;
  node: ReactNode;
}

/** A source of overview cards. `useSlice` is a hook the overview calls on every render;
 *  `slice` is `null` until every read in `queries` has answered. `placeholders` sizes the
 *  loading skeleton so the page does not jump when the slice lands. */
export interface OverviewSource {
  key: string;
  placeholders: { stats: number; panels: number };
  useSlice: () => { queries: readonly OverviewQuery[]; slice: OverviewSlice | null };
}

export const OVERVIEW_SOURCES = new ExtensionPoint<OverviewSource>("portal.overview_sources");

/** A control in the overview's topbar (a search box, a shortcut). */
export interface OverviewAction {
  key: string;
  Action: ComponentType;
}

export const OVERVIEW_ACTIONS = new ExtensionPoint<OverviewAction>("portal.overview_actions");

/** A one-line notice above the app shell on every signed-in page. It decides for itself
 *  whether it shows, and renders nothing otherwise. */
export interface ShellBanner {
  key: string;
  Banner: ComponentType;
}

export const SHELL_BANNERS = new ExtensionPoint<ShellBanner>("portal.shell_banners");

/** A row in the account menu, after Settings. `close` closes the menu. */
export interface AccountMenuRow {
  key: string;
  Row: ComponentType<{ close: () => void }>;
}

export const ACCOUNT_MENU_ROWS = new ExtensionPoint<AccountMenuRow>("portal.account_menu_rows");

/** The admin console's landing page at `/admin`. The first registered one is shown; with none,
 *  `/admin` opens the organizations register. */
export interface AdminHomeEntry {
  key: string;
  element: ReactNode;
}

export const ADMIN_HOME = new ExtensionPoint<AdminHomeEntry>("portal.admin_home");

/** Registered by an extension that charges for machines. With one installed the admin
 *  console shows and edits each machine offering's price; with none, offerings carry no
 *  price and the console shows none. */
export interface MachinePricing {
  key: string;
}

export const MACHINE_PRICING = new ExtensionPoint<MachinePricing>("portal.machine_pricing");

/** Whether an installed extension charges for machines. Read at render, after composition. */
export function machinesArePriced(): boolean {
  return MACHINE_PRICING.items().length > 0;
}

/** A section of the team roster's member drawer, under the member's name, in registration
 *  order. It reads its own data while `open` and renders nothing it has no answer for. */
export interface MemberDrawerSection {
  key: string;
  Section: ComponentType<{ teamId: string; userId: string; open: boolean }>;
}

export const MEMBER_DRAWER_SECTIONS = new ExtensionPoint<MemberDrawerSection>("portal.member_drawer_sections");

/** A card on an org machine's page, after the workspaces on it, in registration order. It
 *  decides for itself whether the viewer sees it, and renders nothing otherwise. */
export interface MachineDetailCard {
  key: string;
  Card: ComponentType<{ machine: OrgMachineDetail; now: number }>;
}

export const MACHINE_DETAIL_CARDS = new ExtensionPoint<MachineDetailCard>("portal.machine_detail_cards");

/** A telemetry sink the product ships (error tracking, product analytics). The open build
 *  registers none, so it loads no third-party script and sends nothing.
 *
 *  `start` runs once, on the first credential-free URL (see `startPortal`), and fails closed
 *  on its own: it confirms against the backend's public config that telemetry is enabled.
 *  `captureException` hands the sink a client error the reporter caught; it does nothing
 *  until `start` has armed the sink. */
export interface PortalTelemetry {
  key: string;
  start: () => Promise<void>;
  captureException?: (error: unknown) => void;
}

export const PORTAL_TELEMETRY = new ExtensionPoint<PortalTelemetry>("portal.telemetry");

/** What a gated org page is handed to say a feature is unavailable. */
export interface UnavailableFeatureProps {
  /** The surface name, e.g. "Single sign-on". */
  feature: string;
  /** What the feature does, e.g. "SSO/SAML & SCIM". */
  blurb: string;
  /** An optional mark for the plate (a page-specific Icon). */
  icon?: ReactNode;
}

/** The plate an org page shows in place of a feature the server says the org may not use
 *  (`enterprise_features_enabled` false on the dashboard). The first registered plate is
 *  drawn; with none, the page says the feature is not turned on. */
export interface UnavailableFeaturePlate {
  key: string;
  Plate: ComponentType<UnavailableFeatureProps>;
}

export const UNAVAILABLE_FEATURE_PLATES = new ExtensionPoint<UnavailableFeaturePlate>("portal.unavailable_feature_plates");

/** What a chat-failure arm is handed to decide the reader's next step. */
export interface ChatFailureNoteContext {
  facts: FailureFacts | undefined;
  /** The reader may open the page that fixes it (the wire's verdict, or an org admin
   *  in the portal). */
  canManage: boolean;
  /** The portal shell, where the reader's role is known. */
  portal: boolean;
}

/** A resolver for failed-turn causes the open chat does not know (money refusals, for
 *  one). The first arm naming a cause answers it. */
export interface ChatFailureNoteArm {
  key: string;
  causes: readonly string[];
  resolve: (cause: string, ctx: ChatFailureNoteContext) => ChatFailureNote | null;
}

export const CHAT_FAILURE_NOTES = new ExtensionPoint<ChatFailureNoteArm>("portal.chat_failure_notes");

/** What the machine pages hand the billing extension's buy entry. */
export interface MachineBuyContext {
  viewer: MachineViewer;
  /** How many machines the reader can see the org holding. */
  held: number | null;
  /** Called with the machine just bought. */
  onBought: (machine: OrgMachineRead) => void;
}

/** A money column of the machine list, before the menu column. */
export interface MachineListColumn {
  key: string;
  header: string;
  width: number;
  /** Whether the column is drawn for these rows. */
  shown: (rows: readonly OrgMachineRead[]) => boolean;
  Cell: ComponentType<{ machine: OrgMachineRead }>;
}

/** The pinned machine's banner facts a money arm reads. */
export interface PinnedStopContext {
  /** The reader may start the machine. */
  manager: boolean;
  /** The reader may add credits (an org admin). */
  mayAddCredits: boolean;
}

/** How org machines are bought and paid for. The open build registers none: machines are
 *  added by their SSH details, nothing is priced, and no page names credits, caps or plans.
 *  The first registration is used. Every slot is drawn where the open page would have it. */
export interface MachineBilling {
  key: string;
  /** The buy button and its dialog, beside Add machine. The hook runs on every render of
   *  the machines page; `button` is drawn in the topbar and the empty state, `dialog` once. */
  useBuyEntry: (ctx: MachineBuyContext) => { button: ReactNode; dialog: ReactNode };
  /** The waiting-for-hardware machine's way to pick another size. */
  WaitingAction: ComponentType<{ machine: OrgMachineDetail }>;
  listColumns: readonly MachineListColumn[];
  /** Under the machine page's header: the funding state behind it. */
  FundingNotice: ComponentType<{ machine: OrgMachineDetail; viewer: MachineViewer }>;
  /** Above the machine page's About card: what its provider means for its manager. */
  ProviderNotice: ComponentType<{ machine: OrgMachineDetail }>;
  /** The About rows after "Owned by" (how it is paid for). */
  PaymentFacts: ComponentType<{ machine: OrgMachineDetail }>;
  /** The About rows at the end (what it cost this cycle). */
  SpendFacts: ComponentType<{ machine: OrgMachineDetail }>;
  /** The use label for an org pool machine the org may not run as a pool, and the note
   *  under the list when one is listed. */
  poolUnavailableLabel: string;
  poolUnavailableNote: string;
  /** The pinned banner for a machine stopped or stopping for money (`stop_reason` credits
   *  or cap), or undefined when the stop is not one of its own. */
  pinnedStopCopy: (card: Pick<MachineCardData, "name" | "state" | "stop_reason" | "drain_stops_at">, ctx: PinnedStopContext) => PinnedCopy | undefined;
  /** What a pinned-banner action this extension offers is called, and what it does. */
  pinnedActions: Readonly<Record<string, { label: string; run: (navigate: (to: string) => void) => void }>>;
  /** What a grow-disk quote costs, in words, or null when the quote names no price. */
  diskPriceLine: (quote: MachineQuote) => string | null;
  /** A machine's monthly spend cap: its draft as typed, the patch a save sends, the
   *  settings field, the settings reading, and the line under the settings dialog. */
  cap: {
    draft: (machine: OrgMachineRead) => string;
    patch: (machine: OrgMachineRead, text: string) => Partial<OrgMachineUpdate>;
    Field: ComponentType<{ value: string; disabled?: boolean; onChange: (text: string) => void }>;
    Reading: ComponentType<{ machine: OrgMachineRead }>;
    Current: ComponentType<{ machine: OrgMachineRead }>;
  };
  /** A refused machine move or start the extension explains (out of credit), or null. */
  refusal: (status: number) => string | null;
}

export const MACHINE_BILLING = new ExtensionPoint<MachineBilling>("portal.machine_billing");

/** The registered machine billing, or null in the open build. */
export function machineBilling(): MachineBilling | null {
  return MACHINE_BILLING.items()[0] ?? null;
}

/** A query parameter the sign-in and sign-up pages carry to each other, and turn into the
 *  landing after sign-in when no deep link or invitation names one. The open pages carry
 *  none; an extension registers the entries it owns (a site's sign-up entry, for one). */
export interface CarriedAuthParam {
  /** The query parameter's name. */
  key: string;
  /** Where a signed-in reader lands for the parameter's value. */
  landing: (value: string) => string;
}

export const CARRIED_AUTH_PARAMS = new ExtensionPoint<CarriedAuthParam>("portal.carried_auth_params");
