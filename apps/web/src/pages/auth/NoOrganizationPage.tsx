import { useState, type FormEvent } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";

import { Button, Callout, TextInput } from "@alkera/ui";

import { AuthLayout } from "./ui/AuthLayout";
import { useCurrentUser } from "../../api/auth";
import { usePublicConfig } from "../../api/config";
import { ApiError, refusalSentence, stepUpLoginUrl } from "../../api/errors";
import { inviteTokenFrom, useLandingCreateOrg, useLandingJoinOrg } from "../../api/orgs";
import { isDisplayName, required, firstError } from "../../lib/validation";

/** What a refused landing action says. A 401 means the sign-in itself is gone; a 404 that
 *  the action is not offered (or, for a link, that the link names nothing). */
function refusal(error: unknown, notFound: string): string | null {
  if (!error) return null;
  // A step-up navigates to the org's own sign-in; nothing to say here.
  if (stepUpLoginUrl(error)) return null;
  return refusalSentence(error, { known: { 401: "Your sign-in has ended. Sign in again.", 404: notFound } });
}

/**
 * Where a person who belongs to no org lands after signing in (`/no-organization`): every
 * org they were in is behind them, so the only ways on are to create an org or accept an
 * invitation. Both act on the sign-in itself and enter the org they produce. The page never
 * renews the session: the sign-in it acts on names no org, and a renewal would end it.
 */
export function NoOrganizationPage() {
  const [params] = useSearchParams();
  const invite = params.get("invite");
  const me = useCurrentUser();
  const config = usePublicConfig();
  const create = useLandingCreateOrg();
  const join = useLandingJoinOrg();
  const [name, setName] = useState("");
  const [link, setLink] = useState(invite ?? "");
  const [nameShown, setNameShown] = useState(false);
  const [linkShown, setLinkShown] = useState(false);

  // Someone who is in an org has nothing to do here.
  if (me.data) return <Navigate to="/" replace />;
  // Nobody reaches a sign-in into no org unless the server runs with several orgs per person.
  if (config.isPending) return null;
  if (config.data?.multi_org_enabled !== true) return <Navigate to="/login" replace />;

  const nameError = firstError(required("Name your organization")(name), isDisplayName()(name));
  const token = inviteTokenFrom(link);
  const linkError = token ? null : "Paste the invitation link or token";
  const busy = create.isPending || join.isPending;

  const onCreate = (event: FormEvent) => {
    event.preventDefault();
    setNameShown(true);
    if (nameError) return;
    join.reset();
    create.mutate(name.trim());
  };
  const onJoin = (event: FormEvent) => {
    event.preventDefault();
    setLinkShown(true);
    if (!token) return;
    create.reset();
    join.mutate(token);
  };

  const createRefused = refusal(create.error, "Creating an organization isn't available.");
  const joinRefused = refusal(join.error, "That invitation link isn't valid.");
  const ended = [create.error, join.error].some((e) => e instanceof ApiError && e.status === 401);

  return (
    <AuthLayout
      title="You're not in any organization"
      footer={
        <Link className="alk-link" to={invite ? `/login?invite=${encodeURIComponent(invite)}` : "/login"}>
          {ended ? "Sign in" : "Sign in with another account"}
        </Link>
      }
    >
      <form className="pa-auth__form" noValidate onSubmit={onCreate} aria-label="Create organization">
        <h2 className="pa-auth__section">Create organization</h2>
        {createRefused ? <Callout tone="danger">{createRefused}</Callout> : null}
        <TextInput
          label="Organization name"
          autoComplete="organization"
          value={name}
          error={nameShown ? nameError : null}
          onChange={(event) => setName(event.target.value)}
        />
        <Button type="submit" fullWidth loading={create.isPending} disabled={busy}>
          Create
        </Button>
      </form>
      <form className="pa-auth__form" noValidate onSubmit={onJoin} aria-label="Accept an invitation">
        <h2 className="pa-auth__section">Accept an invitation</h2>
        {joinRefused ? <Callout tone="danger">{joinRefused}</Callout> : null}
        <TextInput
          label="Invitation link"
          value={link}
          error={linkShown ? linkError : null}
          onChange={(event) => setLink(event.target.value)}
        />
        <Button type="submit" variant="secondary" fullWidth loading={join.isPending} disabled={busy}>
          Accept
        </Button>
      </form>
    </AuthLayout>
  );
}
