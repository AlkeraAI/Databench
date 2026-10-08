import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { BrandLogo, Avatar, Identity, Popover, cx, useFocusTrap } from "@alkera/ui";

import { useCurrentUser, useLogout } from "../api/auth";
import { CreateOrgDialog, OrgMenuSection } from "./OrgMenu";
import { RealtimeStatusIndicator } from "../api/events/RealtimeStatusIndicator";
import { useIdentityDashboard } from "../api/dashboard";
import { sectionTitleFor } from "./documentTitle";
import { SessionNotice } from "./boot/SessionBridge";
import { EmailVerificationBanner } from "./EmailVerificationBanner";
import { ACCOUNT_MENU_ROWS, SHELL_BANNERS } from "./extensions/portal";
import { Icon } from "./icons";
import { filterNav, hasChildren, portalNav, shadowedLeaf, type NavLeaf, type NavParent } from "./nav";
import { useNavGates } from "./useNavGates";
import { Masthead, TopbarSlotsContext } from "./Topbar";
import { useColorScheme, type ColorScheme } from "./useColorScheme";
import { usePersistedSize, type SizeBounds } from "./usePersistedSize";

/** The rail is a CSS grid track, not a draggable column: the width is the
 *  stylesheet's and only the collapsed state is the reader's. The bounds are
 *  here so the one remembered width in the store is the one the sheet draws,
 *  and a drag handle — if the rail ever grows one — has a range to clamp to. */
const SIDEBAR_BOUNDS: SizeBounds = { min: 72, max: 360, size: 256 };

/** Two initials from a display name (or the email's local part as a fallback). */
function initialsOf(name: string, email: string): string {
  const source = name.trim() || email.split("@")[0] || "";
  const parts = source.split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  return source.slice(0, 2).toUpperCase();
}

function NavLeafItem({
  leaf,
  sub,
  shadowed = false,
  onNavigate,
}: {
  leaf: NavLeaf;
  sub?: boolean;
  /** A more exact leaf claims the current path, so this one stays unlit. */
  shadowed?: boolean;
  onNavigate: () => void;
}) {
  return (
    <NavLink
      to={leaf.to}
      end={leaf.end ?? leaf.to === "/"}
      onClick={onNavigate}
      className={({ isActive }) =>
        cx("alk-navitem", sub && "alk-navitem--sub", isActive && !shadowed && "alk-navitem--active")
      }
      aria-current={shadowed ? false : undefined}
    >
      <span className="alk-navitem__ic">
        <Icon name={leaf.icon} size={sub ? 16 : 18} />
      </span>
      <span className="alk-navitem__label">{leaf.label}</span>
    </NavLink>
  );
}

function NavParentItem({ parent, onNavigate }: { parent: NavParent; onNavigate: () => void }) {
  const loc = useLocation();
  const childActive = parent.children.some((c) => loc.pathname.startsWith(c.to));
  const [open, setOpen] = useState(childActive);
  useEffect(() => {
    if (childActive) setOpen(true);
  }, [childActive]);

  return (
    <div className="alk-nav__disc" data-open={open || undefined}>
      <button type="button" className="alk-navitem alk-navitem--parent" data-trail={childActive || undefined} aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <span className="alk-navitem__ic">
          <Icon name={parent.icon} size={18} />
        </span>
        <span className="alk-navitem__label">{parent.label}</span>
        <span className="alk-navitem__caret" data-open={open || undefined}>
          <Icon name="chevron" size={14} />
        </span>
      </button>
      <div className="alk-nav__children">
        <div className="alk-nav__children-inner" role="group" aria-label={parent.label} inert={!open || undefined}>
          {parent.children.map((c) => (
            <NavLeafItem key={c.to} leaf={c} sub onNavigate={onNavigate} />
          ))}
        </div>
      </div>
    </div>
  );
}

/** Whether the signed-in person belongs to more than one org (the switcher's only audience). */
function isMultiOrg(user: { membership_count?: number } | null | undefined): boolean {
  return (user?.membership_count ?? 1) > 1;
}

/** The org this browser is in, as the person reads it. */
function activeOrgName(user: { org_name?: string } | null | undefined): string {
  return user?.org_name?.trim() || "Unnamed organization";
}

