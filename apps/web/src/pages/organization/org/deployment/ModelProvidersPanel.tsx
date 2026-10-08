import { useState } from "react";

import {
  Button,
  Callout,
  Card,
  Inline,
  Pill,
  SegmentedControl,
  Stack,
  Switch,
  TextInput,
  type PillTone,
} from "@alkera/ui";

import { Icon } from "../../../../app/icons";
import {
  useModelProviders,
  useRemoveModelProvider,
  useTestModelProvider,
  useUpdateModelProvider,
  type ModelProvider,
  type ModelProviderUpdate,
  type ProviderName,
} from "../../../../api/orgAdminModelProviders";
import { OrgLoadError, OrgLoading } from "../chrome";
import { formatDateTime } from "@/lib/format/date";
import { errorSentence, type Notify } from "../../../../app/notify";
import { refusalSentence } from "../../../../api/errors";

const PROVIDER_LABEL: Record<ProviderName, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  bedrock: "AWS Bedrock",
};
const KEY_PREFIX: Record<ProviderName, string> = {
  anthropic: "sk-ant-",
  openai: "sk-",
  bedrock: "AKIA",
};
const VERIFY_TONE: Record<string, PillTone> = {
  ok: "success",
  invalid_key: "danger",
  permission: "warning",
  network: "warning",
};

export function ModelProvidersPanel({ notify }: { notify: Notify }) {
  const providers = useModelProviders();
  if (providers.isPending) return <OrgLoading sections={3} />;
  if (providers.isError || !providers.data) {
    // A 404 here means the BYOK feature isn't entitled — render as not-available.
    return (
      <OrgLoadError
        message={refusalSentence(providers.error, { fallback: "provider settings are unavailable" })}
        onRetry={() => void providers.refetch()}
      />
    );
  }
  return (
    <Stack gap={5} align="stretch">
      <Callout tone="info" title="Bring your own provider keys.">
        Requests are billed directly to your provider accounts. Keys are encrypted at rest and never
        shown again after saving. Traffic goes straight from your gateway to the provider.
      </Callout>
      {providers.data.providers.map((p) => (
        <ProviderCard key={p.provider} provider={p} notify={notify} />
      ))}
    </Stack>
  );
}

function ProviderCard({ provider, notify }: { provider: ModelProvider; notify: Notify }) {
  const update = useUpdateModelProvider();
  const remove = useRemoveModelProvider();
  const test = useTestModelProvider();
  const [editing, setEditing] = useState(false);

  const name = PROVIDER_LABEL[provider.provider];
  const masked = provider.secret_hint ? `${KEY_PREFIX[provider.provider]}…${provider.secret_hint.replace("…", "")}` : null;

  const verify = provider.last_verified_status;
  return (
    <Card variant="section" title={name} divided>
      <Stack gap={3} align="stretch">
        {provider.configured ? (
          <Inline justify="space-between" align="center">
            <Stack gap={0}>
              {masked ? <span className="alk-num">{masked}</span> : <span className="alk-caption">configured</span>}
              {verify ? (
                <Inline gap={2} align="center">
                  <Pill tone={VERIFY_TONE[verify] ?? "neutral"} shape="rect" dot>
                    {verify === "ok" ? "Verified" : verify}
                  </Pill>
                  {provider.last_verified_at ? (
                    <span className="alk-meta">checked {formatDateTime(provider.last_verified_at)}</span>
                  ) : null}
                </Inline>
              ) : (
                <span className="alk-meta">not yet tested</span>
              )}
            </Stack>
            <Switch
              checked={provider.enabled}
              onChange={(e) =>
                update.mutate(
                  { provider: provider.provider, body: { enabled: e.target.checked } },
                  { onError: () => notify.error("Could not update the provider.") },
                )
              }
              aria-label={`Enable ${name}`}
            />
          </Inline>
        ) : provider.env_fallback ? (
          <Callout tone="neutral">Configured via environment on this instance.</Callout>
        ) : (
          <span className="alk-caption">Not configured.</span>
        )}

        {editing ? (
          <ProviderForm
            provider={provider}
            busy={update.isPending}
            onCancel={() => setEditing(false)}
            onSave={(body) =>
              update.mutate(
                { provider: provider.provider, body },
                {
                  onSuccess: () => {
                    setEditing(false);
                    notify.success("Provider saved.");
                  },
                  onError: (e) => notify.error(errorSentence(e, "Could not save the provider.")),
                },
              )
            }
          />
        ) : (
          <Inline gap={2}>
            <Button variant="secondary" onClick={() => setEditing(true)}>
              {provider.configured ? "Edit" : "Configure"}
            </Button>
            {provider.configured ? (
              <>
                <Button
                  variant="secondary"
                  fill="ghost"
                  loading={test.isPending}
                  onClick={() =>
                    test.mutate(provider.provider, {
                      onSuccess: (r) =>
                        r.ok ? notify.success("Connection OK.") : notify.error(r.detail ?? "Test failed."),
                      onError: () => notify.error("Could not test the connection."),
                    })
                  }
                >
                  Test
                </Button>
                <Button
                  variant="secondary"
                  fill="ghost"
                  loading={remove.isPending}
                  onClick={() =>
                    remove.mutate(provider.provider, {
                      onSuccess: () => notify.success("Provider removed."),
                      onError: () => notify.error("Could not remove the provider."),
                    })
                  }
                >
                  Remove
                </Button>
              </>
            ) : null}
          </Inline>
        )}
      </Stack>
    </Card>
  );
}

