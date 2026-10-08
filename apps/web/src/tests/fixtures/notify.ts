import type { Notify } from "@/app/notify";

/** A `Notify` that records what a page reported, split by tone, so a test can assert that a
 *  failure was reported as a failure and not merely that some message went out. */
export function recordingNotify(): { notify: Notify; successes: string[]; errors: string[] } {
  const successes: string[] = [];
  const errors: string[] = [];
  return {
    notify: {
      success: (message) => void successes.push(message),
      error: (message) => void errors.push(message),
    },
    successes,
    errors,
  };
}

/** A `Notify` for a test that does not look at outcomes. */
export const silentNotify: Notify = { success: () => undefined, error: () => undefined };
