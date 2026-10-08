"""``fetch_result`` — the core tool that pages a spilled result blob.

A large tool result spills to a content-addressed blob and the model gets a
``BlobHandle`` instead of an inline dump. ``fetch_result``
is how the model reads it back — offset/limit pagination over the canonical
envelope (rows for tabular results, characters for text). It is **hot** (in the
always-on prefix) because a handle can come from *any* tool, so the model must
be able to dereference it without a search first.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from alkera_cli.contracts.tool_types import Effect
from alkera_cli.plugins.plugin_base.delivery import FetchResultOutput, fit_page
from alkera_cli.plugins.plugin_base.result_blob import paginate
from alkera_cli.plugins.plugin_base.tool import Tool, ToolContext, ToolError, ToolRegistry, ToolSpec


class FetchResultInput(BaseModel):
    handle: str
    """The ``sha256`` of a result blob (from a tool result's ``blob`` field)."""
    offset: int = 0
    limit: int | None = None
    """Page size (rows for a tabular result, characters for text). Omit for the
    per-kind default."""


class FetchResultTool(Tool[FetchResultInput, FetchResultOutput]):
    spec: ClassVar[ToolSpec] = ToolSpec(
        name="fetch_result",
        title="Fetch a large result",
        description=(
            "Page through a large result a tool spilled to a blob handle. Pass the "
            "handle from a result's 'blob' field, plus offset (and an optional limit; "
            "the unit is rows for a tabular result, characters for text — omit limit "
            "for a sensible default). The reply carries 'total', 'has_more', and "
            "'next_offset' — keep fetching from 'next_offset' until 'has_more' is false. "
            "These rows are the STRUCTURE of a large result — to compute an answer over "
            "it (filter / aggregate / stats) prefer blob.query or blob.profile rather "
            "than paging the whole thing back into context. "
            'Non-finite numbers appear as {"$nonfinite": "nan"|"inf"|"-inf"} (NaN / '
            "±Infinity)."
        ),
        hot=True,
        effect_hint=Effect.READ,
    )
    Input: ClassVar[type[BaseModel]] = FetchResultInput
    Output: ClassVar[type[BaseModel]] = FetchResultOutput

    async def run(self, args: FetchResultInput, ctx: ToolContext) -> FetchResultOutput:
        try:
            # The reader hands back a fixed-size window; the door fits the full
            # output model to the inline cap, so a page can never be re-wrapped
            # by a second bound.
            window = paginate(ctx.blobs, args.handle, offset=args.offset, limit=args.limit)
            return fit_page(FetchResultOutput(**window))
        except FileNotFoundError as exc:
            raise ToolError(
                f"no result blob {args.handle!r} (it may have been garbage-collected)"
            ) from exc
        except ValueError as exc:
            raise ToolError(f"invalid blob handle: {exc}") from exc


def register_blob_tools(registry: ToolRegistry) -> None:
    """Register the hot ``fetch_result`` tool. Always registered (core) so any
    spilled handle is dereferenceable."""
    registry.register(FetchResultTool)


__all__ = [
    "FetchResultInput",
    "FetchResultTool",
    "register_blob_tools",
]
