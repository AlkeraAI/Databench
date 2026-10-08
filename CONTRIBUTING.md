# Contributing

Thank you for helping. This file covers how to propose a change and what a change needs before it can merge. [AGENTS.md](AGENTS.md) covers how the code is laid out and the rules it follows.

## Developer Certificate of Origin

Every commit must be signed off under the [Developer Certificate of Origin 1.1](https://developercertificate.org/). There is no contributor licence agreement. Signing off states that you wrote the change, or otherwise have the right to submit it under this project's licence (Apache-2.0).

Add the sign-off with `git commit -s`. It appends this line, which must match the commit's author name and email:

```
Signed-off-by: Your Name <you@example.com>
```

Commits that are not signed off by their author are not merged. `uv run python scripts/check_dco.py <base> <head>` checks a range. To fix a branch, sign off every commit on it and force-push:

```bash
git rebase --signoff origin/main
git push --force-with-lease
```

The DCO text, in full:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

## Before you start

- For a bug, open an issue with the steps to reproduce it, what you expected, and what happened.
- For a feature or a change to a public interface (an API route, a CLI command, the notebook format, an extension point), open an issue first so the design can be agreed before you write it.
- Security problems go to the address in [SECURITY.md](SECURITY.md), never to a public issue.

## What a pull request needs

1. **Tests.** Every change ships with a test that drives the code through its public surface. A bug fix starts with a test that fails without the fix. A test must fail when the behaviour it covers is removed or inverted.
2. **Green checks.** Run these locally before you push.

   ```bash
   make lint
   make typecheck
   make test
   ```

   If you changed an API route, a schema, a daemon method or an agent tool, also run `make gen-sdk gen-open-subset` and commit the regenerated files.
3. **Docs.** If behaviour, an interface or the architecture changes, update the matching doc in the same pull request.
4. **Small commits.** Moving code and changing behaviour go in separate commits. The subject is `scope: Sentence-case summary`, for example `cli: The daemon reconnects after a sleep`.
5. **Third-party code.** Do not paste code from another project unless its licence is compatible with Apache-2.0. Say where it came from in the pull request, and add it to [NOTICE](NOTICE). New dependencies must pass `uv run python scripts/licence_audit.py`.

Tests that need a real provider key or cloud account skip without one. A maintainer runs them before merging when the change needs it.

## Review

A maintainer reviews each pull request. Reviews look at correctness first, then simplicity and readability. Expect requests to split large changes.

## Conduct

Be respectful and assume good intent. Harassment, personal attacks and discrimination are not tolerated, and maintainers may remove comments, close issues or block accounts that cross that line. Report a problem to founders@alkera.ai.
