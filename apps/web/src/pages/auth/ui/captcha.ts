import { useCallback, useRef, useState } from "react";

// Cloudflare Turnstile wiring for the public auth pages — the non-component half of
// the captcha (the widget itself lives in CaptchaWidget.tsx). Split out so the
// component file exports only a component (react-refresh / fast-refresh ergonomics).

/** Public site key, baked in at build time. Read lazily (not at module load) so
 *  tests can toggle it with `vi.stubEnv`. Empty → captcha disabled. */
export function siteKey(): string | undefined {
  return import.meta.env.VITE_TURNSTILE_SITE_KEY as string | undefined;
}

/** Whether a site key is configured — the auth forms gate submit on a token only
 *  when this is true. Mirrors the backend's `settings.turnstile_enabled`. */
export function captchaEnabled(): boolean {
  return Boolean(siteKey());
}

export interface CaptchaHandle {
  /** Discard the current single-use token and re-run the challenge (call after a
   *  failed submit; Turnstile tokens can't be replayed). */
  reset: () => void;
}

/**
 * Wiring helper for an auth page: tracks the current token, exposes the widget
 * `ref`, and a stable `reset()` that clears the token and re-runs the challenge.
 * Pass `token` into the request as `turnstileToken`, render
 * `<CaptchaWidget ref={ref} onToken={setToken} />`, disable submit while
 * `captchaEnabled() && !token`, and call `reset()` when the request errors.
 */
export function useCaptcha() {
  const [token, setToken] = useState<string | undefined>(undefined);
  const ref = useRef<CaptchaHandle>(null);
  const reset = useCallback(() => {
    setToken(undefined);
    ref.current?.reset();
  }, []);
  return { token, setToken, ref, reset };
}
