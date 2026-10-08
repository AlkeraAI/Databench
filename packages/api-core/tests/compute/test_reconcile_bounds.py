"""The numbers the reconciler is configured by, and what they may not become.

Three settings decide whether a pass is safe: the name prefix (the whole blast
radius), the grace (how long an unowned pod is left alone), and the window a
create with no answer is given before its row is written off. Each one has a
property that must hold however it is set.

The BOUNDS themselves — default, environment, both edges, and the values the
validators refuse — are pinned in ``packages/api-core/tests/test_settings_compute_bounds.py``.
What is here is the behaviour those numbers buy, which is a different question
from whether a deployment may set them.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from alkera_core.compute import reconcile
from alkera_core.config import settings


def test_a_pod_name_always_carries_the_deployment_prefix_and_the_allocation_id() -> None:
    allocation_id = uuid4()
    name = reconcile.pod_name_for(allocation_id)
    assert name.startswith(reconcile.pod_name_prefix())
    assert name.endswith(allocation_id.hex[:12])
    # Two allocations never share a name, which is what makes the name a join key.
    assert reconcile.pod_name_for(uuid4()) != name


def test_the_default_prefix_is_not_empty() -> None:
    """An empty prefix would make every pod on the provider account this
    deployment's business — including another deployment's."""
    assert settings.compute_pod_name_prefix.strip() != ""
    assert reconcile.pod_name_prefix().endswith("-")


def test_changing_the_prefix_moves_both_the_name_and_the_filter_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name a create commits and the filter a pass lists by are the same
    string. If they could drift, a deployment would stop recognising its own
    pods — and start reaping them as orphans."""
    allocation_id = uuid4()
    monkeypatch.setattr(settings, "compute_pod_name_prefix", "staging")
    name = reconcile.pod_name_for(allocation_id)
    assert name.startswith(reconcile.pod_name_prefix())
    assert name == f"staging-{allocation_id.hex[:12]}"
    # A pod created under the other deployment's prefix is outside the filter.
    assert not f"alkera-{allocation_id.hex[:12]}".startswith(reconcile.pod_name_prefix())


@pytest.mark.parametrize(
    ("grace_seconds", "window_seconds"),
    [
        pytest.param(600, 900, id="configured-window-wins"),
        pytest.param(3600, 60, id="a-window-under-the-grace-is-raised-to-it"),
        pytest.param(60, 60, id="both-at-the-floor"),
    ],
)
def test_the_unconfirmed_window_is_never_shorter_than_the_grace(
    monkeypatch: pytest.MonkeyPatch, grace_seconds: int, window_seconds: int
) -> None:
    """A row must not be written off in a pass that was still forbidden to look
    for the pod it might own — that would free the grant slot and leave the
    machine running with nothing left to match it to."""
    monkeypatch.setattr(settings, "compute_reconcile_grace_seconds", grace_seconds)
    monkeypatch.setattr(settings, "compute_unconfirmed_create_seconds", window_seconds)
    assert reconcile._unconfirmed_window() >= timedelta(seconds=grace_seconds)
    assert reconcile._unconfirmed_window() >= timedelta(seconds=window_seconds)
