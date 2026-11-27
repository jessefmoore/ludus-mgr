# Proxmox / Ludus Host Troubleshooting Runbook

Notes from diagnosing the Ludus v1 Proxmox host (`ludus`, `192.168.0.191`) on
2026-07-11. Two separate faults hit the same day and were related: a DNS
outage first, then a blank Proxmox web GUI. Fixing DNS was a prerequisite for
fixing the GUI (the GUI fix needed to download a package).

Host facts for reference:
- SSH: `ssh root@192.168.0.191` (Debian 12 / Proxmox VE 8.4)
- Web GUI: `https://192.168.0.191:8006/`
- Single node (not clustered — `corosync inactive` and "corosync.conf does not
  exist" are **normal**, not errors).

---

## Problem 1 — Host "has no internet"

### Symptom
SSH into the host works, but things that need the internet fail. `ping` by
name fails with `Temporary failure in name resolution`.

### The one distinction that matters
**Ping by IP vs by name.** If IP works but name doesn't, it's **DNS**, not
connectivity.

```bash
ip route                      # is there a default route?
ip -br addr                   # is the uplink (eno1) up with an IP?
ping -c2 1.1.1.1              # internet by IP  -> tests routing/connectivity
ping -c2 deb.debian.org       # internet by name -> tests DNS
cat /etc/resolv.conf          # which nameserver(s) are configured?
```

### What we found
- Default route fine (`via 192.168.0.1 dev eno1`), uplink up, gateway pingable.
- `ping 1.1.1.1` / `ping 8.8.8.8` → **worked** (internet is fine).
- `ping deb.debian.org` → **failed** (DNS broken).
- `/etc/resolv.conf` listed **only** the router: `nameserver 192.168.0.1`.
- Querying the router directly timed out; querying public DNS worked:

```bash
nslookup deb.debian.org 192.168.0.1   # -> timed out, "no servers could be reached"
nslookup deb.debian.org 1.1.1.1       # -> resolved fine
```

### Root cause
The **router's DNS service (192.168.0.1:53) stopped answering.** It still
routed and pinged, but no longer resolved names. The host had no fallback
resolver configured, so every lookup failed.

### Fix
`/etc/resolv.conf` here is a plain static file (interface is `inet static`;
no `resolvconf`, no `systemd-resolved`, no NetworkManager, not immutable), so
editing it directly persists across reboots.

```bash
cp -a /etc/resolv.conf /etc/resolv.conf.bak.$(date +%Y%m%d%H%M%S)
cat > /etc/resolv.conf <<'RC'
# Router DNS (192.168.0.1) stopped responding; using public resolvers.
# Router kept last so local .home names resolve once it recovers.
nameserver 1.1.1.1
nameserver 8.8.8.8
nameserver 192.168.0.1
search home
RC

# verify
getent hosts deb.debian.org
ping -c2 deb.debian.org
curl -sI https://deb.debian.org/     # expect HTTP/2 200
```

### Notes / follow-up
- The real fix is on the **router** — restart/repair its DNS service. The host
  is now resilient regardless (public resolvers first).
- **Range VMs** that pointed at the router for DNS would fail name resolution
  the same way (e.g. Ansible role installs pulling from the internet). They
  recover on next DHCP renew / redeploy once host DNS is healthy.
- Persistence check — confirm nothing will overwrite `resolv.conf`:
  ```bash
  grep -rE "eno1|dns-nameserver" /etc/network/interfaces*   # static? no dns-* line?
  dpkg -l resolvconf | grep ^ii                             # not installed = good
  systemctl is-active systemd-resolved NetworkManager       # inactive = good
  lsattr /etc/resolv.conf                                    # no 'i' (immutable) flag
  ```

---

## Problem 2 — Proxmox web GUI is blank

### Symptom
`https://192.168.0.191:8006/` loads but renders a **blank white page**. Happens
in multiple browsers (Chrome + Edge), and a hard refresh doesn't help — so it
is **not** a browser cache issue.

### Prove the server is healthy first
Proxmox ships an **empty `<body>`** and builds the entire UI in JavaScript
(`Ext.onReady(() => Ext.create('PVE.StdWorkspace'))`). If any JS throws during
init, the body stays empty → blank page. So a blank page with everything
returning HTTP 200 means the fault is in **JS execution**, not the server.

```bash
# services (single node: corosync inactive is normal)
systemctl is-active pve-cluster pvedaemon pveproxy pvestatd
mountpoint /etc/pve && ls /etc/pve           # pmxcfs mounted + populated
df -h / ; df -i /                            # not full (disk or inodes)
ss -ltnp | grep :8006                        # pveproxy listening

# all assets should be HTTP 200 (run from the host AND from a LAN client):
for a in / /pve2/js/pvemanagerlib.js /pve2/ext6/ext-all.js \
         /proxmoxlib.js /pve2/ext6/locale/locale-en.js /api2/json/version; do
  curl -sk -o /dev/null -w "%{http_code}  $a\n" "https://192.168.0.191:8006$a"
done
# ( / and assets -> 200 ; /api2/json/version -> 401 which is healthy: it wants login )

dpkg --audit                                 # broken/half-configured packages?
dpkg -l libjs-extjs pve-manager proxmox-widget-toolkit | grep ^ii
```

If all of the above is clean (ours was), the answer is in the **browser
DevTools Console** — that's non-negotiable for a blank ExtJS page.

### Get the console error (the actual diagnosis)
In the browser: **F12 → Console**, hard reload, read the **first** red error
(the rest usually cascade from it). Ours were:

```
Uncaught SyntaxError: Illegal return statement (proxmoxlib.js:25405:21)   <-- ROOT CAUSE
Uncaught TypeError: Cannot read properties of undefined (reading 'defaultText')  (pvemanagerlib.js)
GET .../PVE/StdWorkspace.js 500 (no such file)
Uncaught TypeError: c is not a constructor
```

Only the **first** matters: `proxmoxlib.js` had an **illegal top-level
`return`** → the file wouldn't parse → its widget classes never registered →
`PVE.StdWorkspace` couldn't be built → blank page. Everything else is downstream
noise.

### Root cause
Someone had installed a **"remove the No-valid-subscription nag"** hack. It adds
an APT hook that `sed`-patches `proxmoxlib.js` after every package operation:

```
/etc/apt/apt.conf.d/no-nag-script
```

On an older package version the patch had left a dangling `orig_cmd(); return;`
as the **last line of the file** (outside any function) → the illegal-return
syntax error. Confirm the file was modified vs the package and see the break:

```bash
dpkg -V proxmox-widget-toolkit                 # "??5??????  .../proxmoxlib.js" = MD5 differs = modified
tail -3 /usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js
```

### Fix — restore a clean file by reinstalling
Requires working internet/DNS (see Problem 1 — that's why DNS had to be fixed
first).

```bash
JS=/usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js
cp -a "$JS" "$JS.broken.$(date +%Y%m%d%H%M%S)"          # back up the broken copy
apt-get install --reinstall -y proxmox-widget-toolkit    # pulls a clean version
```

The reinstall printed `Removing subscription nag from UI...` — i.e. the APT hook
re-ran and re-applied its patch. On the new version the patch is the **safe**
one (`s/active/NoMoreNagging/` on `data.status` lines) and does **not** break
syntax.

### Verify the JS actually parses (don't guess)
```bash
# on a machine with node, fetch the live file and check it:
curl -sk "https://192.168.0.191:8006/proxmoxlib.js?ver=x" -o /tmp/proxmoxlib.js
node --check /tmp/proxmoxlib.js          # -> must print nothing / exit 0 (syntax OK)
grep -c "orig_cmd(); return" /tmp/proxmoxlib.js   # -> 0
```

Then in the browser: **hard reload `Ctrl+Shift+R`** (the browser cached the
broken JS). GUI renders.

### The lasting risk (decide what to do with the hook)
The nag-removal APT hook is what caused this. Even the "safe" version can
re-break the GUI on a future Proxmox update if the code layout changes again.

- **Leave it:** nag stays gone; small risk on future upgrades. Re-run the
  reinstall + `node --check` if a future update blanks the UI again.
- **Remove it (safer):** `rm /etc/apt/apt.conf.d/no-nag-script` — you'll get the
  harmless "No valid subscription" popup on login, but a package update can
  never corrupt the UI this way again.

---

## Problem 3 — "Provision All" fails: manager can't reach the Ludus admin API

### Symptom
In the ludus-v1-mgr app, **Provision All** errors and every student ends in
`error`. The audit log / backend shows:

```
student.provision_failed | LudusError('Ludus transport error: [Errno 111] Connection refused')
```

Everything *else* in the app works (ranges list, capacity, snapshots, reset) —
only provisioning fails.

### Why (the key insight)
Ludus v1 splits its API across **two** servers:

- **User API** on `*:8080` — ranges, snapshots, user *reads*. Reachable on the
  LAN, so most of the app works.
- **Admin API** on **`127.0.0.1:8081`** (localhost-only) — user **create/delete**
  (`POST/DELETE /user`).

Provisioning's very first step is `user_add`, which must hit the **admin API**.
Because the admin API binds to localhost on the Ludus host, a manager running
elsewhere (here: a Docker container on another box) cannot reach it → connection
refused → provisioning dies before it deploys anything.

Confirm the split:
```bash
# user API rejects user creation:
curl -sk -o /dev/null -w "%{http_code}\n" -X POST https://192.168.0.191:8080/user \
  -H "X-API-KEY: $KEY" -H 'Content-Type: application/json' -d '{}'      # -> 403
# admin API (from the host itself) accepts it (400 = bad body, route works):
ssh root@192.168.0.191 "curl -sk -o /dev/null -w '%{http_code}\n' -X POST \
  https://127.0.0.1:8081/user -H 'X-API-KEY: $KEY' -H 'Content-Type: application/json' -d '{}'"  # -> 400
ss -ltnp | grep -E ':8080|:8081'    # 8080 on *:, 8081 on 127.0.0.1
```

A misconfigured `LUDUS_DEFAULT_ADMIN_URL` (pointing at a dead host/port) has the
same effect. Tip: the source IP the Ludus host sees for the manager is often a
**NAT'd** address (a router between subnets rewrites it) — determine it before
writing a firewall rule:
```bash
# hold a connection open from the manager side, then read the peer on the host:
ssh root@192.168.0.191 "ss -tn state established '( sport = :8080 )'"
```

### Fix — expose the admin API to the manager only, via a source-restricted proxy
The admin API has to be reachable from the manager, which means forwarding
`127.0.0.1:8081` to a LAN address **restricted to the manager's (NAT'd) source
IP**. This example uses the manager source `192.168.0.193`:

```ini
# /etc/systemd/system/ludus-admin-proxy.service  (on the Ludus host)
[Unit]
Description=Expose Ludus admin API (127.0.0.1:8081) to the manager host only
After=network-online.target ludus.service
Wants=network-online.target

[Service]
ExecStartPre=/bin/sh -c '/sbin/iptables -C INPUT -p tcp --dport 18081 ! -s 192.168.0.193 -j DROP 2>/dev/null || /sbin/iptables -I INPUT -p tcp --dport 18081 ! -s 192.168.0.193 -j DROP'
ExecStart=/usr/bin/socat TCP-LISTEN:18081,fork,reuseaddr,bind=192.168.0.191,range=192.168.0.193/32 TCP:127.0.0.1:8081
ExecStopPost=-/sbin/iptables -D INPUT -p tcp --dport 18081 ! -s 192.168.0.193 -j DROP
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```
```bash
apt-get install -y socat            # if missing
systemctl daemon-reload && systemctl enable --now ludus-admin-proxy
ss -ltn | grep 18081                 # listening on 192.168.0.191:18081
```

Two layers of source restriction: socat's `range=192.168.0.193/32` (survives
reboot as part of the enabled unit) **and** the iptables rule (managed by the
unit so it's durable + self-cleaning). TLS passes straight through (raw TCP);
the admin API's self-signed cert is fine because the manager runs with
`verify_tls=false`.

Then point the manager at it and restart it:
```bash
# in the manager's .env (NOT in git):
LUDUS_DEFAULT_ADMIN_URL=https://192.168.0.191:18081
docker compose up -d backend        # recreate to pick up the env change
```

### Verify
```bash
# from the manager's network path, the admin route now answers (400 = reached):
curl -sk -o /dev/null -w "%{http_code}\n" -X POST https://192.168.0.191:18081/user \
  -H "X-API-KEY: $KEY" -H 'Content-Type: application/json' -d '{}'
# then Provision All in the app -> students go "ready", event student.provisioned
```

### Security note
This makes a powerful API (create/delete any Ludus user) reachable on the LAN.
It's protected by (a) the API key, (b) socat `range=` + the iptables rule
limiting the source to the manager host. Tighten further with mTLS or a VPN if
the LAN isn't trusted. A `session.role_scope_failed` warning during provisioning
(e.g. "role already installed") is **benign** — provisioning treats it as
non-fatal and continues.

---

## Problem 4 — Blank GUI keeps coming back (the permanent fix)

### Symptom
You fix the blank GUI (Problem 2), it renders, then **it goes blank again** later —
often right after a Ludus operation (a range deploy, or even removing a user).

### Root cause
This is a recurrence of Problem 2's illegal top-level `return`, but the source is
**Ludus's `lae.proxmox` ansible role**, which runs on Ludus operations (it's
referenced by `proxmox-install/*` **and `user-management/del-user.yml`**, so it
fires on deploy *and* user delete). Its subscription-nag task:

```yaml
- name: Remove subscription check wrapper function in web UI
  ansible.builtin.lineinfile:
    path: /usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js
    line: '        orig_cmd(); return;'
    insertafter: '^\s+checked_command: function\(orig_cmd\) {$'   # <-- BUG
    firstmatch: yes
```

The `insertafter` regex has **`function\(orig_cmd\)` (no space)**, but the real
code is **`function (orig_cmd)` (with a space)**. On `proxmox-widget-toolkit`
>= 4.3 the anchor never matches, so `lineinfile` **appends the line at the end of
the file** — a top-level `return` → illegal-return SyntaxError → blank GUI. Every
Ludus op re-appends it.

> **`node --check` is a false negative here.** It parses proxmoxlib.js as a
> CommonJS module (where top-level `return` is legal), so it prints "OK" even
> when the file is broken. The browser loads it as a **classic script**, where
> top-level `return` is illegal. Verify by confirming there's **no `orig_cmd();
> return;` after the file's final `}));`**, not with `node --check`.

### Permanent fix (two parts — no more running commands each time)

**Part 1 — fix the regex in every copy of the role** (master + per-user copies)
so `lineinfile` matches and inserts the line *inside* `checked_command` (valid)
instead of appending at EOF:
```bash
find / -path '*lae.proxmox/tasks/main.yml' 2>/dev/null | while IFS= read -r f; do
  sed -i 's|checked_command: function\\(orig_cmd\\)|checked_command: function ?\\(orig_cmd\\)|g' "$f"
