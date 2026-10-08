// The generic connection-form field renderer — ONE renderer for every surface
// (VS Code webview + browser portal), driven entirely by a ConnectionFormView.
//
// Purely presentational: no fetches, no daemon, no vscode imports. The host
// wires side effects — `pickPath` is the optional native file picker (absent in
// the browser, where file/directory fields degrade to a plain text input), and
// submit/probe UX belongs to the caller's shell around these fields.

import { Fragment, useId, useState, type ComponentType, type ReactNode } from "react";

import {
  IconCloud,
  IconDatabase,
  IconFileText,
  IconFolder,
  IconKey,
  IconLink,
  IconLock,
  IconShieldLock,
  IconWorld,
  type IconProps,
} from "@tabler/icons-react";

import {
  Button,
  Callout,
  Dropdown,
  DropdownItem,
  FieldShell,
  Inline,
  Switch,
  Textarea,
  TextInput,
} from "../../primitives";
import { isPemField, orderedRows, retainedValuesOnMethodSwitch, selectedMethod } from "../helpers";
import type {
  AuthMethodView,
  ConnectionDraft,
  ConnectionFormView,
  FormFieldView,
  PickPathFn,
} from "../types";

import "./connection-form.css";

export interface ConnectionFormFieldsProps {
  form: ConnectionFormView;
  draft: ConnectionDraft;
  onDraftChange: (draft: ConnectionDraft) => void;
  disabled?: boolean;
  /** Host-native path picker; omit in the browser (plain text input instead). */
  pickPath?: PickPathFn | null;
  /** The handle input's label/description (defaults fit the workspace form). */
  handleLabel?: string;
  handleDescription?: string;
  /** Lock the handle (edit flows — identity is immutable). */
  handleDisabled?: boolean;
  /** Drop the handle input entirely. For a form that fills in the missing half
   *  of a connection somebody else already named. */
  hideHandle?: boolean;
  /** Filter the auth-method picker (the portal shows team-capable methods only). */
  methodFilter?: (method: AuthMethodView) => boolean;
  /** Rendered between the method picker and the fields (e.g. an OAuth callout). */
  belowMethodSlot?: ReactNode;
  /** Suppress the schema-level note banner. The notes are written for the
   *  WORKSPACE form ("run gcloud auth application-default login first") — the
   *  portal's preconfiguration dialog hides them and supplies its own copy. */
  hideNote?: boolean;
  /** What is wrong with the name typed so far, shown on the field itself so the
   *  admin reads it where they are looking rather than below the fold. */
  handleError?: ReactNode;
  /** A control rendered in a second column beside every field, for a form that
   *  asks something ABOUT each value as well as for it. Returning null leaves
   *  the column empty for that row and the field spans the full width. */
  fieldAccessory?: (field: FormFieldView) => ReactNode;
  /** Rendered once in the accessory column's header, on the column it heads: the
   *  control that answers the accessory's question for every field at once. */
  accessoryHeader?: ReactNode;
  /** Names the accessory column, beside its header control. */
  accessoryLabel?: ReactNode;
}

/** The form's field stack: note → handle → method picker → ordered fields
 *  (required → method → optional-last with the "Optional" divider). */
