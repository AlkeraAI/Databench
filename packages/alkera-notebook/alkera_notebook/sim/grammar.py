"""Small cells for the simulator: the code a random agent or person writes.

The grammar covers what notebooks are made of and what goes wrong in them:
assignments that chain names, frame operations, SQL over frames, Markdown
with interpolation, widgets, deliberate errors (a division by zero, an unknown
name, a raised exception, a syntax error), short sleeps and allocations. Names
come from a small pool so cells depend on each other and sometimes define the
same name twice, which is what the graph rules are for.

Two forms: Hypothesis strategies for the state machines, and a seeded
generator for scripted and evaluation drivers.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

from hypothesis import strategies as st

NAMES: tuple[str, ...] = ("a", "b", "c", "df", "clean", "total", "w")
FRAMES: tuple[str, ...] = ("df", "clean")
SETUP_SOURCE = "import pandas as pd\nimport alkera"


@dataclass(frozen=True)
class CellSpec:
    kind: str
    source: str
    name: str = "_"
    meta: dict[str, str] = field(default_factory=dict)


def _python(name: str, other: str, frame: str, choice: int) -> CellSpec:
    forms: list[Callable[[], CellSpec]] = [
        lambda: CellSpec("python", f"{name} = 1"),
        lambda: CellSpec("python", f"{name} = {other} + 1"),
        lambda: CellSpec("python", f"{name} = {other} * 2\n{name}"),
        lambda: CellSpec(
            "python", f"{frame} = pd.DataFrame({{'v': [1, 2, 3], 'k': ['x', 'y', 'x']}})"
        ),
        lambda: CellSpec("python", f"{name} = {frame}[{frame}.v > 1]"),
        lambda: CellSpec("python", f"{name} = {frame}.groupby('k').v.sum()\n{name}"),
        lambda: CellSpec("python", f"{name} = alkera.ui.slider(0, 10, value=3)"),
        lambda: CellSpec("python", f"{name} = {other}.value * 10"),
        lambda: CellSpec("python", f"{name} = 1 / 0"),
        lambda: CellSpec("python", f"{name} = undefined_name_{choice % 3}"),
        lambda: CellSpec("python", "raise ValueError('boom')"),
        lambda: CellSpec("python", f"{name} = ("),
        lambda: CellSpec("python", "import time\ntime.sleep(0.001)"),
        lambda: CellSpec("python", "_buf = bytearray(256 * 1024)\nlen(_buf)"),
        lambda: CellSpec("python", f"print('{name}', {other})"),
        # Cell source the simulator writes into a notebook; never run here.
        lambda: CellSpec(
            "sql",
            f"SELECT k, sum(v) AS s FROM {frame} GROUP BY k",  # noqa: S608
            meta={"output_var": name},
        ),
        lambda: CellSpec("markdown", f"The value is {{{other}}}.", meta={"quote": "rf"}),
        lambda: CellSpec("markdown", "# A heading\n\nPlain prose."),
    ]
    return forms[choice % len(forms)]()


def cell_specs() -> st.SearchStrategy[CellSpec]:
    return st.builds(
        _python,
        st.sampled_from(NAMES),
        st.sampled_from(NAMES),
        st.sampled_from(FRAMES),
        st.integers(min_value=0, max_value=10_000),
    )


def random_cell(rng: random.Random) -> CellSpec:
    return _python(rng.choice(NAMES), rng.choice(NAMES), rng.choice(FRAMES), rng.randrange(10_000))


__all__ = ["FRAMES", "NAMES", "SETUP_SOURCE", "CellSpec", "cell_specs", "random_cell"]