done
```
`function ?\(orig_cmd\)` tolerates the optional space. Because the valid line now
lives inside the function, `lineinfile` also finds it already present and no-ops.

**Part 2 — a self-healing watcher** as a backstop (covers a future Ludus update
re-installing the broken role, or an apt upgrade wiping the file):
```bash
cat > /usr/local/sbin/fix-proxmoxlib.sh <<'SCRIPT'
#!/bin/bash
JS=/usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js
[ -f "$JS" ] || exit 0
# strip stray top-level 'orig_cmd(); return;' at EOF
while [ "$(tail -1 "$JS" 2>/dev/null | tr -d '[:space:]')" = "orig_cmd();return;" ]; do
  sed -i '$d' "$JS"
done
# ensure the valid short-circuit lives inside checked_command
if grep -q '^        checked_command: function (orig_cmd) {$' "$JS"; then
  nxt=$(grep -A1 '^        checked_command: function (orig_cmd) {$' "$JS" | sed -n 2p | tr -d '[:space:]')
  [ "$nxt" != "orig_cmd();return;" ] && \
    sed -i 's|^\(        checked_command: function (orig_cmd) {\)$|\1\n        orig_cmd(); return;|' "$JS"
fi
SCRIPT
chmod +x /usr/local/sbin/fix-proxmoxlib.sh

cat > /etc/systemd/system/proxmoxlib-heal.service <<'SVC'
[Unit]
Description=Repair proxmoxlib.js after nag-patch corruption
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fix-proxmoxlib.sh
SVC

cat > /etc/systemd/system/proxmoxlib-heal.path <<'PATHU'
[Unit]
Description=Watch proxmoxlib.js for nag-patch corruption
[Path]
PathModified=/usr/share/javascript/proxmox-widget-toolkit/
Unit=proxmoxlib-heal.service
[Install]
WantedBy=multi-user.target
PATHU

systemctl daemon-reload && systemctl enable --now proxmoxlib-heal.path
```
The path unit re-runs the repair the instant `proxmoxlib.js` changes, so the GUI
self-heals within ~1s of any future corruption. The script is idempotent (no-op
when healthy). Self-test it: `printf '        orig_cmd(); return;\n' >> "$JS"` and
confirm the last line reverts to `}));` a moment later.

### Verify
```bash
tail -1 /usr/share/javascript/proxmox-widget-toolkit/proxmoxlib.js   # -> }));
systemctl is-active proxmoxlib-heal.path                              # -> active
```
Then hard-reload the GUI.

---

## Quick reference — order of operations

1. **Blank GUI?** Check services + assets are 200. If they are, go to the
   browser **Console** and read the first red error. Don't touch the server
   until you know it's actually a server problem.
2. **"No internet"?** `ping` by IP vs by name. If IP works, it's DNS — check
   `/etc/resolv.conf` and query each nameserver directly with `nslookup`.
3. A syntax error in a `proxmoxlib.js` / `pvemanagerlib.js` almost always means
   a **modified file** (`dpkg -V`) — reinstall the owning package. Verify with
   "no top-level `return` after the final `}));`", **not** `node --check`
   (false negative — it parses as CommonJS).
4. **Provision All "Connection refused"** but everything else works? It's the
   **admin API** (`user_add`) — localhost-only on `:8081`. Expose it to the
   manager with the source-restricted `ludus-admin-proxy` and fix
   `LUDUS_DEFAULT_ADMIN_URL`.
5. **Blank GUI keeps returning** after Ludus ops? The `lae.proxmox` role's
   nag-patch regex mis-anchors and appends an illegal top-level `return`. Fix
   the regex in all role copies + install the `proxmoxlib-heal` path unit
   (Problem 4) — then it self-heals and you never touch it again.