export function ConnectionFormFields({
  form,
  draft,
  onDraftChange,
  disabled = false,
  pickPath,
  handleLabel = "Connection name",
  handleDescription = "A unique name for this connection in the workspace.",
  handleDisabled = false,
  handleError,
  hideHandle = false,
  methodFilter,
  belowMethodSlot,
  hideNote = false,
  fieldAccessory,
  accessoryHeader,
  accessoryLabel,
}: ConnectionFormFieldsProps) {
  const methods = methodFilter ? form.auth_methods.filter(methodFilter) : form.auth_methods;
  const method = selectedMethod({ ...form, auth_methods: methods }, draft.method);
  const rows = orderedRows(form, method?.fields ?? []);
  const setValue = (name: string, v: string) =>
    onDraftChange({ ...draft, values: { ...draft.values, [name]: v } });
  // The header is drawn once, over the first row that has an accessory — the
  // question is the same all the way down.
  let accessoryHeaded = false;
  // A surface that asks something about each value gets a second track. The
  // grid, not a margin, is what puts the column header over its own column and
  // holds every input to one width.
  const columned = fieldAccessory != null;

  return (
    <div
      className="alkconn-fields"
      data-columned={columned ? "" : undefined}
      // A form, so the design gates judge it for density and scannability rather
      // than looking for a hero and an entrance.
      data-measure-surface="product"
    >
      {form.note && !hideNote ? (
        <Callout tone="info" icon={null}>
          {form.note}
        </Callout>
      ) : null}

      {hideHandle ? null : (
        <TextInput
          label={handleLabel}
          description={handleDescription}
          error={handleError}
          placeholder="e.g. analytics_prod"
          required
          value={draft.handle}
          disabled={disabled || handleDisabled}
          onChange={(e) => onDraftChange({ ...draft, handle: e.target.value })}
        />
      )}

      {methods.length > 1 ? (
        <FieldDropdown
          label="Authentication"
          value={method?.name ?? draft.method}
          disabled={disabled}
          options={methods.map((m) => ({ value: m.name, label: m.label, icon: authIcon(m) }))}
          // Switching methods keeps the shared field values (account / host / user) — only the
          // previous method's OWN fields are dropped, so a mis-picked method doesn't wipe what
          // the user already typed (and one method's secret never leaks into another's submit).
          onChange={(v) =>
            onDraftChange({
              ...draft,
              method: v,
              values: retainedValuesOnMethodSwitch(form, draft.values),
            })
          }
        />
      ) : null}

      {belowMethodSlot}

      {rows.map(({ field: f, heading, optionalDivider }) => {
        const accessory = fieldAccessory?.(f) ?? null;
        const headHere = accessory !== null && !accessoryHeaded;
        if (headHere) accessoryHeaded = true;
        return (
          // A Fragment, not a `display: contents` box: the grid places the cells
          // by DOM parentage, and a wrapper element — however boxless — puts them
          // one level below the tracks they are supposed to sit in.
          <Fragment key={f.name}>
            <FieldRow
              columned={columned}
              heading={heading ?? null}
              optionalDivider={Boolean(optionalDivider)}
              accessory={accessory}
              accessoryHead={headHere && accessoryHeader ? accessoryHeader : null}
              accessoryLabel={accessoryLabel}
            >
              <FieldControl
                field={f}
                value={draft.values[f.name] ?? f.default}
                disabled={disabled}
                pickPath={pickPath ?? null}
                onChange={(v) => setValue(f.name, v)}
              />
            </FieldRow>
          </Fragment>
        );
      })}
    </div>
  );
}

/** One field row, emitted as bare grid cells so the parent grid owns both tracks:
 *  the field in the field column, its accessory in the column beside it, and the
 *  section rules across both. The one-time column header rides the same two
 *  tracks, which is what puts it over the column it names. */
function FieldRow({
  columned,
  heading,
  optionalDivider,
  accessory,
  accessoryHead,
  accessoryLabel,
  children,
}: {
  columned: boolean;
  heading: string | null;
  optionalDivider: boolean;
  accessory: ReactNode | null;
  accessoryHead: ReactNode | null;
  accessoryLabel?: ReactNode;
  children: ReactNode;
}) {
  return (
    <>
      {accessoryHead ? (
        <>
          <span className="alkconn-colhead alk-eyebrow">{accessoryLabel}</span>
          <div className="alkconn-acc alkconn-acc--head">{accessoryHead}</div>
        </>
      ) : null}
      {optionalDivider ? <FormDivider label="Optional" /> : null}
      {heading ? <FormDivider label={heading} /> : null}
      {columned ? <div className="alkconn-cell">{children}</div> : children}
      {accessory === null ? null : <div className="alkconn-acc">{accessory}</div>}
    </>
  );
}

