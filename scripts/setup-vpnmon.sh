#!/usr/bin/env bash
#
# setup-vpnmon.sh — provision a least-privilege "vpnmon" user on the Ludus host
# so ludus-mgr can read WireGuard connection state WITHOUT SSHing as root.
#
# ludus-mgr's VPN login tracking (the "VPN Activity" page) needs to run exactly
# one read-only command on the Ludus host:  `wg show <iface> dump`.  This script
# locks that down to the bone:
#
#   * a dedicated unprivileged user           (no login shell needed)
#   * scoped NOPASSWD sudo for ONLY that one command
#   * a wrapper that hardcodes the command    (ignores anything SSH supplies)
#   * a FORCED-COMMAND SSH key with all forwarding/pty disabled
#
# Net effect: even if the app's SSH key leaks, it can only ever run
# `wg show <iface> dump` — no shell, no port-forwarding, no other binaries —
# and that command is strictly read-only. No ludus-mgr code change is required:
# its `VPN_WG_MODE=ssh` path already runs exactly this command, and the forced
# command means the host runs its own copy regardless.
#
# RUN THIS ON THE LUDUS HOST, as root (or via sudo).
#
# Usage:
#   sudo scripts/setup-vpnmon.sh --pubkey /path/to/app_key.pub [options]
#   sudo scripts/setup-vpnmon.sh --pubkey -   < app_key.pub
#
# Options:
#   --pubkey FILE|-   Public key for the app's SSH key (file, or "-" for stdin). Required.
#   --user NAME       Service account to create         (default: vpnmon)
#   --interface IFACE WireGuard interface to expose      (default: wg0)
#   --wg PATH         Path to the wg binary              (default: autodetect)
#   -h, --help        Show this help.
#
# After running, on the ludus-mgr box set in .env:
#   VPN_TRACKING_ENABLED=true
#   VPN_WG_MODE=ssh
#   VPN_SSH_HOST=<this host>
#   VPN_SSH_USER=vpnmon
#   VPN_SSH_KEY_PATH=/app/data/<private-key>   # mounted into the backend container
# and verify from the ludus-mgr host:
#   ssh -i <private-key> vpnmon@<this host>   # should print the wg dump and exit
#
set -euo pipefail

USER_NAME="vpnmon"
IFACE="wg0"
PUBKEY_SRC=""
WG_BIN=""

die() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pubkey)    PUBKEY_SRC="${2:-}"; shift 2 ;;
    --user)      USER_NAME="${2:-}"; shift 2 ;;
    --interface) IFACE="${2:-}"; shift 2 ;;
    --wg)        WG_BIN="${2:-}"; shift 2 ;;
    -h|--help)   sed -n '2,45p' "$0"; exit 0 ;;
    *)           die "unknown option: $1 (see --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "must run as root (try: sudo $0 ...)"
[[ -n "$PUBKEY_SRC" ]] || die "--pubkey is required (see --help)"

# Resolve the wg binary and validate the interface name.
if [[ -z "$WG_BIN" ]]; then
  WG_BIN="$(command -v wg || true)"
fi
[[ -n "$WG_BIN" && -x "$WG_BIN" ]] || die "wg binary not found; pass --wg /path/to/wg"
[[ "$IFACE" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid interface name: $IFACE"

# Read the public key (from a file or stdin) and sanity-check it.
if [[ "$PUBKEY_SRC" == "-" ]]; then
  PUBKEY="$(cat)"
else
  [[ -r "$PUBKEY_SRC" ]] || die "cannot read pubkey file: $PUBKEY_SRC"
  PUBKEY="$(cat "$PUBKEY_SRC")"
fi
PUBKEY="$(echo "$PUBKEY" | tr -d '\r' | sed '/^$/d' | head -n1)"
[[ "$PUBKEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-|sk-) ]] \
  || die "that does not look like an SSH PUBLIC key (expected ssh-ed25519/ssh-rsa/...)"

WRAPPER="/usr/local/bin/wg-dump"
SUDOERS="/etc/sudoers.d/${USER_NAME}"

echo ">> [1/4] service account: ${USER_NAME}"
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash "$USER_NAME"
  echo "   created."
else
  echo "   already exists; leaving as-is."
fi

echo ">> [2/4] command wrapper: ${WRAPPER}  (hardcodes: wg show ${IFACE} dump)"
cat > "$WRAPPER" <<EOF
#!/bin/sh
# Installed by setup-vpnmon.sh. Runs ONE read-only command. Args are ignored.
exec sudo -n ${WG_BIN} show ${IFACE} dump
EOF
chmod 0755 "$WRAPPER"

echo ">> [3/4] scoped sudo: ${SUDOERS}"
# NOPASSWD for EXACTLY this one command and nothing else.
cat > "$SUDOERS" <<EOF
${USER_NAME} ALL=(root) NOPASSWD: ${WG_BIN} show ${IFACE} dump
EOF
chmod 0440 "$SUDOERS"
# Validate the whole sudoers tree; roll back our file if it breaks.
if ! visudo -cf "$SUDOERS" >/dev/null; then
  rm -f "$SUDOERS"
  die "sudoers validation failed; removed ${SUDOERS}"
fi

echo ">> [4/4] forced-command SSH key for ${USER_NAME}"
SSH_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)/.ssh"
AUTH="${SSH_DIR}/authorized_keys"
mkdir -p "$SSH_DIR"
OPTS='command="'"$WRAPPER"'",no-port-forwarding,no-agent-forwarding,no-x11-forwarding,no-pty,restrict'
LINE="${OPTS} ${PUBKEY}"
touch "$AUTH"
# Idempotent: drop any prior entry for this exact key, then add the locked one.
KEY_FIELD="$(echo "$PUBKEY" | awk '{print $2}')"
if [[ -n "$KEY_FIELD" ]]; then
  grep -vF "$KEY_FIELD" "$AUTH" > "${AUTH}.tmp" 2>/dev/null || true
  mv "${AUTH}.tmp" "$AUTH"
fi
echo "$LINE" >> "$AUTH"
chmod 0600 "$AUTH"
chmod 0700 "$SSH_DIR"
chown -R "$USER_NAME":"$USER_NAME" "$SSH_DIR"

echo
echo "Done. Quick self-test from THIS host:"
echo "    sudo -u ${USER_NAME} ${WRAPPER}"
echo
echo "And end-to-end from the ludus-mgr host (with the matching PRIVATE key):"
echo "    ssh -i <private-key> ${USER_NAME}@\$(hostname -I | awk '{print \$1}')"
echo "(should print the WireGuard dump and immediately exit — no shell)"
