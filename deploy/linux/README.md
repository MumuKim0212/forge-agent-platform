# Installing the platform on a Linux host

Runbook for a fresh **Ubuntu LTS** host. The platform is two host processes (the API and the worker, as systemd units) behind one TLS origin that a Traefik container terminates. It is not containerised. Everything here is in this directory:

| File | What it is |
|---|---|
| `forge-api.service`, `forge-worker.service` | the two units |
| `build.sh` | venv from `constraints.txt`, frontend build, image builds, `alembic upgrade head` |
| `traefik/run-traefik.sh`, `traefik/forge-platform.yml.template` | the reference Traefik: static flags and the file-provider route for the platform origin |
| `traefik/make-selfsigned-cert.sh` | a self-signed certificate for test installs |

Placeholders to replace throughout: `forge.example.com` (the platform host), `apps.forge.example.com` (the apps base domain), `/opt/forge` (checkout), user `forge`. If you change the checkout path or user, change them in both unit files too.

Step 1 needs `sudo`. Steps 2 to 7 run as the `forge` user; the `systemctl` lines in steps 8 and 10 run as your admin account (the `forge` user has no `sudo`).

## Required before going live, and what is only recommended

Three groups. The first is enforced by the code, the second fails at the moment you use the function, the third is silent until something breaks.

**Startup refuses without it** (`FORGE_ENV=prod`; the API, the worker and every command refuse alike, and there is no override flag). Each row is a guard in `backend/app/config.py`:

| Setting | Without it | Explained in |
|---|---|---|
| `FORGE_ENV` | refused whatever the value of anything else; it has no default (`Settings.model_post_init`, first check) | step 4 |
| `DATABASE_URL` | refused when unset, blank, or a SQLite URL: prod needs Postgres (`model_post_init`, `ProdDatabaseUrlMissingError`) | steps 1.5, 4 |
| `SESSION_SIGNING_SECRET`, `PREVIEW_SIGNING_SECRET` | refused when unset, blank or under 32 bytes (`_require_prod_signing_secret`) | step 4 |
| `SERVICE_ENV_ENCRYPTION_KEY` | refused when unset or blank (`model_post_init`, `ProdServiceEnvEncryptionKeyError`) | step 4 |
| `PLATFORM_PUBLIC_URL` | refused when unset or blank, and when it is not an `https://` origin (`model_post_init`; `_check_platform_public_url`) | steps 0, 4 |
| `AUTH_MODE` | `dev` is refused in prod (`ProdDevAuthModeError`); `oidc` needs `OIDC_ISSUER`, `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` (`OidcConfigMissingError`) | steps 0, 4 |
| `BOOTSTRAP_ADMIN_EMAILS`, `BOOTSTRAP_ADMIN_PASSWORD` | with `AUTH_MODE=local`: exactly one email, and a password that is set, is not the dev default and is at least 12 characters (`LocalBootstrapAdminEmailCountError`, `ProdWeakBootstrapPasswordError`) | steps 0, 4 |

Also refused at startup, but only if you set them: a `CORS_ORIGINS` entry that is `*`, `null`, not a bare origin, or under `APPS_BASE_DOMAIN` (`_check_cors_origin`), and, in prod, a `NOTIFY_WEBHOOK_URL` that is not `https://` or `NOTIFY_SMTP_TLS=none` (`_check_notify_settings`).

**The platform starts, but a core function fails without it:**

| Setting | Without it | Explained in |
|---|---|---|
| `SERVICE_POSTGRES_ADMIN_DSN` and the service Postgres | no service can be deployed: the confirm step refuses (`PROVISION_UNCONFIGURED`) | step 3 |
| The first team, `python -m app.teams create` | no project can be created: a prod install seeds no team | step 9 |
| `FRONTEND_DIST_DIR` and the frontend build | the API answers, but there is no web UI | steps 2, 4 |
| An engine API key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) | builds cannot run an agent | steps 0, 4 |
| `APPS_BASE_DOMAIN`, `TRAEFIK_ENABLED` and the Traefik container | deployed services and previews get no route | steps 4, 5 |
| The service Redis image (`SERVICE_REDIS_IMAGE`) on the host | the first deployment fails and no preview is shown | steps 2, 7 |

**Recommended:**

