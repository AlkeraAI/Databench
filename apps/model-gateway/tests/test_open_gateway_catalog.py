"""An open deployment, seeded the way `init` seeds it, can list a model and chat.

The probe runs in a fresh interpreter with no private extension installed: the
gateway an open deployment runs. It runs the open seeds, sets one provider's
key, and asks the gateway for its models and for a turn on one of them. The
provider upstream is a mock behind the gateway's own HTTP client.

What it pins: the default catalog arrives with the open seeds, a provider whose
key is set lists its models and one without a key lists none, and a request is
routed by a gateway nothing meters, which reads no price.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

PROBE = textwrap.dedent(
    """
    import asyncio, json, secrets, sys
    from datetime import UTC, datetime

    import httpx
    from alkera_core.auth import encode_cli_token, register_token
    from alkera_core.config import settings
    from alkera_core.db.session import AsyncSessionLocal
    from alkera_core.extensions import installed_extensions
    from alkera_core.models import Team, TokenType, User
    from backend.seeds import run_seeds
    from httpx import ASGITransport, AsyncClient
    from model_gateway.app_factory import create_app

    SSE = "".join(
        f"event: {name}\\ndata: {json.dumps(data)}\\n\\n"
        for name, data in [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_1", "type": "message", "role": "assistant", "content": [],
                "model": "x", "usage": {"input_tokens": 5, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": "hello"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 3}}),
            ("message_stop", {"type": "message_stop"}),
        ]
    ).encode()


    async def main():
        settings.anthropic_api_key = "sk-ant-test"
        settings.openai_api_key = None
        await run_seeds(AsyncSessionLocal)
        async with AsyncSessionLocal() as s:
            team = Team(name=f"org-{secrets.token_hex(4)}", is_root=True)
            s.add(team)
            await s.flush()
            user = User(home_org_team_id=team.id, email=f"u-{secrets.token_hex(6)}@alkera.dev",
                        first_name="U", last_name="User", email_verified_at=datetime.now(UTC))
            s.add(user)
            await s.flush()
            token, claims = encode_cli_token(user_id=user.id, email=user.email,
                                             org_team_id=team.id, platform_role=None)
            await register_token(s, claims=claims, token_type=TokenType.CLI)
            await s.commit()

        app = create_app()
        upstream = []

        def handler(request):
            upstream.append(json.loads(request.content).get("model"))
            return httpx.Response(200, content=SSE, headers={"content-type": "text/event-stream"})

        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        auth = {"Authorization": f"Bearer {token}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw") as c:
            listed = await c.get("/v1/models", headers=auth)
            data = listed.json().get("data", []) if listed.status_code == 200 else []
            listed_ids = {m["id"] for m in data}
            chosen = "claude-sonnet-5.5" if "claude-sonnet-5.5" in listed_ids else None
            turn = None
            if chosen:
                turn = await c.post("/anthropic/v1/messages", headers=auth, json={
                    "model": chosen, "max_tokens": 16, "stream": True,
                    "messages": [{"role": "user", "content": "hi"}]})
        print("REPORT" + json.dumps({
            "installed": list(installed_extensions()),
            "status": listed.status_code,
            "providers": sorted({m.get("provider") or m.get("owned_by") for m in data}),
            "models": [m["id"] for m in data],
            "turn": turn.status_code if turn is not None else None,
            "turn_body": turn.text[-300:] if turn is not None else None,
            "upstream": upstream,
        }))

    asyncio.run(main())
    """
)


def _boot(tmp_path: Path) -> dict[str, Any]:
    env = {**os.environ, "ALKERA_HOME": str(tmp_path / "home"), "NO_COLOR": "1"}
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    line = next(r for r in result.stdout.splitlines() if r.startswith("REPORT"))
    report: dict[str, Any] = json.loads(line.removeprefix("REPORT"))
    return report


@pytest.mark.timeout(300)
def test_the_open_stack_lists_the_keyed_providers_models_and_routes_a_turn(
    tmp_path: Path,
) -> None:
    report = _boot(tmp_path)

    assert report["installed"] == []
    assert report["status"] == 200
    # The default catalog arrived with the open seeds, filtered to the one
    # provider whose key is set.
    assert report["models"], report
    assert "claude-sonnet-5.5" in report["models"]
    assert not any(slug.startswith("gpt-") for slug in report["models"])
    # A turn routes to the provider through the unmetered gateway, which reads
    # no price: nothing in this process installed a meter.
    assert report["turn"] == 200, report["turn_body"]
    assert report["upstream"] == ["claude-sonnet-5-5"]
