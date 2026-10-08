import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NOTEBOOK_SETTINGS_SCHEMA } from "../../generated/notebookSettings";
import type { SettingSource } from "../../model/settings";
import { SettingsForm } from "./SettingsForm";

type Change = [string, unknown];

function form(props: { stored?: Record<string, unknown>; effective?: Record<string, unknown>; sources?: Record<string, SettingSource>; canEdit?: boolean; changes?: Change[] } = {}) {
  return render(
    <SettingsForm
      specs={NOTEBOOK_SETTINGS_SCHEMA.notebook}
      stored={props.stored ?? {}}
      effective={props.effective}
      sources={props.sources}
      options={{ env: [{ value: "default", label: "Workspace default" }, { value: "./proj", label: "proj" }] }}
      canEdit={props.canEdit ?? true}
      onChange={(name, value) => props.changes?.push([name, value])}
    />,
  );
}

const row = (name: string) => document.querySelector<HTMLElement>(`[data-setting="${name}"]`)!;

describe("the settings form", () => {
  it("draws a labelled control for every setting the schema exports, or says why it has none", () => {
    form();
    for (const spec of NOTEBOOK_SETTINGS_SCHEMA.notebook) {
      if (spec.control === "none") {
        expect(spec.reason ?? "").not.toBe("");
        continue;
      }
      const control = within(row(spec.name)).getByLabelText(spec.label);
      expect(control).toBeInTheDocument();
    }
  });

  it("sets the SQL row limit, refuses what is out of range, and empties it back to the default", async () => {
    const user = userEvent.setup();
    const changes: Change[] = [];
    form({ changes });
    const box = within(row("sql_row_limit")).getByLabelText("Rows per SQL result");
    await user.type(box, "1000{Enter}");
    expect(changes).toEqual([["sql_row_limit", 1000]]);
    await user.clear(box);
    await user.type(box, "0{Enter}");
    expect(within(row("sql_row_limit")).getByRole("alert")).toHaveTextContent("Enter a whole number from 1 to 10,000,000.");
    expect(changes).toEqual([["sql_row_limit", 1000]]);
  });

  it("says where a value it does not set itself came from, and hands a value it sets back", async () => {
    const user = userEvent.setup();
    const changes: Change[] = [];
    form({
      stored: { reactivity: "lazy" },
      effective: { dataframe: "pandas", sql_row_limit: 5000 },
      sources: { dataframe: "workspace", env: "detected", autoreload: "default" },
      changes,
    });
    const source = (name: string) => row(name).querySelector(".nb-setting__source")?.textContent ?? null;
    expect(source("dataframe")).toBe("From the workspace");
    expect(within(row("dataframe")).getByLabelText("Frames as")).toHaveValue("pandas");
    expect(source("env")).toBe("Detected");
    expect(source("autoreload")).toBe("Default");
    expect(source("reactivity")).toBeNull();
    await user.click(within(row("reactivity")).getByRole("button", { name: "Use the default value" }));
    expect(changes).toEqual([["reactivity", null]]);
  });

  it("changes an enum, a switch and the environment by their own values", async () => {
    const user = userEvent.setup();
    const changes: Change[] = [];
    form({ changes });
    await user.selectOptions(within(row("reactivity")).getByLabelText("When a cell runs"), "lazy");
    await user.click(within(row("outputs_in_git")).getByLabelText("Save outputs with the file"));
    await user.selectOptions(within(row("env")).getByLabelText("Environment"), "./proj");
    expect(changes).toEqual([
      ["reactivity", "lazy"],
      ["outputs_in_git", true],
      ["env", "./proj"],
    ]);
    expect(within(row("env")).getByText("Changing it restarts the kernel.", { exact: false })).toBeInTheDocument();
  });

  it("shows a reader every value and lets them change none", () => {
    form({ canEdit: false, stored: { reactivity: "lazy" } });
    for (const control of screen.getAllByRole("combobox")) expect(control).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Use the/ })).toBeNull();
  });
});
