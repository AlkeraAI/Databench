# Installing Databench (self-hosted)

This guide covers the Docker Compose reference deployment,
`deploy/docker/compose.prod.example.yml`, for a deployment other people reach. To
try Databench on your own machine, use the one-command stack in the repository
root instead (`docker compose up --build`; see the README).

Every setting is described where it is declared, in
`packages/api-core/alkera_core/config.py`; `.env.example` lists each with its
default.

---

## 0. Build the images

Build them locally, then push them to your registry or load them on an
air-gapped host:

```bash
make docker-images IMAGE_VERSION=<version>
```

This builds `alkera/backend`, `alkera/worker`, `alkera/gateway` and `alkera/web`.
The bundled Temporal server and its optional console are the two third-party
images beside Postgres, pinned by digest in the compose file.

## 1. Start it

You need two hostnames, both pointed at the host through your TLS proxy:

- the app, `PUBLIC_BASE_URL` (for example `https://databench.example.com`);
- the Files content origin, `FILES_CONTENT_BASE_URL`, on a registrable domain
  of its own (for example `https://files.databench-content.example`), never a
  subdomain of the app. Uploaded files are served from it, so a file never runs
  where the session cookie lives.

Write the `.env` in the repository root. Every setting you put in it reaches the
backend, the worker and the gateway. Generate each password with
`python -c 'import secrets; print(secrets.token_urlsafe(32))'`. Keep comments on
their own lines: a comment after a value becomes part of the value.

```bash
cat > .env <<'EOF'
APP_VERSION=<version>
PUBLIC_BASE_URL=https://databench.example.com
FILES_CONTENT_BASE_URL=https://files.databench-content.example
DB_PASSWORD=<random>
TEMPORAL_DB_PASSWORD=<a different random value>
EMAIL_ENABLED=false
ANTHROPIC_API_KEY=<your key>
EOF
```

The server secrets (`AUTH_JWT_SECRET`, `TOKEN_HASH_PEPPER`,
`FILES_CONTENT_SIGNING_KEY`, `SECRET_BOX_KEY`) are generated on first boot into
the `alkera_secrets` volume and read back on every later boot. Back that volume
up with the database: losing it signs everyone out and strands stored
credentials. To choose a secret yourself, set it in `.env`.

Name the compose file and the `.env` on every command. The repository root has
its own `compose.yaml` (the one-machine stack), which Compose would otherwise
pick. A shell function does both:

```bash
prod() { docker compose -f deploy/docker/compose.prod.example.yml --env-file .env "$@"; }
prod up -d
ADMIN_BOOTSTRAP_EMAIL=admin@acme.example ADMIN_BOOTSTRAP_ORG_NAME="Acme" \
  prod --profile bootstrap run --rm bootstrap
```

The backend runs the migrations when it starts. The bootstrap creates the first
admin and prints a generated password; running it again changes nothing. The
optional Temporal console starts with `prod --profile temporal-ui up -d`, on
`127.0.0.1:8233` only.

Point both hostnames at the `web` container (port 80) through your
TLS-terminating proxy. The container serves the app on `PUBLIC_BASE_URL` and
only `/c` on the content hostname. The proxy must pass websocket upgrades, must
not buffer event streams, and must allow 64 MiB request bodies and one-hour read
and send timeouts.

**Email.** `EMAIL_ENABLED=false` needs no relay: verification is satisfied
automatically, invitations are accepted in the app, and passwords are reset by
an admin or through SSO. To send email, set `EMAIL_ENABLED=true`, `SMTP_HOST`,
and `SMTP_USERNAME` and `SMTP_PASSWORD` if the relay needs them.

**Your own name.** `BRAND_PRODUCT_NAME` and `BRAND_SUPPORT_EMAIL` appear in the
app and in email.

---

## 2. Files

Files is on: the shared drive, uploads, notebooks, chat templates, and the
folder each chat keeps its work in. The file bytes live in the
`alkera_files_data` volume on this host (`FILES_STORE_PROVIDER=filesystem`).
Back it up together with the database; Postgres cannot rebuild it.

Without Files a deployment loses the drive, uploads, notebooks and templates,
and a chat's files and its agent's memory of the conversation stay on the one
machine that ran it: a chat that sleeps and wakes on another machine starts with
an empty folder. To run without it anyway, set `FILES_ENABLED=false` and
`FILES_CONTENT_BASE_URL=` (empty).

To keep the bytes in an S3 bucket instead (AWS S3, MinIO, Backblaze B2), set
`FILES_STORE_PROVIDER=s3_compatible`, `FILES_STORE_ENDPOINT`,
`FILES_STORE_BUCKET`, `FILES_STORE_REGION`, `FILES_STORE_ACCESS_KEY` and
`FILES_STORE_SECRET_KEY`. The bucket must exist.

A content-security policy you set with `ALKERA_CSP` replaces the web image's
default, so it must name the content origin in `connect-src`, `frame-src` and
`media-src` itself.

---

## 3. Machines

Chats and notebooks run on a machine, and the compose file starts none. An
organisation admin adds one under Organization, Machines, Add machine, with the
host's address, SSH port, a user that can use sudo, and a password or private
key. Databench installs its node daemon over SSH and uses the host as that
organisation's machine (`SSH_MACHINES_ENABLED`, on by default). A host on a
private network also needs `SSH_MACHINES_ALLOW_PRIVATE_ADDRESSES=true`.

