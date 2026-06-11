#!/usr/bin/env bash
# vast-setup.sh — permanent unified-fleet-key fix for Vast.ai pods.
#
# Vast does NOT auto-inject registered account keys into new pods (only the key
# chosen at create time gets in). This sets up ONE unified "fleet" keypair, puts
# it on every machine listed in local config, registers it once with Vast, and attaches
# it to all running pods so they're SSH-reachable. Overwatch then auto-attaches
# it to any future pod on each scan, so you never hand-fix a pod again.
#
# Reads the same JSON config as overwatch.py (ssh_users -> the nodes that get
# the key, vast_key -> the keypair path). Config search order matches:
#   $FLEET_OVERWATCH_CONFIG, ./overwatch.config.json,
#   ~/.config/fleet-overwatch/config.json
#
# Idempotent — safe to re-run. Use --prune to also delete the old/stale Vast key
# registrations (consolidate to the single fleet key).
set -uo pipefail

PRUNE=0; [ "${1:-}" = "--prune" ] && PRUNE=1

# Resolve config the same way overwatch.py does, then pull key path + nodes.
CFG=""
for c in "${FLEET_OVERWATCH_CONFIG:-}" "overwatch.config.json" "$HOME/.config/fleet-overwatch/config.json"; do
  [ -n "$c" ] && [ -f "$c" ] && { CFG="$c"; break; }
done

KEY="$(python3 - "$CFG" <<'PY'
import json, os, sys
cfg = {}
if sys.argv[1]:
    cfg = json.load(open(sys.argv[1]))
print(os.path.expanduser(cfg.get("vast_key") or "~/.ssh/fleet_overwatch_ed25519"))
PY
)"
PUBFILE="$KEY.pub"

# Fleet nodes to receive the keypair: user@name from the config's ssh_users
# (names resolve over the tailnet via MagicDNS). Empty config -> no distribution
# step, the key is still generated/registered/attached.
NODES=( $(python3 - "$CFG" <<'PY'
import json, sys
cfg = {}
if sys.argv[1]:
    cfg = json.load(open(sys.argv[1]))
for name, user in (cfg.get("ssh_users") or {}).items():
    print(f"{user}@{name}")
PY
) )
SSH_OPTS=( -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new )

say() { printf '\033[96m[vast-setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[93m[vast-setup] WARN:\033[0m %s\n' "$*"; }

[ -n "$CFG" ] && say "config: $CFG" || say "no config found — using defaults (no fleet distribution)"

# 1) Generate the unified fleet keypair (passphrase-less for non-interactive use).
if [ -f "$KEY" ]; then
  say "fleet key exists: $KEY"
else
  say "generating fleet key: $KEY"
  ssh-keygen -t ed25519 -f "$KEY" -N "" -C "fleet-overwatch" >/dev/null
fi
chmod 600 "$KEY"; chmod 644 "$PUBFILE"
PUB="$(cat "$PUBFILE")"
say "pubkey: $PUB"

# 2) Register the pubkey with Vast (only if not already present).
if vastai show ssh-keys --raw 2>/dev/null | grep -qF "${PUB% *}"; then
  say "fleet key already registered with Vast"
else
  say "registering fleet key with Vast"
  vastai create ssh-key "$PUB" -y >/dev/null 2>&1 && say "  registered" || warn "  register failed"
fi

# 3) Distribute the keypair to every reachable fleet node over the tailnet.
# (${NODES[@]+...} keeps `set -u` happy on an empty array under old bash.)
for node in ${NODES[@]+"${NODES[@]}"}; do
  if ! ssh "${SSH_OPTS[@]}" "$node" true 2>/dev/null; then
    warn "unreachable, skipped: $node (re-run later)"; continue
  fi
  remote_key=".ssh/$(basename "$KEY")"
  ssh "${SSH_OPTS[@]}" "$node" "umask 077; mkdir -p ~/.ssh; cat > '$remote_key'" < "$KEY"
  ssh "${SSH_OPTS[@]}" "$node" "cat > '$remote_key.pub' && chmod 600 '$remote_key' && chmod 644 '$remote_key.pub' && grep -qxF \"\$(cat '$remote_key.pub')\" ~/.ssh/authorized_keys 2>/dev/null || cat '$remote_key.pub' >> ~/.ssh/authorized_keys" < "$PUBFILE"
  say "distributed to $node"
done

# 4) Attach the fleet key to all currently-running pods.
IDS="$(vastai show instances-v1 --raw 2>/dev/null | python3 -c '
import sys, json
raw = sys.stdin.read()
s = min([i for i in (raw.find("["), raw.find("{")) if i >= 0] or [-1])
data = json.loads(raw[s:]) if s >= 0 else []
items = data.get("instances", data) if isinstance(data, dict) else data
print(" ".join(str(i["id"]) for i in items if i.get("actual_status") == "running"))
' 2>/dev/null)"
if [ -n "${IDS// }" ]; then
  for id in $IDS; do
    vastai attach ssh "$id" "$PUB" >/dev/null 2>&1 && say "attached to pod $id" || warn "attach failed for pod $id"
  done
else
  say "no running pods to attach right now"
fi

# 5) Clean up broken key registrations (public_key that isn't an ssh-* key body).
BROKEN="$(vastai show ssh-keys --raw 2>/dev/null | python3 -c '
import sys, json
raw = sys.stdin.read(); raw = raw[raw.find("["):]
for k in json.loads(raw or "[]"):
    pk = (k.get("public_key") or "").strip()
    if not pk.startswith("ssh-"):
        print(k["id"])
' 2>/dev/null)"
for id in $BROKEN; do
  vastai delete ssh-key "$id" >/dev/null 2>&1 && say "deleted broken key $id" || warn "could not delete key $id"
done

# Optional: prune stale (non-fleet) key registrations to consolidate.
if [ "$PRUNE" = 1 ]; then
  STALE="$(vastai show ssh-keys --raw 2>/dev/null | python3 -c "
import sys, json
raw = sys.stdin.read(); raw = raw[raw.find('['):]
fleet = '''$PUB'''.split()[1]
for k in json.loads(raw or '[]'):
    pk = (k.get('public_key') or '').strip()
    if pk.startswith('ssh-') and pk.split()[1] != fleet:
        print(k['id'])
" 2>/dev/null)"
  for id in $STALE; do
    vastai delete ssh-key "$id" >/dev/null 2>&1 && say "pruned stale key $id" || warn "could not prune key $id"
  done
fi

say "done. Verify: ssh -i $KEY -p <port> <user>@<host> nvidia-smi -L"
