// The one declarative table of preference controls, for every surface that
// edits them.
//
// A preference is stored once — `alkera_core.schemas.preferences.Preferences` —
// and edited from two places: the editor's Settings pane, over the daemon, and
// the portal's chat preferences page, over the server. Two hand-written tables
// would drift on the first field anyone added, and the drift would look like a
// missing feature rather than a bug.
//
// A control declares WHICH surfaces honour it (`surfaces`). That is the seam a
// new preference is added through: the field is written once here and named by
// the surfaces that actually act on it — because a toggle a surface does not
// read is worse than a missing one. It tells the person they changed something
// when nothing changed.
//
// A control renders only when the store actually returns its key, so an older
// daemon simply omits it. Structured prefs with no flat editor yet
// (`tool_card_disclosure`, `model_efforts`) and the theme string are edited via
// the CLI and are deliberately absent.

import { PERMISSION_MODES } from "@alkera/chat-model";

export interface SelectOption {
  value: string;
  label: string;
}

/** Where a preference has an effect — and therefore where it may be offered.
 *
 *  `editor`: honoured by the daemon the extension and the CLI drive.
 *  `web`: honoured by the server, for chats started in the browser. */
export type PreferenceSurface = "editor" | "web";

interface ControlBase {
  key: string;
  label: string;
  description?: string;
  /** The surfaces that act on this preference. Absent = editor only, which is
   *  where every preference started. */
  surfaces?: readonly PreferenceSurface[];
}

export type Control =
  | (ControlBase & { kind: "toggle" })
  | (ControlBase & { kind: "select"; options: SelectOption[] })
  | (ControlBase & { kind: "duration"; options: SelectOption[] });

export interface ControlGroup {
  title: string;
  controls: Control[];
}

// Exported for the onboarding tour's preferences page, which offers the same pick.
// Read from the generated mode registry, in its order; a select row uses the
// short label where the registry names one ("Bypass").
export const PERMISSION_MODE_OPTIONS: SelectOption[] = PERMISSION_MODES.map((mode) => ({
  value: mode.value,
  label: mode.short ?? mode.label,
}));

const TOOL_CARD_LOAD_OPTIONS: SelectOption[] = [
  { value: "use_finished_default", label: "Use finished default" },
  { value: "use_spawn_default", label: "Use spawn default" },
  { value: "all_open", label: "All open" },
  { value: "all_closed", label: "All closed" },
];

// Durations are stored as an integer number of seconds; the select carries them as
// strings (option values are strings) and the "duration" control coerces back to a
// number on save + matches the stored number by its string form.
// Exported for the onboarding tour's preferences step, which offers the same pick.
export const SQL_TIMEOUT_OPTIONS: SelectOption[] = [
  { value: "60", label: "1 minute" },
  { value: "300", label: "5 minutes" },
  { value: "600", label: "10 minutes" },
  { value: "900", label: "15 minutes" },
  { value: "1800", label: "30 minutes" },
  { value: "3600", label: "60 minutes" },
  { value: "0", label: "No limit" },
];

export const GENERAL_GROUPS: ControlGroup[] = [
  {
    title: "Display",
    controls: [
      {
        key: "show_banner",
        kind: "toggle",
        label: "Show banner",
        description: "Show the wordmark and art at the top of the chat screens.",
      },
      {
        key: "reduce_motion",
        kind: "toggle",
        label: "Reduce motion",
        description: "Hold animated spinners and waves to a static frame.",
      },
      {
        key: "tool_card_border",
        kind: "toggle",
        label: "Tool card border",
        description: "Frame tool cards with a border instead of the borderless spine look.",
      },
    ],
  },
  {
    title: "Agent",
    controls: [
      {
        key: "default_permission_mode",
        kind: "select",
        label: "Default permission mode",
        description: "The permission mode new chats start in.",
        options: PERMISSION_MODE_OPTIONS,
        // The server reads this when a browser chat is created, and the daemon
        // when a local one is. Narrowed per surface below: a cloud chat may only
        // start in a stance the product offers on the web.
        surfaces: ["editor", "web"],
      },
      {
        key: "tool_card_load_policy",
        kind: "select",
        label: "Tool card load policy",
        description: "How tool cards open when a chat is loaded from disk.",
        options: TOOL_CARD_LOAD_OPTIONS,
      },
    ],
  },
  {
    title: "Data",
    controls: [
      {
        key: "sql_statement_timeout_seconds",
        kind: "duration",
        label: "SQL statement timeout",
        description: "Cancel a SQL query that runs longer than this. 'No limit' disables it.",
        options: SQL_TIMEOUT_OPTIONS,
      },
    ],
  },
  {
    title: "Privacy",
    controls: [
      {
        key: "telemetry_enabled",
        kind: "toggle",
        label: "Send usage & error reports",
        description:
          "Share anonymous error reports to help us fix bugs. Never includes " +
          "your prompts, file contents, or secrets.",
      },
    ],
  },
];

/** Every curated or intentionally hidden key — excluded from the forward-compat "Other" list. */
export const CURATED_KEYS: ReadonlySet<string> = new Set([
  ...GENERAL_GROUPS.flatMap((group) => group.controls.map((control) => control.key)),
  "show_thinking",
]);

export const INSTRUCTIONS_PLACEHOLDER =
  "# Global instructions\n\nThese apply to the agent in every project, alongside a repo's ALKERA.md / AGENTS.md.";

/** Whether `surface` acts on this control. Absent `surfaces` = editor only. */
export function honours(control: Control, surface: PreferenceSurface): boolean {
  return (control.surfaces ?? ["editor"]).includes(surface);
}

/** The groups one surface offers: its own controls, with empty groups dropped.
 *
 *  `allowed` narrows a select's options where a surface supports fewer than the
 *  preference can hold, so a picker never offers a value that surface's server
 *  would refuse. A narrowing that removed every option drops the control rather
 *  than rendering an empty select. */
export function groupsFor(
  surface: PreferenceSurface,
  allowed: Readonly<Record<string, readonly string[]>> = {},
): ControlGroup[] {
  const groups: ControlGroup[] = [];
  for (const group of GENERAL_GROUPS) {
    const controls: Control[] = [];
    for (const control of group.controls) {
      if (!honours(control, surface)) continue;
      const narrow = allowed[control.key];
      if (!narrow || control.kind === "toggle") {
        controls.push(control);
        continue;
      }
      const options = control.options.filter((option) => narrow.includes(option.value));
      if (options.length > 0) controls.push({ ...control, options });
    }
    if (controls.length > 0) groups.push({ ...group, controls });
  }
  return groups;
}
