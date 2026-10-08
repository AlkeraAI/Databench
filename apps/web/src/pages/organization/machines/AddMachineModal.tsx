// Adding a machine the org runs: its SSH details, a connection test that shows
// the host key fingerprint and whether the host can run the product, then Add. The
// add names the fingerprint the reader saw, and the server refuses it when the
// host presents another key. Editing a connection field clears the test.

import { useState } from "react";

import {
  Button,
  Callout,
  Inline,
  Modal,
  NumberInput,
  SegmentedControl,
  Stack,
  TextInput,
  Textarea,
} from "@alkera/ui";

import { ApiError } from "../../../api/errors";
import {
  NAME_TAKEN,
  useAddSshMachine,
  useTestSshMachine,
  type OrgMachineRead,
  type SshMachineTarget,
  type SshMachineTestRead,
} from "../../../api/machines";

import { AudiencePicker, toGrants, type AudienceDraft } from "./AudiencePicker";
import { mayChoosePool } from "./MachineActions";
import { COPY, errorText, hostKeyText, type MachineViewer } from "./model";
import styles from "./machines.module.css";

export const ADD_COPY = {
  title: "Add machine",
  test: "Test connection",
  submit: "Add machine",
  fingerprint: "Host key",
  fingerprintHint: "Check this matches the host before you add it.",
  nameTaken: "Another machine already has this name.",
  untested: "Test the connection first.",
  assignedHint: "People use an assigned machine by moving a workspace onto it.",
} as const;

type AuthKind = "password" | "private_key";

/** The facts a passing test found, in one line. */
export function hostLine(result: SshMachineTestRead): string {
  const parts = [`${result.os} ${result.arch}`.trim(), `${result.vcpu} vCPU`, `${result.memory_gb} GB memory`];
  if (result.gpu_count > 0) parts.push(`${result.gpu_count} GPU`);
  if (result.disk_gb > 0) parts.push(`${result.disk_gb} GB disk`);
  return parts.join(" · ");
}

export interface AddMachineModalProps {
  open: boolean;
  onClose: () => void;
  viewer: MachineViewer;
  onAdded: (machine: OrgMachineRead) => void;
}

