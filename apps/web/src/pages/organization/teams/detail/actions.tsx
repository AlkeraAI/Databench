import { Fragment } from "react";

import { Dropdown, DropdownDivider, DropdownItem, Pill } from "@alkera/ui";

import { Icon, type IconName } from "../../../../app/icons";
import { ROLE_LABEL, type Role } from "../data/model";
import styles from "./actions.module.css";

/**
 * The two anchored menus, built on the shared `Dropdown` so they portal out of the roster's
 * scroll-clipping ancestor, z-stack above their siblings, and animate both ways. The triggers keep
 * the page's chrome (the role chip — a rect Pill); only the panel mechanics come from the library.
 * `RoleSelect` edits a direct member's role inline; `ActionsMenu` is the row / header overflow menu
 * driven by a `MenuItem[]` registry — each item's `danger` / `disabled` flag maps straight to the
 * shared `DropdownItem` (no page-local menu-row variants). Both dots triggers are an icon-only
 * `Button` — the row one a quiet `ghost`, the header one a `size="lg"` `secondary` so it reads as a
 * button beside the adjacent primary Button as one set.
 */

export function RoleChip({ role }: { role: Role }) {
  return (
    <Pill
      shape="rect"
      tone={role === "admin" ? "brand" : "neutral"}
      icon={role === "admin" ? <Icon name="shield" size={13} /> : undefined}
    >
      {ROLE_LABEL[role]}
    </Pill>
  );
}

export function RoleSelect({ value, onChange, who }: { value: Role; onChange: (role: Role) => void; who: string }) {
  const roles: Role[] = ["member", "admin"];
  return (
    <Dropdown
      label={`Role for ${who}`}
      align="start"
      panelClassName={styles.menu}
      trigger={{
        kind: "menu",
        className: styles.roletrigger,
        ariaLabel: `Role for ${who}: ${ROLE_LABEL[value]}`,
        content: <RoleChip role={value} />,
      }}
    >
      {roles.map((r) => (
        <DropdownItem
          key={r}
          selected={r === value}
          icon={<Icon name={r === "admin" ? "shield" : "user"} size={15} />}
          onSelect={() => {
            if (r !== value) onChange(r);
          }}
        >
          {ROLE_LABEL[r]}
        </DropdownItem>
      ))}
    </Dropdown>
  );
}

export interface MenuItem {
  key: string;
  label: string;
  icon: IconName;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
  /** Render a separator above this item. */
  separated?: boolean;
}

/**
 * A row/header overflow menu — a dots trigger over a `MenuItem[]` registry. Items declare their own
 * icon, optional danger tint, optional disabled state, and an optional preceding divider, so a
 * caller lists only the actions it allows (a registry, not an if/elif wall). `variant="header"`
 * renders the trigger at the primary button's height (a `secondary` so it reads as a button) so the
 * two sit as one set; `"row"` (default) is the compact table-row size — a quiet `ghost`.
 */
export function ActionsMenu({ items, label, variant = "row" }: { items: MenuItem[]; label: string; variant?: "row" | "header" }) {
  return (
    <Dropdown
      label={label}
      align="end"
      panelClassName={styles.menu}
      trigger={{
        kind: "icon",
        icon: <Icon name="dotsV" size={18} />,
        ariaLabel: label,
        variant: "secondary",
        fill: variant === "header" ? "filled" : "ghost",
        size: variant === "header" ? "lg" : "md",
      }}
    >
      {items.map((item) => (
        <Fragment key={item.key}>
          {item.separated ? <DropdownDivider /> : null}
          <DropdownItem
            icon={<Icon name={item.icon} size={15} />}
            danger={item.danger}
            disabled={item.disabled}
            onSelect={item.onClick}
          >
            {item.label}
          </DropdownItem>
        </Fragment>
      ))}
    </Dropdown>
  );
}
