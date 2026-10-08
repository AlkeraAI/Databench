"""Widgets through the engine on the real kernel and the widget hub: frames
get comm-open replays, each frontend message is authorized and delivered as a
run, the sender's frame gets its idle status, and a value change re-runs the
cells reading it with their submitted code."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_notebook.document.ops import ReplaceCell
from alkera_notebook.engine import AllTarget, ForbiddenError, NotebookClient, WidgetAction
from alkera_notebook.events.models import FrameMessage
from nbeng_harness import BOB, VIEWER, engine_for, notebook, run_cells, statuses, text_of, until

SLIDER = "import alkera.ui as ui\nslider = ui.slider(0, 10, value=5)\nslider"
READER = "t = slider.value * 2\nt"


def frame_messages(client: NotebookClient) -> list[FrameMessage]:
    out = []
    while len(client.queue):
        event = client.queue._items.popleft()
        if isinstance(event, FrameMessage):
            out.append(event)
    return out


async def model_id(client: NotebookClient) -> str:
    widgets = (await client.widget(WidgetAction(action="list"))).widgets
    assert len(widgets) == 1
    return widgets[0].model_id


async def test_widgets_value_change_reruns_readers_with_submitted_code(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, [SLIDER, READER])
        await (await ann.run(AllTarget())).wait(30)
        assert await text_of(ann, b) == "10"
        mid = await model_id(ann)
        info = (await ann.widget(WidgetAction(action="get", model_id=mid))).widgets[0]
        assert (info.value, info.cell_id) == (5, a)
        # Ann edits b without running it; a widget change must not run that.
        await ann.apply([ReplaceCell(cell_id=b, source="t = slider.value * 1000\nt")], None)
        result = await ann.widget(WidgetAction(action="set", model_id=mid, state={"value": 7}))
        assert result.run is not None
        runtime = session.runtime
        await until(lambda: runtime.records[result.run.run_id].status not in ("queued", "running"))
        caused = runtime.records[result.run.run_id]
        assert caused.trigger == "widget" and [p.cell_id for p in caused.plan] == [b]
        assert await text_of(ann, b) == "14"
        assert (await statuses(ann))[b] == "edited"
        assert (await ann.widget(WidgetAction(action="get", model_id=mid))).widgets[0].value == 7


async def test_widgets_frames_get_replays_updates_and_their_own_idle(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (_a, _b) = await notebook(engine, [SLIDER, READER])
        bob = session.attach(BOB)
        await (await ann.run(AllTarget())).wait(30)
        mid = await model_id(ann)
        replays = ann.attach_frame("ann-f1", mid).opens
        assert [r.message["type"] for r in replays] == ["comm.open"]
        assert replays[0].message["data"]["state"]["value"] == 5
        bob.attach_frame("bob-f1", mid)
        frame_messages(ann), frame_messages(bob)
        await ann.comm_send(
            mid, "m-1", {"data": {"method": "update", "state": {"value": 3}}}, [], frame_id="ann-f1"
        )
        await until(
            lambda: any(
                m.message.get("type") == "comm.status"
                for m in list(ann.queue._items)
                if isinstance(m, FrameMessage)
            )
        )
        mine, theirs = frame_messages(ann), frame_messages(bob)
        assert {m.frame_id for m in mine} == {"ann-f1"} and {m.frame_id for m in theirs} == {
            "bob-f1"
        }
        assert [m.message for m in mine if m.message["type"] == "comm.status"] == [
            {"type": "comm.status", "msg_id": "m-1", "execution_state": "idle"}
        ]
        assert not [m for m in theirs if m.message["type"] == "comm.status"]
        assert any(m.message["type"] == "comm.msg" for m in theirs)  # the kernel's echo
        # Bob's client cannot send through Ann's frame.
        with pytest.raises(ForbiddenError):
            await bob.comm_send(
                mid, "m-2", {"data": {"method": "update", "state": {}}}, [], frame_id="ann-f1"
            )


async def test_widgets_viewer_and_readonly_frames_cannot_send(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        session, ann, (_a, _b) = await notebook(engine, [SLIDER, READER])
        await (await ann.run(AllTarget())).wait(30)
        mid = await model_id(ann)
        viewer = session.attach(VIEWER)
        viewer.attach_frame("v-f1", mid)
        with pytest.raises(ForbiddenError):
            await viewer.comm_send(
                mid, "v-1", {"data": {"method": "update", "state": {"value": 1}}}, []
            )
        ann.attach_frame("ann-ro", mid, readonly=True)
        with pytest.raises(ForbiddenError):
            await ann.comm_send(
                mid,
                "a-1",
                {"data": {"method": "update", "state": {"value": 1}}},
                [],
                frame_id="ann-ro",
            )
        assert (await ann.widget(WidgetAction(action="get", model_id=mid))).widgets[0].value == 5


async def test_widgets_die_with_the_kernel(tmp_path: Path) -> None:
    async with engine_for(tmp_path) as engine:
        _, ann, (a, _b) = await notebook(engine, [SLIDER, READER])
        await run_cells(ann, a)
        mid = await model_id(ann)
        ann.attach_frame("f", mid)
        await ann.kernel("restart")
        assert (await ann.widget(WidgetAction(action="list"))).widgets == []
