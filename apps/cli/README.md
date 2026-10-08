# apps/cli

The `alkera` command line, the local daemon (`alkera serve`), the agent harness and the box supervisor that runs chats on a machine. Package `alkera-cli`, module `alkera_cli`.

## Run

It is part of the uv workspace, so after `make bootstrap`:

```bash
uv run alkera --help
make dev-cli ARGS="whoami"
```

Running `alkera` with no command shows the help.

## Commands

| Command | What it does |
| --- | --- |
| `alkera login` | Signs in with the device grant: prints a code and a `/device` URL, then waits until you approve it in the browser. The token is saved to `~/.alkera/auth.yml`. |
| `alkera logout` | Signs out of the current organisation. |
| `alkera whoami` | Shows which sign-in commands in this folder act as. |
| `alkera org list`, `alkera org switch <org>` | Lists the organisations you are signed in to and switches between them. `--org` on any command, or `ALKERA_ORG`, picks one for a single command. |
| `alkera run` | Runs one prompt headless, for scripts. |
| `alkera files push`, `pull`, `mount`, `unmount`, `mounts` | Moves files between this machine and the organisation's drive, or mounts a drive folder locally. |
| `alkera env capture`, `alkera env recreate` | Records a workspace's Python environment and rebuilds it elsewhere. |
| `alkera serve` | The daemon: JSON-RPC over stdio with LSP-style framing. |
| `alkera version` | Prints the version. |
| `alkera update`, `alkera uninstall` | Update or remove an installer-managed install. |

`run`, `files`, `env` and `serve` are not listed by `--help`.

## Configuration

The API URL comes from `ALKERA_API_URL`, then the system config file (`/etc/alkera/config.yml`, or `%PROGRAMDATA%\Alkera\config.yml` on Windows), then a source checkout's own env files, then the built-in default `http://localhost:8000`. State lives under `~/.alkera/` (auth, daemon scratch, logs); `ALKERA_HOME` moves it, which the tests use.

## More

- The harness contract, its invariants and how the agent binary is found: [`alkera_cli/harness/README.md`](alkera_cli/harness/README.md).

## Tests

```bash
make test
uv run pytest apps/cli/tests
```
