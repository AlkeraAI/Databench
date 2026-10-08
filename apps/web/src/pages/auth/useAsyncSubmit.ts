import { useCallback, useState } from "react";

import { AuthError } from "./auth-actions";
import { GENERIC_FAILURE } from "../../api/errors";

// Tracks one async submission's pending / error / success state so each auth page
// doesn't re-implement the same three-way machine. A thrown AuthError surfaces its
// message + backend code; anything else becomes a generic fallback with a null code
// (never leak an internal error).

interface SubmitState {
  pending: boolean;
  error: string | null;
  /** The backend error `code` (e.g. `mfa_required`), present only for an AuthError.
   *  A page branches on this — the MFA challenge reveals its field when it's set. */
  errorCode: string | null;
  /** The refusal's `details` (e.g. the `next` of `account_exists`), only for an AuthError. */
  errorDetails: Readonly<Record<string, unknown>> | null;
  success: boolean;
}

const IDLE: SubmitState = { pending: false, error: null, errorCode: null, errorDetails: null, success: false };

export function useAsyncSubmit() {
  const [state, setState] = useState<SubmitState>(IDLE);

  const run = useCallback(async (action: () => Promise<void>) => {
    setState({ pending: true, error: null, errorCode: null, errorDetails: null, success: false });
    try {
      await action();
      setState({ pending: false, error: null, errorCode: null, errorDetails: null, success: true });
    } catch (err) {
      const isAuth = err instanceof AuthError;
      setState({
        pending: false,
        error: isAuth ? err.message : GENERIC_FAILURE,
        errorCode: isAuth ? err.code : null,
        errorDetails: isAuth ? err.details : null,
        success: false,
      });
    }
  }, []);

  const reset = useCallback(() => setState(IDLE), []);

  return { ...state, run, reset };
}
