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
- **VPN activity tracking**: live WireGuard connection status, per-user login
  counts, date-range filtering, and PDF/CSV report export (see below)
- Per-student **lab reset** (triggers Ludus snapshot revert)
- **One-click teardown**: cleanup all Ludus users, configs, and artifacts
- Full **Ludus management UI**: ranges, snapshots, users, groups, testing mode, Ansible roles

## Architecture

```
┌─────────────────────────────┐
│  React UI (Stitch design)   │
│  Lab Manager dashboard       │
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
- **Auth:** Single Lab Manager account, JWT
- **Deployment:** Docker Compose (frontend nginx reverse proxy)


## Quick start

```bash
cp .env.example .env
# edit .env: set ADMIN_PASSWORD, APP_SECRET_KEY, LUDUS_DEFAULT_API_KEY
docker compose up -d
# UI at http://localhost (frontend nginx), API at http://localhost/api
```

`.env` lives in this repo's root **on the machine running `docker compose`**
(not committed - it's git-ignored, since it holds secrets). Every lab manager
standing up their own instance creates their own from `.env.example` and fills
in their own `LUDUS_DEFAULT_API_KEY` - see
[Connecting to Ludus](#connecting-to-ludus) for where to get that key.

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

### Getting an admin API key

`LUDUS_DEFAULT_API_KEY` must belong to a Ludus user with `isAdmin: true` -
this platform creates/deletes Ludus users and (optionally) authenticates lab
manager logins against Ludus/Proxmox credentials, both of which are
admin-only Ludus operations.

1. Log into the Ludus web UI (`https://<ludus-host>:8080/ui/`) as an admin
   user.
2. Go to your user settings and reset/copy your API key (this is the same
   thing the `GET /user/apikey` route does).
3. Paste it into `.env` as `LUDUS_DEFAULT_API_KEY` on the machine running
   `docker compose`, then restart: `docker compose up -d --build`.

**Resetting an API key invalidates the previous one immediately** - if
another tool or teammate is still using the old key, it will start getting
`401 Invalid API key` until `.env` is updated with the new one.

### Real host stats for the dashboard (Proxmox API token)

The dashboard's "Host Capacity" card prefers real, measured CPU/RAM/storage
usage pulled directly from Proxmox over the manually-configured-capacity
fallback (which can't account for storage at all, and silently drifts out
of sync with the actual hardware). This needs a **read-only** Proxmox API
token - never a full admin account:

1. In the Proxmox web UI: **Datacenter → Permissions → Users → Add**. Create
   a dedicated user, e.g. `ludus-mgr-readonly@pve`, with no password needed
   (the token is the credential).
2. **Datacenter → Permissions → Add → User Permission**: Path `/`, User
   `ludus-mgr-readonly@pve`, Role `PVEAuditor` (built-in, read-only - no
   write/exec access anywhere).
3. **Datacenter → Permissions → API Tokens → Add**: User
   `ludus-mgr-readonly@pve`, Token ID e.g. `ludus-mgr`, and **uncheck
   "Privilege Separation"** so the token inherits the user's `PVEAuditor`
   role directly instead of needing its own separate permission grant.
4. Copy the full token value shown (**only shown once**) - it looks like
   `ludus-mgr-readonly@pve!ludus-mgr=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`.
5. Paste it into `.env` as `PROXMOX_API_TOKEN`, then restart:
   `docker compose up -d --build`.

Only needed if the Proxmox cluster has more than one node: set `PROXMOX_NODE`
to the node to report on (auto-detected otherwise). Currently only wired for
the `default` Ludus server - other configured servers still use the
manual-capacity fallback.

### VPN activity tracking (least-privilege host access)

The **VPN Activity** page shows who is connected to their ranges over
WireGuard and summarises their usage:

- **Live status** — a green/grey online indicator per user (also surfaced as a
  column on each range's detail page), from the peer's latest handshake.
- **Login counts** — every offline→online transition is written to the audit
  feed as a `student.vpn_connected` event; the page shows a lifetime login
  count per user plus a headline summary (users / online now / total logins /
  most active).
- **Date-range filter** — scope the login counts and summary to a window
  (From/To pickers, or *Last 7 days* / *Last 30 days* / *All time* presets).
- **Report export** — download the current view (honouring the selected
  window) as **PDF** or **CSV**.

The Ludus API exposes no connection data, so ludus-mgr reads it straight from
the host with one read-only command: `wg show <iface> dump`. It can run that
locally (if the manager is co-located on the Ludus host) or over SSH.

Disabled by default. To enable, set in `.env`:

```bash
VPN_TRACKING_ENABLED=true
VPN_WG_INTERFACE=wg0
VPN_WG_MODE=ssh            # or "local" if ludus-mgr runs ON the Ludus host
VPN_SSH_HOST=<ludus-host>
VPN_SSH_USER=vpnmon
VPN_SSH_KEY_PATH=/app/data/vpnmon_ed25519   # key mounted into the backend container
# VPN_ONLINE_WINDOW_SECONDS=180   # handshake newer than this = "online"
# VPN_LOGIN_GAP_SECONDS=600       # a reconnect after this gap = a new login event
```

**Do not point this at a root SSH account.** A key in the container that can
`ssh root@ludus` turns an app compromise into root on the Ludus host, when all
we need is one read-only command. Instead, provision a locked-down `vpnmon`
user with [`scripts/setup-vpnmon.sh`](scripts/setup-vpnmon.sh) **on the Ludus
host**:

```bash
# 1. On the ludus-mgr box: generate a dedicated key pair for this.
ssh-keygen -t ed25519 -f ./data/vpnmon_ed25519 -N "" -C "ludus-mgr-vpnmon"

# 2. Copy the PUBLIC key to the Ludus host and run the setup there as root.
sudo ./scripts/setup-vpnmon.sh --pubkey vpnmon_ed25519.pub --interface wg0
```

The script creates an unprivileged `vpnmon` user whose SSH key is pinned to a
**forced command** (`wg show wg0 dump` and nothing else — no shell, no
port-forwarding, no pty) and grants NOPASSWD sudo for exactly that one
command. Even if the key leaks, it can only ever read WireGuard state. The
private key stays under `./data` (mounted read-only into the backend as
`/app/data`), so no code changes are needed — `VPN_WG_MODE=ssh` already runs
precisely the command the host forces.

For a no-SSH deployment you can instead run a read-only exporter (e.g.
`prometheus-wireguard-exporter`) on the host and scrape it; that path needs a
small additional monitor mode and is not wired up yet.

### Credits
This was forked from https://github.com/whiteov3rflow/ludus-helm and modified to work with Ludus version 2

## Roadmap

- **UW NetID SSO login** — add an option to log into ludus-mgr (Lab Manager dashboard,
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

- **"Session" → "Range" rename, the rest of the way** - the GUI now says
  "Range" everywhere a lab manager sees it, but underneath it's still the
  `Session` model, `sessions` DB table, and `/api/sessions` routes. Renaming
  those too is a separate, larger project (touches migrations, every
  backend module that imports `Session`, and the frontend's `sessions` API
  client) - worth doing for consistency, but not bundled into the display
  text change.

- **Lab-manager feature set (capacity trends, scheduled range expiration,
  Discord notifications, observer role, bulk range actions, audit export,
  self-service student reset, and more)** - a larger, multi-phase roadmap.
  Not started yet; see [`docs/roadmap-lab-manager-features.md`](docs/roadmap-lab-manager-features.md)
  for the full plan, phase breakdown, and the design decisions already made
  (read-only Observer role with no per-range ownership; Discord notifications
  limited to provisioning failures and ranges entering `ERROR` state).

## License

[MIT](LICENSE)
