"""Credentials never reach an environment spec: URLs are redacted at capture,
and a spec still holding anything credential-shaped is refused at write."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alkera_cli.environment import (
    LocalRunner,
    SpecSecretError,
    capture,
    find_secrets,
    redact_url,
    render_spec,
    write_spec,
)
from alkera_cli.environment.redact import redact_text
from alkera_cli.environment.spec import EnvironmentSpec, IndexRef, PackageSpec, VcsRef


@pytest.mark.parametrize(
    "url,cleaned,removed",
    [
        pytest.param(
            "https://user:s3cret@pkgs.example/simple",
            "https://pkgs.example/simple",
            True,
            id="userinfo",
        ),
        pytest.param(
            "https://ghp_abcdefghijklmnopqrstuvwxyz0123@github.com/o/r.git",
            "https://github.com/o/r.git",
            True,
            id="token-as-user",
        ),
        pytest.param(
            "https://${USER}:${TOKEN}@pkgs.example/simple",
            "https://${USER}:${TOKEN}@pkgs.example/simple",
            False,
            id="env-references-kept",
        ),
        pytest.param(
            "https://${TOKEN}@pkgs.example/simple",
            "https://${TOKEN}@pkgs.example/simple",
            False,
            id="env-reference-user-only",
        ),
        pytest.param(
            "https://${USER}:literal@pkgs.example/simple",
            "https://pkgs.example/simple",
            True,
            id="half-reference-is-a-secret",
        ),
        pytest.param(
            "https://files.example/a.whl?token=abc&v=1",
            "https://files.example/a.whl",
            True,
            id="query-token-takes-the-whole-query",
        ),
        pytest.param(
            "https://files.example/a.whl#sha256=0123abcd",
            "https://files.example/a.whl#sha256=0123abcd",
            False,
            id="hash-pin-kept",
        ),
        pytest.param(
            "git+https://github.com/o/r.git@v1#egg=tool&subdirectory=pkg",
            "git+https://github.com/o/r.git@v1#egg=tool&subdirectory=pkg",
            False,
            id="egg-and-subdirectory-kept",
        ),
        pytest.param(
            "https://pkgs.example/simple?token=${PIP_TOKEN}",
            "https://pkgs.example/simple?token=${PIP_TOKEN}",
            False,
            id="env-reference-parameter-kept",
        ),
        pytest.param(
            "https://s3.example/a.whl?X-Amz-Signature=zz&X-Amz-Credential=cc",
            "https://s3.example/a.whl",
            True,
            id="presigned",
        ),
        pytest.param(
            "https://conda.anaconda.org/t/tk-0123456789/acme/linux-64",
            "https://conda.anaconda.org/acme/linux-64",
            True,
            id="conda-token-segment",
        ),
        pytest.param("https://pypi.org/simple", "https://pypi.org/simple", False, id="clean"),
        pytest.param(
            "ssh://git@github.com/o/r.git",
            "ssh://git@github.com/o/r.git",
            False,
            id="ssh-login-kept",
        ),
        pytest.param(
            "https://a1b2c3d4e5f6a7b8c9d0e1f2@github.com/o/r.git",
            "https://github.com/o/r.git",
            True,
            id="long-random-user-is-a-token",
        ),
        pytest.param("/srv/wheels", "/srv/wheels", False, id="local-folder"),
    ],
)
def test_redact_url(url: str, cleaned: str, removed: bool) -> None:
    assert redact_url(url) == (cleaned, removed)


def test_redact_text_cleans_every_url_in_a_line() -> None:
    text, removed = redact_text(
        "-i https://a:b@one.example/s --extra-index-url https://two.example/s"
    )
    assert text == "-i https://one.example/s --extra-index-url https://two.example/s"
    assert removed is True


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("https://bob:pw@x.example/simple", id="userinfo"),
        pytest.param("glpat-abcdefghijklmnopqrst", id="gitlab-token"),
        pytest.param("ghp_abcdefghijklmnopqrstuvwxyz0123", id="github-token"),
        pytest.param("AKIAABCDEFGHIJKLMNOP", id="aws-key"),
        pytest.param("Bearer abcdef0123456789abcdef", id="bearer"),
    ],
)
def test_find_secrets_names_where_each_secret_is(value: str) -> None:
    data = {"packages": [{"name": "ok", "url": ""}, {"name": "bad", "url": value}]}
    assert find_secrets(data) == ["packages[1].url"]


def test_a_clean_spec_has_no_findings() -> None:
    spec = EnvironmentSpec(
        packages=[PackageSpec(name="a", version="1", url="https://files.example/a.whl")],
        indexes=[IndexRef(url="https://${U}:${T}@corp.example/simple")],
    )
    assert find_secrets(spec.model_dump(mode="json")) == []


def test_writing_a_spec_with_a_secret_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    spec = EnvironmentSpec(
        packages=[
            PackageSpec(
                name="tool",
                source="vcs",
                vcs=VcsRef(url="https://oauth2:glpat-abcdefghijklmnopqrst@gitlab.example/o/t.git"),
            )
        ]
    )
    with pytest.raises(SpecSecretError) as err:
        write_spec(tmp_path, spec)
    assert err.value.locations == ["packages[0].vcs.url"]
    assert "glpat" not in str(err.value)
    assert list(tmp_path.iterdir()) == []


def test_a_token_in_an_index_url_never_reaches_the_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "pypi-AgEIcHlwaS5vcmcCJGFiY2RlZmdoLWlqa2wtbW5vcC1xcnN0LXV2d3h5ejEyMzQ1"
    (tmp_path / "requirements.txt").write_text(
        f"--index-url https://__token__:{secret}@pkgs.example/simple\nrequests==2.32.3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://ci:hunter2hunter2@ci.example/simple")

    spec = asyncio.run(capture(LocalRunner(), root=str(tmp_path), python=None))
    text = render_spec(spec)

    assert secret not in text and "hunter2" not in text
    assert {(i.url, i.credentials_removed) for i in spec.indexes} == {
        ("https://pkgs.example/simple", True),
        ("https://ci.example/simple", True),
    }


#: URLs that carry a credential in a parameter whose name no fixed list held:
#: a GCS V4 signed URL, GitLab's ``private_token``, a hyphenated
#: ``access-token``, an OAuth ``code`` and a token in the fragment.
CREDENTIAL_URLS = [
    pytest.param(
        "https://storage.googleapis.com/wheels/tool-1.0-py3-none-any.whl"
        "?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Credential=svc%40p.iam%2F20261005"
        "&X-Goog-Date=20261005T000000Z&X-Goog-Expires=604800&X-Goog-Signature=9f8e7d6c5b4a",
        "https://storage.googleapis.com/wheels/tool-1.0-py3-none-any.whl",
        "9f8e7d6c5b4a",
        id="gcs-v4-signed",
    ),
    pytest.param(
        "https://gitlab.example/api/v4/projects/7/packages/pypi/simple?private_token=Zx81kQ2mNa",
        "https://gitlab.example/api/v4/projects/7/packages/pypi/simple",
        "Zx81kQ2mNa",
        id="gitlab-private-token",
    ),
    pytest.param(
        "https://pkgs.example/simple?access-token=Qm9vYmVlcA",
        "https://pkgs.example/simple",
        "Qm9vYmVlcA",
        id="hyphenated-access-token",
    ),
    pytest.param(
        "https://files.example/tool.whl?code=s3cr3tc0de",
        "https://files.example/tool.whl",
        "s3cr3tc0de",
        id="oauth-code",
    ),
    pytest.param(
        "https://files.example/tool.whl#token=fr4gm3nt",
        "https://files.example/tool.whl",
        "fr4gm3nt",
        id="fragment-token",
    ),
]


@pytest.mark.parametrize(("url", "cleaned", "secret"), CREDENTIAL_URLS)
def test_a_credential_in_any_parameter_is_removed_and_the_raw_url_is_flagged(
    url: str, cleaned: str, secret: str
) -> None:
    assert redact_url(url) == (cleaned, True)
    assert find_secrets({"packages": [{"url": url}]}) == ["packages[0].url"]
    assert find_secrets({"packages": [{"url": cleaned}]}) == []


@pytest.mark.parametrize(("url", "cleaned", "secret"), CREDENTIAL_URLS)
def test_a_signed_package_url_never_reaches_the_spec(
    tmp_path: Path, url: str, cleaned: str, secret: str
) -> None:
    """Through the capture's own path: a ``url`` package's direct_url and an
    index named in requirements.txt both come out without the credential and
    marked as having had one removed."""
    from alkera_cli.environment.project import ProjectFacts

    facts = ProjectFacts()
    facts.add_index(url, "find_links", "requirements.txt")
    assert [(i.url, i.credentials_removed) for i in facts.indexes] == [(cleaned, True)]
    spec = EnvironmentSpec(
        packages=[PackageSpec(name="tool", version="1.0", source="url", url=redact_url(url)[0])],
        indexes=facts.indexes,
    )
    assert secret not in render_spec(spec)


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        pytest.param("X-Goog-Signature", True, id="goog-signature"),
        pytest.param("x_amz_security_token", True, id="amz-token"),
        pytest.param("private-token", True, id="hyphen-is-underscore"),
        pytest.param("api_key", True, id="ends-in-key"),
        pytest.param("keyid", True, id="starts-with-key"),
        pytest.param("sig", True, id="sig"),
        pytest.param("code", True, id="code"),
        pytest.param("v", False, id="a-version-is-not"),
        pytest.param("egg", False, id="egg-is-not"),
        pytest.param("subdirectory", False, id="subdirectory-is-not"),
        pytest.param("codec", False, id="code-only-whole"),
    ],
)
def test_which_parameter_names_are_credentials(name: str, secret: bool) -> None:
    from alkera_cli.environment.redact import is_secret_param

    assert is_secret_param(name) is secret


def test_a_named_uv_index_with_credentials_is_captured_without_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``UV_INDEX=name=url`` names the index before its URL. The capture records
    the URL with the credential removed, rather than refusing the whole spec."""
    for key in [k for k in __import__("os").environ if k.startswith(("UV_", "PIP_"))]:
        monkeypatch.delenv(key)
    monkeypatch.setenv(
        "UV_INDEX", "internal=https://deploy:hunter2hunter2@corp.example/simple other=/srv/wheels"
    )
    (tmp_path / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")

    spec = asyncio.run(capture(LocalRunner(), root=str(tmp_path), python=None))
    text = render_spec(spec)

    assert "hunter2" not in text
    assert {(i.url, i.credentials_removed) for i in spec.indexes} == {
        ("https://corp.example/simple", True),
        ("/srv/wheels", False),
    }
