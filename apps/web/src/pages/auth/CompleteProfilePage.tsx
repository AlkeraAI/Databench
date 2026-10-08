import { useState } from "react";
import { Navigate, useSearchParams } from "react-router-dom";

import { Button, Skeleton, TextInput } from "@alkera/ui";

import { AuthLayout } from "./ui/AuthLayout";
import { FormError } from "./ui/FormError";
import { isProfileIncomplete, useCurrentUser } from "../../api/auth";
import { GoTo, safeReturnTo, useAuthActions } from "./auth-actions";
import { useAuthForm } from "./useAuthForm";
import { firstError, isDisplayName, required } from "../../lib/validation";

/**
 * The step right after a minimal signup ("Finish setting up"): collect the
 * name, and — for a new-org admin whose org isn't named yet — the organization name.
 * The guard sends every name-less account here before the app; an invited member just
 * fills in their name (the org is the inviter's, so no org field).
 */
export function CompleteProfilePage() {
  const { data: user, isPending } = useCurrentUser();
  const actions = useAuthActions();
  const { submit, showErrors, handleSubmit, formRef } = useAuthForm();
  const [params] = useSearchParams();
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [org, setOrg] = useState("");

  if (isPending) {
    return (
      <AuthLayout title="One moment" lede="Loading your account.">
        <div className="pa-auth__success" role="status" aria-label="Loading your account">
          <Skeleton width={200} height={14} />
        </div>
      </AuthLayout>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
  // Already named → nothing to finish; send them on to where they were going
  // (the guard's `?return_to=`, sanitized) or into the app.
  if (!isProfileIncomplete(user)) {
    return <GoTo to={safeReturnTo(params.get("return_to")) ?? "/"} />;
  }

  // A new-org signup leaves the org unnamed (""); that's the only case that needs
  // the org field. An invited member's org is already named, so it's hidden.
  const needsOrg = user.org_name === "";

  // Every one of these lands in an invitation email this account later sends, so
  // each carries the display-name policy alongside its presence check.
  const errors = {
    firstName: firstError(required("Enter your first name")(firstName), isDisplayName()(firstName)),
    lastName: firstError(required("Enter your last name")(lastName), isDisplayName()(lastName)),
    org: needsOrg
      ? firstError(required("Name your organization")(org), isDisplayName()(org))
      : null,
  };
  const shown = showErrors ? errors : null;
  const hasError = Boolean(firstError(...Object.values(errors)));

  return (
    <AuthLayout title="Finish setting up">
      <form
        ref={formRef}
        className="pa-auth__form"
        noValidate
        onSubmit={handleSubmit(hasError, () =>
          actions.completeProfile({
            firstName,
            lastName,
            orgName: needsOrg ? org : undefined,
          }),
        )}
      >
        <FormError title="Couldn't save your profile" error={submit.error} />
        <div className="pa-auth__names">
          <TextInput
            label="First name"
            autoComplete="given-name"
            placeholder="Ada"
            required
            value={firstName}
            error={shown?.firstName}
            onChange={(event) => setFirstName(event.target.value)}
          />
          <TextInput
            label="Last name"
            autoComplete="family-name"
            placeholder="Lovelace"
            required
            value={lastName}
            error={shown?.lastName}
            onChange={(event) => setLastName(event.target.value)}
          />
        </div>
        {needsOrg ? (
          <TextInput
            label="Organization name"
            autoComplete="organization"
            placeholder="Acme Data"
            required
            value={org}
            error={shown?.org}
            onChange={(event) => setOrg(event.target.value)}
          />
        ) : null}
        <Button type="submit" fullWidth loading={submit.pending}>
          Continue
        </Button>
      </form>
    </AuthLayout>
  );
}