export function AddMachineModal({ open, onClose, viewer, onAdded }: AddMachineModalProps) {
  const test = useTestSshMachine();
  const add = useAddSshMachine();
  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("22");
  const [username, setUsername] = useState("");
  const [authKind, setAuthKind] = useState<AuthKind>("private_key");
  const [password, setPassword] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [useMode, setUseMode] = useState<"assigned" | "pool">("assigned");
  const [audience, setAudience] = useState<AudienceDraft[]>(() =>
    viewer.userId ? [{ kind: "user", user_id: viewer.userId, label: "You" }] : [],
  );

  // A connection field changed: the last test no longer describes it.
  const connection = (set: (value: string) => void) => (value: string) => {
    if (test.data || test.error) test.reset();
    if (add.error) add.reset();
    set(value);
  };

  const portNumber = Number(port);
  const target: SshMachineTarget | null =
    host.trim() !== "" && username.trim() !== "" && Number.isInteger(portNumber) && portNumber >= 1 && portNumber <= 65535
      ? {
          host: host.trim(),
          port: portNumber,
          username: username.trim(),
          auth_kind: authKind,
          password: authKind === "password" ? password : null,
          private_key: authKind === "private_key" ? privateKey : null,
          passphrase: authKind === "private_key" && passphrase !== "" ? passphrase : null,
        }
      : null;
  const hasSecret = authKind === "password" ? password !== "" : privateKey.trim() !== "";
  const result = test.data ?? null;
  const passed = result !== null && result.reachable && result.prerequisites_met;
  const ready =
    passed && target !== null && name.trim() !== "" && (useMode === "pool" || audience.length > 0);

  const error = add.error instanceof ApiError ? add.error : null;
  const nameError = error?.status === 409 && error.code === NAME_TAKEN ? ADD_COPY.nameTaken : undefined;

  const runTest = () => {
    if (target && hasSecret) test.mutate(target);
  };
  const submit = () => {
    if (!ready || !target || !result?.host_key_fingerprint) return;
    add.mutate(
      {
        ...target,
        name: name.trim(),
        host_key_fingerprint: result.host_key_fingerprint,
        use_mode: useMode,
        audience: useMode === "pool" ? [] : toGrants(audience),
      },
      { onSuccess: onAdded },
    );
  };

  const footer = (
    <Inline gap={3} justify="flex-end" block>
      <Button variant="secondary" fill="ghost" onClick={onClose} disabled={add.isPending}>
        Cancel
      </Button>
      <Button variant="secondary" disabled={!target || !hasSecret} loading={test.isPending} onClick={runTest}>
        {ADD_COPY.test}
      </Button>
      <Button disabled={!ready} loading={add.isPending} onClick={submit}>
        {ADD_COPY.submit}
      </Button>
    </Inline>
  );

  return (
    <Modal open={open} onClose={onClose} title={ADD_COPY.title} size="lg" footer={footer} footerDivided>
      <Stack gap={4} align="stretch">
        <TextInput label="Name" value={name} maxLength={64} error={nameError} onChange={(e) => setName(e.target.value)} />
        <Inline gap={3} align="flex-start" block>
          <TextInput
            label="Host"
            placeholder="gpu-1.example.com"
            value={host}
            onChange={(e) => connection(setHost)(e.target.value)}
            rootStyle={{ flex: 1 }}
          />
          <NumberInput
            label="Port"
            mode="integer"
            value={port}
            onValueChange={connection(setPort)}
            rootStyle={{ maxWidth: 120 }}
          />
        </Inline>
        <TextInput label="Username" value={username} onChange={(e) => connection(setUsername)(e.target.value)} />
        <SegmentedControl
          label="Sign in with"
          options={[
            { key: "private_key", label: "Private key" },
            { key: "password", label: "Password" },
          ]}
          value={authKind}
          onChange={(key) => connection((v) => setAuthKind(v as AuthKind))(key)}
          semantics="radio"
        />
        {authKind === "password" ? (
          <TextInput
            label="Password"
            type="password"
            autoComplete="off"
            value={password}
            onChange={(e) => connection(setPassword)(e.target.value)}
          />
        ) : (
          <>
            <Textarea
              label="Private key"
              placeholder="Paste the private key"
              autoComplete="off"
              spellCheck={false}
              rows={5}
              value={privateKey}
              onChange={(e) => connection(setPrivateKey)(e.target.value)}
            />
            <TextInput
              label="Passphrase"
              type="password"
              autoComplete="off"
              requiredMark={false}
              placeholder="None"
              value={passphrase}
              onChange={(e) => connection(setPassphrase)(e.target.value)}
            />
          </>
        )}
        <TestResult result={result} error={test.error} />
        {mayChoosePool(viewer) ? (
          <SegmentedControl
            label="Use"
            options={[
              { key: "assigned", label: "Assigned" },
              { key: "pool", label: COPY.orgPool },
            ]}
            value={useMode}
            onChange={(key) => setUseMode(key as "assigned" | "pool")}
            semantics="radio"
          />
        ) : null}
        {useMode === "assigned" ? (
          <div className={styles.field}>
            <span className={styles.fieldLabel}>Who can use it</span>
            <span className="alk-meta">{ADD_COPY.assignedHint}</span>
            <AudiencePicker value={audience} onChange={setAudience} viewer={viewer} disabled={add.isPending} />
          </div>
        ) : null}
        {add.error && !nameError ? (
          <Callout tone="danger" role="alert">
            {errorText(add.error, "The machine could not be added.")}
          </Callout>
        ) : null}
        {!passed && result === null && !test.error ? <p className="alk-meta">{ADD_COPY.untested}</p> : null}
      </Stack>
    </Modal>
  );
}

/** What the last connection test found: the fingerprint and the host's facts,
 *  what it lacks, or why it could not connect. */
function TestResult({ result, error }: { result: SshMachineTestRead | null; error: unknown }) {
  if (error) {
    return (
      <Callout tone="danger" role="alert">
        {errorText(error, "The connection could not be tested.")}
      </Callout>
    );
  }
  if (!result) return null;
  if (!result.reachable) {
    return (
      <Callout tone="danger" role="alert">
        {result.message ?? "Couldn't connect to the host."}
      </Callout>
    );
  }
  return (
    <Callout tone={result.prerequisites_met ? "success" : "warning"} data-testid="ssh-test-result">
      <Stack gap={1} align="stretch">
        <span>
          {ADD_COPY.fingerprint}{" "}
          <code>{hostKeyText(result.host_key_type, result.host_key_fingerprint ?? "")}</code>
        </span>
        <span className="alk-meta">{ADD_COPY.fingerprintHint}</span>
        <span>{hostLine(result)}</span>
        {result.missing?.length ? <span>The host needs {result.missing.join(", ")}.</span> : null}
        {result.message ? <span>{result.message}</span> : null}
      </Stack>
    </Callout>
  );
}
