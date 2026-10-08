import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { EnvInfo } from "../../model/types";
import { EnvironmentPanel, installLine, parsePackageSpecs, requirementName } from "./EnvironmentPanel";

const env: EnvInfo = {
  env_id: "env-1",
  kind: "uv",
  spec_root: "notebooks/pyproject.toml",
  python: "3.13.1",
  state: "ready",
  recorded_in_file: true,
};
const packages = [{ name: "polars", version: "1.9.0" }, { name: "numpy", version: "2.1.0" }, { name: "marimo" }];

describe("parsePackageSpecs", () => {
  it.each([
    ["polars", ["polars"]],
    ["  polars   numpy\n", ["polars", "numpy"]],
    ["pandas>=2,<3 scikit-learn==1.5", ["pandas>=2,<3", "scikit-learn==1.5"]],
    ["   ", []],
  ])("splits %j", (text, expected) => {
    expect(parsePackageSpecs(text)).toEqual(expected);
  });
});

describe("EnvironmentPanel", () => {
  it.each([
    [false, true],
    [true, false],
    [undefined, false],
  ])("says the environments aren't shared only when they are not (shared=%s)", (shared, said) => {
    render(<EnvironmentPanel env={env} packages={[]} canRun shared={shared} onInstall={() => {}} />);
    const line = screen.queryByText("Environments aren't shared on this machine");
    expect(line !== null).toBe(said);
  });

  it("shows the environment, the kernel and the packages by name", () => {
    render(<EnvironmentPanel env={env} kernelState="idle" packages={packages} canRun onInstall={() => {}} />);
    const facts = screen.getByRole("region", { name: "Environment" });
    expect(facts).toHaveTextContent("Python3.13.1");
    expect(facts).toHaveTextContent("Specnotebooks/pyproject.toml");
    expect(facts).toHaveTextContent("KernelIdle");
    expect(screen.getByText("Recorded in the notebook file")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Installed (3)" })).toBeInTheDocument();
    const names = within(screen.getByRole("list", { name: "Packages" }))
      .getAllByRole("listitem")
      .map((li) => li.textContent);
    expect(names).toEqual(["marimo", "numpy2.1.0", "polars1.9.0"]);
  });

  it("says when the environment is not recorded in the file, or absent", () => {
    const { rerender } = render(<EnvironmentPanel env={{ ...env, recorded_in_file: false }} packages={[]} canRun onInstall={() => {}} />);
    expect(screen.getByText("Not recorded in the notebook file")).toBeInTheDocument();
    expect(screen.getByText("No packages")).toBeInTheDocument();
    rerender(<EnvironmentPanel env={null} packages={[]} canRun onInstall={() => {}} />);
    expect(screen.getByText("No environment")).toBeInTheDocument();
  });

  it("installs what was typed and clears the box", async () => {
    const user = userEvent.setup();
    const installs: string[][] = [];
    render(<EnvironmentPanel env={env} packages={packages} canRun onInstall={(p) => installs.push(p)} />);
    const box = screen.getByRole("textbox", { name: "Packages to install" });
    const install = screen.getByRole("button", { name: "Install" });
    expect(install).toBeDisabled();
    await user.type(box, "pandas>=2,<3  rich");
    await user.click(install);
    await user.type(box, "httpx{Enter}");
    expect(installs).toEqual([["pandas>=2,<3", "rich"], ["httpx"]]);
    expect(box).toHaveValue("");
  });

  it("does not install from blank input on Enter", async () => {
    const user = userEvent.setup();
    const installs: string[][] = [];
    render(<EnvironmentPanel env={env} packages={packages} canRun onInstall={(p) => installs.push(p)} />);
    await user.type(screen.getByRole("textbox"), "   {Enter}");
    expect(installs).toEqual([]);
  });

  it("offers no install to someone who cannot run code", () => {
    render(<EnvironmentPanel env={env} packages={packages} canRun={false} onInstall={() => {}} />);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Install" })).toBeNull();
  });

  it("waits while an install is under way", async () => {
    const user = userEvent.setup();
    const installs: string[][] = [];
    render(<EnvironmentPanel env={env} packages={packages} canRun installing onInstall={(p) => installs.push(p)} />);
    expect(screen.getByRole("textbox")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Installing…" })).toBeDisabled();
    expect(installs).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Installing…" }));
    expect(installs).toEqual([]);
  });

  it.each([
    [{ status: "running", packages: ["polars"], message: null }, "Installing polars…", "status"],
    [{ status: "ok", packages: ["polars", "rich"], message: null }, "Installed polars, rich.", "status"],
    [{ status: "error", packages: ["six"], message: "add failed (exit 1)." }, "Could not install six: add failed (exit 1).", "alert"],
    [{ status: "error", packages: ["six"], message: null }, "Could not install six.", "alert"],
    [{ status: "unknown", packages: ["nope"], message: null }, "No result arrived for installing nope.", "status"],
  ] as const)("says how the last install went (%o)", (install, text, role) => {
    render(<EnvironmentPanel env={env} packages={packages} canRun install={install} onInstall={() => {}} />);
    expect(screen.getByTestId("install-status")).toHaveTextContent(text);
    expect(screen.getByTestId("install-status")).toHaveAttribute("role", role);
  });

  it("says nothing about installs when none was asked for", () => {
    render(<EnvironmentPanel env={env} packages={packages} canRun onInstall={() => {}} />);
    expect(screen.queryByTestId("install-status")).toBeNull();
  });

  it("names an environment on a machine without its path, and leaves out a version nobody knows", () => {
    const box: EnvInfo = {
      env_id: "uv_project:.",
      kind: "uv_project",
      spec_root: "/opt/alkera-work/orgs/0/work/.alkera/chats/fdc3271d/scratch",
      python: "",
      state: "ready",
      recorded_in_file: false,
    };
    render(<EnvironmentPanel env={box} packages={[]} canRun onInstall={() => {}} />);
    const facts = screen.getByRole("region", { name: "Environment" });
    expect(facts).toHaveTextContent("Nameuv project");
    expect(facts).toHaveTextContent("Spec.");
    expect(facts).not.toHaveTextContent("/opt/");
    expect(within(facts).queryByText("Python")).toBeNull();
  });
});

describe("what a person can do to the environment", () => {
  type Change = [string, string[]];
  const panel = (over: Partial<EnvInfo>, props: { canRun?: boolean; requirements?: string[]; changes?: Change[] } = {}) =>
    render(
      <EnvironmentPanel
        env={{ ...env, ...over }}
        packages={packages}
        requirements={props.requirements ?? []}
        canRun={props.canRun ?? true}
        onInstall={() => {}}
        onChange={(action, names) => props.changes?.push([action, names])}
      />,
    );

  it("builds an environment that is not built yet", async () => {
    const changes: Change[] = [];
    panel({ state: "missing", allowed_actions: ["build", "install", "remove"] }, { changes });
    expect(screen.getByText("Not built yet")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Build" }));
    expect(changes).toEqual([["build", []]]);
  });

  it("cancels a build under way and offers nothing else meanwhile", async () => {
    const changes: Change[] = [];
    panel({ state: "building", allowed_actions: ["cancel"] }, { changes, requirements: ["polars"] });
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove polars" })).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Cancel build" }));
    expect(changes).toEqual([["cancel", []]]);
  });

  it("lists the spec's requirements and removes one by its name", async () => {
    const changes: Change[] = [];
    panel({ allowed_actions: ["install", "remove"] }, { changes, requirements: ["pandas>=2,<3", "mysqlclient"] });
    const listed = within(screen.getByRole("list", { name: "Requirements" })).getAllByRole("listitem");
    expect(listed.map((li) => li.querySelector(".nb-mono")?.textContent)).toEqual(["pandas>=2,<3", "mysqlclient"]);
    await userEvent.setup().click(screen.getByRole("button", { name: "Remove pandas" }));
    expect(changes).toEqual([["remove", ["pandas"]]]);
  });

  it("offers no install on an environment managed outside Alkera", () => {
    panel({ kind: "venv", allowed_actions: [] }, { requirements: [] });
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("button", { name: "Build" })).toBeNull();
  });

  it("offers nothing that changes the environment to someone who cannot run code", () => {
    panel({ state: "missing", allowed_actions: ["build", "install", "remove"] }, { canRun: false, requirements: ["polars"] });
    expect(screen.queryByRole("button", { name: "Build" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove polars" })).toBeNull();
  });

  it("says the last attempt failed while the previous build stays in use", () => {
    panel({ last_failure: "Installing torch failed.", allowed_actions: ["install", "remove"] });
    expect(screen.getByTestId("env-last-failure")).toHaveTextContent("Installing torch failed. The previous build is still in use.");
  });

  it.each([
    ["pandas>=2,<3", "pandas"],
    ["scikit-learn[all]==1.5", "scikit-learn"],
    ["  rich ", "rich"],
  ])("names the package %j requires as %j", (requirement, name) => {
    expect(requirementName(requirement)).toBe(name);
  });

  it.each([
    [{ status: "running", action: "build", packages: [], message: null }, "Building the environment…"],
    [{ status: "ok", action: "remove", packages: ["six"], message: null }, "Removed six."],
    [{ status: "error", action: "cancel", packages: [], message: "Nothing was building" }, "Could not cancel the build: Nothing was building."],
    [{ status: "unknown", action: "build", packages: [], message: null }, "No result arrived for building the environment."],
  ] as const)("says how %j went", (install, line) => {
    expect(installLine(install)).toBe(line);
  });
});
