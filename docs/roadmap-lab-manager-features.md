# Lab-manager feature roadmap (Discord instead of Slack)

Not yet implemented - saved for later. See the README's Roadmap section for
a one-line pointer to this file.

## Context

A brainstormed list of 17 feature ideas for the lab-manager workflow
(capacity planning, range lifecycle, student experience, access control, lab
templates, bulk ops, observability), gathered after fixing several real pain
points live on this Ludus server in the same session (host-capacity blind
spots, orphaned ranges cluttering the UI, opaque quota breaches). The goal is
to build all of them except Slack, using Discord instead.

This is a large initiative (~17 features) touching every layer of the app,
including infrastructure that doesn't exist yet. Decided via discussion
before this was shelved:
- **Roles**: add a read-only **Observer** role only. No per-range ownership -
  every lab manager (observer or not) still sees every range, matching
  today's behavior. (Multiple lab-manager *accounts* already exist today -
  every distinct Ludus/Proxmox admin who logs in gets their own JIT-
  provisioned `User` row - what's missing is a role that's actually enforced,
  and a place to assign it.)
- **Discord events**: only **provisioning failures** and **range entered
  ERROR state** post to Discord for now. (Quota-breach-blocked and
  lifecycle/expiry notifications were explicitly *not* selected - don't wire
  those to Discord without revisiting this.)

Research findings that shape the approach (confirmed via code, not assumed,
as of 2026-11-26):
- **No background job/scheduler infra exists at all** - `app/main.py`'s
  lifespan only runs migrations/bootstrap/seeding then `yield`s (comment:
  `# No shutdown work for MVP.`). No Celery/APScheduler/cron, no worker
  service in `docker-compose.yml`. Several features below need one
  (expiration, trend snapshots, ERROR-state detection) - building it once as
  shared infra is the right move rather than three different timers.
- **No outbound notification infra exists** (no SMTP, no webhooks anywhere).
- **`role` on `User` (`app/models/user.py:19`) is stored but never read for
  authorization anywhere** - confirmed via full grep of `app/api/` and
  `app/core/deps.py`. Every authenticated user currently has identical
  permissions.