const THEMES: { key: ColorScheme; label: string }[] = [
  { key: "light", label: "Light" },
  { key: "dark", label: "Dark" },
  { key: "system", label: "Auto" },
];

function AccountMenu({ collapsed }: { collapsed: boolean }) {
  const { scheme, setScheme } = useColorScheme();
  const navigate = useNavigate();
  // Rows an installed extension adds after Settings, frozen before the first render.
  const [extraRows] = useState(() => ACCOUNT_MENU_ROWS.items());
  const user = useCurrentUser().data;
  const identity = useIdentityDashboard().data;
  const logout = useLogout();
  // A person in several orgs sees which one this browser is in under their name, and only there;
  // the org rows themselves (switch, pending, create) are read only once the menu opens.
  const multiOrg = isMultiOrg(user);
  const orgName = multiOrg ? activeOrgName(user) : null;
  const [createOpen, setCreateOpen] = useState(false);

  const name = user?.display_name?.trim() || user?.email || "Your account";
  const email = user?.email ?? "";
  // No document, no claim. Reading an absent identity as a negative told an org admin they were a
  // member for as long as the dashboard endpoint was down. React Query holds the last successful
  // document through a failing refetch, so a role once established rides out an outage.
  const role = identity == null ? null : identity.is_org_admin ? "Org admin" : "Member";
  const secondary = [orgName, role].filter(Boolean).join(" · ") || undefined;
  const initials = initialsOf(name, email);

  const signOut = (close: () => void) => {
    close();
    logout.mutate(undefined, { onSettled: () => navigate("/login", { replace: true }) });
  };

  // Arrow-key navigation over the panel's rows, the same walk the shared Dropdown does
  // over `[role^="menuitem"]`. A container that claims `role="menu"` promises this — a
  // menu a screen-reader user can only Tab through is worse than one that never claimed
  // to be a menu at all. The theme swatches carry menuitemradio, so they join the walk.
  // The panel is portaled away from the trigger, so both ends of the control drive the
  // walk through this ref rather than through their own subtree.
  const panelRef = useRef<HTMLDivElement | null>(null);
  const onMenuKeys = (e: ReactKeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
    const panel = panelRef.current;
    if (!panel) return;
    const items = [...panel.querySelectorAll<HTMLElement>('[role^="menuitem"]:not(:disabled)')];
    if (items.length === 0) return;
    e.preventDefault();
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next =
      e.key === "Home" ? 0
      : e.key === "End" ? items.length - 1
      : e.key === "ArrowDown" ? (at < 0 ? 0 : (at + 1) % items.length)
      : at <= 0 ? items.length - 1
      : at - 1;
    items[next].focus();
  };

  return (
    <>
    <Popover
      label="Account"
      side="top"
      align="start"
      matchWidth={!collapsed}
      trigger={(p) => (
        <button
          {...p}
          type="button"
          className="alk-account"
          onKeyDown={onMenuKeys}
          title={collapsed ? (secondary == null ? name : `${name} · ${secondary}`) : undefined}
        >
          {collapsed ? (
            <Avatar initials={initials} />
          ) : (
            <Identity
              initials={initials}
              name={name}
              secondary={secondary}
              trailing={
                <span className="alk-account__chev" data-open={p["data-open"]}>
                  <Icon name="chevron" size={16} />
                </span>
              }
            />
          )}
        </button>
      )}
    >
      {({ close }) => (
        <div ref={panelRef} className="alk-accmenu" role="menu" aria-label="Account" onKeyDown={onMenuKeys}>
          <span className="alk-accmenu__email">{email}</span>
          <button
            type="button"
            role="menuitem"
            className="alk-accmenu__item"
            onClick={() => {
              navigate("/settings/profile");
              close();
            }}
          >
            Profile
          </button>
          {/* The reader's OWN settings, beside their profile — every member has them, and they
              are not the organization's ledger below. */}
          <button
            type="button"
            role="menuitem"
            className="alk-accmenu__item"
            onClick={() => {
              navigate("/preferences");
              close();
            }}
          >
            Preferences
          </button>
          <button
            type="button"
            role="menuitem"
            className="alk-accmenu__item"
            onClick={() => {
              navigate("/settings/organization");
              close();
            }}
          >
            Settings
          </button>
          {extraRows.map(({ key, Row }) => (
            <Row key={key} close={close} />
          ))}
          <div className="alk-accmenu__sep" role="separator" />
          <div className="alk-theme">
            <span className="alk-theme__label">Theme</span>
            {/* Inside a menu the choice is a group of menuitemradios, not a radiogroup —
                a radio is not a valid child of role="menu", and menuitemradio is what
                puts the swatches in the same arrow-key walk as the rows above. */}
            <div className="alk-theme__opts" role="group" aria-label="Theme">
              {THEMES.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  role="menuitemradio"
                  aria-checked={scheme === t.key}
                  data-on={scheme === t.key || undefined}
                  className="alk-swatch"
                  onClick={() => setScheme(t.key)}
                >
                  <span className={`alk-swatch__chip alk-swatch__chip--${t.key}`} aria-hidden="true" />
                  <span className="alk-swatch__name">{t.label}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="alk-accmenu__sep" role="separator" />
          <OrgMenuSection
            user={user}
            close={close}
            onCreate={() => {
              close();
              setCreateOpen(true);
            }}
          />
          <button
            type="button"
            role="menuitem"
            className="alk-accmenu__item alk-accmenu__item--danger"
            onClick={() => signOut(close)}
            disabled={logout.isPending}
          >
            Sign out
          </button>
        </div>
      )}
    </Popover>
    {/* Outside the popover: the menu closes as the dialog opens. */}
    {createOpen ? <CreateOrgDialog open onClose={() => setCreateOpen(false)} /> : null}
    </>
  );
}

/** The signed-in product shell: a collapsible sidebar nav + a sticky masthead with a workspace
 *  search, hosting the routed pages. Auth pages render outside it on their own layout route. */
export function AppLayout() {
  const loc = useLocation();
  // Notices an installed extension shows above the shell, frozen before the first render.
  const [banners] = useState(() => SHELL_BANNERS.items());
  // The rail's state is the reader's arrangement of their own screen, so it
  // survives a reload: collapsing it on every navigation and finding it open
  // again on the next visit is the shell forgetting something it was told.
  // One answer for the whole product, not one per page.
  const [rail, setRail] = usePersistedSize("app.sidebar", SIDEBAR_BOUNDS);
  const collapsed = rail.collapsed;
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.matchMedia("(max-width: 900px)").matches);
  const [mobileOpen, setMobileOpen] = useState(false);
  // Masthead slots a routed page portals its subtitle + actions into (see ./Topbar).
  const [subtitleEl, setSubtitleEl] = useState<HTMLElement | null>(null);
  const [actionsEl, setActionsEl] = useState<HTMLElement | null>(null);
  // A page can hide the route title (useHideTopbarTitle) — a full-canvas surface spends the
  // masthead on its own chrome instead of repeating the nav rail's current item.
  const [titleHidden, setTitleHidden] = useState(false);
  // A page can hide the whole masthead (useHideTopbar).
  const [topbarHidden, setTopbarHidden] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(max-width: 900px)");
    const apply = () => {
      setNarrow(mq.matches);
      if (!mq.matches) setMobileOpen(false);
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);

  useEffect(() => {
    setMobileOpen(false);
  }, [loc.pathname]);

  const railCollapsed = collapsed && !narrow;
  const closeMobile = useCallback(() => setMobileOpen(false), []);
  // Narrow, the rail is the page's only navigation and opens as a modal drawer over a scrim:
  // focus moves into it, Tab stays inside it, Escape closes it and focus goes back to Open menu.
  // The page behind is inert while it is up, so neither Tab nor a screen reader reaches what the
  // scrim covers.
  const drawerOpen = narrow && mobileOpen;
  const sidebarRef = useRef<HTMLElement | null>(null);
  useFocusTrap(sidebarRef, { active: drawerOpen, onClose: closeMobile });

  // Hide nav doorways the signed-in role can't pass; the route guards still
  // enforce the real boundary.
  const gates = useNavGates();
  const visibleNav = useMemo(() => filterNav(portalNav(), gates), [gates]);
  const topLeaves = useMemo(() => visibleNav.flatMap((g) => g.items.filter((i): i is NavLeaf => !hasChildren(i))), [visibleNav]);

  return (
    <TopbarSlotsContext.Provider value={{ subtitle: subtitleEl, actions: actionsEl, framed: true, setTitleHidden, setTopbarHidden }}>
      <div className="alk-shell">
        <EmailVerificationBanner />
        {banners.map(({ key, Banner }) => (
          <Banner key={key} />
        ))}
        <SessionNotice />
        <div className="alk-app" data-collapsed={railCollapsed || undefined} data-mobile-open={(narrow && mobileOpen) || undefined}>
        {/* The keyboard's way past the nav. Without it every navigation starts the reader on the
            first nav item and costs them the whole rail again before the page. Out of sight until
            it takes focus, so nothing changes for a reader using a mouse. */}
        <a
          className="alk-skip"
          href="#main"
          inert={drawerOpen || undefined}
          onClick={(e) => {
            // Move focus, don't just scroll: a fragment jump alone leaves the tab order in the nav
            // (and Safari ignores it entirely), so the next Tab would land back on nav item two.
            e.preventDefault();
            document.getElementById("main")?.focus();
          }}
        >
          Skip to content
        </a>
        <aside
          ref={sidebarRef}
          className="alk-sidebar"
          inert={narrow && !mobileOpen ? true : undefined}
          {...(drawerOpen ? { role: "dialog", "aria-modal": true, "aria-label": "Menu" } : {})}
        >
          <div className="alk-sidebar__head">
            <Link to="/" className="alk-wordmark" aria-label="Overview" draggable={false}>
              <BrandLogo wordmark={!railCollapsed} size={18} className="alk-wordmark__logo" />
            </Link>
            <button
              type="button"
              className="alk-iconbtn"
              aria-label={narrow ? "Close menu" : railCollapsed ? "Expand sidebar" : "Collapse sidebar"}
              title={narrow ? "Close menu" : railCollapsed ? "Expand sidebar" : "Collapse sidebar"}
              onClick={() => (narrow ? closeMobile() : setRail({ collapsed: !collapsed }))}
            >
              <Icon name="panel" size={18} />
            </button>
          </div>
          {/* <div className="alk-sidebar__new">
            <Button fullWidth leftSection={<Icon name="plus" size={16} />}>
              New chat
            </Button>
          </div> */}
          <nav className="alk-nav" aria-label="Primary">
            {visibleNav.map((g) => (
              <div className="alk-nav__group" key={g.group}>
                {g.titled !== false && <span className="alk-nav__label">{g.group}</span>}
                {g.items.map((item) =>
                  hasChildren(item) ? (
                    <NavParentItem key={item.label} parent={item} onNavigate={closeMobile} />
                  ) : (
                    <NavLeafItem
                      key={item.to}
                      leaf={item}
                      shadowed={shadowedLeaf(item, loc.pathname, topLeaves)}
                      onNavigate={closeMobile}
                    />
                  ),
                )}
              </div>
            ))}
          </nav>
          <div className="alk-sidebar__foot">
            <AccountMenu collapsed={railCollapsed} />
          </div>
        </aside>

        <div className="alk-scrim" aria-hidden="true" onClick={closeMobile} />

        {/* `tabIndex={-1}`: the hash alone scrolls without moving focus, so the skip link would
            hand the next Tab straight back to the nav. */}
        <main className="alk-main" id="main" tabIndex={-1} inert={drawerOpen || undefined}>
          <Masthead
            // The browser tab names the page with this same string (see ./documentTitle);
            // "Overview" is the masthead's own fallback for a path the nav does not claim.
            title={sectionTitleFor(loc.pathname) ?? "Overview"}
            titleHidden={titleHidden}
            hidden={topbarHidden}
            onOpenMenu={() => setMobileOpen(true)}
            setSubtitleEl={setSubtitleEl}
            setActionsEl={setActionsEl}
          />
          {/* Lands in the masthead's actions slot only while live updates are paused. */}
          <RealtimeStatusIndicator />
          <div className="alk-content">
            <Outlet />
          </div>
        </main>
        </div>
      </div>
    </TopbarSlotsContext.Provider>
  );
}
