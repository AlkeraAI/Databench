import { Navigate, useLocation, useParams } from "react-router-dom";

/** Redirect an older path to its current home, carrying the query string and
 *  hash along (Stripe returns `?checkout=…`; invitation emails carry
 *  `?tab=invites`). Params in `to` win over same-named params in the current URL. */
export function LegacyRedirect({ to }: { to: string }) {
  const location = useLocation();
  const [path, toQuery] = to.split("?");
  const params = new URLSearchParams(location.search);
  for (const [key, value] of new URLSearchParams(toQuery ?? "")) params.set(key, value);
  const search = params.toString();
  return <Navigate to={`${path}${search ? `?${search}` : ""}${location.hash}`} replace />;
}

/** Redirect an older path whose `:param` names the thing to `to(param)`, query and hash
 *  carried along like LegacyRedirect. */
export function LegacyParamRedirect({ param, to }: { param: string; to: (value: string) => string }) {
  const value = useParams()[param] ?? "";
  return <LegacyRedirect to={to(value)} />;
}
