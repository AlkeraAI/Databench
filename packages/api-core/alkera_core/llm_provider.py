"""The upstream LLM providers the model gateway routes to.

One vocabulary for the gateway's routes and costs, a model's catalog row, a
usage record and a bring-your-own-key credential.
"""

from __future__ import annotations

from enum import StrEnum


class Provider(StrEnum):
    """Upstream LLM provider a request is fulfilled by (route + cost keying)."""

    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"
    OPENAI = "openai"


__all__ = ["Provider"]
