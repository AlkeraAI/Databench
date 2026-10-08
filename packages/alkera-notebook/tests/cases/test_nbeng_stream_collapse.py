"""Console streams fold like a terminal, across batches: carriage return,
cursor-up and erase-line. The kernel only appends, so the engine must keep
the cursor between ``cell.stream`` notifications."""

from __future__ import annotations

from pathlib import Path

import pytest
from alkera_notebook.engine.config import OutputLimits
from alkera_notebook.outputs import CellOutputs, StreamItem
from alkera_notebook.outputs.console import ConsoleScreen, collapse
from nbeng_harness import notebook, run_cells, text_of

ESC = "\x1b"


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        pytest.param("plain\ntext\n", "plain\ntext\n", id="plain_text_unchanged"),
        pytest.param("abc\rx", "xbc", id="cr_overwrites_not_erases"),
        pytest.param("abc\r\n", "abc\n", id="cr_lf_is_a_newline"),
        pytest.param("10%\r20%\r30%", "30%", id="cr_redraw_keeps_last"),
        pytest.param("ab\bc", "ac", id="backspace_moves_left"),
        pytest.param("\b\bx", "x", id="backspace_stops_at_column_zero"),
        pytest.param(f"a\nb{ESC}[1Ac", "ac\nb", id="cursor_up_one"),
        pytest.param(f"a\nb{ESC}[Ac", "ac\nb", id="cursor_up_default_one"),
        pytest.param(f"a\nb\nc{ESC}[2A\rX", "X\nb\nc", id="cursor_up_n"),
        pytest.param(f"a\nb{ESC}[9A\rX", "X\nb", id="cursor_up_clamps_at_top"),
        pytest.param(f"a{ESC}[1Ab", "ab", id="cursor_up_on_first_line_stays"),
        pytest.param(f"hello\r{ESC}[Kbye", "bye", id="erase_to_end"),
        pytest.param(f"hello\r{ESC}[0Kbye", "bye", id="erase_to_end_explicit"),
        pytest.param(f"hello\b\b{ESC}[1K", "    o", id="erase_to_cursor"),
        pytest.param(f"hello{ESC}[2K", "", id="erase_line"),
        pytest.param(f"hello{ESC}[2Kx", "     x", id="erase_line_keeps_column"),
        pytest.param(f"hello{ESC}[2K\rx", "x", id="erase_line_then_cr"),
        pytest.param(
            f"{ESC}[31mred{ESC}[0m\n", f"{ESC}[31mred{ESC}[0m\n", id="colour_kept_as_written"
        ),
        pytest.param(f"ab{ESC}[31mcd\r123", "123cd", id="escape_is_one_cell_never_split"),
        pytest.param(f"x{ESC}[2Jy", f"x{ESC}[2Jy", id="other_csi_kept"),
        pytest.param(f"x{ESC}[2;3Ay", f"x{ESC}[2;3Ay", id="cursor_up_with_two_params_kept"),
        pytest.param(f"x{ESC}[3Ky", f"x{ESC}[3Ky", id="unknown_erase_mode_kept"),
        pytest.param(f"x{ESC}(By", f"x{ESC}(By", id="non_csi_escape_kept"),
        pytest.param(f"top\nbar 1{ESC}[1A\n", "top\nbar 1", id="up_then_newline_moves_back"),
    ],
)
def test_collapse_one_batch(text: str, shown: str) -> None:
    assert collapse(text) == shown


@pytest.mark.parametrize(
    ("batches", "shown"),
    [
        pytest.param(["abc\r", "x"], "xbc", id="cr_at_batch_end_overwrites_next_batch"),
        pytest.param(["abc", "\r", "\n", "d"], "abc\nd", id="cr_lf_split"),
        pytest.param(
            [
                "  0%|          |",
                "\r 10%|#",
                "         |",
                *[f"\r{p:3d}%|" for p in range(20, 100, 10)],
                "\r100%|##########|\n",
                "done\n",
            ],
            "100%|##########|\ndone\n",
            id="progress_bar_over_many_batches_ends_as_final_line",
        ),
        pytest.param(
            ["outer 1\ninner 1", f"{ESC}[1A\router 2", "\n\rinner 2"],
            "outer 2\ninner 2",
            id="two_bars_redrawn_with_cursor_up_across_batches",
        ),
        pytest.param(["a\nb", ESC, "[", "1", "A", "X"], "aX\nb", id="cursor_up_split_byte_by_byte"),
        pytest.param([f"hello\r{ESC}[", "K", "bye"], "bye", id="erase_split_across_batches"),
        pytest.param(["hello", f"{ESC}[2K", "\rnew"], "new", id="erase_line_in_its_own_batch"),
        pytest.param(["x", ESC], "x", id="unfinished_escape_held_back"),
        pytest.param(["x", ESC, "(By"], f"x{ESC}(By", id="held_escape_released_when_not_csi"),
    ],
)
def test_screen_folds_across_batches(batches: list[str], shown: str) -> None:
    screen = ConsoleScreen()
    for batch in batches:
        screen.feed(batch)
    assert screen.text() == shown


