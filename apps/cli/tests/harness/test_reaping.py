"""A daemon must read its children's exit status. One that inherited an
ignored ``SIGCHLD`` reads every ``subprocess.run`` as 0 and every asyncio
child exit as 255 — the probe then reports mechanisms the host lacks and the
launch built on them fails with the real error in its stderr and 255 for a
code. The seams that run a child restore the default disposition first.
These tests run the real mechanism in a child interpreter so the test process
itself never has its ``SIGCHLD`` touched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest
from alkera_core import process as core_process

#: The tests that ignore and restore a real ``SIGCHLD`` need one: Windows's
#: interpreter has no such signal (the parser and the no-signal case run there).
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no SIGCHLD")


def _in_child(body: str) -> dict[str, object]:
    """Run ``body`` in a fresh interpreter and return the JSON it prints last."""
    script = textwrap.dedent(body)
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stderr
    loaded: dict[str, object] = json.loads(done.stdout.strip().splitlines()[-1])
    return loaded


@posix_only
def test_an_ignored_sigchld_makes_every_child_exit_read_as_success_until_reclaimed() -> None:
    """The mechanism, end to end: with ``SIGCHLD`` ignored ``/bin/false`` reads
    as 0; after :func:`reclaim_children` it reads as 1 again, and the helper
    says it had to act."""
    seen = _in_child(
        """
        import json, signal, subprocess
        from alkera_core.process import children_reapable, reclaim_children
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        blind = subprocess.run(["false"], capture_output=True).returncode
        before = children_reapable()
        reclaimed = reclaim_children()
        after = children_reapable()
        sighted = subprocess.run(["false"], capture_output=True).returncode
        again = reclaim_children()
        print(json.dumps({"blind": blind, "before": before, "reclaimed": reclaimed,
                          "after": after, "sighted": sighted, "again": again}))
        """
    )
    assert seen == {
        "blind": 0,
        "before": False,
        "reclaimed": True,
        "after": True,
        "sighted": 1,
        "again": True,
    }


@posix_only
def test_a_process_that_already_reaps_is_left_alone() -> None:
    seen = _in_child(
        """
        import json, signal
        from alkera_core.process import children_reapable, reclaim_children
        marker = lambda *_: None
        signal.signal(signal.SIGCHLD, marker)  # a handler of its own, not SIG_IGN
        ok = reclaim_children()
        kept = signal.getsignal(signal.SIGCHLD) is marker
        print(json.dumps({"reapable": children_reapable(), "ok": ok, "kept": kept}))
        """
    )
    assert seen == {"reapable": True, "ok": True, "kept": True}


@posix_only
def test_only_the_main_thread_can_restore_the_disposition_and_elsewhere_it_is_reported() -> None:
    """The probe runs on the event loop's thread but a step may run in a worker
    thread; there the helper cannot change the disposition and must say so
    rather than raise or pretend."""
    seen = _in_child(
        """
        import json, signal, threading
        from alkera_core.process import children_reapable, reclaim_children
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        result = {}
        def worker():
            result["reclaimed"] = reclaim_children()
            result["reapable"] = children_reapable()
        t = threading.Thread(target=worker); t.start(); t.join()
        result["main_reclaimed"] = reclaim_children()
        result["main_reapable"] = children_reapable()
        print(json.dumps(result))
        """
    )
    assert seen == {
        "reclaimed": False,
        "reapable": False,
        "main_reclaimed": True,
        "main_reapable": True,
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Name:\tx\nSigIgn:\t0000000001011000\nSigCgt:\t0\n", True, id="bit-16-set"),
        pytest.param("SigIgn:\t0000000001001000\n", False, id="bit-16-clear"),
        pytest.param("SigIgn:\t0000000000010000\n", True, id="only-sigchld"),
        pytest.param("SigIgn:\tnot-hex\n", None, id="unparseable"),
        pytest.param("Name:\tx\n", None, id="no-sigign-line"),
    ],
)
def test_the_kernels_account_of_sigchld_is_read_from_the_ignored_mask(
    text: str, expected: bool | None
) -> None:
    from alkera_core.process import kernel_ignores_sigchld

    assert kernel_ignores_sigchld(lambda: text) is expected
    assert kernel_ignores_sigchld(lambda: None) is None  # no /proc: unknowable


@pytest.mark.skipif(sys.platform != "linux", reason="the kernel's account lives in /proc")
def test_a_sigchld_ignored_behind_pythons_back_is_seen_in_the_kernels_account_and_restored() -> (
    None
):
    """The released binary's startup calls C ``signal(SIGCHLD, SIG_IGN)`` after
    the interpreter is up. ``signal.getsignal`` never learns of it — Python's
    table records only what Python set or found at start — which is how a
    daemon that believed it was reaping still read every child as success.
    The check must read the kernel's account and the restore must act on it."""
    seen = _in_child(
        """
        import ctypes, ctypes.util, json, signal, subprocess
        from alkera_core.process import children_reapable, reclaim_children
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        libc.signal.restype = ctypes.c_void_p
        libc.signal.argtypes = [ctypes.c_int, ctypes.c_void_p]
        libc.signal(signal.SIGCHLD, 1)  # SIG_IGN, behind Python's back
        table = signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
        blind = subprocess.run(["false"], capture_output=True).returncode
        seen = children_reapable()
        reclaimed = reclaim_children()
        sighted = subprocess.run(["false"], capture_output=True).returncode
        print(json.dumps({"table_says_default": table, "blind": blind, "seen": seen,
                          "reclaimed": reclaimed, "sighted": sighted}))
        """
    )
    assert seen == {
        "table_says_default": True,
        "blind": 0,
        "seen": False,
        "reclaimed": True,
        "sighted": 1,
    }