/** A thin labelled section rule inside the form — used for "Optional" and group headings so
 *  the required credentials read as one block above the optional session defaults. */
export function FormDivider({ label }: { label: string }) {
  return (
    <div className="alkconn-divider" role="separator" aria-label={label}>
      <span className="alkconn-divider__label">{label}</span>
    </div>
  );
}

export function FieldControl({
  field,
  value,
  disabled,
  pickPath,
  onChange,
}: {
  field: FormFieldView;
  value: string;
  disabled: boolean;
  pickPath: PickPathFn | null;
  onChange: (v: string) => void;
}) {
  if (field.enum_values.length > 0) {
    // Only where the glyph says something. A field with no mark of its own gets
    // none: one repeated dot down a column of options is a bullet, and a mark
    // that means nothing costs the ones that do.
    const Icon = enumFieldIcon(field.name);
    const icon = Icon ? <Icon size={15} stroke={1.8} /> : undefined;
    const options: DropOption[] = field.enum_values.map((opt) => ({
      value: opt,
      label: field.enum_labels?.[opt] ?? opt,
      icon,
    }));
    // An unanswered select shows that it is unanswered. Falling through to the
    // first option would put a real answer on screen that the draft does not
    // hold and the submit does not carry — the deployment tier would read
    // "prod" on a brand-new form while Save sat disabled with nothing to fix.
    options.unshift({ value: "", label: field.required ? "Choose…" : "Not set", icon });
    return (
      <FieldDropdown
        label={field.label}
        required={field.required}
        description={field.help || undefined}
        value={value}
        disabled={disabled}
        options={options}
        onChange={onChange}
      />
    );
  }

  if (isPemField(field)) {
    return <PemField field={field} value={value} disabled={disabled} onChange={onChange} />;
  }

  if (field.secret) {
    return (
      <TextInput
        type="password"
        label={field.label}
        required={field.required}
        description={field.help || undefined}
        placeholder={field.placeholder || undefined}
        value={value}
        disabled={disabled}
        autoComplete="off"
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  if (field.type === "bool") {
    return (
      <FieldShell htmlFor="" description={field.help || undefined}>
        <Switch
          checked={value === "true"}
          disabled={disabled}
          label={field.label}
          onChange={(e) => onChange(e.target.checked ? "true" : "false")}
        />
      </FieldShell>
    );
  }

  if (field.type === "file" || field.type === "directory") {
    const directory = field.type === "directory";
    // No host picker (the browser portal) → the path is a plain text input.
    if (!pickPath) {
      return (
        <TextInput
          label={field.label}
          required={field.required}
          description={field.help || undefined}
          placeholder={directory ? "Path to a folder…" : "Path to a file…"}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    }
    const browse = () => {
      void pickPath({ directory }).then((picked) => {
        if (picked) onChange(picked);
      });
    };
    return (
      <FieldShell
        htmlFor=""
        label={field.label}
        required={field.required}
        description={field.help || undefined}
      >
        <Inline gap={2} wrap={false} block>
          <TextInput
            aria-label={field.label}
            value={value}
            disabled={disabled}
            placeholder={directory ? "Select a folder…" : "Select a file…"}
            onChange={(e) => onChange(e.target.value)}
          />
          <Button variant="secondary" fill="outline" disabled={disabled} onClick={browse}>
            Browse…
          </Button>
        </Inline>
      </FieldShell>
    );
  }

  return (
    <TextInput
      label={field.label}
      required={field.required}
      description={field.help || undefined}
      placeholder={field.placeholder || undefined}
      type={field.type === "number" ? "number" : "text"}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

/** The box a certificate is pasted into.
 *
 *  A PEM is a multi-line document, so it gets a textarea — a one-line password box
 *  cannot hold one, and the eye toggle it came with masked a value nobody could
 *  type in the first place. A field the schema marks secret still opens masked
 *  once it holds something, with a Show beside the label, so a stored
 *  certificate is not read over a shoulder; an empty box offers no Show, since a
 *  toggle over nothing is a control that does nothing. The value is carried
 *  verbatim either way. */
function PemField({
  field,
  value,
  disabled,
  onChange,
}: {
  field: FormFieldView;
  value: string;
  disabled: boolean;
  onChange: (v: string) => void;
}) {
  const [revealed, setRevealed] = useState(false);
  const hidable = field.secret && value !== "";
  const masked = hidable && !revealed;
  return (
    <Textarea
      label={field.label}
      labelAccessory={
        hidable ? (
          <Button
            variant="secondary"
            fill="ghost"
            size="sm"
            disabled={disabled}
            aria-pressed={revealed}
            onClick={() => setRevealed((v) => !v)}
          >
            {revealed ? "Hide" : "Show"}
          </Button>
        ) : undefined
      }
      required={field.required}
      description={field.help || undefined}
      placeholder={field.placeholder || undefined}
      rows={5}
      spellCheck={false}
      autoComplete="off"
      className="alkconn-pem"
      data-masked={masked ? "" : undefined}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

interface DropOption {
  value: string;
  label: string;
  icon?: ReactNode;
}

/** A fixed-option field built on the ui Dropdown — each row carries a leading icon, and
 *  the selected option mirrors onto the trigger. */
export function FieldDropdown({
  label,
  required,
  description,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  required?: boolean;
  description?: ReactNode;
  value: string;
  options: DropOption[];
  disabled?: boolean;
  onChange: (v: string) => void;
}) {
  const current = options.find((o) => o.value === value) ?? options[0];
  // The trigger names itself off the field label plus its own content, so it
  // announces "Deployment tier, Production". Named by content alone it announced
  // "Choose…" and a screen-reader user had no way to tell what it chose.
  const labelId = useId();
  const triggerId = useId();
  const valueId = useId();
  return (
    <FieldShell
      htmlFor={triggerId}
      labelId={labelId}
      label={label}
      required={required}
      description={description}
      // The Dropdown trigger takes no disabled prop, so the field goes inert whole.
      className={disabled ? "alkconn-field--off" : undefined}
    >
      <Dropdown
        label={`Select ${label}`}
        menuSize="lg"
        matchWidth
        // With no leading glyph the rows flush left instead of indenting past an
        // empty mark column, and the tick trails the row it marks.
        tickSide={options.some((o) => o.icon != null) ? "left" : "right"}
        trigger={{
          kind: "select",
          block: true,
          id: triggerId,
          valueId,
          ariaLabelledBy: `${labelId} ${valueId}`,
          leadingIcon: current?.icon,
          value: current?.label,
        }}
      >
        {options.map((o) => (
          <DropdownItem
            key={o.value}
            icon={o.icon}
            selected={o.value === value}
            onSelect={() => onChange(o.value)}
          >
            {o.label}
          </DropdownItem>
        ))}
      </Dropdown>
    </FieldShell>
  );
}

// --- leading-icon maps -------------------------------------------------------

export function authIcon(method: AuthMethodView): ReactNode {
  const n = method.name.toLowerCase();
  let Icon: ComponentType<IconProps> = IconLock;
  if (method.oauth || n.includes("adc") || n.includes("oauth")) Icon = IconCloud;
  else if (n.includes("key")) Icon = IconKey;
  else if (n.includes("service")) Icon = IconFileText;
  else if (n.includes("token") || n.includes("pat")) Icon = IconKey;
  else if (n.includes("dsn")) Icon = IconLink;
  return <Icon size={15} stroke={1.8} />;
}

function enumFieldIcon(fieldName: string): ComponentType<IconProps> | null {
  if (fieldName === "sslmode") return IconShieldLock;
  if (fieldName === "location") return IconWorld;
  if (fieldName === "dialect") return IconDatabase;
  if (fieldName.includes("dir") || fieldName.includes("path")) return IconFolder;
  return null;
}
