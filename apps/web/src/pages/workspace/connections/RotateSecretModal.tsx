// Replacing a stored credential — the one verb that cuts access off now.
//
// Whoever holds the row borrows the credential from the server minutes at a
// time, so removing an entitlement only stops the next borrow; a new secret is
// what retires the old one.

import { useState } from "react";

import { Callout, Modal, Stack, TextInput } from "@alkera/ui";

import {
  useRotateMyConnectionSecret,
  useRotateTeamConnectionSecret,
  type TeamConnection,
} from "../../../api/connections";
import { refusalSentence } from "../../../api/errors";

/** Replacing the shared credential is how an admin cuts access off now. Members
 *  borrow it from the server for minutes at a time, so revoking a member's
 *  entitlement only stops the next borrow — a new secret retires the old one. */
export function RotateSecretModal({
  connection,
  onClose,
}: {
  connection: TeamConnection | null;
  onClose: () => void;
}) {
  // A row's owner decides which door the rotation goes through. Both hooks are
  // wired up because a hook cannot be called conditionally; only one runs.
  const rotateTeam = useRotateTeamConnectionSecret(connection?.team_id ?? "");
  const rotateMine = useRotateMyConnectionSecret();
  const rotate = connection?.owner_user_id ? rotateMine : rotateTeam;
  const [secret, setSecret] = useState("");
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    setSecret("");
    setError(null);
    onClose();
  };

  const confirmRotate = () => {
    if (!connection) return;
    setError(null);
    rotate.mutate(
      { connectionId: connection.id, secret: secret.trim() },
      {
        onSuccess: close,
        onError: (err) =>
          setError(refusalSentence(err, { fallback: "Couldn't rotate the credential." })),
      },
    );
  };

  return (
    <Modal
      open={connection !== null}
      onClose={close}
      size="sm"
      title={connection ? `Rotate the credential for ${connection.handle}` : ""}
      confirmLabel="Rotate credential"
      confirmBusy={rotate.isPending}
      confirmDisabled={!secret.trim()}
      onConfirm={confirmRotate}
    >
      <Stack gap={3} align="stretch">
        <p className="alk-caption">
          {connection?.owner_user_id
            ? "Your machine borrows this credential from the server a few minutes at a time. A new secret takes effect on the next sync, and the one you replace stops working as the borrowed copies expire."
            : "Members borrow this credential from the server a few minutes at a time. A new secret takes effect on their next sync, and the one you replace stops working as the borrowed copies expire."}
        </p>
        <TextInput
          type="password"
          label="New service credential"
          description={
            connection?.owner_user_id
              ? "The replacement for the connector's password or token."
              : "The replacement for the connector's password or token. Members never see it."
          }
          required
          autoComplete="off"
          value={secret}
          disabled={rotate.isPending}
          onChange={(e) => setSecret(e.target.value)}
        />
        {error ? (
          <Callout tone="danger" title="Couldn't rotate the credential">
            {error}
          </Callout>
        ) : null}
      </Stack>
    </Modal>
  );
}
