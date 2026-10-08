# Security policy

## Reporting a vulnerability

Report privately, never in a public issue or pull request. Either:

- use GitHub's **Report a vulnerability** button on this repository's Security tab, or
- email **security@alkera.ai** with the details and, if you can, a proof of concept.

We acknowledge a report within 2 business days and send a first assessment within 5 business days. We keep you updated until it is fixed, and credit you in the advisory if you want.

Give us a reasonable window to fix the problem before you disclose it. We publish a GitHub security advisory, with a CVE where one applies, when the fix is released.

## Scope

In scope is the code in this repository: the backend, worker, model gateway, web app, CLI and daemon, box supervisor, notebook packages, and the Docker images and Compose files built from it.

Out of scope:

- vulnerabilities in a dependency or vendored project that this repository does not make worse. Report those upstream. Tell us too if this project is exposed.
- denial of service by volume alone.
- findings that need an already compromised host or physical access.
- a deployment's own configuration, such as a missing TLS proxy or a weak secret chosen by the operator.

## Supported versions

Fixes land on `main` and in the latest release. Self-hosted installs should track the latest release. `GET /health/info` reports the running version and build.

## Hardening a deployment

- Set every secret in the reference Compose file to a fresh random value, including `SECRET_BOX_KEY`, which encrypts stored credentials.
- Terminate TLS in front of the `web` container. Keep the Temporal UI bound to localhost; it has no authentication.
- `APP_ENV=production` refuses to start with insecure defaults. Do not work around that check.
