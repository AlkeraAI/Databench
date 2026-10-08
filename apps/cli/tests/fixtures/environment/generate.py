"""Write the environment-spec fixture for the current SCHEMA_VERSION.

Run after a version bump: ``uv run python apps/cli/tests/fixtures/environment/generate.py``.
Never edit or delete an older fixture; the lineage test reads every one.
"""

from __future__ import annotations

from pathlib import Path

from alkera_cli.environment.spec import (
    CondaPackage,
    CondaSpec,
    EnvironmentSpec,
    IndexRef,
    NotPortable,
    PackageSpec,
    PlatformInfo,
    ProjectFile,
    PythonInfo,
    VcsRef,
)


def current() -> EnvironmentSpec:
    return EnvironmentSpec(
        captured_at="2026-10-04T12:00:00+00:00",
        env_kind="conda",
        python=PythonInfo(version="3.12.4", implementation="cpython"),
        platform=PlatformInfo(sys_platform="linux", machine="x86_64", tag="linux-x86_64"),
        packages=[
            PackageSpec(
                name="pandas",
                version="2.2.3",
                requested=True,
                hashes=["sha256:aa"],
                installer="pip",
            ),
            PackageSpec(
                name="mylib", version="0.1.0", source="editable", path="libs/mylib", requested=True
            ),
            PackageSpec(
                name="vendored", version="1.0", source="path", path="vendor/vendored-1.0.whl"
            ),
            PackageSpec(
                name="tool",
                version="0.3",
                source="vcs",
                vcs=VcsRef(
                    vcs="git",
                    url="https://github.com/o/tool.git",
                    commit="abc",
                    requested_revision="main",
                ),
                subdirectory="py",
            ),
            PackageSpec(
                name="wheelpkg", version="1.0", source="url", url="https://files.example/w.whl"
            ),
        ],
        conda=CondaSpec(
            subdir="linux-64",
            channels=["conda-forge"],
            specs=["python=3.12", "numpy"],
            packages=[
                CondaPackage(
                    name="numpy",
                    version="2.1.0",
                    build="py312h1",
                    channel="conda-forge",
                    url="https://conda.anaconda.org/conda-forge/linux-64/numpy-2.1.0-py312h1.conda",
                    md5="m1",
                    subdir="linux-64",
                )
            ],
        ),
        project_files=[ProjectFile(path="uv.lock", kind="uv_lock", sha256="0" * 64)],
        indexes=[
            IndexRef(
                url="https://pkgs.example/simple",
                kind="extra_index",
                origin="requirements.txt",
                credentials_removed=True,
            )
        ],
        path_entries=["tools"],
        not_portable=[
            NotPortable(kind="editable_outside_workspace", name="extlib", detail="~/code/extlib")
        ],
    )


if __name__ == "__main__":
    spec = current()
    out = Path(__file__).parent / f"v{spec.SCHEMA_VERSION.replace('.', '_')}.json"
    out.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(out)
