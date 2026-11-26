# Changelog

All notable changes to this project are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Every PR that changes behavior, user-facing API, configuration, or
deployment should append an entry under `[Unreleased]`. When cutting a
release, rename the `[Unreleased]` heading to the new version + date and
start a fresh `[Unreleased]` block.

## [Unreleased]

### Fixed
- **Ludus v2 API prefix** - `LudusClient` was sending every request to the
  bare path (e.g. `/user/all`) instead of `/api/v2/user/all`, so it never
  worked against a real Ludus v2 server (confirmed live: every route 404'd
  at the root, succeeded under `/api/v2`). This silently broke all Ludus
  calls, including the Ludus/Proxmox login path (`authenticate_ludus`
  couldn't list users to resolve a login identifier).
- **Ludus Management: destroyed ranges never left the Ranges list** - Ludus
  v2 doesn't delete a range's row from `GET /range/all` on destroy, it just
  sets `numberOfVMs: 0` / `rangeState: "DESTROYED"` and keeps it forever
  (confirmed live: 22 of 34 ranges on this server are such tombstones). The
  Ranges tab's "destroy in progress" completion check also never matched
  this state, so the "Range X destroyed" success toast never fired and the
  row never cleared. Added a shared `isLiveRange()` filter (excludes
  `DESTROYED`) applied everywhere ranges are listed or offered as a pick
  target (Ranges tab, snapshot/power/logs range pickers, assign-user-to-range
  modal) - destroyed ranges now drop out of the UI as soon as the destroy
  completes, and the completion toast fires correctly.
- **Ludus Management: orphaned ranges (owner deleted) also cluttered the
  Ranges list** - a range whose owning Ludus user was deleted keeps showing
  up forever (e.g. `rangeState: "ERROR"`, Proxmox pool long gone), and its
  `rangeID` doesn't necessarily match any single owner's `userID`, so it
  can't be detected from the range list alone. `isLiveRange()` now also
  excludes ranges where `GET /ranges/{rangeID}/users` reports no owner
  (`GET /api/ludus/ranges/orphaned`, computed server-side so the frontend
  isn't firing 30+ per-range requests).

### Added
- **Settings page: Proxmox Login URL** - shows the Proxmox host that
  Ludus/Proxmox-credential logins are validated against (derived from
  `LUDUS_DEFAULT_URL`'s host on port 8006, or `PROXMOX_URL` if set), so
  lab managers don't have to read `.env` to find it.
- **Ranges tab: click an ERROR range for diagnostics** - clicking an
  `ERROR` state pill opens a panel with who (if anyone) has access to the
  range, the direct-lookup error if its Proxmox pool is gone, and its full
  deploy log history + latest log content - the same checks previously done
  by hand against the raw Ludus API, bundled into one
  `GET /api/ludus/ranges/{range_id}/diagnostics` call.
- **Host Capacity card now prefers real Proxmox stats** - the live-Ludus-
  range-sum fix above was still an approximation (declared VM cost, no
  storage visibility at all). `PROXMOX_API_TOKEN` (see README) switches the
  dashboard's Host Capacity card to real, measured CPU/RAM/storage pulled
  directly from the Proxmox host (`app/services/proxmox.py`, read-only -
  expects a token scoped to the built-in `PVEAuditor` role). Falls back to
  the live-range-sum approach when unset, or for any server other than
  `default` (not yet wired for multi-server Proxmox).
- **Host Capacity card: "if every VM ran at once" planning row** - alongside
  the real Proxmox-measured load, the card now also shows VM-declared
  CPU/RAM committed across every live range (the same figure the
  live-range-sum fallback uses as its primary number) - live load can sit
  near zero while merely-idle VMs still reserve this much, so this answers
  "what happens when everyone starts using their VMs at once" independent
  of current load.

### Changed
- **"Session" renamed to "Range" throughout the GUI** - Dashboard heading,
  stat cards, "New Session"/"Create Session" buttons, breadcrumbs, delete/
  rebuild/teardown dialog titles, and related toasts/tooltips now say
  "Range" - a session can still fan out to multiple actual Ludus ranges in
  dedicated mode, so a few sentences were reworded (not just find/replaced)
  to avoid reading as "this range's range(s)". Display text only - the
  underlying `Session` model, `/api/sessions` routes, and DB table are
  unchanged; see the Roadmap for the larger rename this would need to be
  more than cosmetic.

## [1.0.0] - 2026-04-21

First major release. Full-stack platform with instructor dashboard, Ludus
management UI, and project rebrand from insec-platform to ludus-helm.

### Added
- **Frontend SPA** - complete instructor-facing React + TypeScript + Tailwind
  application with Vite, matching `docs/DESIGN_SYSTEM.md` exactly.
- **Auth flow** - `AuthContext` with `useAuth()` hook; cookie-based session;
  `ProtectedRoute` gate; global 401 redirect to `/login`.
- **API client** - typed `fetch` wrapper (`src/api/client.ts`) with
  `ApiError` class, `credentials: 'include'`, named exports per resource
  (`auth`, `labs`, `sessions`, `students`).
- **Pages** - Login, Dashboard (stat cards + sessions table + create modal),
  SessionDetail (students table, provision, add/delete/reset student, bulk
  select, invite copy, activity log stub), LabTemplates (card grid + create
  modal).
- **Shared components** - Button (4 variants + loading), Input (with icon +
  error), Card, StatusPill (session + student statuses), Modal (focus trap +
  Escape), Sidebar, TopBar (breadcrumbs + actions), AppLayout.
- **Vite dev proxy** - `/api/*`, `/health`, `/invite/*` forwarded to `:8000`.

- **Toast notifications** - `ToastProvider` + `useToast()` hook replacing all
  `alert()` calls with auto-dismissing slide-in toasts (success/error/info).
- **Confirmation modals** - proper Modal dialogs replacing all `confirm()`
  calls for destructive actions (delete session, remove student, bulk remove).
- **Provisioning progress bar** - teal progress indicator shown during
  provisioning with ready/total counter.
- **Audit log** - `GET /api/events` backend endpoint with `session_id`,
  `limit`, `offset` query params; collapsible Activity Log panel on
  SessionDetail showing real events with timestamps.
- **CSV bulk import** - `POST /api/sessions/{id}/students/import` accepts
  CSV with `full_name,email` columns; frontend file-picker replaces the
  disabled stub button; partial-failure summary returned.
- **Reset cooldown** - 2-minute server-side cooldown between student resets
  (HTTP 429); frontend shows info toast with remaining time.

- **Ludus management page** - full-featured `/ludus` page with 8 tabbed
  sections: Ranges (power on/off, deploy, destroy), Snapshots (create,
  revert, delete per range), Templates (list, delete), Users (create, delete,
  WireGuard download), Groups (create, delete, view members), Ansible
  (installed roles/collections), Testing (start/stop, allow/deny rules),
  Logs (deployment history with output viewer).
- **Ludus user management** - `POST /api/ludus/users` (create, returns
  one-time API key), `DELETE /api/ludus/users/{user_id}`,
  `GET /api/ludus/users/{user_id}/wireguard` (file download). Frontend
  Users tab with DataTable, search, create modal, API key copy dialog,
  WireGuard download, and delete confirmation. 12 backend tests.
- **Ludus management API** - 40+ new endpoints wrapping the full Ludus API:
  range detail/VMs, power management, snapshot CRUD, template management,
  group CRUD, ansible role/collection management, testing mode, deployment
  logs, range config/etchosts/sshconfig/rdpconfigs/ansibleinventory.
- **Multi-server support** - `LudusClientRegistry` resolving multiple Ludus
  servers from `LUDUS_<NAME>_URL` / `LUDUS_<NAME>_API_KEY` env vars; server
  selector dropdown in Ludus management UI; `?server=` query param on all
  Ludus endpoints.
- **Command palette** - `Ctrl+K` / `Cmd+K` quick-navigate overlay with
  fuzzy search across all pages.
- **Settings page** - view platform config, change password, test Ludus
  connection, multi-server status display.
- **Reusable UI components** - `DataTable` (sortable columns, pagination,
  search), `Tabs`, `Skeleton` (loading placeholders), `PageTransition`
  (fade-in animation).

### Changed
- Dashboard and SessionDetail pages refactored to use new shared components
  (DataTable, Tabs, etc.).
- Sidebar tagline font bumped from `text-xs` to `text-sm`.

- **Rebrand** - project renamed from insec-platform to ludus-helm across
  all backend, frontend, Docker, CI, and documentation files.
- **Logo assets** - custom logomark (helm + network nodes) integrated as
  inline SVG in sidebar and login page; favicon (ICO + SVG + apple-touch-icon);
  OG/Twitter social preview meta tags.

### Changed
- Docker container names: `ludus-backend`, `ludus-frontend`, `ludus-postgres`,
  `ludus-caddy`. Docker network: `ludus-helm`.
- Package names: `ludus-helm-backend`, `ludus-helm-frontend`.

### Fixed
- Orphan range destroy no longer requires userID for admin operations.
- Ludus API range endpoints now pass both `userID` and `rangeID` params.
- Logs tab uses correct `range_logs` endpoint.

## [0.1.0] - 2026-04-16

First functional backend release. Replaces the manual `add_player.sh`
workflow with an HTTP-driven provisioning pipeline.

### Added
- **Auth** - JWT-in-httpOnly-cookie login, `GET /api/auth/me`, logout,
  idempotent admin bootstrap from `ADMIN_EMAIL` / `ADMIN_PASSWORD`.
- **Lab templates** - `GET/POST /api/labs`, `GET /api/labs/{id}` with
  YAML validation on create.
- **Sessions** - `GET/POST /api/sessions`, `GET /api/sessions/{id}`
  (with embedded students + per-student `invite_url`),
  `DELETE /api/sessions/{id}` guarded against non-draft/non-ended
  state and ready students.
- **Students** - `POST /api/sessions/{id}/students` (generates
  `ludus_userid` + 32-hex `invite_token`, retries on collision),
  `DELETE /api/students/{id}` (removes Ludus user and on-disk config
  when provisioned), `POST /api/students/{id}/reset` for snapshot
  revert.
- **Provisioning** - `POST /api/sessions/{id}/provision`: end-to-end
  `user_add` → `range_assign` (shared) / `range_deploy` (dedicated) →
  `user_wireguard`, writes `.conf` to `CONFIG_STORAGE_DIR` at mode
  `0600` (parent dir `0700`). Idempotent across retries; tolerates
  `LudusUserExists`.
- **Invite (public, no auth)** - `GET /invite/{token}` renders a
  minimal Jinja2 landing page; `GET /invite/{token}/config` returns
  the WireGuard config as `application/octet-stream` attachment and
  logs `invite.redeemed` / `invite.redownloaded` events.
- **LudusClient** - single HTTP integration point at
  `app/services/ludus.py`. All Ludus calls (CLI and future HTTP) go
  through this wrapper. Typed exceptions: `LudusError`,
  `LudusAuthError`, `LudusUserExists`, `LudusNotFound`, `LudusTimeout`.
- **Models + migrations** - SQLAlchemy 2.0 models for `User`,
  `LabTemplate`, `Session`, `Student`, `Event`; Alembic wired to
  `Base.metadata` with initial migration `0001_initial`.
- **Event audit log** - `lab_template.created`, `session.created`,
  `session.deleted`, `student.created`, `student.provisioned`,
  `student.provision_failed`, `student.reset`, `student.deleted`,
  `invite.redeemed`, `invite.redownloaded`.
- **Config** - 13 environment-driven `Settings` fields covering
  bootstrap, DB, Ludus server, invite TTL, config storage, public
  base URL.
- **Container** - multi-stage backend Dockerfile (`python:3.11-slim`,
  non-root `app` user, 219 MB image). `docker-compose.yml` with
  `./data` bind mount and `/health` healthcheck. `APP_ENV=production`
  triggers real `alembic upgrade head` on lifespan start.
- **CI** - `.github/workflows/backend-ci.yml` running ruff check,
  ruff format check, mypy, pytest, and docker build on every PR
  touching `backend/**`.
- **Tests** - 110 passing, including an end-to-end smoke at
  `tests/test_app_smoke.py` that exercises login → lab → session →
  student → provision (mocked Ludus) → invite download.

### Known follow-ups
- Run `ruff format backend/app backend/tests` and flip the CI format
  step off `continue-on-error`.
- Add `types-python-jose`, `types-passlib`, `types-PyYAML` dev deps;
  address pydantic-settings `call-arg` false-positives; flip the CI
  mypy step off `continue-on-error`.
- Replace deprecated `HTTP_422_UNPROCESSABLE_ENTITY` with
  `HTTP_422_UNPROCESSABLE_CONTENT`.
- Populate `student.range_id` for dedicated-mode sessions once
  `LudusClient.range_deploy` surfaces the returned range id.

## [0.0.2] - 2026-04-16

### Added
- `docs/DESIGN_SYSTEM.md` - full visual spec (colors, typography,
  components, per-screen layouts).
- `docs/STITCH_PROMPTS.md` - archived Google Stitch prompts with
  known quirks documented.
- `docs/AGENTS.md` - five-role multi-agent workflow (PM, backend,
  frontend, devops, supervisor) with invocation templates.
- `CLAUDE.md` - top-level guidance for Claude Code.

## [0.0.1] - 2026-04-16

### Added
- Initial scaffold: `README.md`, `.gitignore`, `.env.example`,
  `docker-compose.yml`, `docs/ROADMAP.md`, `docs/ARCHITECTURE.md`,
  backend `pyproject.toml` + `app/main.py` (health endpoint only),
  frontend `package.json` + `Dockerfile`, `data/.gitkeep`.

[Unreleased]: https://github.com/whiteov3rflow/ludus-helm/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/whiteov3rflow/ludus-helm/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/whiteov3rflow/ludus-helm/compare/v0.0.2...v0.1.0
[0.0.2]: https://github.com/whiteov3rflow/ludus-helm/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/whiteov3rflow/ludus-helm/releases/tag/v0.0.1
