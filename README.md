# ludus-mgr

Training lab deployment management platform powered by [Ludus](https://ludus.cloud).

<img width="1355" height="753" alt="image" src="https://github.com/user-attachments/assets/f89f59f0-5b8e-4a3e-9956-3520a1b603d9" />




A self-hosted web platform that wraps [Ludus](https://ludus.cloud) to help range managers
provision, monitor, and tear down lab environments in bulk for security
trainings and workshops.


## What it does

- Define reusable **lab templates** (Ludus range-config YAML + metadata)
- Create a **training session**: select lab, pick mode (shared/dedicated), add students
- **One-click bulk provision**: creates Ludus users, assigns ranges, generates WireGuard configs
- Share per-student **invite links** - students download their VPN config
- **Live dashboard**: student status, range health, snapshot state
- Per-student **lab reset** (triggers Ludus snapshot revert)
- **One-click teardown**: cleanup all Ludus users, configs, and artifacts
- Full **Ludus management UI**: ranges, snapshots, users, groups, testing mode, Ansible roles

## Architecture

```
┌─────────────────────────────┐
│  React UI (Stitch design)   │
│  Instructor dashboard       │
└──────────────┬──────────────┘
               │ REST / JSON
               ▼
┌─────────────────────────────┐
│  FastAPI backend             │
│  - sessions / students       │
│  - ludus wrapper             │
│  - invites                   │
└──────┬──────────────┬────────┘
       │              │
       ▼              ▼
  [Postgres]     [Ludus API]
                 (one or more servers)
```

## Stack

- **Backend:** Python 3.11+, FastAPI, SQLAlchemy 2.0, Pydantic 2, httpx
- **Frontend:** React 18 + TypeScript + Vite + Tailwind CSS
- **DB:** PostgreSQL (via Docker Compose)
- **Auth:** Single instructor account, JWT
- **Deployment:** Docker Compose (frontend nginx reverse proxy)


## Quick start

```bash
cp .env.example .env
# edit .env: set ADMIN_PASSWORD, APP_SECRET_KEY, LUDUS_DEFAULT_API_KEY
docker compose up -d
# UI at http://localhost (frontend nginx), API at http://localhost/api
```

## Rebuilding / updating

After pulling new code (or making local changes), rebuild and restart with:

```bash
cd ludus-mgr

# 1. Stop and remove the containers - NOT the data volume (no -v, ever)
docker compose down

# 2. Rebuild images from the current code and start the stack.
#    The backend's lifespan handler runs `alembic upgrade head`
#    automatically on startup, so pending migrations apply themselves.
docker compose up -d --build

# 3. Clean up the now-superseded old images (safe: only removes
#    untagged/dangling images, never anything still in use).
docker image prune -f
```

Verify it came back up healthy:

```bash
docker compose ps                    # all three services should show healthy/running
curl -s http://localhost/health      # {"status":"ok","db":true,"storage":true,"ludus":true}
```

**Never append `-v`/`--volumes` to `docker compose down`** - that deletes the
Postgres volume (`pgdata`), destroying every session, student, and lab
template in the database.

## Connecting to Ludus

Some Ludus deployments still expose **two** API ports, and this platform supports both:

- **`:8080`** — user API (all interfaces). Ranges, snapshots, WireGuard configs, testing.
- **`:8081`** — admin API (**`127.0.0.1` only**). Creating and deleting Ludus users. Bulk
  provision and teardown fail without it.

Because the admin API is bound to localhost on the Ludus host, expose it to this platform
with a **source-restricted socat proxy** running on the Ludus host as a systemd service. It
forwards a non-loopback port back to `127.0.0.1:8081`, restricted to the manager's IP:

```ini
# /etc/systemd/system/ludus-admin-proxy.service  (on the Ludus host)
[Unit]
Description=Ludus admin API proxy (source-restricted)
After=network-online.target
Wants=network-online.target

[Service]
# range= locks the source to the manager box only; still API-key protected behind it.
# Raw TCP passthrough: TLS stays end-to-end between the manager and :8081.
ExecStart=/usr/bin/socat \
  TCP-LISTEN:18081,bind=<ludus-host-ip>,reuseaddr,fork,range=<manager-ip>/32 \
  TCP:127.0.0.1:8081
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ludus-admin-proxy.service
```

Then point `.env` at the two endpoints (use an **admin** Ludus user's API key):

```bash
LUDUS_DEFAULT_URL=https://<ludus-host-ip>:8080        # user API
LUDUS_DEFAULT_ADMIN_URL=https://<ludus-host-ip>:18081 # admin API via the proxy above
LUDUS_DEFAULT_API_KEY=<admin-ludus-user-api-key>
LUDUS_DEFAULT_VERIFY_TLS=false                        # Ludus uses a self-signed cert
```

See `.env.example` for the full list of Ludus variables.

### Credits
This was forked from https://github.com/whiteov3rflow/ludus-helm and modified to work with Ludus version 2

## Roadmap

- **UW NetID SSO login** — add an option to log into ludus-mgr (instructor dashboard,
  and/or the student invite flow) via University of Washington SSO instead of the
  current single-admin JWT login. Notes for when this gets picked up:
  - UW's generally-available protocol is **SAML 2.0** via their Shibboleth IdP, with
    self-service SP registration ([Register a Service Provider with the UW](https://wiki.cac.washington.edu/display/infra/Register+a+Service+Provider+with+the+UW),
    [Use SAML 2.0 for single sign-on](https://itconnect.uw.edu/tools-services-support/access-authentication/sso/saml/)).
    UW's OIDC support is alpha/limited availability (requires emailing
    iam-support@uw.edu), so SAML is the realistic path.
  - This is **not** the same thing as [Ludus's own built-in SSO](https://docs.ludus.cloud/docs/administration/sso),
    which is OAuth2-only (PocketBase-backed) and configured on the Ludus server itself
    (`/opt/ludus/config.yml`) — UW doesn't offer a generally-available OAuth2 IdP, so
    wiring UW SSO directly into Ludus isn't currently feasible. The practical
    integration point is SAML login into ludus-mgr itself; Ludus API access stays on
    the existing per-server API key.
  - UW's default attribute release (NetID, etc.) requires the SP's entityID/ACS
    endpoint to be on a `washington.edu`/`uw.edu` DNS name; otherwise non-default
    attribute release needs UW-IT approval.

## License

[MIT](LICENSE)
