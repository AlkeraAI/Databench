"""Every Pydantic model under ``alkera_core.schemas`` is complete once imported.

A model whose annotations name a class defined later in its module is left
incomplete at import, and pydantic builds its schema on first use instead.
That first use reads the module's names as they are at that moment, and
``freezegun`` swaps the module's ``datetime`` for its fake while time is
frozen. So when the first ``MyCreditsResponse`` of a process was built inside
``freeze_time``, the build failed with "Unable to generate pydantic-core schema
for <class 'datetime.datetime'>", and
``test_me_credits_org_plan.py::test_the_members_own_cap_binds_when_it_is_below_the_allocation``
failed whenever it ran first. Building every schema at import removes the
dependence on what ran before.

The walk runs in a fresh interpreter. Any earlier use of an incomplete model
in this process completes it, so an in-process walk would pass whenever
another test on the same worker had already built the model.
"""

from __future__ import annotations

import json
import subprocess
import sys

_WALK = """
import importlib, json, pkgutil
import alkera_core.schemas
from pydantic import BaseModel

incomplete = []
for info in pkgutil.walk_packages(
    alkera_core.schemas.__path__, f"{alkera_core.schemas.__name__}."
):
    module = importlib.import_module(info.name)
    for name, value in vars(module).items():
        if not (isinstance(value, type) and issubclass(value, BaseModel)):
            continue
        if value.__module__ == module.__name__ and not value.__pydantic_complete__:
            incomplete.append(f"{module.__name__}.{name}")
print(json.dumps(incomplete))
"""


def _incomplete_models() -> list[str]:
    done = subprocess.run(
        [sys.executable, "-c", _WALK], capture_output=True, text=True, check=False, timeout=60
    )
    assert done.returncode == 0, done.stderr
    result: list[str] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def test_every_schema_model_is_complete_at_import() -> None:
    assert _incomplete_models() == []
