// The reader's own preferences, in the browser.
//
// The same document the editor's Settings pane edits, reached over the server
// instead of the daemon — so a person who only ever uses the web has the
// controls a desktop user has had all along: which model a new chat opens on,
// at what reasoning effort, and in which permission stance.
//
// Two rules the surface follows rather than documents:
//
//   * it offers only what the WEB acts on. The shared table (`@/lib/preferences`)
//     says which surfaces honour each control, and a toggle nothing reads is
//     worse than a missing one — it tells the person they changed something
//     when nothing changed.
//   * a save re-seeds every open composer immediately (the mutation declares
//     the chat family), because a default nobody's next chat used would be a
//     setting in name only.

import { useEffect, useMemo, useState } from "react";

import { PERMISSION_MODE_VALUES } from "@alkera/chat-model";
import { Select, Stack } from "@alkera/ui";

import { useMyChatModels } from "@/api/chatModels";
import { useMyPreferences, useSetMyPreferences, type PreferenceValues } from "@/api/preferences";
import { groupsFor, type Control } from "@/lib/preferences/fields";

import {
  LoadError,
  SaveBar,
  SettingsDoc,
  SettingsSection,
  SettingsSkeleton,
  ToggleRow,
  errText,
} from "@/pages/organization/settings/fields";
import shell from "@/pages/organization/settings/shell.module.css";
import type { Notify } from "../../../app/notify";

/** The controls the web honours, with the stance picker narrowed to the modes a
 *  cloud chat may actually start in — the picker must not offer a stance the
 *  server would refuse. A pure function of the shared table, so it is computed
 *  once. */
const WEB_GROUPS = groupsFor("web", { default_permission_mode: PERMISSION_MODE_VALUES });

/** The "let the workspace decide" row. A person who has never chosen is not
 *  pinned to whatever the catalog happens to list first, and can go back to
 *  that state after choosing. */
const NO_PREFERENCE = "";

function asString(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return "";
}

/** Preferences — the reader's OWN settings page, reached from the account menu
 *  rather than the nav rail: it is not the organization's ledger and never asks
 *  for admin. The page is the shared settings document around the body below. */
export function PreferencesPage() {
  return (
    <SettingsDoc>
      {(notify) => <PreferencesBody notify={notify} />}
    </SettingsDoc>
  );
}

