#!/usr/bin/env python
"""Re-measure the gateway's per-family token calibration.

The admission hold estimates input tokens from the request body (see
``model_gateway.estimate``). The ratio it uses is measured, not guessed: the
recorded real provider interactions under
``vendor/opencode/packages/llm/test/fixtures/recordings/`` each pair a genuine
request body with the ``input_tokens`` that provider reported for it.

Run this after the recordings are refreshed, or when adding a family, and move
``CALIBRATIONS`` to sit BELOW the aggregate ratio on the bulk-text samples (so
the estimate stays an over-estimate), then update the table in that module's
docstring with what you measured.

    uv run python scripts/calibrate_token_estimate.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "apps" / "model-gateway"))

from model_gateway.adapters import _body_char_count  # noqa: E402
from model_gateway.estimate import estimate_input_tokens  # noqa: E402

RECORDINGS = REPO / "vendor/opencode/packages/llm/test/fixtures/recordings"


def sse_objects(raw: str) -> list[dict[str, Any]]:
    out = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def reported_input_tokens(url: str, raw: str) -> tuple[str, int] | None:
    if "anthropic.com" in url:
        for obj in sse_objects(raw):
            if obj.get("type") == "message_start":
                usage = obj.get("message", {}).get("usage", {}) or {}
                return "anthropic", (
                    int(usage.get("input_tokens", 0) or 0)
                    + int(usage.get("cache_read_input_tokens", 0) or 0)
                    + int(usage.get("cache_creation_input_tokens", 0) or 0)
                )
        return None
    if "openai.com" in url:
        tokens = 0
        for obj in sse_objects(raw):
            usage = obj.get("response", {}).get("usage") if obj.get("response") else None
            if isinstance(usage, dict):
                tokens = max(tokens, int(usage.get("input_tokens", 0) or 0))
        return ("openai", tokens) if tokens else None
    return None


def main() -> int:
    samples: dict[str, list[tuple[int, int, int, str]]] = defaultdict(list)
    for path in sorted(RECORDINGS.rglob("*.json")):
        recording = json.loads(path.read_text())
        for index, interaction in enumerate(recording.get("interactions", [])):
            request, response = interaction.get("request") or {}, interaction.get("response") or {}
            body, raw = request.get("body"), response.get("body")
            if not isinstance(body, str) or not isinstance(raw, str):
                continue
            try:
                parsed = json.loads(body)
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                continue
            found = reported_input_tokens(request.get("url", ""), raw)
            if found is None or found[1] <= 0:
                continue
            family, tokens = found
            samples[family].append(
                (
                    _body_char_count(parsed),
                    tokens,
                    estimate_input_tokens(parsed, family=family),
                    f"{path.parent.name}/{path.stem}#{index}",
                )
            )

    if not samples:
        print(f"no recordings under {RECORDINGS}", file=sys.stderr)
        return 1

    for family, rows in sorted(samples.items()):
        rows.sort(key=lambda r: -r[0])
        bulk = [r for r in rows if r[0] > 5_000]
        print(f"=== {family}: {len(rows)} samples ({len(bulk)} over 5k chars) ===")
        if bulk:
            print(
                "  chars/token on bulk text: "
                f"{sum(r[0] for r in bulk) / sum(r[1] for r in bulk):.2f}"
            )
        print(f"{'chars':>9} {'reported':>9} {'estimate':>9} {'est/real':>9}  case")
        for chars, tokens, estimate, name in rows:
            print(f"{chars:>9} {tokens:>9} {estimate:>9} {estimate / tokens:>9.2f}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
