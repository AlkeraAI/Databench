"""python-hcl2, parsed once per text per worker.

The terraform shape tests read the same few files hundreds of times: a module
of a hundred cases that each resolve both stacks re-parses every env file per
case, and one case that decides fifty routes re-parsed the ALB module for each.
The parse is a pure function of the text, so it is memoized on the text, and
every caller gets its own deep copy: no test can see what another one did to
the tree it was handed. A file that changes on disk is new text, and is parsed
again.
"""

from __future__ import annotations

import copy
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=256)
def _parsed(text: str) -> dict[str, Any]:
    import hcl2

    doc: dict[str, Any] = hcl2.loads(text)
    return doc


def loads(text: str) -> dict[str, Any]:
    """``hcl2.loads`` with CRLF normalised (the parser rejects it), memoized."""
    return copy.deepcopy(_parsed(text.replace("\r\n", "\n")))