| Setting | Without it | Explained in |
|---|---|---|
| `NOTIFY_WEBHOOK_URL`, or `NOTIFY_SMTP_*` with `NOTIFY_EMAIL_TO` | the platform runs, but no one is told when a service or the worker is down, or the disk is low, unless they watch the admin console or poll `/api/ready` | `.env.example` |
| A readiness monitor on `/api/ready` | an outage is noticed only when a user reports it; it answers 200 or 503 and names `database` and/or `worker` | `.env.example` (`READINESS_TIMEOUT_SECONDS`) |
| Backups: `SERVICE_DATA_BACKUP_DIR`, the platform database, `backend/local-git`, `SERVICE_ENV_ENCRYPTION_KEY` | nothing is lost until the host is, and then everything the platform does not back up is | step 11 |

## 0. What you must decide first

- **DNS and TLS.** `forge.example.com` and `*.apps.forge.example.com` must resolve to this host, and the certificate must cover both. The reference uses one certificate for both names; use your organisation's CA, ACME, or `make-selfsigned-cert.sh` for a test. TLS is required: in prod the session cookie is `Secure` and `__Host-` prefixed, so login fails over plain HTTP, and startup refuses a non-`https` `PLATFORM_PUBLIC_URL`.
- **Login.** `AUTH_MODE=oidc` (needs your IdP's `OIDC_*` values) or `AUTH_MODE=local` (a bootstrap admin with a password of at least 12 characters).
- **An engine API key** (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) if builds are to run an agent.

## 1. Prerequisites (sudo)

Paste the blocks in order.

### 1.1 Packages

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl python3 python3-venv nodejs npm postgresql
node --version      # needs 22 or newer (CI builds with 22); if older, install Node 22 from your organisation's mirror or NodeSource
python3 --version   # should match the minor in the header of backend/constraints.txt (3.14 at the time of writing)
```

### 1.2 Docker Engine

From Docker's apt repository (docs.docker.com/engine/install/ubuntu has the current form). If Docker has not published your release yet, `sudo apt-get install -y docker.io` is the fallback.

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<DOCKER_REPO
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER_REPO
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
```

### 1.3 Docker daemon settings

Every deployed service gets private networks, and Docker's built-in pool holds about 30. The `default-address-pools` below holds 4096 (`10.64.0.0/12` in /24s); pick a range that does not collide with your network. The log caps stop a chatty container filling the disk. The preflight checks the pool against `SERVICE_DATA_EXPECTED_SERVICES`.

`"bip"` pins the default bridge `docker0` to `172.17.0.1/24`. Set it: without `bip`, once `default-address-pools` is present `docker0` takes the pool's first subnet (`10.64.0.1/24`), and the API unit, which binds `172.17.0.1`, then fails with `Errno 99 cannot assign requested address`. `bip` must lie outside the pool and every network you use.

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "bip": "172.17.0.1/24",
  "default-address-pools": [{ "base": "10.64.0.0/12", "size": 24 }],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
sudo systemctl enable docker
sudo systemctl restart docker
sudo docker info --format '{{json .DefaultAddressPools}}'
ip -4 addr show docker0        # must show 172.17.0.1/24
```

The API binds this address (step 8). If you chose a different `bip`, override the unit with `sudo systemctl edit forge-api` and `[Service]` / `Environment=FORGE_API_BIND=<docker0 address>` and, for Traefik, `FORGE_API_ADDR=<address>:8000`. Never put `FORGE_API_BIND` in `.env`: `Settings` rejects unknown keys.

### 1.4 User and permissions

```bash
sudo useradd --system --create-home --home-dir /home/forge --shell /bin/bash forge
sudo usermod -aG docker forge
sudo install -d -o forge -g forge /opt/forge /etc/forge /etc/forge/certs
```

**The `docker` group is root-equivalent.** Anyone who can talk to the Docker socket can start a privileged container that mounts the host's `/`. The `forge` user, the API and the worker therefore hold root on this host by design (ADR 0002: the host is the trust boundary). Do not add other people to the group, do not give the `forge` account a login you would not give root, and keep `/opt/forge/backend/.env` mode `0600`.

### 1.5 The platform database (Postgres on the host)

This is the platform's own store, separate from the service Postgres in step 3 (ADR 0023 §Amendment 7). Use a password without URL-special characters; hex is simplest. Keep it for step 4.

```bash
PLATFORM_DB_PASSWORD="$(openssl rand -hex 24)"
sudo -u postgres psql -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE forge LOGIN PASSWORD '$PLATFORM_DB_PASSWORD'" \
  -c "CREATE DATABASE forge OWNER forge"
echo "$PLATFORM_DB_PASSWORD"    # copy it; it goes into DATABASE_URL
```

The host Postgres listens on `127.0.0.1:5432` by default, which is all the platform needs.

## 2. Get the code and build

As the `forge` user (`sudo -iu forge`):

```bash
git clone <your-repo-url> /opt/forge     # or copy a release checkout into it
cd /opt/forge
deploy/linux/build.sh --skip-migrate
```

This creates `backend/.venv` from `backend/constraints.txt`, builds `frontend/dist`, and builds `forge-llm-proxy:latest` and `forge-agent-sandbox:latest`, and pulls the service Redis image (`SERVICE_REDIS_IMAGE`, default `redis:8-alpine`; the platform creates each project's Redis without pulling, so a host without it fails the first deployment and shows no preview) (add `--with-openai-image` for the OpenAI engine's sandbox). It does not migrate: the database needs `.env` first. If you set non-default image names (`AGENT_SANDBOX_IMAGE` and friends), build those images by hand from the same Dockerfiles. A non-default `SERVICE_REDIS_IMAGE` is pulled by `build.sh` once it is in `backend/.env`, so re-run `build.sh --skip-migrate` after setting it. The worker's disk-cap probe creates a container from the generic `SANDBOX_IMAGE` (default `python:3.11-slim`) and never pulls it: it is on the host because `build.sh` builds the agent images `FROM` it, and `python -m app.preflight` (step 7) checks that it is there. If you set a different `SANDBOX_IMAGE`, `docker pull` it.

## 3. The service Postgres

The shared instance every generated service's database lives on. It must be a container **on this same Docker daemon** (the platform attaches it to per-service networks), published on loopback only, with a `CREATEDB CREATEROLE` role that is not a superuser (ADR 0023 §Amendment 5, 6). Size `max_connections` as `SERVICE_DATA_EXPECTED_SERVICES x SERVICE_DATA_CONNECTIONS_PER_SERVICE + 20` (100 x 5 + 20 = 520 by default).

```bash
SERVICE_PG_BOOTSTRAP_PASSWORD="$(openssl rand -hex 24)"
SERVICE_PG_ADMIN_PASSWORD="$(openssl rand -hex 24)"
docker run -d --name forge-service-postgres --restart unless-stopped \
  -e POSTGRES_PASSWORD="$SERVICE_PG_BOOTSTRAP_PASSWORD" \
  -p 127.0.0.1:55432:5432 \
  -v forge-service-postgres-data:/var/lib/postgresql \
  postgres:18-alpine postgres -c max_connections=520
until docker exec forge-service-postgres pg_isready -h 127.0.0.1 -U postgres; do sleep 1; done
docker exec forge-service-postgres psql -U postgres \
  -c "CREATE ROLE forge_admin WITH LOGIN PASSWORD '$SERVICE_PG_ADMIN_PASSWORD' CREATEDB CREATEROLE NOSUPERUSER"
echo "$SERVICE_PG_ADMIN_PASSWORD"   # copy it; it goes into SERVICE_POSTGRES_ADMIN_DSN
```

(After step 1.4 the `forge` user needs a fresh login, `sudo -iu forge`, for the `docker` group to apply.) Its data lives in the named volume `forge-service-postgres-data`; `docker volume rm` of it discards every service database. The bootstrap superuser password is not needed again and does not go into `.env`.

## 4. `.env`

```bash
cd /opt/forge/backend
cp .env.example .env
chmod 600 .env
```

Edit `.env`. `.env.example` has two live lines, `FORGE_ENV=dev` and `CORS_ORIGINS=http://localhost:5173`: **change `FORGE_ENV` in place** (a second `FORGE_ENV` line is not a fix) and leave `CORS_ORIGINS` alone. Rules, because systemd and the app's dotenv parser both read this file and parse it differently: one `KEY=value` per line; no trailing `# comment` after a value; and **no `$`, quote (`'` or `"`), backslash, or leading or trailing space in any value**. Generated secrets are safe; check operator-chosen ones (`BOOTSTRAP_ADMIN_PASSWORD`, `OIDC_CLIENT_SECRET`, API keys).

Generate three secrets (each different, never reused):

```bash
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"                                 # SESSION_SIGNING_SECRET
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"                                 # PREVIEW_SIGNING_SECRET
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"    # SERVICE_ENV_ENCRYPTION_KEY
```

**Back up `SERVICE_ENV_ENCRYPTION_KEY` now, away from the database.** Lose it and every stored service environment is unrecoverable.

Values this layout needs. With `FORGE_ENV=prod`, startup refuses to run without: `DATABASE_URL`, `SESSION_SIGNING_SECRET`, `PREVIEW_SIGNING_SECRET`, `SERVICE_ENV_ENCRYPTION_KEY`, an `https://` `PLATFORM_PUBLIC_URL`, an `AUTH_MODE` other than `dev` with its login values (below), and, for `local`, a bootstrap password of at least 12 characters. The rest are what this runbook's layout needs:

```
FORGE_ENV=prod
DATABASE_URL=postgresql://forge:<PLATFORM_DB_PASSWORD>@127.0.0.1:5432/forge
PLATFORM_PUBLIC_URL=https://forge.example.com
SESSION_SIGNING_SECRET=<generated>
PREVIEW_SIGNING_SECRET=<generated>
SERVICE_ENV_ENCRYPTION_KEY=<generated>
FRONTEND_DIST_DIR=../frontend/dist
SERVICE_POSTGRES_ADMIN_DSN=postgresql://forge_admin:<SERVICE_PG_ADMIN_PASSWORD>@127.0.0.1:55432/postgres
APPS_BASE_DOMAIN=apps.forge.example.com
TRAEFIK_ENABLED=true
TRAEFIK_ENTRYPOINT=websecure
SERVICE_URL_SCHEME=https
TRAEFIK_METRICS_PORT=8082
GIT_PROVIDER=local
USE_SANDBOX_EXECUTOR=true
ANTHROPIC_API_KEY=<your key>
```

Plus the login mode. For `oidc`: `AUTH_MODE=oidc`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` (the redirect URL is derived from `PLATFORM_PUBLIC_URL` unless you set `OIDC_REDIRECT_URL`), and `BOOTSTRAP_ADMIN_EMAILS=<first admin's email>`. For `local`: `AUTH_MODE=local`, `BOOTSTRAP_ADMIN_EMAILS=<one email>` and `BOOTSTRAP_ADMIN_PASSWORD=<12+ characters>` (read once, when the row is first created).

`GIT_PROVIDER=local` keeps every service's code in `backend/local-git`, which is then the only copy (step 11). Leave `TRAEFIK_CERT_RESOLVER` unset when the certificate is the static one from step 5; set it only for an ACME resolver. Every other setting is explained in `.env.example`.

## 5. Traefik

The preflight checks that Traefik is present, so it comes before step 7. The certificate must cover the platform host and `*.<APPS_BASE_DOMAIN>`. For a test, self-signed:

```bash
cd /opt/forge/deploy/linux/traefik
./make-selfsigned-cert.sh forge.example.com apps.forge.example.com /etc/forge/certs
FORGE_PLATFORM_HOST=forge.example.com FORGE_CERT_DIR=/etc/forge/certs ./run-traefik.sh
```

With your own certificate, put `tls.crt` and `tls.key` in `/etc/forge/certs` instead. The script renders the platform route to `/etc/forge/traefik/dynamic/forge-platform.yml` (watched by Traefik) and starts the `forge-traefik` container with the host-gateway mapping, TLS on `:443`, `:80` redirecting to it, and the loopback-only metrics entrypoint. It writes under `/etc/forge`, which the `forge` user owns (step 1.4). Traefik's own access log stays off; keep it off for the preview and callback routes, whose credentials travel in the URL. For ACME, define the resolver in Traefik's static flags (edit the script) and name it in the template and in `TRAEFIK_CERT_RESOLVER`.

## 6. Migrate

On an upgrade, back the platform database up first; on a first install it is empty.

```bash
cd /opt/forge && deploy/linux/build.sh --migrate-only
```

## 7. Preflight

```bash
cd /opt/forge/backend && .venv/bin/python -m app.preflight
```

It is read-only and prints PASS, WARN or FAIL per check, with the fix under each non-PASS line (Docker reachable, Traefik with the host-gateway mapping, service Postgres and its role, images including the service Redis image, schema at head, public origin, address pools). Fix every FAIL before continuing. Run it as the same user, from `backend/`, as the units will.

## 8. Units

```bash
sudo cp /opt/forge/deploy/linux/forge-api.service /opt/forge/deploy/linux/forge-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now forge-api forge-worker
systemctl status forge-api forge-worker --no-pager
```

Enable the worker once and only once (ADR 0038). The API binds the Docker bridge address `172.17.0.1:8000`, which is what `host.docker.internal` resolves to for Traefik; it is not reachable from outside the host, but **any container on this daemon's bridges can reach it**, not only Traefik, which is why the API keeps its own authentication on every route. The unit's default matches the `bip` from step 1.3; if yours differs, see the override there. Logs: `journalctl -u forge-api -u forge-worker -f`.

## 9. First-login check

```bash
curl -sk --resolve forge.example.com:443:127.0.0.1 https://forge.example.com/api/health
```

Expect `{"status":"ok"}` (drop `--resolve` when DNS already points at the host). Open `https://forge.example.com/` in a browser and sign in as the bootstrap admin. A fresh prod install has no team, and creating a project needs one, so create it first, as the `forge` user from `backend/`:

```bash
cd /opt/forge/backend && .venv/bin/python -m app.teams create "<team name>"
```

Then create a project, and take a build through approval to a deploy. The deployed service answers at `https://<project-id>.apps.forge.example.com/`. A self-signed certificate needs a one-time browser exception (or `curl -k`).

## 10. Upgrade

```bash
sudo systemctl stop forge-worker forge-api
# back up the platform database first (pg_dump forge), then:
cd /opt/forge && git pull            # or deploy the new checkout
deploy/linux/build.sh                # venv, frontend, images, migrate
cd backend && .venv/bin/python -m app.preflight
```

**One-time, when upgrading from before the build-image sweep:** build images made earlier carry no labels, so the sweep never removes them. With the worker still stopped (this sits between the preflight above and the start below), list this installation's images that no build still needs (the keep rules are the sweep's own, `keep_build_ids`), read the list, then remove exactly those tags, unforced:

