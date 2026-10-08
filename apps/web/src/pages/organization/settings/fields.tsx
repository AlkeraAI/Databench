import type { ReactNode } from "react";

import { Callout, Button, Card, CopyReading, IconChip, Inline, Skeleton, Stack, Switch, cx } from "@alkera/ui";

import { Icon } from "../../../app/icons";
import type { IconName } from "../../../app/icons";
import { errorSentence, useNotify, type Notify } from "../../../app/notify";
import { TopbarSubtitle } from "../../../app/Topbar";
import styles from "./fields.module.css";
import page from "./SettingsPage.module.css";
import shell from "./shell.module.css";

/**
 * Settings page-layout vocabulary. Every CONTROL is an @alkera/ui primitive — the action Button,
 * the text inputs (TextInput) and selects (Select) live in the bodies, the Switch under ToggleRow,
 * the dialog is the shared Modal (its confirm footer), and status marks are Pills. Rows and clusters
 * compose the ui Stack/Inline layout primitives (a row inside a divided Card body forces
 * `display: flex` — the body is a block container, so a bare inline-flex cluster would shrink-fit);
 * the one shell with no ui equivalent is the sticky save bar. The copyable org-id / setup-key
 * reading is the shared CopyReading under its label.
 */

/** Blockify an Inline used as a full-width row directly inside a (block) divided Card body. */

/** The server's own sentence, or the written fallback — see `errorSentence`. */
export function errText(e: unknown, fallback: string): string {
  return errorSentence(e, fallback);
}

/* -------------------------------------------------------------------- Doc */

/** A settings document: the masthead subtitle, one compact column of record plates, and the toast
 *  viewport its sections report outcomes through (`notify.success` / `notify.error`). Every
 *  settings-shaped page (Profile, Organization, the reader's own Preferences) is this shell around a
 *  body, so a page lands with no layout of its own. There is NO in-page page-switcher — each is a
 *  separate route. A product surface (density + scannability), so it carries no hero and no
 *  scroll-entrance. */
export function SettingsDoc({
  subtitle,
  children,
}: {
  /** A masthead sub-line, only when it changes what the reader does on the page. */
  subtitle?: string;
  children: (notify: Notify) => ReactNode;
}) {
  const { notify, viewport } = useNotify();

  return (
    <div className="pset" data-measure-surface="product">
      {subtitle ? <TopbarSubtitle>{subtitle}</TopbarSubtitle> : null}

      <Stack gap={7} align="stretch" className={page.doc}>
        {children(notify)}
      </Stack>

      {viewport}
    </div>
  );
}

/* ----------------------------------------------------------------- Section */

export function SettingsSection({
  id,
  icon,
  title,
  actions,
  children,
}: {
  id: string;
  icon: IconName;
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card
      id={id}
      region
      headerDivider
      divided
      icon={<Icon name={icon} />}
      title={title}
      actions={actions}
      headingLevel={2}
    >
      {children}
    </Card>
  );
}

/* ----------------------------------------------------- Copyable reading */

/** A read-only identifier reading (the org id, an MFA setup key): a labelled precious CopyReading —
 *  the shared brass-framed mono value with its copy affordance. */
export function ReadingField({ label, value }: { label: string; value: string }) {
  return (
    <Stack gap={2} align="stretch" className={shell.capped}>
      <span className="alk-name">{label}</span>
      <CopyReading value={value} precious copyLabel={`Copy ${label.toLowerCase()}`} />
    </Stack>
  );
}

/* ------------------------------------------------------------------ Switch */

/** A toggle row: an optional leading logo chip, label (+ optional help), and the ui Switch on
 *  the right. The switch never wraps; the text region shrinks and wraps. */
export function ToggleRow({
  label,
  help,
  logo,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  help?: ReactNode;
  logo?: ReactNode;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <Inline gap={5} wrap={false} block>
      {logo ? (
        <IconChip size="md" tone="surface" className={shell.brandLogo}>
          {logo}
        </IconChip>
      ) : null}
      <Stack grow>
        <span className={cx("alk-strong", disabled && "alk-muted")}>{label}</span>
        {help ? <span className="alk-meta">{help}</span> : null}
      </Stack>
      <Switch checked={checked} disabled={disabled} aria-label={label} onChange={(e) => onChange(e.target.checked)} />
    </Inline>
  );
}

