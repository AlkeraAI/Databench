"""``python -m worker``: the open worker, with no extension installed.

The product's own worker entry runs the same dispatch, ``worker.entry``, after
installing its extensions.
"""

from __future__ import annotations

from worker.entry import main

if __name__ == "__main__":
    raise SystemExit(main())
