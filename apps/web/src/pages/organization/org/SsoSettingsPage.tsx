import { useState } from "react";

import { Callout, Button, Card, ConfirmDialog, CopyReading, Inline, Stack, cx, SegmentedControl, Select, Switch, Textarea, TextInput } from "@alkera/ui";

import { Icon } from "../../../app/icons";
import {
  useMintScimToken,
  useRevokeScimToken,
  useSsoConfig,
  useUpdateSsoConfig,
  type SsoUpdate,
} from "../../../api/orgAdminSso";
import { OrgLoadError, OrgLoading, OrgMutationError, OrgPage } from "./chrome";
import { UnavailableFeature, useFeatureGate } from "./FeatureGate";
import styles from "./SsoSettingsPage.module.css";
import type { Notify } from "../../../app/notify";
import { refusalSentence } from "../../../api/errors";

type Protocol = "oidc" | "saml";
type Role = "admin" | "member";
interface MapRow {
  group: string;
  role: Role;
}

const PROTOCOLS = [
  { key: "oidc", label: "OIDC" },
  { key: "saml", label: "SAML" },
] as const;

export function SsoSettingsPage() {
  // Gated by the server: an org it refuses sees the unavailable plate, and
  // SsoBody (which fetches the now-403 config) is never mounted.
  const gate = useFeatureGate();
  return (
    <OrgPage subtitle="Connect your identity provider and provision members automatically.">
      {(notify) =>
        gate.status === "loading" ? (
          <OrgLoading sections={2} />
        ) : gate.status === "error" ? (
          <OrgLoadError message="could not check your access" onRetry={gate.refetch} />
        ) : gate.status === "gated" ? (
          <UnavailableFeature
            feature="Single sign-on"
            blurb="SSO/SAML & SCIM"
            icon={<Icon name="shield" size={48} />}
          />
        ) : (
          <SsoBody notify={notify} />
        )
      }
    </OrgPage>
  );
}

function SsoBody({ notify }: { notify: Notify }) {
  const config = useSsoConfig();
  if (config.isPending) return <OrgLoading sections={2} />;
  if (config.isError || !config.data) {
    return <OrgLoadError message={refusalSentence(config.error, { fallback: "could not load SSO settings" })} onRetry={() => void config.refetch()} />;
  }
  // Key on the loaded config so the editable form seeds once from server state. A delimiter keeps
  // the parts unambiguous, so two different (protocol, domains) pairs can't collide into one key.
  return <SsoForm key={`${config.data.protocol}|${config.data.allowed_domains}`} notify={notify} />;
}