SAMPLE = (
    f"epoch 1\n  0%\r 50%\r100%\nloss{ESC}[31m0.5{ESC}[0m\nA\nB{ESC}[1A\r{ESC}[2Kz\n\bq{ESC}[K!"
)


@pytest.mark.parametrize("cut", range(len(SAMPLE) + 1))
def test_any_split_shows_the_same_as_one_batch(cut: int) -> None:
    screen = ConsoleScreen()
    screen.feed(SAMPLE[:cut])
    screen.feed(SAMPLE[cut:])
    assert screen.text() == collapse(SAMPLE)


@pytest.mark.parametrize(
    ("text", "rest", "shown"),
    [
        pytest.param("one\ntwo\nthree", "\r", "ree", id="start_of_last_line"),
        pytest.param("one\ntwo\nthree", f"{ESC}[1A\r", "o\nthree", id="line_above"),
        pytest.param("abc", "\b", "", id="inside_the_cut_line"),
        pytest.param("one\ntwo\nthree", "\b\b", "", id="inside_the_line"),
        pytest.param("one\ntwo", f"{ESC}[2K", "", id="past_an_erased_line"),
    ],
)
@pytest.mark.parametrize(
    "prefix",
    [
        pytest.param("PREFIX", id="part_of_the_first_line"),
        pytest.param("old 1\nold 2\nPRE", id="whole_lines_and_part"),
        pytest.param("old\n", id="whole_lines_only"),
    ],
)
def test_dropping_the_head_keeps_the_cursor(text: str, rest: str, shown: str, prefix: str) -> None:
    control = ConsoleScreen.from_text(prefix + text)
    control.feed(rest)
    cut = ConsoleScreen.from_text(prefix + text)
    cut.feed(rest)
    cut.drop_head(len(prefix))
    assert cut.text() == control.text().removeprefix(prefix)
    control.feed("XY")
    cut.feed("XY")
    assert cut.text() == control.text().removeprefix(prefix)
    assert cut.text().endswith("XY" + shown)


LIMITS = OutputLimits()


def test_cell_outputs_fold_a_progress_bar_into_its_last_state() -> None:
    out = CellOutputs()
    for pct in range(0, 101, 5):
        out.apply_stream("stderr", f"\r{pct:3d}%|{'#' * (pct // 10):<10}|", LIMITS)
    out.apply_stream("stderr", "\n", LIMITS)
    out.apply_stream("stdout", "trained\n", LIMITS)
    assert [(i.name, i.text) for i in out.console] == [
        ("stderr", "100%|##########|\n"),
        ("stdout", "trained\n"),
    ]
    assert out.summary().text == "100%|##########|\ntrained\n"


def test_a_return_never_reaches_back_past_another_stream() -> None:
    out = CellOutputs()
    out.apply_stream("stdout", "50%", LIMITS)
    out.apply_stream("stderr", "warn\n", LIMITS)
    out.apply_stream("stdout", "\r99%", LIMITS)
    assert [(i.name, i.text) for i in out.console] == [
        ("stdout", "50%"),
        ("stderr", "warn\n"),
        ("stdout", "99%"),
    ]


def test_a_saved_stream_continues_from_its_text() -> None:
    out = CellOutputs(console=[StreamItem("stdout", "abc")])
    out.apply_stream("stdout", "\rX", LIMITS)
    assert [(i.name, i.text) for i in out.console] == [("stdout", "Xbc")]


def test_a_capped_stream_keeps_its_cursor() -> None:
    limits = OutputLimits(stream_bytes_per_cell=64 * 1024 + 200)
    out = CellOutputs()
    out.apply_stream("stdout", "H" * (64 * 1024), limits)
    out.apply_stream("stdout", "x" * 1000 + "\nbar  50%\r", limits)  # this batch is cut
    assert any("omitted" in i.text for i in out.console)
    out.apply_stream("stdout", "bar 100%\n", limits)
    assert out.console[-1].text.endswith("\nbar 100%\n")
    assert "50%" not in out.console[-1].text


@pytest.mark.parametrize(
    ("code", "shown"),
    [
        pytest.param(
            "import sys, time\n"
            "for i in range(6):\n"
            "    sys.stdout.write(f'\\rstep {i}/5')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.08)\n"
            "print()\n"
            "print('done')",
            "step 5/5\ndone",
            id="carriage_return_bar_flushed_in_separate_batches",
        ),
        pytest.param(
            "import sys, time\n"
            "for i in range(4):\n"
            "    sys.stdout.write(f'a {i}\\nb {i}\\n')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.08)\n"
            "    if i < 3:\n"
            "        sys.stdout.write('\\x1b[2A\\x1b[2K')\n"
            "        sys.stdout.flush()\n"
            "        time.sleep(0.08)",
            "a 3\nb 3",
            id="cursor_up_redraw_flushed_in_separate_batches",
        ),
    ],
)
async def test_real_kernel_stream_is_stored_folded(tmp_path: Path, code: str, shown: str) -> None:
    from nbeng_harness import engine_for

    async with engine_for(tmp_path) as engine:
        _, ann, (a,) = await notebook(engine, [code])
        record = await run_cells(ann, a)
        assert record.status == "ok"
        assert await text_of(ann, a) == shown