function ProviderForm({
  provider,
  busy,
  onSave,
  onCancel,
}: {
  provider: ModelProvider;
  busy: boolean;
  onSave: (body: ModelProviderUpdate) => void;
  onCancel: () => void;
}) {
  const configured = provider.configured;
  const keyPlaceholder = configured ? "Leave blank to keep the current key" : "";
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(provider.base_url ?? "");
  const [orgId, setOrgId] = useState(provider.openai_organization_id ?? "");
  const [region, setRegion] = useState(provider.bedrock_region ?? "");
  const [authMode, setAuthMode] = useState<"iam" | "access_key">(provider.bedrock_auth_mode ?? "iam");
  const [accessKeyId, setAccessKeyId] = useState("");
  const [secretKey, setSecretKey] = useState("");

  const isBedrock = provider.provider === "bedrock";

  const submit = () => {
    if (isBedrock) {
      onSave({
        enabled: provider.enabled,
        bedrock_region: region.trim(),
        bedrock_auth_mode: authMode,
        aws_access_key_id: authMode === "access_key" && accessKeyId.trim() ? accessKeyId.trim() : null,
        aws_secret_access_key: authMode === "access_key" && secretKey.trim() ? secretKey.trim() : null,
      });
    } else {
      onSave({
        enabled: provider.enabled,
        api_key: apiKey.trim() || null,
        base_url: baseUrl.trim() || null,
        openai_organization_id: provider.provider === "openai" ? orgId.trim() || null : undefined,
      });
    }
  };

  return (
    <Stack gap={3} align="stretch">
      {isBedrock ? (
        <>
          <SegmentedControl
            label="Authentication"
            value={authMode}
            onChange={(v) => setAuthMode(v as "iam" | "access_key")}
            options={[
              { key: "iam", label: "IAM role (recommended)" },
              { key: "access_key", label: "Access keys" },
            ]}
          />
          <TextInput label="Region" value={region} onChange={(e) => setRegion(e.target.value)} placeholder="us-east-1" />
          {authMode === "iam" ? (
            <span className="alk-caption">Uses the gateway host's ambient AWS role. The connection test verifies that role.</span>
          ) : (
            <>
              <TextInput label="Access key ID" value={accessKeyId} onChange={(e) => setAccessKeyId(e.target.value)} placeholder={configured ? "Leave blank to keep" : "AKIA…"} />
              <TextInput label="Secret access key" type="password" value={secretKey} onChange={(e) => setSecretKey(e.target.value)} placeholder={configured ? "Leave blank to keep" : ""} />
            </>
          )}
        </>
      ) : (
        <>
          <TextInput label="API key" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder={keyPlaceholder} />
          <TextInput
            label="Base URL (optional)"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="provider default"
            description={configured && !apiKey.trim() ? "Re-enter the API key to change the endpoint." : undefined}
          />
          {provider.provider === "openai" ? (
            <TextInput label="Organization ID (optional)" value={orgId} onChange={(e) => setOrgId(e.target.value)} />
          ) : null}
        </>
      )}
      <Inline gap={2}>
        <Button leftSection={<Icon name="key" size={15} />} loading={busy} onClick={submit}>
          Save
        </Button>
        <Button variant="secondary" fill="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </Inline>
    </Stack>
  );
}