/* ----------------------------------------------------------------- Person */

/** A device / provider row cell — an icon (or a brand logo) chip, a name (sans), and a sub-line of
 *  meta (sans, NOT mono — it's prose, not a reading). */
export function PersonCell({
  icon,
  logo,
  name,
  meta,
  detail,
  badge,
}: {
  icon?: IconName;
  /** A brand mark (e.g. the Google / GitHub logo) shown in the chip instead of a named icon. */
  logo?: ReactNode;
  name: ReactNode;
  meta: string;
  /** A second, quieter line — the exact moments behind a relative `meta`, say. Rendered as text
   *  rather than a `title`: a tooltip is not reachable by keyboard and is unreliable for screen
   *  readers, so anything a reader needs to decide about the row belongs on the row. */
  detail?: string;
  badge?: ReactNode;
}) {
  return (
    <Inline gap={5} grow wrap={false} style={{ minWidth: 0 }}>
      <IconChip size="lg" tone={logo ? "surface" : "neutral"} className={logo ? shell.brandLogo : undefined}>
        {logo ?? (icon ? <Icon name={icon} /> : null)}
      </IconChip>
      <Stack style={{ lineHeight: 1.3 }}>
        <span className={cx("alk-inline", "alk-strong")}>
          {name}
          {badge}
        </span>
        <span className="alk-meta">{meta}</span>
        {detail ? <span className="alk-meta">{detail}</span> : null}
      </Stack>
    </Inline>
  );
}

/* ----------------------------------------------------------------- Rows */

/** A generic action row — label/help on the left, a control or button on the right. */
export function ActionRow({ label, help, children }: { label: string; help?: ReactNode; children: ReactNode }) {
  return (
    <Inline gap={7} justify="space-between" wrap={false} block>
      <Stack grow>
        <span className="alk-strong">{label}</span>
        {help ? <span className="alk-meta">{help}</span> : null}
      </Stack>
      <Inline gap={3} wrap={false} style={{ flexShrink: 0 }}>
        {children}
      </Inline>
    </Inline>
  );
}

/* ----------------------------------------------------- Loading / error */

/** A loading placeholder built from the real section plate (Card), so the resolve doesn't jump. */
export function SettingsSkeleton({ sections = 2 }: { sections?: number }) {
  return (
    // `role="status"`: ARIA forbids a name on a role-less div, so the label was announced as
    // nothing; status admits one and announces the wait.
    <Stack role="status" gap={7} align="stretch" style={{ maxWidth: 640 }} aria-busy="true" aria-label="Loading settings">
      {Array.from({ length: sections }, (_, i) => (
        <Card key={i} headerDivider divided title={<Skeleton width={140} height={18} />} icon={<Skeleton width={28} height={28} />} headingLevel={2}>
          <Inline gap={7} justify="space-between" wrap={false} block>
            <Skeleton width={180} height={14} />
            <Skeleton width={96} height={32} />
          </Inline>
          <Inline gap={7} justify="space-between" wrap={false} block>
            <Skeleton width={220} height={14} />
            <Skeleton width={96} height={32} />
          </Inline>
        </Card>
      ))}
    </Stack>
  );
}

/** The failed-load surface — names the cause and offers a retry. */
export function LoadError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Callout tone="danger" title="Couldn't load this page">
      <p>{message}</p>
      {onRetry ? (
        <Button variant="secondary" leftSection={<Icon name="refresh" size={15} />} onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </Callout>
  );
}

/* --------------------------------------------------------------- Save bar */

export function SaveBar({
  dirty,
  saving,
  onSave,
  onDiscard,
}: {
  dirty: boolean;
  saving?: boolean;
  onSave: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className={styles.savebar} data-show={dirty || undefined} role="region" aria-label="Unsaved changes" aria-hidden={!dirty}>
      <span className="alk-name">You have unsaved changes</span>
      <Inline gap={3} wrap={false}>
        <Button variant="secondary" fill="ghost" onClick={onDiscard} tabIndex={dirty ? undefined : -1}>
          Discard
        </Button>
        <Button variant="primary" loading={saving} onClick={onSave} tabIndex={dirty ? undefined : -1}>
          {saving ? "Saving" : "Save changes"}
        </Button>
      </Inline>
    </div>
  );
}