The machine calls this server back:

- **The API.** `ALKERA_NODE_API_URL` is the address the machine reaches this
  server at, for example `https://databench.example.com`, or
  `http://10.0.0.4` on a private network. Unset, it is `PUBLIC_BASE_URL`.
- **The model gateway.** The `web` container serves the gateway at `/gateway`
  on the same origin, so the machine uses `ALKERA_NODE_API_URL` plus
  `/gateway`. Set `ALKERA_NODE_GATEWAY_URL` only to send it elsewhere.
- **Files.** The machine pulls and pushes each chat's folder through
  `FILES_CONTENT_BASE_URL`, so it must reach that hostname too.

Test connection fetches `/health/live` at the API and gateway addresses from the
host, and Add machine is refused when either fails, with the reason. A machine
whose node installs but never registers reads "Couldn't start" after two tries
(`MACHINE_BOOT_RETRY_COUNT`), with the node's last error on its page.

### Machines of both architectures

The backend and worker images carry the node bundle a machine installs, built
for the image's own architecture by default (`NODE_BUNDLE_ARCHES=native`). An
image built on an Apple-silicon Mac installs only arm64 hosts, and one built on
an x86_64 computer installs only x86_64 hosts. Test connection and Add machine
refuse a host whose architecture has no bundle, and say which one is missing.

To install hosts of both architectures, build with `NODE_BUNDLE_ARCHES=all`
(or `amd64` or `arm64` for one in particular):

```bash
NODE_BUNDLE_ARCHES=all docker compose build backend worker
```

The bundle for the other architecture builds under emulation, which Docker must
be able to run:

- **Linux:** `docker run --privileged --rm tonistiigi/binfmt --install amd64,arm64`
- **Docker Desktop (Windows and macOS):** built in.
- **OrbStack:** turn on its Rosetta (x86 emulation) setting.

Without it the build stops at the bundle stage's first step with
`exec format error`; the Dockerfile lines it prints say what to enable. The
emulated build takes several times longer than the native one.

---

## 4. Smoke test

```bash
curl -fsS $BASE/health/info      # {"version": "<version>", "env": "production"}
curl -fsS $BASE/health/ready     # {"status":"ok","db":"ok"}
curl -fsS $BASE/api/v1/config    # {"product_name": "...", "support_email": "..."}
# The worker is healthy only once it polls Temporal.
prod ps --format '{{.Service}}={{.Health}}' | grep -E 'worker=healthy|temporal=healthy'
```

Then sign in as the bootstrapped admin. A scripted version of these checks is
[`../ops/test/selfdeploy/compose-smoke.sh`](../ops/test/selfdeploy/compose-smoke.sh).

---

## 5. Background work (Temporal)

Every scheduled and queued job (connection probes, Files maintenance, token
pruning, the deployment-health snapshot) runs as a Temporal workflow. The **worker** image polls four task queues (`money`, `email`,
`sync`, `default`) and registers the periodic schedules idempotently at every boot,
so there is no separate scheduler process and any number of worker replicas is safe.

- **Compose** runs one worker process for all four queues
  (`python -m worker run --queues money,email,sync,default`). To scale a hot queue
  on its own, copy the `worker` service with `--queues sync` (etc.) under a new name.
- **The server.** The bundled `temporal` (`temporalio/auto-setup`, one node) stores
  its state in two databases, `temporal` and `temporal_visibility`. In compose they
  belong to a dedicated `temporal` Postgres role with its own `TEMPORAL_DB_PASSWORD`:
  the one-shot `temporal-db-init` service creates the role and the two databases it
  owns before the server starts, so the third-party image never holds the `alkera`
  superuser credential that reads application data, and the server itself runs with
  `SKIP_DB_CREATE=true`. `temporal-db-init` runs on every `up` and is idempotent;
  re-running it is how you rotate `TEMPORAL_DB_PASSWORD`. On an external Postgres
  either grant that role `CREATEDB` or pre-create both
  databases (`CREATE DATABASE temporal OWNER <user>; CREATE DATABASE
  temporal_visibility OWNER <user>;`). The server
  is reachable in-network only, on 7233; nothing is published on the host. For HA point
  `TEMPORAL_ADDRESS` at a clustered or managed
  Temporal (an API key turns TLS on). Its data is disposable: after a restore of a
  fresh Temporal the workers re-register every schedule and every activity is
  idempotent; at most in-flight retries are lost.
- **Config.** Every Python service reads `TEMPORAL_ADDRESS` and `TEMPORAL_NAMESPACE`
  (the production validator requires the address everywhere); the compose file sets them for you. `TEMPORAL_API_KEY` is optional and blank
  means unset.
- **Health.** The worker serves `GET /health/live` on `:9000`, which answers 200 only
  while its pollers run. `python -m worker health` probes it (the compose healthcheck).
- **Console.** `temporalio/ui` shows every workflow's input and can signal or
  terminate workflows, and it has **no authentication**. Compose binds it to
  `127.0.0.1:8233` under the `temporal-ui` profile (reach it over `ssh -L`). Never expose it
  to the network.
