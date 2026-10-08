import { useState, type ReactNode } from "react";
import { Navigate, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { Button, Callout, currentBrand, cx, Select, TextInput } from "@alkera/ui";

import { AuthFrame } from "./ui/AuthFrame";
import { CodeCells } from "./ui/CodeCells";
import { StatusMark } from "./ui/StatusMark";
import { ApiError, refusalSentence, stepUpLoginUrl } from "../../api/errors";
import {
  useApproveDevice,
  useCurrentUser,
  useDenyDevice,
  useDeviceInfo,
  useMemberships,
} from "../../api/auth";
import "./ui/device.css";

/**
 * The RFC 8628 device-grant consent page (`/device`). The CLI / VS Code extension
 * shows a short `user_code` and opens this page with the code pre-filled. The user confirms
 * it matches what their device shows and approves — binding the pending authorization to
 * their account. No token is displayed and there is no callback; the client learns of the
 * approval by polling the backend. Reached signed-out, it routes through /login and back so
 * the code survives the round-trip. Reached without a code (bare `/device`), it renders a
 * manual entry form instead — the code shown in the terminal / editor can be typed (or the
 * approval link pasted) on any device.
 */
export function DeviceApprovalPage() {
  const [params, setSearchParams] = useSearchParams();
  const { search } = useLocation();
  const navigate = useNavigate();
  const userCode = params.get("user_code") ?? "";
  // `alkera login --org <id>` opens this page with the org it wants pre-selected.
  const requestedOrg = params.get("org");

  const { data: currentUser, isPending: userPending } = useCurrentUser();
  const info = useDeviceInfo(userCode);
  const approve = useApproveDevice();
  const deny = useDenyDevice();
  const memberships = useMemberships({ enabled: Boolean(currentUser) });
  const [pickedOrg, setPickedOrg] = useState<string | null>(null);
  const orgs = memberships.data?.memberships ?? [];
  const requestedKnown = requestedOrg !== null && orgs.some((m) => m.org_team_id === requestedOrg);
  // An org the link names that is not one of this person's: nothing is approved into
  // another org by accident, and the page says so instead of quietly using this one.
  const requestedUnknown = requestedOrg !== null && memberships.isSuccess && !requestedKnown;
  const chosenOrg =
    pickedOrg ?? (requestedKnown ? requestedOrg : memberships.data?.active_org_team_id) ?? null;
  const offerOrgs = orgs.length > 1;

  const dashboardAction = (
    <Button variant="secondary" fullWidth onClick={() => navigate("/", { replace: true })}>
      Go to dashboard
    </Button>
  );

  if (userPending) {
    return (
      <DeviceShell
        phase="loading"
        title="Authorize a device"
        sub="Checking your sign-in request."
        figure={<CodeCells code="" skeleton />}
      />
    );
  }

  // Logged out → bounce to login, then back here. Carry the FULL path incl. ?user_code
  // as `?return_to=` so the code survives the round-trip (the whole point of a
  // pre-filled approval link).
  if (!currentUser) {
    return <Navigate to={`/login?return_to=${encodeURIComponent(`/device${search}`)}`} replace />;
  }

  if (approve.isSuccess) {
    return (
      <DeviceShell
        phase="approved"
        title="Device approved"
        sub="You're signed in. Return to your terminal or editor."
        figure={<StatusMark kind="success" />}
        actions={dashboardAction}
      />
    );
  }

  if (deny.isSuccess) {
    return (
      <DeviceShell
        phase="denied"
        title="Sign-in denied"
        sub="No device was authorized. You can close this tab."
        figure={<StatusMark kind="denied" />}
        actions={dashboardAction}
      />
    );
  }

  if (!userCode) {
    return (
      <DeviceShell
        phase="enter-code"
        title="Authorize a device"
        sub="Enter the code shown in your terminal or editor."
        figure={<DeviceCodeEntryForm onSubmit={(code) => setSearchParams({ user_code: code })} />}
      />
    );
  }

  // A definite 404/410 means the code is invalid or expired. Any other info failure is
  // transient and must NOT mislabel a still-valid code or block its approval.
  const isDefinitelyInvalid =
    info.error instanceof ApiError && (info.error.status === 404 || info.error.status === 410);
  const isTransientInfoError = info.isError && !isDefinitelyInvalid;

  if (isDefinitelyInvalid) {
    return (
      <DeviceShell
        phase="expired"
        title="This code has expired"
        sub="This sign-in code is invalid or has expired. Check it and try again, or start a new sign-in from the CLI or VS Code extension."
        figure={<StatusMark kind="expired" />}
        actions={
          <>
            <Button variant="primary" fullWidth onClick={() => navigate("/device")}>
              Enter a different code
            </Button>
            {dashboardAction}
          </>
        }
      />
    );
  }

  const clientName = info.data?.client_name ?? `The ${currentBrand().productName} client`;
  const mutationError = approve.error ?? deny.error;
  const ssoUrl = stepUpLoginUrl(approve.error);
  const note = requestedUnknown ? (
    <Callout tone="danger">You don't have access to that organization.</Callout>
  ) : mutationError ? (
    <Callout tone="danger">
      {refusalSentence(mutationError, { fallback: "Could not complete the request." })}
      {ssoUrl ? (
        <div className="pa-device__note-action">
          <Button variant="secondary" size="sm" onClick={() => window.location.assign(ssoUrl)}>
            Continue with single sign-on
          </Button>
        </div>
      ) : null}
    </Callout>
  ) : isTransientInfoError ? (
    <Callout tone="danger">
      Couldn't load this device code. Check your connection and retry. You can still approve
      if you started this sign-in.
    </Callout>
  ) : null;

  return (
    <DeviceShell
      phase="confirm"
      title="Authorize a device"
      sub={
        <>
          {clientName} wants to sign in as <strong>{currentUser.email}</strong>. Approve only if
          you started this sign-in.
        </>
      }
      figure={
        <>
          <CodeCells code={userCode} />
          {offerOrgs && chosenOrg ? (
            <Select
              rootClassName="pa-device__org"
              label="Organization"
              requiredMark={false}
              value={chosenOrg}
              onChange={(event) => setPickedOrg(event.target.value)}
              disabled={approve.isPending}
            >
              {orgs.map((m) => (
                <option key={m.org_team_id} value={m.org_team_id}>
                  {m.org_name || "Unnamed organization"}
                </option>
              ))}
            </Select>
          ) : null}
        </>
      }
      note={note}
      split
      actions={
        <>
          <Button
            variant="secondary"
            onClick={() => deny.mutate(userCode)}
            loading={deny.isPending}
            disabled={approve.isPending}
          >
            Deny
          </Button>
          <Button
            variant="primary"
            onClick={() =>
              approve.mutate({
                userCode,
                orgTeamId: offerOrgs || requestedKnown ? (chosenOrg ?? undefined) : undefined,
              })
            }
            loading={approve.isPending}
            disabled={deny.isPending || requestedUnknown}
          >
            Approve
          </Button>
        </>
      }
    />
  );
}

interface DeviceShellProps {
  /** Keys the body so its rise animation replays when the state changes. */
  phase: string;
  title: ReactNode;
  sub: ReactNode;
  figure: ReactNode;
  note?: ReactNode;
  actions?: ReactNode;
  /** Lay the actions out as an equal-width row (the approve / deny pair). */
  split?: boolean;
}

/** The backend's user_code shape: 8 chars over an uppercase no-ambiguity alphabet, hyphenated
 *  4-4. The lookup is an exact string match, so manual input is canonicalized to that form. */
const USER_CODE_LENGTH = 8;

/**
 * Canonicalize manual input as the user types: pull the code out of a pasted approval link,
 * uppercase, drop separators, and re-insert the hyphen. Deliberately permissive beyond shape —
 * the backend is the only authority on which characters make a valid code.
 */
function formatUserCode(raw: string): string {
  let text = raw.trim();
  const fromLink = /[?&]user_code=([^&\s#]+)/i.exec(text);
  if (fromLink) {
    try {
      text = decodeURIComponent(fromLink[1]);
    } catch {
      text = fromLink[1];
    }
  } else if (/^https?:\/\//i.test(text)) {
    // A pasted URL with no code in it — nothing usable.
    return "";
  }
  const stripped = text
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, "")
    .slice(0, USER_CODE_LENGTH);
  const half = USER_CODE_LENGTH / 2;
  return stripped.length > half ? `${stripped.slice(0, half)}-${stripped.slice(half)}` : stripped;
}

/** The manual code-entry form for a bare `/device` visit — the code shown in the terminal /
 *  editor can be typed (or its approval link pasted) on any device. */
function DeviceCodeEntryForm({ onSubmit }: { onSubmit: (code: string) => void }) {
  const [code, setCode] = useState("");
  const complete = code.replace("-", "").length === USER_CODE_LENGTH;
  return (
    <form
      className="pa-device__form"
      onSubmit={(event) => {
        event.preventDefault();
        if (complete) onSubmit(code);
      }}
    >
      <TextInput
        label="Device code"
        value={code}
        onChange={(event) => setCode(formatUserCode(event.target.value))}
        placeholder="XXXX-XXXX"
        autoFocus
        autoComplete="off"
        autoCapitalize="characters"
        autoCorrect="off"
        spellCheck={false}
      />
      <Button type="submit" variant="primary" fullWidth disabled={!complete}>
        Continue
      </Button>
    </form>
  );
}

/** The cardless device body inside the shared auth frame. */
function DeviceShell({ phase, title, sub, figure, note, actions, split }: DeviceShellProps) {
  return (
    <AuthFrame vine={false}>
      <main className="pa-device">
        <div className="pa-device__body" key={phase}>
          <h1 className="pa-device__title">{title}</h1>
          <p className="pa-device__sub">{sub}</p>
          <div className="pa-device__figure">{figure}</div>
          {note ? <div className="pa-device__note">{note}</div> : null}
          {actions ? (
            <div className={cx("pa-device__actions", split && "pa-device__actions--split")}>
              {actions}
            </div>
          ) : null}
        </div>
      </main>
    </AuthFrame>
  );
}