export function PreferencesBody({ notify }: { notify: Notify }) {
  const stored = useMyPreferences();
  const models = useMyChatModels();
  const save = useSetMyPreferences();

  // The edit buffer, re-seeded whenever the server's answer changes — so a save
  // made in another tab is not silently overwritten by a buffer this tab has
  // been holding since before it.
  const [draft, setDraft] = useState<PreferenceValues | null>(null);
  const serverValues = stored.data;
  useEffect(() => {
    if (serverValues) setDraft({ ...serverValues });
  }, [serverValues]);

  const values: PreferenceValues = draft ?? serverValues ?? {};
  // Dirty means "a save would change something", so the comparison is over the
  // fields this page can actually send, normalized exactly as the PATCH
  // normalizes them. Two reasons it is not a plain walk of the draft's keys:
  // a document that simply OMITS a preference (a reader who has never set one)
  // held `undefined` where a picked-then-unpicked control writes `null`, which
  // left the page claiming unsaved changes with every control back where it
  // started and a save that would have written nothing; and a key this build
  // does not edit — `schema_version`, or a field a newer client owns — must
  // never raise the bar, because no button here can put it back.
  const dirty = useMemo(() => {
    if (!draft || !serverValues) return false;
    const next = editableFields(draft);
    const saved = editableFields(serverValues);
    // Every editable preference is a scalar or null (see the control table), so
    // identity is equality; a structured field would need its own comparison.
    return EDITABLE_KEYS.some((key) => !Object.is(next[key], saved[key]));
  }, [draft, serverValues]);

  const set = (key: string, value: unknown): void =>
    setDraft((current) => ({ ...(current ?? serverValues ?? {}), [key]: value }));

  const catalog = models.data ?? [];
  const chosenModel = asString(values.default_chat_model);
  // The efforts belong to the CHOSEN model, so a variant one model offers is
  // never presented for another that does not.
  const efforts = catalog.find((model) => model.id === chosenModel)?.efforts ?? [];
  const chosenEffort = asString(values.default_chat_effort);

  if (stored.isPending) return <SettingsSkeleton sections={2} />;
  if (stored.isError) {
    return (
      <LoadError
        message={errText(stored.error, "Couldn't load your preferences.")}
        onRetry={() => void stored.refetch()}
      />
    );
  }

  const commit = (): void =>
    save.mutate(editableFields(values), {
      onSuccess: () => notify.success("Preferences saved"),
      onError: (err) => notify.error(errText(err, "Couldn't save your preferences.")),
    });

  return (
    <>
      <SettingsSection id="chat-defaults" icon="chats" title="New chats">
        <Stack gap={5} align="stretch">
          <Select
            label="Default model"
            description={
              models.isError || catalog.length === 0
                ? "The model catalog is unavailable. Your saved default is unchanged."
                : "The model a chat you start on the web opens on."
            }
            className={shell.capped}
            value={chosenModel}
            disabled={models.isPending}
            onChange={(event) => {
              // The effort belongs to the model; keeping the old one would pin
              // a variant the new model may not offer.
              const next = event.currentTarget.value;
              setDraft((current) => ({
                ...(current ?? serverValues ?? {}),
                default_chat_model: next || null,
                default_chat_effort: null,
              }));
            }}
          >
            <option value={NO_PREFERENCE}>Let the workspace choose</option>
            {catalog.map((model) => (
              <option key={model.id} value={model.id}>
                {model.display_name}
              </option>
            ))}
            {/* A saved model the catalog no longer offers still shows, so the
                person sees what they are pinned to instead of a blank. */}
            {chosenModel && !catalog.some((model) => model.id === chosenModel) ? (
              <option value={chosenModel} disabled>
                {chosenModel}
              </option>
            ) : null}
          </Select>
          {efforts.length > 0 ? (
            <Select
              label="Default reasoning effort"
              description="How hard the chosen model thinks before it answers."
              className={shell.capped}
              value={chosenEffort}
              onChange={(event) => set("default_chat_effort", event.currentTarget.value || null)}
            >
              <option value={NO_PREFERENCE}>Let the model choose</option>
              {efforts.map((effort) => (
                <option key={effort} value={effort}>
                  {titleCase(effort)}
                </option>
              ))}
            </Select>
          ) : null}
        </Stack>
      </SettingsSection>

      {WEB_GROUPS.map((group) => (
        <SettingsSection
          key={group.title}
          id={`prefs-${group.title.toLowerCase()}`}
          icon="settings"
          title={group.title}
        >
          <Stack gap={5} align="stretch">
            {group.controls.map((control) => (
              <ControlRow
                key={control.key}
                control={control}
                value={values[control.key]}
                onChange={(next) => set(control.key, next)}
              />
            ))}
          </Stack>
        </SettingsSection>
      ))}

      <SaveBar
        dirty={dirty}
        saving={save.isPending}
        onSave={commit}
        onDiscard={() => setDraft(serverValues ? { ...serverValues } : null)}
      />
    </>
  );
}

function ControlRow({
  control,
  value,
  onChange,
}: {
  control: Control;
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  if (control.kind === "toggle") {
    return (
      <ToggleRow
        label={control.label}
        help={control.description}
        checked={value === true}
        onChange={onChange}
      />
    );
  }
  const current = asString(value);
  const known = control.options.some((option) => option.value === current);
  return (
    <Select
      label={control.label}
      description={control.description}
      className={shell.capped}
      value={current}
      onChange={(event) =>
        onChange(
          control.kind === "duration"
            ? Number(event.currentTarget.value)
            : event.currentTarget.value,
        )
      }
    >
      {control.options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
      {/* A value a newer client wrote is shown rather than blanked, and is not
          offered as a pick — this page must never be the thing that silently
          rewrites a setting it does not understand. */}
      {!known && current !== "" ? (
        <option value={current} disabled>
          {current}
        </option>
      ) : null}
    </Select>
  );
}

/** Every preference this page can write — the PATCH's key set, and the only
 *  keys a difference in may raise the save bar. */
const EDITABLE_KEYS: readonly string[] = [
  "default_chat_model",
  "default_chat_effort",
  ...WEB_GROUPS.flatMap((group) => group.controls.map((control) => control.key)),
];

/** The fields this page edits, as a PATCH.
 *
 *  Only these: a body that echoed the whole document back would re-write every
 *  key — including ones a newer client owns — with the values this build
 *  happened to read. A preference the document omits is sent as `null`, the
 *  same shape the "no preference" pick writes, so the two are never mistaken
 *  for a change. */
function editableFields(values: PreferenceValues): PreferenceValues {
  return Object.fromEntries(EDITABLE_KEYS.map((key) => [key, values[key] ?? null]));
}

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
