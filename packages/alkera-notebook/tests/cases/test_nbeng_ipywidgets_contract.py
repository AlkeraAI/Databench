"""A real ipywidgets slider, from a real kernel, through the engine's widget
hub: the frame's comm-open replays are what the Jupyter widget manager in the
frame accepts.

The frame's manager refuses any comm open whose metadata does not carry the
widget protocol version, so the version the library sends has to survive the
kernel's comm provider, the engine and the hub. The replays are also the
shared vector ``packages/widgets`` renders in its own test
(``ipywidgetsContract.test.ts``), so the two sides cannot drift apart.

Regenerate the vector after an ipywidgets upgrade with
``NB_WIDGET_VECTORS=write``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "alkera-kernel" / "tests" / "kernel"))
from alkera_notebook.engine import AllTarget, FrameAttached
from alkera_notebook.envs.static import StaticEnvRegistry
from nbeng_harness import engine_for, notebook
from nbkrn_harness import rich_python

__all__ = ["rich_python"]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="v1 has no Windows kernel")

VECTOR = (
    Path(__file__).resolve().parents[3]
    / "widgets"
    / "src"
    / "tests"
    / "fixtures"
    / "ipywidgets-slider-opens.json"
)

SLIDER = "import ipywidgets as w\ns = w.IntSlider(value=3, min=0, max=10)\ns"


def _library(python: str) -> tuple[str, str]:
    """(ipywidgets version, the widget protocol version it speaks)."""
    out = subprocess.run(
        [
            python,
            "-c",
            "import ipywidgets as w; from ipywidgets._version import __protocol_version__ as p;"
            "print(w.__version__, p)",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return out[0], out[1]


def _normalized(opens: list[dict[str, Any]], shown: str) -> dict[str, Any]:
    """The replays with the kernel's random comm ids replaced by their
    position, so two runs of the same library compare equal."""
    names = {o["comm_id"]: f"model-{i}" for i, o in enumerate(opens)}
    text = json.dumps(opens, sort_keys=True)
    for real, name in names.items():
        text = text.replace(real, name)
    return {"model_id": names[shown], "opens": json.loads(text)}


async def _slider_replays(tmp_path: Path, python: str) -> dict[str, Any]:
    async with engine_for(tmp_path, envs=StaticEnvRegistry(interpreter=python)) as engine:
        _session, ann, (cell,) = await notebook(engine, [SLIDER])
        record = await (await ann.run(AllTarget())).wait(60)
        assert record.status == "ok", record
        attached = ann.attach_frame(output_id=cell)
        assert isinstance(attached, FrameAttached)
        [shown] = attached.model_ids
        return _normalized([o.message for o in attached.opens], shown)


async def test_every_replay_carries_the_protocol_version_the_library_sent(
    tmp_path: Path, rich_python: str
) -> None:
    _version, protocol = _library(rich_python)
    replays = await _slider_replays(tmp_path, rich_python)
    opens = replays["opens"]
    # The slider, its layout and its style: three models, all replayed.
    assert sorted(o["data"]["state"]["_model_name"] for o in opens) == [
        "IntSliderModel",
        "LayoutModel",
        "SliderStyleModel",
    ]
    assert [o["metadata"] for o in opens] == [{"version": protocol}] * 3
    assert protocol.split(".")[0] == "2"


async def test_the_replays_are_the_vector_the_frame_manager_renders(
    tmp_path: Path, rich_python: str
) -> None:
    version, _protocol = _library(rich_python)
    replays = {"ipywidgets": version, **await _slider_replays(tmp_path, rich_python)}
    if os.environ.get("NB_WIDGET_VECTORS") == "write":
        VECTOR.write_text(json.dumps(replays, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    recorded = json.loads(VECTOR.read_text(encoding="utf-8"))
    if recorded["ipywidgets"] != version:
        pytest.skip(
            f"the vector was recorded with ipywidgets {recorded['ipywidgets']}, "
            f"this environment has {version}; regenerate it with NB_WIDGET_VECTORS=write"
        )
    assert replays == recorded
