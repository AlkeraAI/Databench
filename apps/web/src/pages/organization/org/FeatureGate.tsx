import { EmptyState } from "@alkera/ui";

import { useIdentityDashboard } from "../../../api/dashboard";
import { UNAVAILABLE_FEATURE_PLATES, type UnavailableFeatureProps } from "../../../app/extensions/portal";

/** Whether the caller's org may use the gated org surfaces (SSO, audit log).
 *
 *  - `"loading"` — the dashboard hasn't resolved yet.
 *  - `"error"`   — the dashboard request failed; the page should surface a retry, NOT
 *                  the unavailable plate (a transient failure isn't "you may not use it").
 *                  (`RequireOrgAdmin` — same dashboard query — already redirects on error,
 *                  so this is defense-in-depth.)
 *  - `"gated"`   — the server says the org may not use the surface. Fail-closed: never
 *                  reveal the form.
 *  - `"enabled"` — the real page renders.
 *
 *  The gate is computed server-side (`enterprise_features_enabled` on the dashboard), so the
 *  SPA never re-derives the rule and can never disagree with the backend's 403. `refetch`
 *  retries the dashboard query. */
export function useFeatureGate(): {
  status: "loading" | "error" | "gated" | "enabled";
  refetch: () => void;
} {
  const { data, isPending, isError, refetch } = useIdentityDashboard();
  const status = isPending
    ? "loading"
    : isError
      ? "error"
      : data?.enterprise_features_enabled
        ? "enabled"
        : "gated";
  return { status, refetch: () => void refetch() };
}

/** The plate shown in place of a gated page. An installed extension's plate is drawn when
 *  there is one; otherwise the page says the feature is not turned on. Pure presentation:
 *  the caller decides when to render it (see `useFeatureGate`). */
export function UnavailableFeature(props: UnavailableFeatureProps) {
  const Plate = UNAVAILABLE_FEATURE_PLATES.items()[0]?.Plate;
  if (Plate) return <Plate {...props} />;
  return (
    <EmptyState
      icon={props.icon}
      title={`${props.feature} is not turned on`}
      body={`${props.blurb} is not available to this organization.`}
    />
  );
}