- **`Session.end_date` already exists** (`app/models/session.py:43`) but
  nothing reads it - a clean hook for expiration, no migration needed for
  that field. The create-range form already has an End Date input
  (`Dashboard.tsx`'s `CreateSessionModal`), so "range expiration" and
  "scheduled auto-teardown" are the *same feature*: auto-teardown when
  `end_date` passes. Treating them as one avoids building two overlapping
  mechanisms.
- **`compute_range_resources()` (`app/services/resources.py:83`)** already
  computes a template's CPU/RAM/VM cost and is reused by the quota feature -
  reuse it again for cost-preview/auto-suggest-quota, don't recompute.
- **`provision_session` / `rebuild_session` / `teardown_session`** all take
  `(db, session_id, settings, *, ludus=None, registry=None)` - directly
  callable from a scheduler tick, no signature changes needed.
- **`DataTable` (`frontend/src/components/DataTable.tsx`) already supports
  multi-select** (`selectable`/`selected`/`onSelectionChange`, lines
  106-121, 158-228) - just not wired into `RangesTab`. Bulk actions is a
  `RangesTab`-only change.
- **`GET /api/events` already exists** (`app/api/events.py`) with
  `session_id`/`limit`/`offset` filtering, backed by a well-structured
  `Event` model with ~20 distinct `action` values already emitted across the
  app - this is the audit trail; export and notifications both build on it
  rather than inventing new instrumentation.
- No charting library in `frontend/package.json` - trend charts need a new
  dependency (`recharts`, the common React choice).
- "Owner" filtering from the original bulk-ops idea is **dropped** - no
  ownership column exists and the roles decision above doesn't add one.
  Bulk-ops filtering would be by state/name/last-deploy-date instead.

## Delivery approach (when this is picked back up)

Given the size, this should NOT land as one pass. Implement and verify one
phase at a time - rebuild, run the test suite, deploy, and spot-check against
the real Ludus/Proxmox server before moving to the next phase. **Phase 0
first** - everything else either depends on it or is independent and can be
reordered based on what's most valuable to see working soonest.

---

## Phase 0 - Foundational infra (scheduler, Discord, email)

No user-visible feature yet; unlocks Phases 1, 2 (email), 3.

- **Scheduler**: add `APScheduler` (`BackgroundScheduler`, in-process - no
  new container, fits the existing single-backend-container deployment).
  Start it in `app/main.py`'s lifespan alongside the existing startup steps;
  shut it down cleanly on app shutdown. One periodic job function per
  concern (expiration check, ERROR-state diff, capacity snapshot), each
  registered as a separate interval trigger (e.g. every 5-15 min,
  configurable via env) so one slow/failing job doesn't block the others.
- **Discord**: new `app/services/discord.py` - a thin `httpx.post(webhook_url,
  json={"content": ...})` helper. Config: optional `DISCORD_WEBHOOK_URL` env
  var (same optional-integration pattern as `PROXMOX_API_TOKEN` -
  unconfigured = silently a no-op, never blocks the calling code path).
- **Email**: new `app/services/mail.py` - generic SMTP send helper. Config:
  optional `SMTP_HOST`/`PORT`/`USERNAME`/`PASSWORD`/`FROM_ADDRESS`/`USE_TLS`
  env vars, same optional pattern. Needed by Phase 2's invite-ready email.
- Document both in `.env.example` and README (same style as the Proxmox
  token section), with a note that both are optional and features degrade
  gracefully (no Discord/email configured = that notification is just
  skipped, nothing breaks).

**Verify**: unit tests for `discord.py`/`mail.py` with mocked transport
(same `httpx_mock` pattern as `test_proxmox_client.py`); confirm the
scheduler starts/stops cleanly via the existing `TestClient` lifespan
fixture pattern already used in the API test files.

---

## Phase 1 - Range expiration / auto-teardown + the two Discord events

- **Auto-teardown on expiry**: scheduler job queries
  `Session.end_date < now() AND status IN (active, provisioning)`, calls the
  existing `teardown_session(db, session_id, settings)` per match, emits a
  new `session.auto_torn_down` `Event`. No new DB column needed (`end_date`
  already exists and is already settable in the create-range form).
- **Range ERROR-state detection**: new small table (e.g. `range_state_cache`:
  server, range_id, last_state, updated_at) so the scheduler can diff "was
  this range already ERROR last tick" and only notify on the *transition*
  into ERROR, not every tick. Job calls `ludus.range_list()` per configured
  server, diffs against the cache, upserts, and fires Discord for new
  ERROR transitions.
- **Provisioning-failure Discord notification**: hook directly into the
  existing failure path in `provision_session`
  (`app/services/provision.py`) where a per-student failure is already
  recorded (`status=error` + `Event`) - add the Discord call alongside the
  existing `Event` creation at that exact call site, not a new polling
  mechanism (provisioning failures are already synchronous/known at the
  moment they happen).

**Verify**: unit tests for the expiry query logic and the state-diff logic
with fixture data (no real Ludus calls in tests, mock the client same as
existing provision/teardown tests); then a live check - set a test range's
`end_date` to the past, wait for a scheduler tick (or trigger manually via a
test-only endpoint/CLI hook), confirm it tears down and the `Event` appears
in `GET /api/events`.

---

## Phase 2 - Audit CSV export + invite-ready email

- **CSV export**: new `GET /api/events/export?session_id=&format=csv`
  reusing the exact query already in `app/api/events.py`, streamed via
  `StreamingResponse` (same pattern already used for Ludus log streaming in
  `app/api/ludus.py:262-279`). PDF is explicitly out of scope for now - CSV
  covers "hand off records," and a PDF library is a heavier dependency for
  uncertain marginal value; revisit only if CSV turns out insufficient.
  Frontend: an "Export" button on the Activity Log panel in
  `SessionDetail.tsx`.
- **Invite-ready email**: when a student's range finishes provisioning
  (`student.provisioned` event site in `provision.py`), send an email with
  their invite link via the Phase 0 mail helper, reusing the exact
  `invite_url` construction already used in `StudentRead`
  (`app/schemas/student.py`). No email configured = silently skipped.

**Verify**: CSV export test asserting correct headers/rows for seeded
events; mail test with mocked SMTP transport confirming the invite link in
the sent message matches `StudentRead.invite_url`.

---

## Phase 3 - Capacity trend chart

- New table `capacity_snapshots` (timestamp, server, source, cpu_allocated,
  ram_allocated_gb, storage_used_gb, storage_capacity_gb) populated by a
  Phase-0 scheduler job that calls the *existing*
  `capacity_service.build_proxmox_capacity_view`/`build_ranges_capacity_view`
  logic (no new capacity-computation code, just periodic persistence of
  what `/api/ludus/capacity` already computes on demand).
- New endpoint `GET /api/ludus/capacity/history?server=&hours=`.
- Frontend: add `recharts` dependency, new line-chart component under the
  existing `HostCapacityCard` on the Dashboard.

**Verify**: service-level test that a snapshot job writes a row with the
expected shape; manual check that the chart renders after a few scheduler
ticks (may need to temporarily shorten the interval for testing, then reset).

---

## Phase 4 - Quota/template UX

- **Cost preview + auto-suggest quota**: add `estimated_cpus`/
  `estimated_ram_gb`/`estimated_vm_count` to the existing `LabTemplateRead`
  schema, computed via `compute_range_resources()` at serialization time (no
  new endpoint). `CreateSessionModal` pre-fills the quota fields from this
  value × mode multiplier (shared=1, dedicated=student count) when a
  template is selected - still editable, so this directly prevents the
  CPU-quota-too-low mistake found earlier in this session.
- **Capacity warning before provisioning**: in `CreateSessionModal`, combine
  the template's estimated cost with the already-fetched
  `GET /api/ludus/capacity` to show a warning banner if this range would
  push the target server over capacity - pure frontend computation, both
  data sources already exist.
- **Per-server "which has room" hint**: show each configured server's
  `cpu_available`/`ram_available_gb` next to its name in the
  template/server picker.
- **Template validation warnings**: extend the existing cap-check in
  `app/services/labs.py` (`_validate_range_cap`) to also return non-fatal
  warnings (not hard blocks) when a VM entry is missing `cpus`/`ram_gb`/
  `template` - shown as a dismissible warning in the Lab Templates editor,
  not a blocking error (matches the "validate at the boundary, don't
  silently coerce to 0" lesson from the earlier quota incident).

**Verify**: schema test confirming `estimated_*` fields match
`compute_range_resources` output for a known template; manual check in the
Create Range modal that quota fields pre-fill correctly per mode.

---

## Phase 5 - Ludus Management bulk actions, filtering, range clone

- **Bulk actions**: wire `DataTable`'s existing `selectable`/
  `onSelectionChange` into `RangesTab`, add a toolbar ("Destroy selected" /
  "Power On selected" / "Power Off selected") that loops the existing
  single-range handlers (`handleDestroy`/`handlePowerOn`/`handlePowerOff`)
  client-side with per-item progress/error toasts - no new backend endpoint
  needed, these already exist per-range.
- **Filter/search**: extend `RangesTab`'s existing `searchFilter` to also
  match `rangeState`, plus a state-filter dropdown (reusing
  `RangeStatePill`'s known states) and a last-deploy-date sort (the column
  is already sortable). All frontend-only - the data's already in the
  `/api/ludus/ranges` response.
- **Range clone**: new `POST /api/sessions/{id}/clone` - new service
  function in `app/services/sessions.py` alongside the existing
  `create_session`, copying `lab_template_id`/`mode`/quota fields into a new
  `draft` session with no students and fresh dates.

**Verify**: existing `DataTable` selection tests (if any) extended to
`RangesTab`'s new toolbar; service test for `clone_session` asserting the
new row matches source quotas/mode but starts empty/draft.

---

## Phase 6 - Observer role

- New `app/core/deps.py` dependency `require_role(*allowed)` wrapping
  `get_current_user`; applied to mutating routes (POST/PUT/DELETE across
  sessions/students/labs/ludus/settings) to reject `role == "observer"` with
  403. Read-only GETs stay open to any authenticated role - no ownership
  filtering (per the roles decision above).
- New Settings-page "Lab Managers" view: `GET`/`PATCH
  /api/settings/users` listing all `User` rows (local + JIT-provisioned) with
  a role selector, so the admin can actually assign "observer" to an
  account - today there's no UI to see or manage these accounts at all.

**Verify**: API test confirming an observer-role user gets 403 on a mutating
route and 200 on the equivalent GET; confirm the existing JIT-provisioning
flow (`app/api/auth.py`) still defaults new accounts to "lab manager", not
"observer".

---

## Phase 7 - Self-service student reset

- New public `POST /invite/{token}/reset` alongside the existing
  `GET /invite/{token}` / `GET /invite/{token}/config` routes
  (`app/api/invite.py`). Reuses the **existing cooldown logic** already
  built for lab-manager-triggered resets (there's already a
  `test_reset_cooldown.py` - confirm and reuse that exact mechanism rather
  than inventing a second cooldown scheme) and the same snapshot-revert call
  the manual reset button uses. Rate-limited via `slowapi` (already a
  dependency, already used on `/api/auth/login`).
- Invite page gets a "Reset my range" button, gated by the same cooldown
  the backend enforces (disabled client-side with a "try again in X" message
  when on cooldown).

**Verify**: API test hitting the new endpoint twice in quick succession,
confirming the second is rejected by the reused cooldown logic; manual check
on a real invite page.
