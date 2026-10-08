"""A running chat follows the stance its record states, and a re-opened one too.

``test_cloud_stance_journey`` pins the OPEN: the box reads the row and starts
the session in it. This file pins the two ways a live box can drift from the
row afterwards, and what the model is told each time:

* the reader flipped the chip while the box's document socket was down, so
  the relay reached nobody — the row is re-read on every tick and every
  ``chat.updated``, and that read moves the running session exactly as the
  relay would have, switch notice included;
* the chat is re-opened over its own folder (a sleep, a box restart, a pull
  onto a fresh box) whose manifest and harness session still carry the stance
  it was born with — the CURRENT record wins, never the pinned session's past.
"""

from __future__ import annotations

from alkera_cli.harness.permission_mode import MODE_LABELS
from test_cloud_stance_journey import CHAT_ID, STEERING, _Box, _record
from test_cloud_stance_journey import box as _journey_box

#: The journey file's box — the production factory over a FakeAdapter — registered
#: here under the same name, so both files open a chat the way the box does.
box = _journey_box

SWITCH = {
    ("read_only", "default"): (
        f"switched the permission mode from {MODE_LABELS['read_only']} to {MODE_LABELS['default']}"
    ),
    ("default", "read_only"): (
        f"switched the permission mode from {MODE_LABELS['default']} to {MODE_LABELS['read_only']}"
    ),
}


async def _steering_of_next_prompt(handle: _Box, adapter_index: int, text: str) -> str:
    assert handle.mirror is not None and handle.mirror.session is not None
    adapter = handle.factory.adapters[adapter_index]
    before = len(adapter.sent_prompts)
    await handle.mirror.session.send_prompt(text)
    return "\n".join(p.system or "" for p in adapter.sent_prompts[before:])


async def test_a_row_read_moves_a_running_chat_the_way_the_relay_does(box: _Box) -> None:
    """No relay is delivered here at all: the row alone says the stance moved,
    and the next prompt is steered in the new mode with the switch notice a
    reader's relay would have produced."""
    mirror = await box.open(_record("read_only"))
    box.service._mirrors[CHAT_ID] = mirror
    first = await _steering_of_next_prompt(box, 0, "what mode are you in")
    assert STEERING["read_only"] in first

    await box.service._ensure_mirror(CHAT_ID, _record("default"))

    assert mirror.permission_mode == "default"
    assert mirror.session is not None and mirror.session.permission_mode == "default"
    steering = await _steering_of_next_prompt(box, 0, "and now?")
    assert SWITCH[("read_only", "default")] in steering
    assert STEERING["default"] in steering
    assert STEERING["read_only"] not in steering.split("Adjust your approach")[-1]

    await box.service._ensure_mirror(CHAT_ID, _record("read_only"))

    assert mirror.session.permission_mode == "read_only"
    steering = await _steering_of_next_prompt(box, 0, "and now?")
    assert SWITCH[("default", "read_only")] in steering
    assert STEERING["read_only"] in steering


async def test_a_row_that_states_no_stance_moves_nothing(box: _Box) -> None:
    """The floor applies to a chat being OPENED without a stance; a running
    chat is never dragged to it by a read that simply does not mention one."""
    mirror = await box.open(_record("default"))
    box.service._mirrors[CHAT_ID] = mirror
    record = _record("default")
    del record["permission_mode"]

    await box.service._ensure_mirror(CHAT_ID, record)

    assert mirror.permission_mode == "default"
    assert mirror.session is not None and mirror.session.permission_mode == "default"
    steering = await _steering_of_next_prompt(box, 0, "what mode are you in")
    assert STEERING["default"] in steering
    assert "switched the permission mode" not in steering


async def test_a_row_naming_no_stance_any_harness_runs_reads_as_the_floor_on_a_running_chat(
    box: _Box,
) -> None:
    """``accept_edits`` is a retired word no harness runs a session in, so a row
    saying it comes from a backend this box does not know; it is read as the
    floor here exactly as it is at open, never as a stance of its own."""
    mirror = await box.open(_record("default"))
    box.service._mirrors[CHAT_ID] = mirror
    record = _record("default")
    record["permission_mode"] = "accept_edits"

    await box.service._ensure_mirror(CHAT_ID, record)

    assert mirror.session is not None and mirror.session.permission_mode == "read_only"


async def test_a_row_moving_a_running_chat_into_bypass_tells_the_model_the_prompts_are_off(
    box: _Box,
) -> None:
    """The stance a reader picks to leave a long job running unattended has to
    reach the MODEL, not only the policy: a session told it is in read-only
    while the box auto-allows everything spends the turn refusing work nobody
    refused. The switch notice names the move and the new stance's own rule,
    and the read-only sentence must be gone from the turn it lands on."""
    mirror = await box.open(_record("read_only"))
    box.service._mirrors[CHAT_ID] = mirror
    await _steering_of_next_prompt(box, 0, "what mode are you in")

    await box.service._ensure_mirror(CHAT_ID, _record("bypass"))

    assert mirror.permission_mode == "bypass"
    assert mirror.session is not None and mirror.session.permission_mode == "bypass"
    steering = await _steering_of_next_prompt(box, 0, "and now?")
    assert (
        f"switched the permission mode from {MODE_LABELS['read_only']} "
        f"to {MODE_LABELS['bypass']}" in steering
    )
    assert STEERING["bypass"] in steering
    assert "All permission prompts are off" in steering
    assert STEERING["read_only"] not in steering


async def test_a_reopened_chat_runs_in_the_current_record_not_the_stance_it_was_born_with(
    box: _Box,
) -> None:
    """The first life of the chat runs read-only and steers the model so; its
    manifest and pinned harness session now say read-only. The second open —
    the same folder, the same pinned session — reads a record that says
    ``default``, and that is what the model is told, with no relay involved."""
    first = await box.open(_record("read_only"))
    steering = await _steering_of_next_prompt(box, 0, "what mode are you in")
    assert STEERING["read_only"] in steering
    assert first.session is not None
    assert first.session.manifest.permission_mode == "read_only"
    await first.stop()

    second = await box.open(_record("default"))

    assert second is not first
    assert second.session is not None
    assert second.session.permission_mode == "default"
    assert second.session.manifest.permission_mode == "default", "the manifest follows the record"
    steering = await _steering_of_next_prompt(box, 1, "what mode are you in now")
    assert STEERING["default"] in steering
    assert STEERING["read_only"] not in steering
