import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { useAsyncSubmit } from "./useAsyncSubmit";

// The shared machine behind every auth page: submit state, whether to reveal field
// errors yet, and the submit handler that ties them together. Keeping it here means a
// page only declares its fields and validators — the gating, error reveal, and
// focus-on-error behavior are defined once instead of copied four times.

export function useAuthForm() {
  const submit = useAsyncSubmit();
  const [showErrors, setShowErrors] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const formRef = useRef<HTMLFormElement>(null);

  // After each submit attempt, move focus to the first field flagged invalid, so a
  // keyboard or screen-reader user lands on the error rather than hunting for it. A
  // clean (valid) submit finds nothing to focus and lets the action run.
  useEffect(() => {
    if (attempt === 0) return;
    formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [attempt]);

  /** Build the form's onSubmit. `hasError` is computed by the page from the same
   *  validator results it renders, so nothing is validated twice. */
  const handleSubmit = useCallback(
    (hasError: boolean, action: () => Promise<void>) => (event: FormEvent) => {
      event.preventDefault();
      setShowErrors(true);
      setAttempt((n) => n + 1);
      if (hasError) return;
      void submit.run(action);
    },
    [submit],
  );

  return { submit, showErrors, handleSubmit, formRef };
}
