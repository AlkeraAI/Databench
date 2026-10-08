"""Generate the cases the page rule is pinned against on BOTH sides.

Where a transcript page opens is decided by one function,
:func:`alkera_core.objects.transcript_page.align_boundary`, and mirrored in
TypeScript for the browser's own model of the server. Two implementations of
one rule drift in silence unless something makes them answer the same
questions, so this writes those questions — and this rule's answers — to a
committed file both sides replay.

Seeded and pure: the same seed gives the same file, byte for byte, which is
what lets the test assert the committed one is still what the rule produces.
Run it to re-bless the file after a deliberate change to the rule::

    uv run python packages/api-core/tests/objects/gen_transcript_page_cases.py
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from alkera_core.objects.transcript_page import PageReach, PageRow, align_boundary

#: Bumping this re-rolls every generated case, so it moves only when the
#: corpus is deliberately re-cut.
SEED = 20260921

#: Where the committed file lives, next to the other api-core fixtures.
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "transcript_page" / "cases.json"

_Row = tuple[bool, str | None, bool]


def _prompt() -> _Row:
    return True, None, False


def _plain() -> _Row:
    return False, None, False


def _part(name: str) -> _Row:
    return False, name, False


def _opens(name: str) -> _Row:
    return False, name, True


def _hand_built() -> list[list[_Row]]:
    """The shapes that trapped the rule while it was being written.

    A prompt landing inside the answer being written; answers nested inside
    answers; the box's echo of the person's message, whose opening row comes
    LAST and is re-announced later from inside the turn; a message whose rows
    sit further apart than a page may reach; rows belonging to no message; a
    log with no turn start in it at all.
    """
    shapes: list[list[_Row]] = []
    # The reported shape: the next prompt recorded inside the stopped answer.
    shapes.append(
        [_prompt(), _opens("m0"), _part("m0"), _prompt(), _opens("m1"), _part("m1")]
        + [_prompt(), _part("m1"), _part("m1"), _opens("m2")]
        + [_part("m2")] * 8
    )
    # The box's echo: parts first, opening row last, re-announced later.
    shapes.append(
        [_prompt(), _part("u"), _part("u"), _opens("u"), _opens("a")]
        + [_part("a"), _part("u"), _part("a")] * 3
        + [_opens("u"), _part("a")]
    )
    # Answers nested inside answers, a prompt inside each.
    shapes.append(
        [_prompt(), _opens("outer"), _part("outer"), _prompt(), _opens("inner")]
        + [_part("inner"), _prompt(), _part("inner"), _part("outer"), _part("outer")]
        + [_opens("last")]
        + [_part("last")] * 6
    )
    # A message whose rows sit far apart, with nothing of it in between.
    shapes.append(
        [_prompt(), _part("far")] + [_plain()] * 30 + [_prompt(), _opens("far"), _part("far")]
    )
    # Nothing that opens a turn at all.
    shapes.append([_opens("only")] + [_part("only")] * 14)
    # Rows belonging to no message whatsoever.
    shapes.append([_prompt()] + [_plain()] * 20 + [_prompt()] + [_plain()] * 5)
    # One answer longer than any reach below, still running at the tail.
    shapes.append([_prompt(), _opens("big")] + [_part("big")] * 60)
    return shapes


def _generated(rng: random.Random) -> list[_Row]:
    """A short transcript with every shape a real one interleaves."""
    script: list[_Row] = [_prompt()]
    for turn in range(rng.randint(2, 5)):
        echo = f"u{turn}"
        script += [_part(echo) for _ in range(rng.randint(1, 3))]
        script.append(_opens(echo))
        running: list[str] = []
        again = False
        for index in range(rng.randint(1, 2)):
            answer = f"a{turn}-{index}"
            script.append(_opens(answer))
            running.append(answer)
            for _ in range(rng.randint(1, 7)):
                script.append(_part(rng.choice(running)))
                if rng.random() < 0.2:
                    script.append(_plain())
                if not again and rng.random() < 0.2:
                    script.append(_opens(echo))
                    again = True
            if len(running) > 1 and rng.random() < 0.5:
                running.pop(0)
        if rng.random() < 0.5:
            script.append(_prompt())
            script += [_part(name) for name in running]
        else:
            script += [_part(name) for name in running]
            script.append(_prompt())
    return script


def _case(script: list[_Row], *, start_seq: int, page_low: int, reach: PageReach) -> dict[str, Any]:
    rows = [
        PageRow(seq=start_seq + index, starts_turn=turn, message_id=name, opens_message=opens)
        for index, (turn, name, opens) in enumerate(script)
    ]
    oldest = start_seq
    floor = max(page_low - reach.budget, oldest)
    known = [row for row in rows if floor <= row.seq]
    answer = align_boundary(known, page_low=page_low, oldest=oldest, reach=reach)
    return {
        "rows": [[row.seq, row.starts_turn, row.message_id, row.opens_message] for row in known],
        "page_low": page_low,
        "oldest": oldest,
        "reach": {"limit": reach.limit, "turn": reach.turn, "message": reach.message},
        "expect": {"seq": answer.seq, "cut": answer.cut},
    }


def build(seed: int = SEED) -> dict[str, Any]:
    """The whole corpus: the hand-built traps at several reaches and page
    positions, then generated logs — some starting well above sequence 1, some
    read from a page whose floor is the transcript's own first row."""
    rng = random.Random(seed)
    cases: list[dict[str, Any]] = []
    reaches = [
        PageReach(limit=3, turn=2, message=1),
        PageReach(limit=4, turn=4, message=2),
        PageReach(limit=6, turn=1, message=1),
    ]
    for script in _hand_built():
        for reach in reaches:
            for end in (len(script), max(len(script) - 3, 1)):
                for start_seq in (1, 41):
                    page_low = max(end - reach.limit, 0) + start_seq
                    cases.append(_case(script, start_seq=start_seq, page_low=page_low, reach=reach))
    while len(cases) < 300:
        script = _generated(rng)
        reach = rng.choice(reaches)
        start_seq = rng.choice([1, 1, 7, 101])
        end = rng.randint(1, len(script))
        page_low = max(end - reach.limit, 0) + start_seq
        cases.append(_case(script, start_seq=start_seq, page_low=page_low, reach=reach))
    return {
        "rule": "align_boundary",
        "generator": {"seed": seed, "cases": len(cases)},
        "cases": cases,
    }


def render(corpus: dict[str, Any]) -> str:
    """One case per line, so a diff says which case moved."""
    rule = json.dumps(corpus["rule"])
    header = json.dumps(corpus["generator"], sort_keys=True)
    lines = [f'{{"rule": {rule}, "generator": {header}, "cases": [']
    for index, case in enumerate(corpus["cases"]):
        tail = "," if index < len(corpus["cases"]) - 1 else ""
        lines.append(json.dumps(case, sort_keys=True) + tail)
    lines.append("]}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover - a hand-run generator
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(render(build()), encoding="utf-8")
    print(f"wrote {FIXTURE}")
