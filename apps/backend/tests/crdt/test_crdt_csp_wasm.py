"""The portal may compile WebAssembly (the live draft's Loro), and nothing more.

Every CSP the SPA is served under — the hosted CDN's for prod and staging, and
the self-hosted image's default — admits ``'wasm-unsafe-eval'`` in
``script-src`` and never ``'unsafe-eval'``, which would let any string become
code. And both deploy paths upload the ``.wasm`` asset as ``application/wasm``,
the only type a browser compiles it from.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
CDN_POLICIES = {
    "prod": ROOT / "ops" / "terraform" / "envs" / "prod" / "app" / "web-cdn.tf",
    "staging": ROOT / "ops" / "terraform" / "envs" / "staging" / "app" / "web-cdn.tf",
}
ENTRYPOINT = ROOT / "apps" / "web" / "docker-entrypoint.sh"
DEPLOYS = [ROOT / ".github" / "workflows" / name for name in ("deploy-env.yml", "deploy-ecs.yml")]


def _cdn_script_src(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    policy = text[text.index("csp_policy = join(") :]
    policy = policy[: policy.index("])")]
    [script] = [p for p in re.findall(r'"([^"]*)"', policy) if p.startswith("script-src ")]
    return script


def _entrypoint_script_src() -> str:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    default = re.search(r'ALKERA_CSP:=([^"]*)\}"', text)
    assert default is not None
    [script] = [
        d.strip() for d in default.group(1).split(";") if d.strip().startswith("script-src")
    ]
    return script


@pytest.mark.parametrize(
    "script_src",
    [
        *(pytest.param(_cdn_script_src(p), id=f"cdn-{env}") for env, p in CDN_POLICIES.items()),
        pytest.param(_entrypoint_script_src(), id="self-hosted-image"),
    ],
)
def test_script_src_admits_webassembly_and_never_eval(script_src: str) -> None:
    sources = script_src.split()[1:]
    assert "'wasm-unsafe-eval'" in sources
    assert "'unsafe-eval'" not in sources
    assert "'self'" in sources


@pytest.mark.parametrize("path", [pytest.param(p, id=p.name) for p in CDN_POLICIES.values()])
def test_no_cdn_policy_admits_eval_anywhere(path: Path) -> None:
    assert "'unsafe-eval'" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("workflow", [pytest.param(p, id=p.name) for p in DEPLOYS])
def test_the_spa_deploy_uploads_webassembly_as_application_wasm(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    upload = re.search(
        r'aws s3 cp apps/web/dist "s3://\$\{BUCKET\}" --recursive --exclude "\*" '
        r'--include "\*\.wasm" \\\s*'
        r'--content-type "application/wasm"',
        text,
    )
    assert upload is not None
    # After the sync that would otherwise have guessed the type, not before it.
    assert upload.start() > text.index('aws s3 sync apps/web/dist "s3://${BUCKET}"')