@pytest.mark.skipif(sys.platform != "linux", reason="the kernel's account lives in /proc")
def test_the_probe_restores_reaping_set_behind_pythons_back_before_it_answers() -> None:
    """The path the compiled daemon takes: the ignore is already in place when
    the probe runs, and nothing in Python's table says so. The probe's own
    reclaim must see it and restore it, whatever else the host lacks."""
    seen = _in_child(
        """
        import ctypes, ctypes.util, json, signal, subprocess
        from alkera_cli.harness import sandbox as sb
        from alkera_cli.harness.sandbox_probe import probe
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        libc.signal.restype = ctypes.c_void_p
        libc.signal.argtypes = [ctypes.c_int, ctypes.c_void_p]
        libc.signal(signal.SIGCHLD, 1)
        blind = subprocess.run(["false"], capture_output=True).returncode
        cap = probe(settings=sb.SandboxSettings(mode="none"))
        after = subprocess.run(["false"], capture_output=True).returncode
        print(json.dumps({"blind": blind, "exit_status": cap.exit_status, "after": after}))
        """
    )
    assert seen == {"blind": 0, "exit_status": True, "after": 1}


@posix_only
def test_spawn_command_and_the_step_runner_restore_reaping_before_running() -> None:
    """The two seams every harness child goes through, the spawn seam and a
    sandbox step, each restore the disposition, so a daemon started under an
    ancestor that ignores ``SIGCHLD`` still reads real exit codes."""
    seen = _in_child(
        """
        import json, os, signal, subprocess
        from alkera_cli.harness import sandbox
        from alkera_core.process import SpawnSpec, run
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        run(SpawnSpec(argv=["true"], env=dict(os.environ)), timeout=30)
        after_spawn = subprocess.run(["false"], capture_output=True).returncode
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        # A checked step that fails refuses the launch only if its verdict is read.
        try:
            sandbox.run_steps([sandbox.ShellStep(("false",))])
            step = "passed"
        except sandbox.SandboxRefusedError:
            step = "refused"
        after_step = subprocess.run(["false"], capture_output=True).returncode
        print(json.dumps({"after_spawn": after_spawn, "step": step, "after_step": after_step}))
        """
    )
    assert seen == {"after_spawn": 1, "step": "refused", "after_step": 1}


def test_an_interpreter_without_sigchld_always_reads_its_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no ``SIGCHLD``; a spawn there must not reach for the name
    even when a test (or a launcher) claims another platform, because the
    answer is the interpreter's, not the platform string's."""
    from alkera_core import process as reaping

    monkeypatch.setattr(core_process, "_SIGCHLD", None)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(core_process, "kernel_ignores_sigchld", lambda: None)
    assert reaping.children_reapable() is True
    assert reaping.reclaim_children() is True
