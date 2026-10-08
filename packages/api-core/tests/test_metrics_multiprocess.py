"""``/metrics`` when several processes serve one task (uvicorn ``--workers``).

Real processes share one metrics directory, as a task's workers do. One stays
alive, one records and exits (a worker the supervisor replaced). A third
renders what a scrape would see. Counters add up across processes and keep a
dead process's share; each gauge follows its own rule, over the live
processes only; and the per-process collectors are not reported at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from prometheus_client.parser import text_string_to_metric_families

_RECORD = textwrap.dedent(
    """
    import sys
    from alkera_core.observability import metrics

    listener, checked_out, subscribers, stay = sys.argv[1:5]
    metrics.record_request(component="backend", method="GET", status=200, duration_seconds=0.01)
    metrics.record_listener_connected(listener == "1")
    metrics.record_db_pool(checked_out=int(checked_out), capacity=30)
    metrics.record_hub_subscribers(int(subscribers))
    if stay == "stay":
        # A live worker: it answers the scrape, as one of the task's workers
        # does (any process importing the metrics holds gauges of its own).
        print("ready", flush=True)
        sys.stdin.readline()
        sys.stdout.write(metrics.exposition().decode())
        sys.stdout.flush()
    """
)

_RENDER = textwrap.dedent(
    """
    sys.stdout.write(metrics.exposition().decode())
    """
)


def _env(directory: Path) -> dict[str, str]:
    return {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(directory), "METRICS_ENABLED": "true"}


def _samples(text: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == "alkera_http_requests_total":
                found["requests"] = found.get("requests", 0.0) + sample.value
            elif not sample.labels or set(sample.labels) <= {"pid"}:
                found[sample.name] = sample.value
    return found


def test_a_scrape_answers_for_every_process_of_the_task(tmp_path: Path) -> None:
    directory = tmp_path / "metrics"
    alive = subprocess.Popen(
        [sys.executable, "-c", _RECORD, "1", "3", "2", "stay"],
        env=_env(directory),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert alive.stdout is not None and alive.stdout.readline().strip() == "ready"
        subprocess.run(
            [sys.executable, "-c", _RECORD, "0", "7", "5", "exit"],
            env=_env(directory),
            check=True,
            capture_output=True,
        )
        rendered, _ = alive.communicate("scrape\n", timeout=60)
    finally:
        if alive.poll() is None:
            alive.kill()
            alive.wait()
    seen = _samples(rendered)
    # Both requests: the exited worker's count is still part of the total.
    assert seen["requests"] == 2
    # The live process's view; the exited one's gauges are forgotten.
    assert seen["alkera_realtime_listener_connected"] == 1
    assert seen["alkera_db_pool_checked_out"] == 3
    assert seen["alkera_db_pool_capacity"] == 30
    assert seen["alkera_realtime_hub_subscribers"] == 2
    # One process's RSS or GC counts would pass for the task's.
    assert not [name for name in seen if name.startswith(("process_", "python_gc"))]


def test_one_process_alone_keeps_its_own_registry(tmp_path: Path) -> None:
    """Without the directory (one process per task) nothing changes: the
    default registry, process collectors included."""
    env = {k: v for k, v in os.environ.items() if k != "PROMETHEUS_MULTIPROC_DIR"}
    env["METRICS_ENABLED"] = "true"
    rendered = subprocess.run(
        [
            sys.executable,
            "-c",
            _RECORD.replace('if stay == "stay"', "if False") + _RENDER,
            "1",
            "3",
            "2",
            "exit",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert (
        "process_resident_memory_bytes" in rendered
        or "python_gc_objects_collected_total" in rendered
    )
    assert not (tmp_path / "metrics").exists()
