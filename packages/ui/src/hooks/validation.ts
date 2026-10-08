// Small, pure field validators. Each returns an error message or null, so a form can run them on
// blur/submit without a form library. Composable via firstError. Framework-agnostic — no React.

export type Validator = (value: string) => string | null;

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export const required =
  (message = "This field is required"): Validator =>
  (value) =>
    value.trim() ? null : message;

export const isEmail =
  (message = "Enter a valid email address"): Validator =>
  (value) =>
    EMAIL_RE.test(value.trim()) ? null : message;

export const minLength =
  (n: number, message?: string): Validator =>
  (value) =>
    value.length >= n ? null : (message ?? `Must be at least ${n} characters`);

/** First non-null result, or null if every check passed. */
export function firstError(...results: (string | null)[]): string | null {
  return results.find((r) => r !== null) ?? null;
}

/** A confirmation field matches its source, or returns the message. */
export function matches(value: string, other: string, message = "Values do not match"): string | null {
  return value === other ? null : message;
}
