"""Every liveness deadline in this suite is asserted with the run's allowance.

The live-folder claims are real-time claims, and they run on the pr-gate's
shared shard: sixteen workers, one Postgres, one disk. The claim has to stay —
a file written on the box reaches the member inside the live window — but the
number a shared machine asserts it with is not the number a quiet one asserts,
which is the ruling the Files performance budgets already made and the knob they
already read.

Two things are pinned here. The helper gives the shared-machine allowance under
the gate's scale and the published window itself under the nightly one, and a
failure says which of the two it used. And every deadline in
``test_cloud_live_folder`` goes through it — a window spelled as a bare number
anywhere in that module is a deadline the shard gets to decide, which is the
whole thing this replaced.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _live_window import SCALE_ENV, live_window
from test_cloud_live_folder import _until

#: The module whose deadlines are pinned, read as text rather than imported:
#: what is asserted is how the deadlines are SPELLED, which an import cannot
#: see once the constants have been evaluated.
LIVE_FOLDER = Path(__file__).with_name("test_cloud_live_folder.py")


@pytest.mark.parametrize(
    ("scale", "allowance", "label"),
    [
        # What the pr-gate's `test` job sets, and what the Windows shards leave
        # unset — both are the shared, sixteen-worker machine.
        pytest.param("smoke", 3.0, "PR size", id="the-gate"),
        pytest.param("pr", 3.0, "PR size", id="the-default"),
        # The quiet scheduled runner, which asserts the window as published.
        pytest.param("nightly", 1.0, "nightly", id="nightly"),
    ],
)
def test_the_allowance_follows_the_scale_the_perf_budgets_read(
    monkeypatch: pytest.MonkeyPatch, scale: str, allowance: float, label: str
) -> None:
    monkeypatch.setenv(SCALE_ENV, scale)
    window = live_window(5.0)
    assert window.spec == 5.0, "the claim moved with the allowance"
    assert window.allowance == allowance
    assert window.seconds == 5.0 * allowance
    assert window.label == label


def test_an_unset_scale_is_the_shared_machine_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows shard sets no scale at all, and it is the shard that fails."""
    monkeypatch.delenv(SCALE_ENV, raising=False)
    assert live_window(5.0).seconds == 15.0


def test_an_unknown_scale_refuses_rather_than_taking_the_loose_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo that silently asserted three times the window is a green that
    proves nothing — the same refusal ``perf_sizes`` makes."""
    monkeypatch.setenv(SCALE_ENV, "quiet")
    with pytest.raises(ValueError, match="quiet"):
        live_window(5.0)


@pytest.mark.parametrize(
    ("scale", "expected"),
    [
        pytest.param("smoke", "15.0s (5.0s x 3, PR size)", id="the-gate"),
        pytest.param("nightly", "5.0s (5.0s x 1, nightly)", id="nightly"),
    ],
)
def test_a_window_says_what_it_allowed_and_what_the_claim_was(
    monkeypatch: pytest.MonkeyPatch, scale: str, expected: str
) -> None:
    """A failure that named only "15.0s" sends its reader looking for a
    fifteen-second claim that exists nowhere in the spec or the suite."""
    monkeypatch.setenv(SCALE_ENV, scale)
    assert str(live_window(5.0)) == expected


async def test_a_deadline_that_passes_names_the_window_it_was_measured_against(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real waiter, run against a predicate that never holds."""
    monkeypatch.setenv(SCALE_ENV, "smoke")
    with pytest.raises(AssertionError) as raised:
        await _until(lambda: None, window=live_window(0.1), what="the write reaching the drive")
    assert str(raised.value) == (
        "the write reaching the drive did not happen within 0.3s (0.1s x 3, PR size)"
    )


def _waiting_calls(tree: ast.Module) -> list[ast.Call]:
    """Every call in the module that waits on a real-time deadline."""
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        named = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        if named in {"_until", "until"}:
            found.append(node)
    return found


def test_every_live_folder_deadline_is_routed_through_the_allowance() -> None:
    """Not one window in that module is a number the shard gets to decide.

    A deadline reintroduced as ``seconds=5.0`` — or as a constant that is a
    plain float again — is exactly the shape that made a green claim depend on
    how loaded the runner was that evening.
    """
    tree = ast.parse(LIVE_FOLDER.read_text(encoding="utf-8"))

    calls = _waiting_calls(tree)
    assert len(calls) >= 9, f"only {len(calls)} deadlines found; did the waiters get renamed?"

    windows: set[str] = set()
    for call in calls:
        keywords = {keyword.arg for keyword in call.keywords}
        assert "seconds" not in keywords, (
            f"line {call.lineno} still waits on a raw number of seconds"
        )
        given = next(keyword.value for keyword in call.keywords if keyword.arg == "window")
        assert isinstance(given, ast.Name), (
            f"line {call.lineno} spells its window inline instead of naming one"
        )
        windows.add(given.id)

    assert windows, "no deadline named a window"
    built: set[str] = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "live_window"
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert windows <= built, (
        f"{sorted(windows - built)} is waited on but not built by live_window(), "
        "so the run's allowance never reaches it"
    )