function SsoForm({ notify }: { notify: Notify }) {
  const config = useSsoConfig();
  const save = useUpdateSsoConfig();
  const mintScim = useMintScimToken();
  const revokeScim = useRevokeScimToken();
  const data = config.data!;
  const domains = (data.allowed_domains ?? "").split(",").filter(Boolean);

  const [protocol, setProtocol] = useState<Protocol>((data.protocol as Protocol) || "oidc");
  const [enabled, setEnabled] = useState(data.enabled);
  const [enforced, setEnforced] = useState(data.enforced);
  const [issuer, setIssuer] = useState(data.oidc_issuer ?? "");
  const [clientId, setClientId] = useState(data.oidc_client_id ?? "");
  const [clientSecret, setClientSecret] = useState("");
  const [idpEntityId, setIdpEntityId] = useState(data.saml_idp_entity_id ?? "");
  const [ssoUrl, setSsoUrl] = useState(data.saml_sso_url ?? "");
  const [cert, setCert] = useState("");
  const [mappingRows, setMappingRows] = useState<MapRow[]>(
    Object.entries(data.groups_mapping ?? {}).map(([group, role]) => ({ group, role: role as Role })),
  );
  const [scimSecret, setScimSecret] = useState<string | null>(null);

  const hasSecret = data.has_client_secret;
  const hasCert = data.has_saml_cert;

  const addRow = () => setMappingRows((rows) => [...rows, { group: "", role: "member" }]);
  const removeRow = (i: number) => setMappingRows((rows) => rows.filter((_, idx) => idx !== i));
  const updateRow = (i: number, patch: Partial<MapRow>) =>
    setMappingRows((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));

  const secretMissing = enabled && protocol === "oidc" && !hasSecret && !clientSecret;
  const certMissing = enabled && protocol === "saml" && !hasCert && !cert.trim();
  const ready =
    protocol === "oidc"
      ? Boolean(issuer && clientId)
      : Boolean(idpEntityId && ssoUrl && (hasCert || cert.trim()));

  const submit = () => {
    const mapping: Record<string, Role> = {};
    for (const row of mappingRows) {
      const g = row.group.trim();
      if (g) mapping[g] = row.role;
    }
    const base = {
      protocol,
      enabled,
      enforced,
      groups_mapping: mapping,
    };
    const body: SsoUpdate =
      protocol === "oidc"
        ? { ...base, oidc_issuer: issuer.trim(), oidc_client_id: clientId.trim(), oidc_client_secret: clientSecret || null }
        : { ...base, saml_idp_entity_id: idpEntityId.trim(), saml_sso_url: ssoUrl.trim(), saml_x509_cert: cert.trim() || null };
    save.mutate(body, {
      onSuccess: () => {
        setClientSecret("");
        setCert("");
        notify.success("SSO settings saved.");
      },
    });
  };

  return (
    <>
      <Card variant="section" icon={<Icon name="shield" />} title="Single sign-on">
        <Stack gap={7} align="stretch">
          <SegmentedControl
            options={PROTOCOLS}
            value={protocol}
            onChange={(v) => setProtocol(v as Protocol)}
            label="Protocol"
          />

          {protocol === "oidc" ? (
            <>
              <TextInput label="Issuer URL" placeholder="https://your-org.okta.com" value={issuer} onChange={(e) => setIssuer(e.target.value)} />
              <TextInput label="Client ID" value={clientId} onChange={(e) => setClientId(e.target.value)} />
              <TextInput
                label={hasSecret ? "Client secret (leave blank to keep current)" : "Client secret"}
                type="password"
                placeholder={hasSecret ? "••••••••" : ""}
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
              />
            </>
          ) : (
            <>
              <Callout tone="neutral" role="note" title="Register these with your IdP">
                <Stack gap={5} align="stretch">
                  <CopyField label="SP Entity ID / Audience" value={data.saml_sp_entity_id ?? ""} />
                  <CopyField label="ACS URL (Reply URL)" value={data.saml_acs_url ?? ""} />
                </Stack>
              </Callout>
              <TextInput
                label="IdP Entity ID (Issuer)"
                placeholder="https://idp.your-org.com/saml/metadata"
                value={idpEntityId}
                onChange={(e) => setIdpEntityId(e.target.value)}
              />
              <TextInput
                label="IdP SSO URL"
                placeholder="https://idp.your-org.com/saml/sso"
                value={ssoUrl}
                onChange={(e) => setSsoUrl(e.target.value)}
              />
              <Textarea
                label={hasCert ? "IdP signing certificate (leave blank to keep current)" : "IdP signing certificate (X.509)"}
                rows={4}
                placeholder={hasCert ? "•••• (a certificate is stored)" : "-----BEGIN CERTIFICATE-----"}
                value={cert}
                onChange={(e) => setCert(e.target.value)}
              />
            </>
          )}

          <EmailDomains domains={domains} />
          <Switch label="Enable SSO for these domains" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <Switch
            label="Require SSO (hide password sign-in)"
            checked={enforced}
            disabled={!enabled}
            onChange={(e) => setEnforced(e.target.checked)}
          />
          {secretMissing ? <Callout tone="warning">A client secret is required before SSO can be enabled.</Callout> : null}
          {certMissing ? <Callout tone="warning">An IdP certificate is required before SSO can be enabled.</Callout> : null}

          <MappingEditor rows={mappingRows} onAdd={addRow} onRemove={removeRow} onUpdate={updateRow} />

          <Inline gap={3} justify="flex-end">
            <Button loading={save.isPending} disabled={!ready} onClick={submit}>
              Save
            </Button>
          </Inline>
          <OrgMutationError error={save.error} />
        </Stack>
      </Card>

      {/* No toast from this card: the token callout and the status line say what happened, in
          place, and a toast over the card's own buttons hid the one a reader reached for next. */}
      <ScimCard
        hasToken={data.has_scim_token}
        baseUrl={data.scim_base_url ?? ""}
        secret={scimSecret}
        minting={mintScim.isPending}
        revoking={revokeScim.isPending}
        error={mintScim.error ?? revokeScim.error}
        onMint={(done) =>
          mintScim.mutate(undefined, {
            onSuccess: (r) => setScimSecret(r.token),
            // A refusal closes the dialog too, so the card's error line is not hidden behind it.
            onSettled: done,
          })
        }
        onRevoke={(done) =>
          revokeScim.mutate(undefined, {
            onSuccess: () => setScimSecret(null),
            onSettled: done,
          })
        }
      />
    </>
  );
}

/** The email domains this IdP signs in. Platform staff assign them, so they are read-only here. */
function EmailDomains({ domains }: { domains: string[] }) {
  return (
    <Stack gap={2} align="stretch">
      <span className="alk-strong">Email domains</span>
      {domains.length > 0 ? (
        <ul className={styles.domains} aria-label="Email domains">
          {domains.map((domain) => (
            <li key={domain} className="alk-code">
              {domain}
            </li>
          ))}
        </ul>
      ) : (
        <span className="alk-meta">No domains assigned.</span>
      )}
      <span className="alk-meta">A platform administrator adds and removes these.</span>
    </Stack>
  );
}

