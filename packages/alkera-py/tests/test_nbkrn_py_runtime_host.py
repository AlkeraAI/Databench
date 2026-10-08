"""``alkera`` inside a real Alkera kernel: cells call the public API and the
assertions read the events the kernel sends the service."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from alkera_notebook.rpc import Call
from nbkrn_py_kernel_harness import KernelFactory, RunResult, kernel_mount, start_kernel, step

__all__ = ["kernel_mount", "start_kernel"]  # fixtures, registered by import


def _outputs(result: RunResult, cell_id: str) -> list[tuple[str, dict[str, Any]]]:
    return [(p["mode"], p["output"]) for p in result.of("cell.output", cell_id)]


async def test_md_appended_from_a_cell_arrives_as_html_and_markdown(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    result = await ks.run(
        step("a", "import alkera\nalkera.output.append(alkera.md('# Hi **there**'))\n")
    )
    assert result.status == "ok", result.events
    ((mode, bundle),) = _outputs(result, "a")
    assert mode == "append"
    assert "<h1>Hi <strong>there</strong></h1>" in bundle["text/html"]
    assert bundle["text/markdown"] == "# Hi **there**"


async def test_an_output_object_as_the_last_expression_renders_through_the_kernel(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    code = "import alkera\nalkera.callout(alkera.md('*careful*'), 'danger')"
    result = await ks.run(step("a", code))
    assert result.status == "ok", result.events
    ((_, bundle),) = _outputs(result, "a")
    assert "alkera-callout-danger" in bundle["text/html"]
    assert "<em>careful</em>" in bundle["text/html"]
    assert bundle["text/plain"] == "[danger] *careful*"


async def test_replace_and_clear_reach_the_kernel_as_replace_events(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    code = (
        "import alkera, time\n"
        "alkera.output.append(alkera.md('first'))\n"
        "time.sleep(0.15)\n"
        "alkera.output.replace(alkera.md('second'))\n"
        "time.sleep(0.15)\n"
        "alkera.output.clear()\n"
    )
    result = await ks.run(step("a", code))
    assert result.status == "ok", result.events
    events = _outputs(result, "a")
    assert [mode for mode, _ in events] == ["append", "replace", "replace"]
    assert events[0][1]["text/markdown"] == "first"
    assert events[1][1]["text/markdown"] == "second"
    assert events[2][1] == {}


async def test_stop_ends_the_cell_as_stopped_after_showing_its_output(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    code = "import alkera\nalkera.stop(True, alkera.md('no data yet'))\nprint('after stop')\n"
    result = await ks.run(step("a", code), step("b", "print('downstream')\n"))
    assert result.finished("a")["status"] == "stopped", result.events
    assert "error" not in result.finished("a")
    ((_, bundle),) = _outputs(result, "a")
    assert bundle["text/markdown"] == "no data yet"
    assert result.stdout("a") == ""
    # The stop ends the run: the next step is never reached.
    assert result.of("cell.started", "b") == []


async def test_stop_with_a_false_predicate_lets_the_cell_continue(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "import alkera\nalkera.stop(0)\nprint('went on')\n"))
    assert result.finished("a")["status"] == "ok"
    assert result.stdout("a") == "went on\n"


async def test_progress_bar_replaces_the_output_at_a_limited_rate(
    start_kernel: KernelFactory,
) -> None:
    ks = await start_kernel()
    code = (
        "import alkera, time\n"
        "for _ in alkera.status.progress_bar(range(60), title='Loading'):\n"
        "    time.sleep(0.01)\n"
    )
    result = await ks.run(step("a", code))
    assert result.status == "ok", result.events
    events = _outputs(result, "a")
    assert events and all(mode == "replace" for mode, _ in events)
    # About 0.6 s of work at ten frames a second: far fewer frames than items.
    assert 3 <= len(events) <= 15, len(events)
    final = events[-1][1]
    assert final["text/plain"].startswith("Loading: 60/60 (100%)")
    assert '<progress value="60" max="60"' in final["text/html"]


async def test_spinner_shows_then_clears(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    code = "import alkera, time\nwith alkera.status.spinner('Fetching'):\n    time.sleep(0.2)\n"
    result = await ks.run(step("a", code))
    events = _outputs(result, "a")
    assert events[0][1]["text/plain"] == "Fetching..."
    assert events[-1] == ("replace", {})


async def test_call_reaches_a_method_the_client_has_never_heard_of(
    start_kernel: KernelFactory,
) -> None:
    seen: list[Call] = []

    async def handler(call: Call) -> dict[str, Any]:
        seen.append(call)
        return {"doubled": call.params["n"] * 2}

    ks = await start_kernel(
        methods={"zz_lane_probe.double": handler}, run_scoped=["zz_lane_probe.double"]
    )
    code = "import alkera\nanswer = alkera.call('zz_lane_probe.double', n=21)\nanswer"
    result = await ks.run(step("a", code))
    assert result.status == "ok", result.events
    ((_, bundle),) = _outputs(result, "a")
    assert bundle["text/plain"] == "{'doubled': 42}"
    (call,) = seen
    # The request carries the run it came from, so run-scoped methods admit it.
    assert call.ctx is not None and call.ctx["run_id"] == result.run_id
    assert call.ctx["cell_id"] == "a"


async def test_call_to_an_unregistered_method_fails_the_cell(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "import alkera\nalkera.call('zz_lane_probe.absent')\n"))
    finished = result.finished("a")
    assert finished["status"] == "error"
    assert "zz_lane_probe.absent" in "".join(finished["error"]["traceback"])


async def test_args_come_from_the_service_and_are_read_only(start_kernel: KernelFactory) -> None:
    ks = await start_kernel(settings={"args": {"region": "eu", "limit": 3}})
    code = (
        "import alkera\n"
        "a = alkera.args()\n"
        "try:\n"
        "    a['region'] = 'us'\n"
        "except TypeError:\n"
        "    print('read-only')\n"
        "sorted(a.items())"
    )
    result = await ks.run(step("a", code))
    assert result.status == "ok", result.events
    assert result.stdout("a") == "read-only\n"
    ((_, bundle),) = _outputs(result, "a")
    assert bundle["text/plain"] == "[('limit', 3), ('region', 'eu')]"


async def test_widget_registers_the_object_as_reactive(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    code = (
        "import alkera\n"
        "class Model:\n"
        "    model_id = 'model-7'\n"
        "slider = alkera.widget(Model())\n"
        "plain = Model()\n"
    )
    result = await ks.run(step("a", code))
    assert result.status == "ok", result.events
    bindings = [p for m, p in ks.events if m == "ui.bindings" and p.get("cell_id") == "a"]
    # Only the object passed to alkera.widget is bound, under its global name.
    assert bindings and bindings[-1]["bindings"] == {"model-7": ["slider"]}


async def test_interruptibles_registered_through_alkera_are_interrupted(
    start_kernel: KernelFactory, tmp_path: Path
) -> None:
    marker = tmp_path / "interrupted"
    ks = await start_kernel()
    # Like a database query running in C: the cell keeps waiting through
    # KeyboardInterrupt and ends only when its interrupt() is called.
    code = (
        "import alkera, pathlib, threading\n"
        "done = threading.Event()\n"
        "class Query:\n"
        "    def interrupt(self):\n"
        f"        pathlib.Path({str(marker)!r}).write_text('yes')\n"
        "        done.set()\n"
        "query = Query()  # held: the kernel keeps interruptibles weakly\n"
        "alkera._host.current().register_interruptible(query)\n"
        "print('registered', flush=True)\n"
        "while not done.is_set():\n"
        "    try:\n"
        "        done.wait(30)\n"
        "    except KeyboardInterrupt:\n"
        "        pass\n"
        "raise RuntimeError('query cancelled')\n"
    )
    run_id = await ks.start_run([step("a", code)])
    await ks.wait_for(
        lambda: any(m == "cell.stream" and "registered" in p["text"] for m, p in ks.events)
    )
    await ks.interrupt(run_id)
    result = await ks.finish(run_id, limit_s=10)
    assert result.finished("a")["status"] == "interrupted"
    assert marker.read_text() == "yes"


async def test_the_kernel_adopts_its_host_as_runtime(start_kernel: KernelFactory) -> None:
    ks = await start_kernel()
    result = await ks.run(step("a", "import alkera\nalkera._host.current().name"))
    ((_, bundle),) = _outputs(result, "a")
    assert bundle["text/plain"] == "'runtime'"


async def test_a_comm_opened_through_alkera_round_trips(start_kernel: KernelFactory) -> None:
    ks = await start_kernel(settings={"dataframe": "polars", "args": {"n": "3"}})
    code = (
        "import alkera\n"
        "host = alkera._host.current()\n"
        "seen = []\n"
        "comm = host.open_comm('jupyter.widget', {'state': {'value': 1}}, {'version': '2.1.0'}, "
        "seen.append)\n"
        "comm.send({'method': 'update', 'state': {'value': 2}})\n"
        "(comm.comm_id, host.settings()['dataframe'], dict(alkera.args()))"
    )
    result = await ks.run(step("u", code))
    assert result.status == "ok", result.events
    comm_id, dataframe, args = ast.literal_eval(result.outputs("u")[0]["text/plain"])
    assert (dataframe, args) == ("polars", {"n": "3"})
    (opened,) = [p for m, p in ks.events if m == "comm.open"]
    assert opened["content"]["comm_id"] == comm_id
    assert opened["content"]["target_name"] == "jupyter.widget"
    (sent,) = [p for m, p in ks.events if m == "comm.msg"]
    assert sent["comm_id"] == comm_id
    assert sent["content"]["data"]["state"] == {"value": 2}
    # A frontend moves the widget: the engine delivers it as a run.
    ks.service.scope.begin("w1")
    await ks.request(
        "comm.deliver",
        {
            "run_id": "w1",
            "msg_id": "f1",
            "msg": {
                "msg_type": "comm_msg",
                "content": {
                    "comm_id": comm_id,
                    "data": {"method": "update", "state": {"value": 9}},
                },
            },
        },
    )
    await ks.finish("w1")
    probe = await ks.run(step("p", "[(m['header']['msg_id'], m['content']['data']) for m in seen]"))
    assert (
        probe.outputs("p")[0]["text/plain"]
        == "[('f1', {'method': 'update', 'state': {'value': 9}})]"
    )
