"""A cell's outputs as the engine holds them.

:class:`CellOutputs` is plain data: the rich bundles a cell displayed, its
console streams, its error, the provenance of the code that produced them and
the run that did. The ``apply_*`` methods fold kernel notifications in and
enforce the per-cell caps of :class:`~alkera_notebook.engine.config.OutputLimits`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from alkera_notebook.engine.config import OutputLimits
from alkera_notebook.engine.models import (
    DisplayOutput,
    ErrorInfo,
    ErrorOutput,
    OutputDetail,
    OutputItem,
    OutputPart,
    OutputSummary,
    RunActor,
    RunAttribution,
    StreamOutput,
)
from alkera_notebook.outputs.console import ConsoleScreen

MimeBundle = dict[str, Any]
Origin = Literal["kernel", "saved", "unknown"]

IMAGE_MIMES: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
)
CHART_MIMES: tuple[str, ...] = (
    "application/vnd.alkera.chart+json",
    "application/vnd.vegalite.v6+json",
    "application/vnd.vegalite.v5+json",
    "application/vnd.plotly.v1+json",
)
TABLE_MIME = "application/vnd.alkera.table+json"
WIDGET_MIME = "application/vnd.jupyter.widget-view+json"

STREAM_HEAD_BYTES = 64 * 1024
SUMMARY_TEXT_CHARS = 2000


@dataclass
class RunMeta:
    run_id: str
    trigger: str
    by: RunActor
    status: str = "running"
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class StreamItem:
    name: Literal["stdout", "stderr"]
    text: str
    # How many rich outputs the cell had shown when this text began: where
    # it stands among them, in the order the kernel sent them.
    after: int = field(default=0, compare=False)
    # The folded screen behind ``text`` while the stream is still written to.
    screen: ConsoleScreen | None = field(default=None, compare=False, repr=False)


def value_bytes(value: Any) -> int:
    """Size of a MIME value as it is serialized."""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))


def bundle_bytes(bundle: MimeBundle) -> int:
    return sum(value_bytes(v) for v in bundle.values())


def _plain_fallback(bundle: MimeBundle) -> str:
    if TABLE_MIME in bundle:
        return "<table>"
    if any(m in bundle for m in CHART_MIMES):
        return "<chart>"
    if WIDGET_MIME in bundle:
        return "<widget>"
    if any(m in bundle for m in IMAGE_MIMES):
        return "<image>"
    if "text/html" in bundle:
        return "<html>"
    return "<output>"


def with_plain(bundle: MimeBundle) -> MimeBundle:
    """The bundle with a ``text/plain`` entry (every bundle carries one)."""
    if isinstance(bundle.get("text/plain"), str):
        return bundle
    return {**bundle, "text/plain": _plain_fallback(bundle)}


def placeholder(size: int, limit: int) -> MimeBundle:
    return {
        "text/plain": f"Output too large to display ({size} bytes; the limit is {limit} bytes)",
        "application/vnd.alkera.placeholder+json": {"bytes": size, "limit": limit},
    }


def _cut_head(data: bytes, n: int) -> str:
    return data[:n].decode("utf-8", errors="ignore")


def _cut_tail(data: bytes, n: int) -> str:
    return data[len(data) - n :].decode("utf-8", errors="ignore") if n else ""


@dataclass
class CellOutputs:
    bundles: list[MimeBundle] = field(default_factory=list)
    console: list[StreamItem] = field(default_factory=list)
    error: ErrorInfo | None = None
    code_hash: str = ""
    lineage_hash: str | None = None
    env_fingerprint: str | None = None
    run: RunMeta | None = None
    origin: Origin = "kernel"
    stream_omitted: int = 0

    # Folding kernel notifications in ------------------------------------------

    def begin_run(
        self,
        run: RunMeta,
        *,
        code_hash: str,
        lineage_hash: str | None,
        env_fingerprint: str | None,
    ) -> None:
        """A new execution of the cell: previous outputs are cleared."""
        self.bundles = []
        self.console = []
        self.error = None
        self.stream_omitted = 0
        self.origin = "kernel"
        self.run = run
        self.code_hash = code_hash
        self.lineage_hash = lineage_hash
        self.env_fingerprint = env_fingerprint

    def apply_output(
        self, bundle: MimeBundle, mode: Literal["replace", "append"], limits: OutputLimits
    ) -> MimeBundle:
        """Fold a ``cell.output``; returns the bundle as stored (capped)."""
        base = [] if mode == "replace" else self.bundles
        used = sum(bundle_bytes(b) for b in base)
        size = bundle_bytes(bundle)
        cap = limits.rich_bytes_per_cell
        stored = with_plain(bundle) if used + size <= cap else placeholder(size, cap)
        self.bundles = [*base, stored]
        return stored

    def apply_stream(
        self, name: Literal["stdout", "stderr"], text: str, limits: OutputLimits
    ) -> None:
        """Fold a ``cell.stream``: consecutive same-name text merges.

        The kernel only appends; carriage returns, cursor-up and erase-line
        are applied here, across batches, so the stored text is what the
        stream shows (a redrawn progress bar keeps only its last state)."""
        last = self.console[-1] if self.console else None
        shown = len(self.bundles)
        if last is None or last.name != name or _is_note(last) or last.after != shown:
            # A rich output between two writes splits them: each keeps its place.
            if last is not None:
                last.screen = None  # closed: only the open item is folded into
            last = StreamItem(name, "", after=shown)
            self.console.append(last)
        if last.screen is None:
            last.screen = ConsoleScreen.from_text(last.text)
        last.screen.feed(text)
        last.text = last.screen.text()
        self._cap_streams(limits.stream_bytes_per_cell)

    def _cap_streams(self, cap: int) -> None:
        items = [i for i in self.console if not _is_note(i)]
        total = sum(len(i.text.encode("utf-8")) for i in items)
        if total <= cap:
            return
        head_budget = min(STREAM_HEAD_BYTES, cap)
        tail_budget = cap - head_budget
        head: list[StreamItem] = []
        for item in items:
            if head_budget <= 0:
                break
            data = item.text.encode("utf-8")
            if len(data) <= head_budget:
                head.append(item)
                head_budget -= len(data)
            else:
                head.append(StreamItem(item.name, _cut_head(data, head_budget), item.after))
                head_budget = 0
        tail: list[StreamItem] = []
        for item in reversed(items):
            if tail_budget <= 0:
                break
            data = item.text.encode("utf-8")
            if len(data) <= tail_budget:
                tail.append(item)
                tail_budget -= len(data)
            else:
                tail.append(StreamItem(item.name, _cut_tail(data, tail_budget), item.after))
                tail_budget = 0
        tail.reverse()
        last = self.console[-1]
        if tail and tail[-1] is not last and last.screen is not None:
            # The open item was cut: its screen forgets the dropped start and
            # keeps folding from where its cursor was.
            last.screen.drop_head(len(last.text) - len(tail[-1].text))
            tail[-1].screen = last.screen
        for item in head:
            item.screen = None
        kept = sum(len(i.text.encode("utf-8")) for i in head + tail)
        # Earlier caps already dropped bytes; their note is replaced by this one.
        self.stream_omitted += total - kept
        note = StreamItem(
            "stderr",
            f"\n[... {self.stream_omitted} bytes of output omitted ...]\n",
            tail[0].after if tail else (head[-1].after if head else 0),
        )
        self.console = [*head, note, *tail]

    def apply_finished(self, error: ErrorInfo | None, run_status: str | None = None) -> None:
        self.error = error
        if self.run is not None and run_status is not None:
            self.run.status = run_status

    # Reading -----------------------------------------------------------------

    def kinds(self) -> list[str]:
        out: list[str] = []
        for b in self.bundles:
            for m in b:
                if m not in out:
                    out.append(m)
        if self.console and "stream" not in out:
            out.append("stream")
        if self.error is not None:
            out.append("error")
        return out

    def plain_text(self) -> str:
        parts = [i.text for i in self.console]
        parts += [str(b.get("text/plain", "")) for b in self.bundles]
        return "".join(p if p.endswith("\n") or not p else p + "\n" for p in parts)

    def _first(self, mimes: tuple[str, ...]) -> Any:
        for b in self.bundles:
            for m in mimes:
                if m in b:
                    return b[m]
        return None

    def summary(self) -> OutputSummary:
        text = self.plain_text()
        truncated = len(text) > SUMMARY_TEXT_CHARS
        return OutputSummary(
            kinds=self.kinds(),
            text=text[:SUMMARY_TEXT_CHARS],
            error=self.error,
            truncated=truncated or self.stream_omitted > 0,
            has_image=any(m in b for b in self.bundles for m in IMAGE_MIMES),
            has_chart=any(m in b for b in self.bundles for m in CHART_MIMES),
            has_table=any(TABLE_MIME in b for b in self.bundles),
            has_widget=any(WIDGET_MIME in b for b in self.bundles),
        )

    def attribution(self) -> RunAttribution | None:
        if self.run is None:
            return None
        return RunAttribution(
            run_id=self.run.run_id,
            by=self.run.by,
            trigger=self.run.trigger,
            started_at=self.run.started_at,
            finished_at=self.run.finished_at,
        )

    def items(self, cell_id: str) -> list[OutputItem]:
        """The outputs as a client shows them: the rich bundles and the
        console in the order the kernel sent them (text printed before the
        cell's value stays above it), then the error. Ids are stable for
        unchanged outputs."""
        out: list[OutputItem] = []
        streams = iter(enumerate(self.console))
        pending = next(streams, None)
        for i, b in enumerate([*self.bundles, None]):
            while pending is not None and (b is None or pending[1].after <= i):
                n, item = pending
                out.append(
                    StreamOutput(output_id=f"{cell_id}/s{n}", name=item.name, text=item.text)
                )
                pending = next(streams, None)
            if b is not None:
                out.append(DisplayOutput(output_id=f"{cell_id}/{i}", data=dict(b)))
        if self.error is not None:
            out.append(ErrorOutput(output_id=f"{cell_id}/error", error=self.error))
        return out

    def detail(self, part: OutputPart = "all", max_chars: int = 20000) -> OutputDetail:
        every = part == "all"
        text = self.plain_text() if every or part == "text" else ""
        chart = self._first(CHART_MIMES) if every or part == "chart" else None
        table = self._first((TABLE_MIME,)) if every or part == "table" else None
        return OutputDetail(
            text=text[:max_chars],
            truncated=len(text) > max_chars or self.stream_omitted > 0,
            error=self.error if every or part == "error" else None,
            images=[str(b["image/png"]) for b in self.bundles if "image/png" in b]
            if every or part == "image"
            else [],
            chart_spec=chart if isinstance(chart, dict) else None,
            table=table if isinstance(table, dict) else None,
            widgets=[
                b[WIDGET_MIME]
                for b in self.bundles
                if WIDGET_MIME in b and isinstance(b[WIDGET_MIME], dict)
            ]
            if every or part == "widget"
            else [],
            run=self.attribution(),
        )


def _is_note(item: StreamItem) -> bool:
    return (
        item.name == "stderr"
        and item.text.startswith("\n[... ")
        and item.text.endswith(" bytes of output omitted ...]\n")
    )