/** The group → role mapping editor. */
function MappingEditor({
  rows,
  onAdd,
  onRemove,
  onUpdate,
}: {
  rows: MapRow[];
  onAdd: () => void;
  onRemove: (i: number) => void;
  onUpdate: (i: number, patch: Partial<MapRow>) => void;
}) {
  return (
    <Stack gap={7} align="stretch">
      <div>
        <span className="alk-strong">Group → role mapping</span>
        <p className={cx(styles.formHelp, "alk-meta")}>
          Map an IdP group to an org role. A member of a mapped group is assigned that role on each sign-in (admin
          wins). Leave empty to provision everyone as a member.
        </p>
      </div>
      {rows.map((row, i) => (
        <Inline gap={4} align="flex-end" wrap={false} key={i}>
          <TextInput
            label={i === 0 ? "IdP group" : undefined}
            aria-label="IdP group"
            placeholder="alkera-admins"
            value={row.group}
            rootStyle={{ flex: "1 1 auto", minWidth: 0 }}
            onChange={(e) => onUpdate(i, { group: e.target.value })}
          />
          <Select
            label={i === 0 ? "Org role" : undefined}
            aria-label="Org role"
            value={row.role}
            rootStyle={{ flex: "0 0 140px" }}
            onChange={(e) => onUpdate(i, { role: e.target.value as Role })}
          >
            <option value="admin">Admin</option>
            <option value="member">Member</option>
          </Select>
          <Button iconOnly variant="secondary" fill="ghost" aria-label="Remove mapping" onClick={() => onRemove(i)}>
            <Icon name="trash" size={15} />
          </Button>
        </Inline>
      ))}
      <div>
        <Button variant="secondary" leftSection={<Icon name="plus" size={15} />} onClick={onAdd}>
          Add mapping
        </Button>
      </div>
    </Stack>
  );
}

/** What each confirmation says. Shared with the test so the copy and the assertions never drift. */
export const SCIM_CONFIRM = {
  rotate: {
    title: "Rotate the SCIM token?",
    consequence: "The current token stops working now. Provisioning resumes once your IdP has the new one.",
    confirmLabel: "Rotate token",
  },
  revoke: {
    title: "Revoke the SCIM token and turn off provisioning?",
    consequence: "Your IdP can no longer create, update or deactivate members until a new token is generated.",
    confirmLabel: "Revoke and disable",
  },
} as const;

/** The SCIM provisioning card: base URL, token status, mint/rotate/revoke.
 *
 *  Rotating and revoking both cut the IdP off the moment they land, so each asks first. The token
 *  is shown once, with its own copy control. */
function ScimCard({
  hasToken,
  baseUrl,
  secret,
  minting,
  revoking,
  error,
  onMint,
  onRevoke,
}: {
  hasToken: boolean;
  baseUrl: string;
  secret: string | null;
  minting: boolean;
  revoking: boolean;
  error: unknown;
  onMint: (done: () => void) => void;
  onRevoke: (done: () => void) => void;
}) {
  const [confirming, setConfirming] = useState<"rotate" | "revoke" | null>(null);
  const close = () => setConfirming(null);
  const confirm = confirming ? SCIM_CONFIRM[confirming] : null;
  return (
    <Card
      variant="section"
      icon={<Icon name="users" />}
      title="Automated provisioning (SCIM 2.0)"
    >
      <Stack gap={7} align="stretch">
        <p className={cx(styles.formHelp, "alk-meta")}>
          Let your IdP create, update, and deactivate users automatically. Point its SCIM connector at the base URL
          below with a bearer token.
        </p>
        <CopyField label="SCIM base URL" value={baseUrl} />
        <span className={cx("alk-meta", hasToken && "alk-success")} role="status">
          {hasToken ? "A SCIM token is active." : "No SCIM token is active."}
        </span>
        {secret ? (
          <Callout tone="warning" title="SCIM bearer token (shown once)">
            <Stack gap={2} align="stretch">
              <CopyReading size="sm" precious value={secret} copyLabel="Copy SCIM token" />
              <span>Paste this into your IdP's SCIM connector. It won't be shown again, so rotate it if you lose it.</span>
            </Stack>
          </Callout>
        ) : null}
        <div className="alk-inline">
          <Button
            variant="secondary"
            loading={minting}
            onClick={() => (hasToken ? setConfirming("rotate") : onMint(close))}
          >
            {hasToken ? "Rotate token" : "Generate token"}
          </Button>
          {hasToken ? (
            <Button variant="destructive" fill="outline" loading={revoking} onClick={() => setConfirming("revoke")}>
              Revoke and disable
            </Button>
          ) : null}
        </div>
        <OrgMutationError error={error} />
      </Stack>
      <ConfirmDialog
        open={confirm !== null}
        onClose={close}
        onConfirm={() => (confirming === "revoke" ? onRevoke(close) : onMint(close))}
        title={confirm?.title ?? ""}
        consequence={confirm?.consequence}
        confirmLabel={confirm?.confirmLabel ?? ""}
        tone={confirming === "revoke" ? "destructive" : "warning"}
        busy={confirming === "revoke" ? revoking : minting}
      />
    </Card>
  );
}

/** A labelled copyable mono reading (entity id / ACS / SCIM base URL) — the shared CopyReading. */
function CopyField({ label, value }: { label: string; value: string }) {
  return (
    <Stack gap={2} align="stretch">
      <span className="alk-strong">{label}</span>
      <CopyReading size="sm" value={value || "—"} copyLabel={`Copy ${label.toLowerCase()}`} />
    </Stack>
  );
}
