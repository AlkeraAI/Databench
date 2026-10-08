import type { ReactNode } from "react";

import { Button, GitHubMark, GoogleMark } from "@alkera/ui";

import { oauthStartUrl, useOAuthProviders } from "../../../api/auth";

/** Per-provider display: the short label that pairs with the mark, and the mark itself.
 *  A provider not listed here (e.g. the dev `mock`) still renders, with a capitalized
 *  name and no mark, so a misconfiguration never silently drops a working button. */
const PROVIDER_DISPLAY: Record<string, { label: string; mark: (size: number) => ReactNode }> = {
  google: { label: "Google", mark: (size) => <GoogleMark size={size} /> },
  github: { label: "GitHub", mark: (size) => <GitHubMark size={size} /> },
};

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/**
 * The federated sign-in options, secondary to the email form above the divider.
 *
 * Driven by the platform's configured providers (`/auth/oauth/providers`): renders
 * one button per provider and nothing when none are configured (local dev with no
 * OAuth credentials), so the divider never floats above an empty row. Clicking a
 * button is a full-page redirect into the backend OAuth handshake — the SPA is not
 * involved again until the provider bounces back through the callback.
 */
export function OAuthOptions({
  intent,
  inviteToken,
  returnTo,
}: {
  intent: "in" | "up";
  inviteToken?: string;
  returnTo?: string;
}) {
  const { data: providers } = useOAuthProviders();
  if (!providers || providers.length === 0) return null;

  const verb = intent === "in" ? "Sign in" : "Sign up";
  const oauthIntent = intent === "in" ? "login" : "signup";

  return (
    <div>
      <div className="pa-oauth__divider">or</div>
      <div className="pa-oauth__row">
        {providers.map((provider) => {
          const display = PROVIDER_DISPLAY[provider];
          const label = display?.label ?? titleCase(provider);
          return (
            <Button
              key={provider}
              variant="secondary"
              fullWidth
              leftSection={display?.mark(17)}
              aria-label={`${verb} with ${label}`}
              onClick={() => {
                window.location.href = oauthStartUrl(provider, {
                  intent: oauthIntent,
                  inviteToken,
                  returnTo,
                });
              }}
            >
              {label}
            </Button>
          );
        })}
      </div>
    </div>
  );
}
