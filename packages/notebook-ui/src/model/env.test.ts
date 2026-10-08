import { envKindLabel, envLabel, envName, envPython, envSpecPath } from "./env";
import type { EnvInfo } from "./types";

const BOX = "/opt/alkera-work/orgs/0/work/.alkera/chats/fdc3271d/scratch";

const env = (over: Partial<EnvInfo>): EnvInfo => ({
  env_id: "uv_project:.",
  kind: "uv_project",
  spec_root: BOX,
  python: "",
  state: "ready",
  recorded_in_file: false,
  ...over,
});

describe("an environment's name", () => {
  it.each([
    ["the default", env({ env_id: "default:.alkera/envs/default", kind: "default", spec_root: `${BOX}/.alkera/envs/default` }), "Default"],
    ["a uv project at the workspace root", env({}), "uv project"],
    ["a uv project in a folder", env({ env_id: "uv_project:analysis/q3", spec_root: `${BOX}/analysis/q3` }), "uv project in analysis/q3"],
    ["a venv", env({ env_id: "venv:tools/.venv", kind: "venv" }), "Virtual environment in tools/.venv"],
    ["a kind this build does not know", env({ env_id: "pixi:.", kind: "pixi" }), "pixi"],
  ])("names %s without the machine's path", (_case, info, name) => {
    expect(envName(info)).toBe(name);
    expect(envLabel(info)).not.toContain("/opt/");
  });

  it("never offers a machine path as the spec, whether from the id or the root", () => {
    expect(envSpecPath(env({}))).toBe(".");
    expect(envSpecPath(env({ env_id: "uv_project:/opt/alkera-work/x" }))).toBeNull();
    expect(envSpecPath(env({ env_id: "env-1", spec_root: "notebooks/pyproject.toml" }))).toBe("notebooks/pyproject.toml");
    expect(envSpecPath(env({ env_id: "env-1", spec_root: "C:\\work\\env" }))).toBeNull();
    expect(envSpecPath(env({ env_id: "env-1", spec_root: "~/envs/a" }))).toBeNull();
  });

  it("adds the Python version only when the listing knows it", () => {
    expect(envPython(env({}))).toBeNull();
    expect(envPython(env({ python: "  " }))).toBeNull();
    expect(envLabel(env({}))).toBe("uv project");
    expect(envLabel(env({ python: "3.12.13" }))).toBe("uv project · Python 3.12.13");
    expect(envLabel(env({}))).not.toContain("Python");
  });

  it("reads a kind as words", () => {
    expect(envKindLabel("uv_project")).toBe("uv project");
    expect(envKindLabel("conda")).toBe("Conda");
    expect(envKindLabel("pixi")).toBe("pixi");
  });
});