```bash
cd /opt/forge/backend
.venv/bin/python -c "
from app.db import SessionLocal
from app.models.build import Build
from app.services.image_retention import keep_build_ids
from sqlalchemy import select
with SessionLocal() as db:
    keep = keep_build_ids(db)
    for build_id, ref in db.execute(select(Build.id, Build.image_ref).where(Build.image_ref.is_not(None))):
        if build_id not in keep:
            print(ref)
" > /tmp/forge-old-images.txt
cat /tmp/forge-old-images.txt        # read it: these are this installation's tags only
xargs -r -n1 docker image rm < /tmp/forge-old-images.txt
```

It runs against the database `.env` names, so it only ever lists this installation's tags. `docker image rm` without `-f` refuses an image a container still uses, which is the right answer. Never remove by a name prefix (`forge-build-*`) and never `docker image prune`: either would also take another installation's images on a shared engine.

Then start the services onto the new code:

```bash
sudo systemctl start forge-api forge-worker
```

Stop the worker before migrating and restart it onto the new code; an old worker must never run beside a new one (ADR 0038). If the release changes the Traefik flags, rerun `run-traefik.sh --recreate` afterwards.

## 11. Backups

What is backed up by whom, and what survives losing the host: the platform dumps service databases on a schedule into `SERVICE_DATA_BACKUP_DIR` (point it at a mount your backups cover); **you** back up the platform database, `backend/local-git` (with `GIT_PROVIDER=local`) and `SERVICE_ENV_ENCRYPTION_KEY`, the last one not beside the database.

