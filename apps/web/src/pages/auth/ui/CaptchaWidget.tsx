import { Turnstile, type TurnstileInstance } from "@marsidev/react-turnstile";
import { forwardRef, useImperativeHandle, useRef } from "react";

import { type CaptchaHandle, siteKey } from "./captcha";

/**
 * Cloudflare Turnstile (invisible bot-protection) for the public auth pages.
 *
 * Renders NOTHING when no site key is configured, so local dev and the test suite
 * (where `VITE_TURNSTILE_SITE_KEY` is unset) behave exactly as before and the
 * backend's matching seam is also a no-op.
 *
 * We force `appearance: "interaction-only"` so it stays invisible for normal users:
 * the token is obtained in the background and arrives via `onSuccess` (auto-executed
 * on render), and a visible challenge only appears if Cloudflare decides one is
 * required. The token is single-use, so callers `reset()` after a failed submit
 * (see `useCaptcha` in ./captcha).
 */
interface CaptchaWidgetProps {
  /** Receives the token on success, or `undefined` on error / expiry. */
  onToken: (token: string | undefined) => void;
}

export const CaptchaWidget = forwardRef<CaptchaHandle, CaptchaWidgetProps>(
  function CaptchaWidget({ onToken }, ref) {
    const inner = useRef<TurnstileInstance | null>(null);
    useImperativeHandle(ref, () => ({ reset: () => inner.current?.reset() }), []);
    const key = siteKey();
    if (!key) return null;
    return (
      <Turnstile
        ref={inner}
        siteKey={key}
        options={{ refreshExpired: "auto", appearance: "interaction-only" }}
        onSuccess={onToken}
        onError={() => onToken(undefined)}
        onExpire={() => onToken(undefined)}
      />
    );
  },
);
