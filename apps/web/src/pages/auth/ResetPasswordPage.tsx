import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { Callout, Button, TextInput } from "@alkera/ui";

import { AuthLayout } from "./ui/AuthLayout";
import { FormError } from "./ui/FormError";
import { useAuthActions } from "./auth-actions";
import { useAuthForm } from "./useAuthForm";
import { PASSWORD_MIN, isPassword, matches } from "../../lib/validation";

export function ResetPasswordPage() {
  const actions = useAuthActions();
  const navigate = useNavigate();
  const { submit, showErrors, handleSubmit, formRef } = useAuthForm();
  const { token } = useParams<{ token: string }>();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");

  const footer = (
    <Link className="alk-link" to="/login">
      Back to sign in
    </Link>
  );

  // No human-check here: the reset link's single-use token already proves a human
  // reached this page, so the other auth forms' bot-check would be redundant.
  if (!token) {
    return (
      <AuthLayout title="Link expired" lede="This reset link can't be used." footer={footer}>
        <Callout tone="danger" title="Invalid reset link">
          This link is missing or has expired. Request a new one from the sign-in page.
        </Callout>
      </AuthLayout>
    );
  }

  if (submit.success) {
    return (
      <AuthLayout title="Password updated" lede="You can sign in with your new password." footer={footer}>
        <div className="pa-auth__success-action">
          <Button fullWidth onClick={() => navigate("/login")}>
            Go to sign in
          </Button>
        </div>
      </AuthLayout>
    );
  }

  const errors = {
    password: isPassword()(password),
    confirm: matches(confirm, password, "Passwords don't match"),
  };
  const shown = showErrors ? errors : null;
  const hasError = Boolean(errors.password ?? errors.confirm);

  return (
    <AuthLayout
      title="Set a new password"
      lede="Choose a password you don't use anywhere else."
      footer={footer}
    >
      <form
        ref={formRef}
        className="pa-auth__form"
        noValidate
        onSubmit={handleSubmit(hasError, () => actions.resetPassword({ token, password }))}
      >
        <FormError title="Couldn't update your password" error={submit.error} />
        <TextInput type="password"
          label="New password"
          autoComplete="new-password"
          placeholder="Create a password"
          description={`At least ${PASSWORD_MIN} characters.`}
          required
          value={password}
          error={shown?.password}
          onChange={(event) => setPassword(event.target.value)}
        />
        <TextInput type="password"
          label="Confirm new password"
          autoComplete="new-password"
          placeholder="Re-enter your password"
          required
          value={confirm}
          error={shown?.confirm}
          onChange={(event) => setConfirm(event.target.value)}
        />
        <Button type="submit" fullWidth loading={submit.pending}>
          Update password
        </Button>
      </form>
    </AuthLayout>
  );
}