## WSL2 stand-in (testing only)

An Ubuntu distro on WSL2 with `systemd=true` in `/etc/wsl.conf` runs this runbook unchanged, with these differences:

- Install Docker Engine **inside the distro** (1.2). Keep Docker Desktop's WSL integration **off** for that distro (Docker Desktop, Settings, Resources, WSL integration), or its `docker` shim and the Engine's socket mix. Docker Desktop's `docker` shim may stay on `PATH`; `/usr/bin/docker` comes first and wins.
- WSL2 networking is NAT by default: other machines cannot reach the distro, but `localhost` from Windows is forwarded to it. The `*.localtest.me` names (for example `PLATFORM_PUBLIC_URL=https://forge.localtest.me` and `APPS_BASE_DOMAIN=apps.localtest.me`) resolve to loopback with no `hosts` edit. Inside WSL they resolve to `::1`; Traefik publishes on both stacks, so that works.
- Cloning from `/mnt/c` as `forge` needs `git config --global --add safe.directory <path>/.git`.
- The distro stops when idle, which takes both units down. Keep it alive, for example `wsl -d <distro> -- sleep infinity` left running from Windows.
- `systemd-binfmt` fails on start; harmless.
- The kernel is Microsoft's. With `bip` set (1.3), `docker0` is `172.17.0.1` as on a real host; still check with `ip -4 addr show docker0`.
- It is a stand-in, not a production host: no durable disk or backup story, and the Windows side can reach everything in it.
