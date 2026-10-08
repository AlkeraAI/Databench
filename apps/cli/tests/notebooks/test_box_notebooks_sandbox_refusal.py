"""An org box set to gVisor never runs a notebook kernel outside the sandbox.

A local kernel on such a box would run as the org worker, which reaches every
member's chat tree, so a failed ``runsc`` probe refuses kernels the way it
refuses chats instead of falling back to local subprocess kernels.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from alkera_cli.harness.sandbox import SandboxSettings
from alkera_cli.harness.sandbox_probe import SandboxCapability
from alkera_cli.harness.sandbox_steps import SandboxRefusedError
from alkera_cli.notebooks import box_compose


def _capability(*, gvisor: bool) -> SandboxCapability:
    return SandboxCapability(
        platform="linux",
        root=True,
        setpriv="/usr/bin/setpriv",
        runsc="/usr/bin/runsc" if gvisor else None,
        cgroup="systemd",
        reason="test",
        rootfs="/var/lib/alkera/rootfs" if gvisor else None,
    )


_BASE_SETTINGS = SandboxSettings.from_env()


def _settings(mode: str) -> SandboxSettings:
    return replace(_BASE_SETTINGS, mode=mode)  # type: ignore[arg-type]


def _engines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, mode: str, gvisor: bool) -> Any:
    capability = _capability(gvisor=gvisor)
    monkeypatch.setattr(box_compose.SandboxSettings, "from_env", lambda: _settings(mode))
    monkeypatch.setattr(box_compose, "current_capability", lambda: capability)
    return box_compose.sandboxed_engines(object(), (), org_root=tmp_path)  # type: ignore[arg-type]


def test_a_gvisor_org_box_whose_probe_failed_refuses_every_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine_for = _engines(monkeypatch, tmp_path, mode="gvisor", gvisor=False)
    assert engine_for is not None, "a None here falls back to unsandboxed local kernels"
    with pytest.raises(SandboxRefusedError):
        engine_for(object())


def test_a_box_not_set_to_gvisor_keeps_local_kernels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _engines(monkeypatch, tmp_path, mode="none", gvisor=False) is None


def test_a_box_without_an_org_root_keeps_local_kernels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(box_compose.SandboxSettings, "from_env", lambda: _settings("gvisor"))
    monkeypatch.setattr(box_compose, "current_capability", lambda: _capability(gvisor=False))
    assert box_compose.sandboxed_engines(object(), (), org_root=None) is None  # type: ignore[arg-type]
