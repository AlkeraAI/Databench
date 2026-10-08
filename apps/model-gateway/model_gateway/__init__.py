"""Alkera model gateway: an authenticated, metered streaming proxy that lets
opencode reach Anthropic / OpenAI / Bedrock through Alkera. Provider secrets
live only here; every request is metered through the registered meter.
"""
