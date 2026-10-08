"""Output frames at the engine's widget hub, keyed by ``frame_id``: attached
by model or by output, named by the engine when the client does not, owned
by one client, and fed comm traffic and idle status only for themselves."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from alkera_notebook.engine import (
    AllTarget,
    ForbiddenError,
    FrameAttached,
    NotebookClient,
    NotFoundError,
    WidgetAction,
)
from alkera_notebook.events.models import FrameMessage
from nbeng_harness import BOB, engine_for, notebook, until

SLIDER = "import alkera.ui as ui\nslider = ui.slider(0, 10, value=5)\nslider"
PLAIN = "x = 1\nx"


def frame_messages(client: NotebookClient) -> list[FrameMessage]:
    out = []
    while len(client.queue):
        event = client.queue._items.popleft()
        if isinstance(event, FrameMessage):
            out.append(event)
    return out


@asynccontextmanager
async def started(tmp_path: Path) -> AsyncIterator[tuple[Any, NotebookClient, str, str, str]]:
    """A notebook whose first cell shows a slider and second a plain value,
    both run: (session, Ann's client, slider cell, plain cell, model id)."""
    async with engine_for(tmp_path) as engine:
        session, ann, (a, b) = await notebook(engine, [SLIDER, PLAIN])
        await (await ann.run(AllTarget())).wait(30)
        [widget] = (await ann.widget(WidgetAction(action="list"))).widgets
        yield session, ann, a, b, widget.model_id


@pytest.mark.parametrize("output", ["cell", "cell/0"])
async def test_a_frame_attached_by_output_is_named_by_the_engine(
    tmp_path: Path, output: str
) -> None:
    async with started(tmp_path) as (_session, ann, a, _b, mid):
        attached = ann.attach_frame(output_id=output.replace("cell", a))
        assert isinstance(attached, FrameAttached)
        assert attached.frame_id.startswith("frm_")
        assert attached.model_ids == [mid] and attached.output_id == output.replace("cell", a)
        assert mid in [o.message["comm_id"] for o in attached.opens]
        assert {o.frame_id for o in attached.opens} == {attached.frame_id}
        # Messages for the engine-named frame carry its id.
        frame_messages(ann)
        await ann.comm_send(
            mid,
            "m-1",
            {"data": {"method": "update", "state": {"value": 2}}},
            [],
            frame_id=attached.frame_id,
        )
        await until(lambda: len(ann.queue) > 0)
        await until(
            lambda: any(
                isinstance(e, FrameMessage) and e.message["type"] == "comm.status"
                for e in list(ann.queue._items)
            )
        )
        assert {m.frame_id for m in frame_messages(ann)} == {attached.frame_id}


async def test_frames_are_refused_for_what_shows_no_widget(tmp_path: Path) -> None:
    async with started(tmp_path) as (_session, ann, a, b, mid):
        cases: list[tuple[dict[str, Any], type[Exception]]] = [
            ({"output_id": b}, NotFoundError),  # a plain output
            ({"output_id": f"{a}/5"}, NotFoundError),  # no such display
            ({"output_id": f"{a}/x"}, NotFoundError),
            ({"output_id": "no-such-cell"}, NotFoundError),
            ({"model_id": "no-such-model"}, NotFoundError),
            ({"model_id": mid, "model_ids": ["no-such-model"]}, NotFoundError),
            ({}, ValueError),
        ]
        for kwargs, error in cases:
            with pytest.raises(error):
                ann.attach_frame(**kwargs)
        assert ann._session.frames == {}


async def test_a_frame_id_stays_with_the_client_that_attached_it(tmp_path: Path) -> None:
    async with started(tmp_path) as (session, ann, _a, _b, mid):
        bob = session.attach(BOB)
        ann.attach_frame("shared-name", mid)
        with pytest.raises(ForbiddenError):
            bob.attach_frame("shared-name", mid)
        # Bob detaching it does nothing; Ann still sends through it.
        bob.detach_frame("shared-name")
        held = session.runtime.frames
        assert {k: fr.client_id for k, fr in held.items()} == {"shared-name": ann.client_id}
        # Bob naming Ann's frame on a comm it shows is refused by the hub.
        with pytest.raises(ForbiddenError, match="not yours"):
            await bob.comm_send(
                mid, "m-0", {"data": {"method": "update", "state": {}}}, [], frame_id="shared-name"
            )
        await ann.comm_send(
            mid, "m-1", {"data": {"method": "update", "state": {}}}, [], frame_id="shared-name"
        )


async def test_idle_status_reaches_only_the_frame_that_sent(tmp_path: Path) -> None:
    async with started(tmp_path) as (session, ann, _a, _b, mid):
        bob = session.attach(BOB)
        one = ann.attach_frame(model_id=mid).frame_id
        two = ann.attach_frame(model_id=mid).frame_id
        theirs = bob.attach_frame(model_id=mid).frame_id
        assert len({one, two, theirs}) == 3
        frame_messages(ann), frame_messages(bob)
        await ann.comm_send(
            mid, "m-9", {"data": {"method": "update", "state": {"value": 4}}}, [], frame_id=two
        )
        await until(
            lambda: any(
                isinstance(e, FrameMessage) and e.message["type"] == "comm.status"
                for e in list(ann.queue._items)
            )
        )
        mine, bobs = frame_messages(ann), frame_messages(bob)
        status = [
            (m.frame_id, m.message["msg_id"]) for m in mine if m.message["type"] == "comm.status"
        ]
        assert status == [(two, "m-9")]
        assert not [m for m in bobs if m.message["type"] == "comm.status"]
        # The kernel's echo reaches every frame showing the model.
        echoed = {m.frame_id for m in [*mine, *bobs] if m.message["type"] == "comm.msg"}
        assert echoed == {one, two, theirs}


async def test_the_view_reports_the_configured_output_frame_url(tmp_path: Path) -> None:
    url = "https://files.example.test/c/nb-output/abc123"
    for configured in (None, url):
        name = "with" if configured else "without"
        async with engine_for(tmp_path, name=name, output_frame_url=configured) as engine:
            _session, ann, _ids = await notebook(engine, [PLAIN])
            view = await ann.read()
            assert view.output_frame_url == configured
            assert view.model_dump()["output_frame_url"] == configured
