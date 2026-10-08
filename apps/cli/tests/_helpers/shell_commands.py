"""Shell commands that are several actions: a standing grant for one covers
its exact text, never a family."""

from __future__ import annotations

import pytest

LOOP = "for i in $(seq 1 8); do date >> f; sleep 1; done | wc -l"

COMPOUND_COMMANDS = [
    pytest.param("ls | wc -l", id="pipe"),
    pytest.param("git add . && git commit -m wip", id="and-list"),
    pytest.param("make build || make clean", id="or-list"),
    pytest.param("mkdir out; touch out/x", id="sequence"),
    pytest.param(LOOP, id="loop"),
    pytest.param("while true; do date; done", id="while-loop"),
    pytest.param("(cd build && make)", id="subshell"),
    pytest.param("echo $(whoami)", id="command-substitution"),
    pytest.param("date >> log.txt", id="write-redirect"),
    pytest.param("[ -f x ] && rm x", id="test-then-act"),
]
