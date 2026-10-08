"""``python -m alkera_cli``: the open CLI, with the open composition installed.

It runs ``alkera_cli.entry`` with no install of its own, so the entry installs
the open platform's (``OPEN_COMPOSITION``). The product's entry (its compiled
binary and console script) runs the same dispatch with its own install.
"""

from __future__ import annotations

from alkera_cli.entry import main

if __name__ == "__main__":
    main()
