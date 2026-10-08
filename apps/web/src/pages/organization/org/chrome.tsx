import type { ReactNode } from "react";

import { Callout, Button, Card, Skeleton } from "@alkera/ui";

import { useNotify, type Notify } from "../../../app/notify";

import { Icon } from "../../../app/icons";
import { TopbarSubtitle } from "../../../app/Topbar";
import styles from "./chrome.module.css";
import { refusalSentence } from "../../../api/errors";

/**
 * The org-admin page shell. Every org-admin surface (billing, members, SSO, audit)
 * is a product page: a `data-measure-surface="product"` root, a topbar subtitle, a
 * single compact content column, and a shared ToastViewport for mutation feedback.
 * Mirrors the settings page shell so the two account areas feel like one ledger.
 */
export function OrgPage({
  subtitle,
  children,
}: {
  subtitle: string;
  children: (notify: Notify) => ReactNode;
}) {
  const { notify, viewport } = useNotify();
  return (
    <div style={{ minHeight: "100%" }} data-measure-surface="product">
      <TopbarSubtitle>{subtitle}</TopbarSubtitle>
      <div className={styles.doc}>{children(notify)}</div>
      {viewport}
    </div>
  );
}

/** A loading column of section-plate skeletons, matching the resolved layout. */
export function OrgLoading({ sections = 2 }: { sections?: number }) {
  return (
    <div className={styles.doc} aria-busy="true" aria-label="Loading">
      {Array.from({ length: sections }, (_, i) => (
        <Card key={i} title={<Skeleton width={180} height={18} />}>
          <Skeleton width="100%" height={56} />
          <Skeleton width="100%" height={56} style={{ marginTop: 8 }} />
        </Card>
      ))}
    </div>
  );
}

/** The failed-load surface — names the cause and offers a retry. */
export function OrgLoadError({ message, onRetry }: { message: string; onRetry?: () => void }) {
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

/** A mutation error banner, shown only when an error is present. */
export function OrgMutationError({ error }: { error: unknown }) {
  if (!error) return null;
  return <Callout tone="danger">{refusalSentence(error)}</Callout>;
}
