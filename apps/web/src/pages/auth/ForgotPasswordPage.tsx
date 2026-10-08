import { useState } from "react";
import { Link } from "react-router-dom";

import { Callout, Button, TextInput } from "@alkera/ui";

import { AuthLayout } from "./ui/AuthLayout";
import { FormError } from "./ui/FormError";
import { useAuthActions } from "./auth-actions";
import { useAuthForm } from "./useAuthForm";
import { isEmail } from "../../lib/validation";

export function ForgotPasswordPage() {
  const actions = useAuthActions();
  const { submit, showErrors, handleSubmit, formRef } = useAuthForm();
  const [email, setEmail] = useState("");

  const emailError = isEmail()(email);
  const shownEmailError = showErrors ? emailError : null;

  const footer = (
    <Link className="alk-link" to="/login">
      Back to sign in
    </Link>
  );

  if (submit.success) {
    return (
      <AuthLayout title="Check your inbox" lede="If we found your account, a link is on its way." footer={footer}>
        <Callout tone="success" title="Reset link sent">
          If <strong>{email}</strong> matches an account, you'll get a link to set a new password. It can
          take a minute. Check spam if it doesn't arrive.
        </Callout>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Reset your password"
      lede="We'll email you a link to set a new one."
      footer={footer}
    >
      <form
        ref={formRef}
        className="pa-auth__form"
        noValidate
        onSubmit={handleSubmit(Boolean(emailError), () => actions.requestPasswordReset({ email }))}
      >
        <FormError title="Couldn't send the link" error={submit.error} />
        <TextInput
          label="Email"
          type="email"
          autoComplete="email"
          placeholder="you@company.com"
          required
          value={email}
          error={shownEmailError}
          onChange={(event) => setEmail(event.target.value)}
        />
        <Button type="submit" fullWidth loading={submit.pending}>
          Send reset link
        </Button>
      </form>
    </AuthLayout>
  );
}
