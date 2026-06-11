#!/usr/bin/env python3
"""Fleet Overwatch — live GPU/CPU panel for a Tailscale fleet + Vast.ai pods.

Discovers every tailnet device from the tailscale CLI and every rented Vast.ai
pod from the vastai API, polls each over SSH in parallel, and draws a
refreshing grid of per-machine tiles. The default look is phosphor green
snapping to purple only at the top (>=90% load); 'c' cycles through 8 color
schemes, +/- through 24 bar themes, 'v' through 24 gauge dial styles, and 'f'
through 5 TOTALS field styles (choices persist across runs). Offline /
unreachable nodes render greyed. Pure Python stdlib — no pip, no daemons on
the probed machines.

Site-specific wiring (SSH login users, display order, GPU labels, key paths)
lives in an OPTIONAL JSON config file — see config.example.json. Search order:
  --config PATH, $FLEET_OVERWATCH_CONFIG, ./overwatch.config.json,
  ~/.config/fleet-overwatch/config.json
With no config at all the panel still works: every tailnet device is probed as
the current login user, and normal SSH configuration applies.

  ./overwatch.py                 # live TUI (q to quit): FLEET + VAST PODS
  ./overwatch.py --once          # one plain-text frame, then exit (debug/verify)
  ./overwatch.py --interval 2    # refresh cadence in seconds
  ./overwatch.py --nodes node-a,node-b
  ./overwatch.py --only vast     # pods only      (--only fleet for tailnet only)
  ./overwatch.py --no-vast       # hide pods
  ./overwatch.py --theme comet --scheme ember   # start with a chosen look
  ./overwatch.py --vast-attach   # attach fleet key to all running pods, then exit
  ./overwatch.py --no-vast-attach  # disable the auto-heal attach pass

Keys: q quit · +/- bar theme · c/C color scheme · v/V gauge style · f/F field
style · g gauge/bars view
"""

import argparse
import curses
import json
import locale
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Configuration ───────────────────────────────────────────────────────────
# Everything site-specific is read from an optional JSON config (all keys
# optional; see config.example.json). Tailscale supplies discovery, liveness
# and addressing live from `tailscale status` — no LAN IPs, no hardcoded
# hostnames. The only per-node fact it can't supply is the SSH login user.
CONFIG_DIR = os.path.expanduser("~/.config/fleet-overwatch")

SSH_USER = {}            # node name -> SSH login user (config: "ssh_users")
DEFAULT_USER = None      # for unlisted nodes; None -> ssh's own default user
GPU_LABEL = {}           # node name -> cosmetic GPU name when no tool is queryable
EXCLUDE = set()          # tailnet device names to never show (e.g. a Windows alter ego)
ORDER = []               # preferred display order; unknown nodes appended alphabetically
TAILSCALE_CANDIDATES = []
VASTAI_CANDIDATES = []
VAST_KEY = VAST_PUB = None
VAST_USER = "root"
VAST_SSH_OPTS = []

# WSL setups: Tailscale usually lives on the Windows host, so try its exe paths
# before a PATH-resolved tailscale (which covers plain Linux/macOS).
_TAILSCALE_DEFAULTS = [
    "/mnt/c/Program Files/Tailscale/tailscale.exe",
    "/mnt/c/Program Files (x86)/Tailscale/tailscale.exe",
    "tailscale",
]


def _config_path(cli_path=None):
    for c in (cli_path, os.environ.get("FLEET_OVERWATCH_CONFIG"),
              "overwatch.config.json", os.path.join(CONFIG_DIR, "config.json")):
        if c and os.path.exists(c):
            return c
    return None


def apply_config(path=None):
    """Load the JSON config (if any) into the module-level wiring. Returns the
    path used, or None when running on pure defaults."""
    global SSH_USER, DEFAULT_USER, GPU_LABEL, EXCLUDE, ORDER
    global TAILSCALE_CANDIDATES, VASTAI_CANDIDATES, VAST_KEY, VAST_PUB, VAST_USER, VAST_SSH_OPTS
    cfg, used = {}, _config_path(path)
    if used:
        try:
            with open(used) as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"warning: ignoring unreadable config {used}: {e}", file=sys.stderr)
            cfg, used = {}, None
    SSH_USER = {str(k).lower(): str(v) for k, v in (cfg.get("ssh_users") or {}).items()}
    DEFAULT_USER = cfg.get("default_user") or None
    GPU_LABEL = {str(k).lower(): str(v) for k, v in (cfg.get("gpu_labels") or {}).items()}
    EXCLUDE = {str(x).lower() for x in (cfg.get("exclude") or [])}
    ORDER = [str(x).lower() for x in (cfg.get("order") or [])]
    TAILSCALE_CANDIDATES = list(cfg.get("tailscale_paths") or _TAILSCALE_DEFAULTS)
    VASTAI_CANDIDATES = [os.path.expanduser(p) for p in
                         (cfg.get("vastai_paths") or ["vastai", "~/.local/bin/vastai"])]
    # ── Vast.ai wiring: one unified "fleet" key reaches every rented pod (see
    # vast-setup.sh). Pods are ephemeral so host keys churn — disable host-key
    # checking for them ONLY. We use Vast's DIRECT connection (public_ipaddr +
    # mapped 22/tcp port); the sshN.vast.ai proxy is unreliable (kex closes).
    VAST_KEY = os.path.expanduser(cfg.get("vast_key") or "~/.ssh/fleet_overwatch_ed25519")
    VAST_PUB = VAST_KEY + ".pub"
    VAST_USER = str(cfg.get("vast_user") or "root")
    VAST_SSH_OPTS = [
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "IdentitiesOnly=yes", "-i", VAST_KEY,
    ]
    return used


apply_config()           # module load: env var / cwd / XDG paths (no CLI yet)
ATTEMPTED_ATTACH = set()             # pod ids we've fired an auto-attach at this run

# Remote probe: one ssh round-trip. Marker-delimited so we parse with a plain
# split and never assume python3 on the remote. GPU via nvidia-smi (authoritative)
# or xpu-smi (Intel, best-effort); CPU via /proc/stat (delta computed here).
PROBE = r'''
echo "==GPUKIND=="
if command -v nvidia-smi >/dev/null 2>&1; then echo nvidia
elif readlink -f /sys/class/drm/card*/device/driver 2>/dev/null | grep -q '/xe$\|/i915$'; then echo intel
else echo none; fi
echo "==GPU=="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw \
    --format=csv,noheader,nounits 2>/dev/null
else
  # Intel xe (Arc/Battlemage): our own sysfs/procfs reader (no intel_gpu_top, no perf,
  # no elevated privileges). Emits ONE CSV line matching the nvidia format: name,util%,memMiB used,
  # memMiB total,tempC,powerW. util = 1 - gt0 RC6 idle-residency over a 1s window;
  # power = energy1 (card) delta; VRAM used = visible drm clients' resident-vram;
  # VRAM total = PCI BAR2 size.
  XCARD=
  for d in /sys/class/drm/card[0-9]*; do
    case "${d##*/}" in *-*) continue ;; esac
    case "$(readlink -f "$d/device/driver" 2>/dev/null)" in */xe) XCARD=$d; break ;; esac
  done
  if [ -n "$XCARD" ]; then
    DEV=$XCARD/device; GT=$DEV/tile0/gt0; PCI=$(basename "$(readlink -f "$DEV")")
    HW=; for h in "$DEV"/hwmon/hwmon*; do [ -r "$h/name" ] && [ "$(cat "$h/name" 2>/dev/null)" = xe ] && { HW=$h; break; }; done
    TC=; for lf in "$HW"/temp*_label; do [ -r "$lf" ] || continue; v=$(cat "${lf%_label}_input" 2>/dev/null); [ -n "$v" ] && [ "$(cat "$lf" 2>/dev/null)" = pkg ] && TC=$((v/1000)); done
    VT=; if [ -r "$DEV/resource" ]; then set -- $(awk 'NR==3{print $1,$2}' "$DEV/resource"); [ -n "$1" ] && [ "$2" != 0x0000000000000000 ] && VT=$(( (($2-$1)+1)/1048576 )); fi
    VU=$(grep -s '^drm-pdev:\|^drm-client-id:\|^drm-resident-vram0:' /proc/[0-9]*/fdinfo/* 2>/dev/null | awk -v pci="$PCI" '{i=index($0,":");p=substr($0,1,i-1);r=substr($0,i+1);split(r,a,/[ \t]+/);k=a[1];val=a[2];if(k=="drm-pdev:")pd[p]=val;else if(k=="drm-client-id:")ci[p]=val;else if(k=="drm-resident-vram0:")rv[p]=val}END{for(x in pd)if(pd[x]==pci&&ci[x]!=""&&!(s[ci[x]]++))t+=rv[x];printf "%d",t/1024}')
    I=$GT/gtidle/idle_residency_ms; E=$HW/energy1_input
    i1=$(cat "$I" 2>/dev/null); e1=$(cat "$E" 2>/dev/null); t1=$(date +%s%N)
    sleep 1
    i2=$(cat "$I" 2>/dev/null); e2=$(cat "$E" 2>/dev/null); t2=$(date +%s%N)
    dtns=$((t2-t1)); dtms=$((dtns/1000000)); U=; P=
    [ -n "$i1" ] && [ "$dtms" -gt 0 ] && { U=$(( (100*(dtms-(i2-i1)))/dtms )); [ "$U" -lt 0 ] && U=0; [ "$U" -gt 100 ] && U=100; }
    [ -n "$e1" ] && [ "$dtns" -gt 0 ] && P=$(( (e2-e1)*1000/dtns ))
    echo "Intel Arc,$U,$VU,$VT,$TC,$P"
  fi
fi
echo "==CPU=="; grep "^cpu" /proc/stat
echo "==NPROC=="; nproc 2>/dev/null
echo "==LOAD=="; cat /proc/loadavg 2>/dev/null
echo "==MEM=="; grep -E "^MemTotal|^MemAvailable" /proc/meminfo 2>/dev/null
echo "==UPTIME=="; cut -d" " -f1 /proc/uptime 2>/dev/null
echo "==END=="
'''

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=6",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=3",
    "-o", "ServerAliveCountMax=2",
]
SSH_TIMEOUT = 10  # hard wall on a single node's probe

# ── Shared state (single-writer poller thread, lock-guarded reads) ──────────
LOCK = threading.Lock()
LATEST = {}           # name -> rendered node dict (the draw snapshot)
PREV_CPU = {}         # name -> (busy, total) jiffies from previous poll
POLL_INFO = {"ts": 0.0, "dur": 0.0}


# ── Discovery ───────────────────────────────────────────────────────────────
def find_tailscale():
    for c in TAILSCALE_CANDIDATES:
        if os.path.sep in c:
            if os.path.exists(c):
                return c
        elif shutil.which(c):
            return c
    return None


def find_vastai():
    for c in VASTAI_CANDIDATES:
        if os.path.sep in c:
            if os.path.exists(c):
                return c
        elif shutil.which(c):
            return c
    return None


def discover():
    """Return list of tailnet devices: {name, host, ip, os, online, is_self}."""
    ts = find_tailscale()
    if not ts:
        return None                      # can't discover -> caller keeps last view
    try:
        out = subprocess.run([ts, "status", "--json"], capture_output=True,
                             text=True, timeout=10).stdout
        data = json.loads(out)
    except Exception:
        return None

    def short_name(node):
        dns = (node.get("DNSName") or "").strip(".")
        if dns:
            return dns.split(".")[0].lower()
        return (node.get("HostName") or "?").lower()

    devices = []
    self_node = data.get("Self") or {}
    self_name = short_name(self_node) if self_node else None
    for node in [self_node] + list((data.get("Peer") or {}).values()):
        if not node:
            continue
        name = short_name(node)
        if name in EXCLUDE:
            continue
        devices.append({
            "name": name, "source": "fleet",
            "host": node.get("HostName"),
            "ip": (node.get("TailscaleIPs") or [""])[0],
            "os": node.get("OS"),
            "online": bool(node.get("Online")) or (name == self_name),
            "is_self": name == self_name,
        })
    return _dedup_by_host(devices)


# Two tailnet devices sharing a HostName are the SAME physical machine — e.g. a box
# whose Windows and Linux sides each joined the tailnet (mybox + mybox-1). Keep the
# canonical one and record+hide the rest, so a duplicate never shows as an empty,
# un-probeable tile.
DUP_HIDDEN = []          # [(name, host)] collapsed on the most recent discovery sweep


def _dup_rank(d):
    """Sort key to pick which same-host device to KEEP (lower wins): the local node
    first (it gets the LOCAL probe), then preferred ORDER names, then a probeable
    (non-Windows, has-IP) device, then online."""
    return (
        0 if d.get("is_self") else 1,
        ORDER.index(d["name"]) if d["name"] in ORDER else len(ORDER),
        0 if (d.get("ip") and (d.get("os") or "").lower() != "windows") else 1,
        0 if d.get("online") else 1,
        d["name"],
    )


def _dedup_by_host(devices):
    global DUP_HIDDEN
    groups = {}
    for d in devices:
        groups.setdefault((d.get("host") or d["name"]).lower(), []).append(d)
    kept, hidden = [], []
    for members in groups.values():
        members.sort(key=_dup_rank)
        primary = members[0]
        if len(members) > 1:                       # collapse: keep primary, record dups
            primary = {**primary, "aliases": [m["name"] for m in members[1:]]}
            hidden += [(m["name"], m.get("host")) for m in members[1:]]
        kept.append(primary)
    DUP_HIDDEN = hidden
    return kept


def vast_discover():
    """Return list of rented Vast pods as device dicts (source='vast')."""
    v = find_vastai()
    if not v:
        return None                      # no CLI -> caller keeps last view (no flash)
    try:
        out = subprocess.run([v, "show", "instances-v1", "--raw"],
                             capture_output=True, text=True, timeout=20).stdout
        starts = [i for i in (out.find("["), out.find("{")) if i >= 0]
        data = json.loads(out[min(starts):]) if starts else []
        items = data.get("instances", data) if isinstance(data, dict) else data
    except Exception:
        return None                      # transient API/network hiccup -> keep pods

    pods = []
    for i in items:
        # Prefer Vast's DIRECT connection (public_ipaddr + mapped 22/tcp host port).
        ports = i.get("ports") or {}
        p22 = ports.get("22/tcp")
        dport = p22[0].get("HostPort") if p22 else None
        host = i.get("public_ipaddr") if dport else i.get("ssh_host")
        port = dport or i.get("ssh_port")
        state = i.get("actual_status")
        pods.append({
            "name": f"vast {i['id']}", "source": "vast", "id": i["id"],
            "ssh_host": host, "ssh_port": port,
            "num_gpus": i.get("num_gpus") or 1,
            "model": _short_gpu(i.get("gpu_name") or "GPU"),
            "dph": i.get("dph_total"), "geo": i.get("geolocation"),
            "vast_state": state, "online": state == "running",
            "uptime_mins": i.get("uptime_mins"),
            "api": {  # telemetry for fallback when SSH is unavailable
                "gpu_util": i.get("gpu_util"), "gpu_temp": i.get("gpu_temp"),
                "gpu_totalram": i.get("gpu_totalram"), "vmem_usage": i.get("vmem_usage"),
                "cpu_util": i.get("cpu_util"), "cpu_cores": i.get("cpu_cores"),
                "mem_usage": i.get("mem_usage"), "mem_limit": i.get("mem_limit"),
            },
        })
    return pods


# ── Probing ─────────────────────────────────────────────────────────────────
def _run_probe(target, port=None, opts=None):
    """Run PROBE on `target` ('LOCAL' or ssh dest). Returns stdout or raises."""
    if target == "LOCAL":
        cp = subprocess.run(["bash", "-c", PROBE], capture_output=True,
                            text=True, timeout=SSH_TIMEOUT)
    else:
        cmd = ["ssh", *(opts or SSH_OPTS)]
        if port:
            cmd += ["-p", str(port)]
        cmd += [target, PROBE]
        cp = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=SSH_TIMEOUT + (2 if opts else 0))
    if "==END==" not in cp.stdout:
        msg = (cp.stderr or cp.stdout or "no output").strip().splitlines()
        raise RuntimeError(msg[-1] if msg else "no reply")
    return cp.stdout


def _parse_probe(text):
    """Marker-delimited probe output -> sections dict."""
    sections, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^==(\w+)==$", line)
        if m:
            cur = m.group(1)
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line)
    return sections


def _short_gpu(name):
    name = re.sub(r"^NVIDIA\s+GeForce\s+", "", name)
    name = re.sub(r"^NVIDIA\s+", "", name)
    return re.sub(r"\s+GPU$", "", name).strip()


def _cpu_busy_total(cpu_line):
    # "cpu  user nice system idle iowait irq softirq steal guest guest_nice"
    parts = cpu_line.split()
    vals = [int(x) for x in parts[1:] if x.isdigit()]
    if len(vals) < 4:
        return None
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals)
    return total - idle, total


def _parse_metrics(sec):
    """Shared parse of a probe's sections into gpus + cpu/mem fields."""
    def num(x):
        try:
            return float(x)
        except ValueError:
            return None

    gpus = []
    for line in sec.get("GPU", []):
        if not line.strip():
            continue
        f = [c.strip() for c in line.split(",")]
        if len(f) >= 6:
            gpus.append({"name": _short_gpu(f[0]), "util": num(f[1]),
                         "mem_used": num(f[2]), "mem_total": num(f[3]),
                         "temp": num(f[4]), "power": num(f[5])})

    agg_bt, core_bt = None, []
    for line in sec.get("CPU", []):
        parts = line.split()
        if not parts or not parts[0].startswith("cpu"):
            continue
        bt = _cpu_busy_total(line)
        if parts[0] == "cpu":          # the aggregate "cpu " line
            agg_bt = bt
        elif parts[0][3:].isdigit() and bt:   # per-core "cpuN" lines
            core_bt.append(bt)
    ncpu = (sec.get("NPROC") or [""])[0].strip()
    load = (sec.get("LOAD") or [""])[0].split()[:3]
    mem = {}
    for ln in sec.get("MEM", []):
        m = re.match(r"(\w+):\s+(\d+)", ln)
        if m:
            mem[m.group(1)] = int(m.group(2))
    up = (sec.get("UPTIME") or [""])[0].strip()
    return {
        "gpukind": (sec.get("GPUKIND") or ["none"])[0].strip(),
        "gpus": gpus, "cpu_bt": agg_bt, "core_bt": core_bt,
        "ncpu": int(ncpu) if ncpu.isdigit() else None,
        "load": [float(x) for x in load] if load else None,
        "mem_total_kb": mem.get("MemTotal"), "mem_avail_kb": mem.get("MemAvailable"),
        "uptime_s": float(up) if re.match(r"^[\d.]+$", up) else None,
    }


def ssh_target(dev):
    """Tailscale-based SSH destination for a device, or None if not probeable.
    Host is always the device's Tailscale IP; user from SSH_USER (then
    DEFAULT_USER, then ssh's own default user and config)."""
    if dev["is_self"]:
        return "LOCAL"
    if (dev.get("os") or "").lower() == "windows":
        return None              # Windows peers have no /proc to probe
    if not dev.get("ip"):
        return None
    user = SSH_USER.get(dev["name"], DEFAULT_USER)
    return f"{user}@{dev['ip']}" if user else dev["ip"]


def collect(dev):
    """Probe one tailnet device. Returns raw-metrics dict (ok/unreachable/nossh)."""
    target = ssh_target(dev)
    if target is None:
        return {"status": "nossh"}
    try:
        sec = _parse_probe(_run_probe(target))
    except subprocess.TimeoutExpired:
        return {"status": "unreachable", "err": "timeout"}
    except Exception as e:
        return {"status": "unreachable", "err": str(e)[:40]}
    res = {"status": "ok", **_parse_metrics(sec)}
    if res.get("gpukind") == "intel":          # give the xe card its friendly model name
        lbl = GPU_LABEL.get(dev["name"])
        for g in res.get("gpus", []):
            if lbl and g.get("name") in (None, "", "Intel Arc"):
                g["name"] = lbl
    return res


def fleet_pubkey():
    try:
        return open(VAST_PUB).read().strip()
    except OSError:
        return None


def vast_attach(pod_id):
    """Attach the unified fleet key to a running pod (self-heal). Best-effort."""
    v, pub = find_vastai(), fleet_pubkey()
    if not (v and pub):
        return
    try:
        subprocess.run([v, "attach", "ssh", str(pod_id), pub],
                       capture_output=True, text=True, timeout=20)
    except Exception:
        pass


def collect_vast(pod, allow_attach=True):
    """Probe a Vast pod: SSH (real-time per-GPU) preferred, API telemetry fallback."""
    if not pod["online"]:
        return {"status": "offline"}
    a = pod["api"]
    host, port = pod.get("ssh_host"), pod.get("ssh_port")
    if host and port:
        try:
            sec = _parse_probe(_run_probe(f"{VAST_USER}@{host}", port=port, opts=VAST_SSH_OPTS))
            m = _parse_metrics(sec)
            # GPU from SSH nvidia-smi = the pod's assigned card(s), accurate.
            # CPU from REAL /proc (delta + loadavg + nproc) — Vast's API cpu_util is
            # unreliable (observed 96% when the box was at loadavg ~1 / ~1% real). It's
            # host-wide (shared box) so labeled cpu:host, but it's a true measurement.
            # RAM/uptime stay from the API (container limit / rental age, not host).
            ml, mu = a.get("mem_limit"), a.get("mem_usage")
            return {
                "status": "ok", "detail": "gpu:pod cpu:host", "gpus": m["gpus"],
                "cpu_bt": m["cpu_bt"], "ncpu": m["ncpu"], "load": m["load"],
                "mem_total_kb": int(ml * 1024 * 1024) if ml else None,
                "mem_avail_kb": int((ml - mu) * 1024 * 1024) if (ml is not None and mu is not None) else None,
                "uptime_s": pod["uptime_mins"] * 60 if pod.get("uptime_mins") else None,
            }
        except Exception:
            # SSH failed — fire a one-time auto-attach so the next scan succeeds.
            if allow_attach and pod["id"] not in ATTEMPTED_ATTACH:
                ATTEMPTED_ATTACH.add(pod["id"])
                vast_attach(pod["id"])
    # API fallback: aggregate per-instance telemetry (vast-sampled, lagged).
    if a.get("gpu_util") is not None or a.get("cpu_util") is not None:
        model = f"{pod['num_gpus']}x {pod['model']}" if pod["num_gpus"] > 1 else pod["model"]
        vused = a.get("vmem_usage")
        gpus = [{"name": model, "util": a.get("gpu_util"),
                 "mem_used": vused * 1024 if vused is not None else None,  # GB -> MiB
                 "mem_total": a.get("gpu_totalram"), "temp": a.get("gpu_temp"),
                 "power": None}]
        ml, mu = a.get("mem_limit"), a.get("mem_usage")
        return {
            "status": "api", "detail": "api ~lag (agg)", "gpus": gpus,
            "cpu_pct": a.get("cpu_util"), "ncpu": a.get("cpu_cores"), "load": None,
            "mem_total_kb": int(ml * 1024 * 1024) if ml else None,
            "mem_avail_kb": int((ml - mu) * 1024 * 1024) if (ml is not None and mu is not None) else None,
            "uptime_s": pod["uptime_mins"] * 60 if pod.get("uptime_mins") else None,
        }
    return {"status": "unreachable", "detail": "no ssh / no api", "err": "unreachable"}


# ── Poll cycle ──────────────────────────────────────────────────────────────
def _pct_delta(cur, prev):
    """Busy/total jiffie delta -> utilization %, or None if no usable prior sample."""
    if not (cur and prev) or cur[1] <= prev[1]:
        return None
    dbusy, dtotal = cur[0] - prev[0], cur[1] - prev[1]
    return max(0.0, min(100.0, 100.0 * dbusy / dtotal)) if dtotal else None


def _apply_cpu_delta(node, name, bt, core_bt=None):
    """CPU% (macro) + per-core % as deltas between consecutive polls (NOT since-boot)."""
    prev = PREV_CPU.get(name) or {}
    node["cpu_pct"] = _pct_delta(bt, prev.get("agg"))
    pcores, cbt = prev.get("cores") or [], core_bt or []
    if pcores and len(pcores) == len(cbt):
        node["core_pct"] = [_pct_delta(c, p) for c, p in zip(cbt, pcores)]
    else:
        node["core_pct"] = None  # first sample / core-count change -> warming
    PREV_CPU[name] = {"agg": bt, "cores": cbt}


def _blank(extra):
    base = {"status": "offline", "gpus": [], "cpu_pct": None, "core_pct": None,
            "ncpu": None, "load": None, "mem_total_kb": None, "mem_avail_kb": None,
            "uptime_s": None, "gpukind": "none", "gpu_label": None,
            "detail": None, "err": None}
    base.update(extra)
    return base


def _ingest(d, r):
    """Merge one collected node's fresh metrics into LATEST (replacing its entry)."""
    name = d["name"]
    node = _blank({**d, "gpu_label": GPU_LABEL.get(name)})
    node.update({k: v for k, v in r.items() if k not in ("cpu_bt", "core_bt")})
    with LOCK:
        bt = r.get("cpu_bt")
        if bt:
            _apply_cpu_delta(node, name, bt, r.get("core_bt"))
        LATEST[name] = node


LAST_SEEN = {}                  # name -> last time it appeared in a successful discovery
PRUNE_TTL = 30.0                # a node must be absent this long before it's removed


def refresh_source(source, items, ok, collect_fn):
    """Update only `source`'s tiles in LATEST, then collect them in parallel.

    NEVER clears LATEST, only prunes when discovery SUCCEEDED, and even then only after a
    node has been ABSENT for PRUNE_TTL — so a slow/failed/transiently-empty (or paginated)
    `tailscale`/`vastai` call can't blink tiles out of view. Each source runs on its own
    poller, so a slow Vast scan can't stall (or drop) the fast fleet refresh.
    """
    now = time.time()
    if ok:
        names = {d["name"] for d in items}
        with LOCK:
            for d in items:
                LAST_SEEN[d["name"]] = now          # refresh liveness for everything seen
            for n in [k for k, v in list(LATEST.items())
                      if v.get("source") == source and k not in names
                      and now - LAST_SEEN.get(k, 0) > PRUNE_TTL]:
                del LATEST[n]                       # gone from N consecutive scans -> remove
                LAST_SEEN.pop(n, None)
            for d in items:
                name = d["name"]
                if name not in LATEST:              # brand new -> scanning/offline placeholder
                    LATEST[name] = _blank({**d, "gpu_label": GPU_LABEL.get(name),
                                           "status": "scanning" if d["online"] else "offline"})
                elif not d["online"]:               # known node went offline
                    LATEST[name] = _blank({**d, "gpu_label": GPU_LABEL.get(name)})
                # known & online -> keep current data; _ingest will refresh it
            if not POLL_INFO.get("ts"):
                POLL_INFO["ts"] = time.time()
    online = [d for d in items if d.get("online")]
    if not online:
        return
    try:
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = {ex.submit(collect_fn, d): d for d in online}
            for fut in as_completed(futs):
                try:
                    _ingest(futs[fut], fut.result())
                except Exception as e:
                    _log_err(f"collect {futs[fut].get('name')}", e)
    except RuntimeError:
        pass        # interpreter shutting down (daemon poller mid-cycle) -> exit quietly


def _fleet_items(filter_names):
    res = discover()
    items = res or []
    if filter_names:
        items = [d for d in items if d["name"] in filter_names]
    return items, res is not None


def poll_once(filter_names=None, want_fleet=True, want_vast=True, allow_attach=True):
    """One synchronous sweep of both sources (used by --once and the plain fallback)."""
    t0 = time.time()
    if want_fleet:
        items, ok = _fleet_items(filter_names)
        refresh_source("fleet", items, ok, collect)
    if want_vast:
        res = vast_discover()
        refresh_source("vast", res or [], res is not None,
                       lambda p: collect_vast(p, allow_attach))
    with LOCK:
        POLL_INFO["ts"] = time.time()
        POLL_INFO["dur"] = time.time() - t0


def fleet_poller(stop, interval, filter_names):
    while not stop.is_set():
        try:
            items, ok = _fleet_items(filter_names)
            refresh_source("fleet", items, ok, collect)
            with LOCK:
                POLL_INFO["ts"] = time.time()
        except Exception as e:
            if not stop.is_set():               # don't log teardown noise on quit
                _log_err("fleet_poll", e)
        if stop.wait(interval):                  # True == stop was set -> exit promptly
            break


def vast_poller(stop, interval, allow_attach):
    while not stop.is_set():
        try:
            res = vast_discover()
            refresh_source("vast", res or [], res is not None,
                           lambda p: collect_vast(p, allow_attach))
        except Exception as e:
            if not stop.is_set():
                _log_err("vast_poll", e)
        if stop.wait(interval):
            break


# ── Rendering helpers ───────────────────────────────────────────────────────
TILE_W = 28                 # total tile width incl. both border columns
CW = TILE_W - 2             # inner content width

# color keys -> resolved to curses pairs in ui(); plain mode ignores them.
# Phosphor-green base; load/temp use a green→purple gradient (g0=green … g{N-1}=purple).
TEXT, LABEL, DIM, OK, WARN, CRIT, FRAME = "text", "label", "dim", "ok", "warn", "crit", "frame"

# ── Color schemes (ported from dotmax src/color/schemes.rs) ─────────────────
# Each scheme: a 6-stop body ramp for 0..CRIT_AT load plus a 2-stop "redline" for
# the top (>= CRIT_AT), and the UI accents (text/label/frame/status). Color keys
# stay stable across schemes — switching just re-inits the curses pairs, so the
# whole panel (bars, dials, braille tower) recolors live. 'c' cycles schemes.
CRIT_AT = 90.0
RAMP_KEYS = [f"g{i}" for i in range(6)]
CRIT_KEYS = ["c0", "c1"]

SCHEMES = [
    # name      ramp (6 × 256-color, low→high)   crit (2)    text lbl  frame ok  warn critc  8-color base/crit
    {"name": "phosphor", "ramp": [22, 28, 34, 40, 46, 48], "crit": [129, 165],
     "text": 40, "label": 46, "frame": 28, "ok": 46, "warn": 129, "critc": 165,
     "base8": curses.COLOR_GREEN, "crit8": curses.COLOR_MAGENTA},
    {"name": "ember", "ramp": [52, 88, 124, 160, 202, 208], "crit": [220, 231],
     "text": 180, "label": 214, "frame": 95, "ok": 208, "warn": 220, "critc": 196,
     "base8": curses.COLOR_RED, "crit8": curses.COLOR_YELLOW},
    {"name": "cyber", "ramp": [51, 45, 39, 99, 135, 141], "crit": [201, 207],
     "text": 51, "label": 87, "frame": 31, "ok": 51, "warn": 135, "critc": 201,
     "base8": curses.COLOR_CYAN, "crit8": curses.COLOR_MAGENTA},
    {"name": "ice", "ramp": [24, 25, 31, 38, 45, 87], "crit": [159, 195],
     "text": 45, "label": 123, "frame": 24, "ok": 87, "warn": 159, "critc": 203,
     "base8": curses.COLOR_CYAN, "crit8": curses.COLOR_WHITE},
    {"name": "rainbow", "ramp": [27, 39, 49, 46, 184, 208], "crit": [196, 201],
     "text": 250, "label": 255, "frame": 240, "ok": 46, "warn": 208, "critc": 196,
     "base8": curses.COLOR_WHITE, "crit8": curses.COLOR_RED},
    {"name": "amber", "ramp": [94, 130, 172, 178, 214, 220], "crit": [226, 230],
     "text": 178, "label": 214, "frame": 94, "ok": 214, "warn": 226, "critc": 196,
     "base8": curses.COLOR_YELLOW, "crit8": curses.COLOR_RED},
    {"name": "vapor", "ramp": [54, 92, 129, 162, 198, 212], "crit": [219, 225],
     "text": 176, "label": 213, "frame": 60, "ok": 213, "warn": 219, "critc": 198,
     "base8": curses.COLOR_MAGENTA, "crit8": curses.COLOR_WHITE},
    {"name": "mono", "ramp": [238, 242, 246, 250, 254, 231], "crit": [231, 231],
     "text": 250, "label": 231, "frame": 242, "ok": 252, "warn": 250, "critc": 196,
     "base8": curses.COLOR_WHITE, "crit8": curses.COLOR_RED},
]
SCHEME_IDX = 0


def grad_key(pct):
    """Map 0..100 to a color key: the scheme's body ramp below CRIT_AT, its
    redline pair at/above it (phosphor default: greens then purple)."""
    if pct is None:
        return DIM
    pct = max(0.0, min(100.0, pct))
    if pct >= CRIT_AT:
        f = (pct - CRIT_AT) / max(1e-9, 100.0 - CRIT_AT)
        return CRIT_KEYS[int(round(f * (len(CRIT_KEYS) - 1)))]
    return RAMP_KEYS[int(round(pct / CRIT_AT * (len(RAMP_KEYS) - 1)))]


def cycle_scheme(delta):
    global SCHEME_IDX
    SCHEME_IDX = (SCHEME_IDX + delta) % len(SCHEMES)
    save_prefs()


def set_scheme(name):
    global SCHEME_IDX
    for i, s in enumerate(SCHEMES):
        if s["name"] == (name or "").lower():
            SCHEME_IDX = i
            return True
    return False


def pct_key(pct):
    return grad_key(pct)


def temp_key(t):
    if t is None:
        return DIM
    return grad_key((t - 30.0) / 60.0 * 100.0)  # ~30°C green … ~90°C purple


# ── Loading-bar themes (ported from dotmax progress styles; +/- cycles them) ──
# Each theme: (fill_frac 0..1, width W, time t) -> list of W (char, color_key). Filled
# cells take the green→purple position gradient; animation is expressed via glyph density
# so it composes with the color system. Recipes derived from dotmax src/progress/styles/*.
ANIM_T = 0.0          # animation clock (seconds); the draw loop sets it each frame
THEME_IDX = 0
LIVE_TOWER = False     # set True while the TOTALS tower is on screen: its beat backdrop
                       # animates continuously, so the draw loop must run the fast cadence
_EIGHTHS = " ▏▎▍▌▋▊▉█"
_SHADE = " ░▒▓█"
_VBLK = " ▁▂▃▄▅▆▇█"


def _gp(i, W):
    return grad_key((i + 1) / W * 100.0)


def _t_gradient(f, W, t):                              # classic solid gradient fill
    n = int(f * W)
    return [("█", _gp(i, W)) if i < n else ("░", DIM) for i in range(W)]


def _t_smooth(f, W, t):                                # blocks: eighth-precise smooth edge
    e = f * W * 8.0
    full, rem = int(e // 8), int(e % 8)
    out = []
    for i in range(W):
        if i < full:
            out.append(("█", _gp(i, W)))
        elif i == full and rem:
            out.append((_EIGHTHS[rem], _gp(i, W)))
        else:
            out.append(("░", DIM))
    return out


def _t_shaded(f, W, t):                                # atari/dither: ░▒▓ anti-aliased edge
    pos = f * W
    out = []
    for i in range(W):
        d = pos - i
        if d >= 1:
            out.append(("█", _gp(i, W)))
        elif d > 0:
            out.append((_SHADE[1 + min(3, int(d * 4))], _gp(i, W)))
        else:
            out.append(("░", DIM))
    return out


def _t_scanline(f, W, t):                              # retro CRT: bright spot scans the fill
    n = int(f * W)
    spot = ((t * 0.8) % 1.0) * max(1, n)
    return [("█" if i < n and abs(i - spot) < 1.2 else "▓" if i < n else "░",
             _gp(i, W) if i < n else DIM) for i in range(W)]


def _t_signal(f, W, t):                                # tech: 8 pulsing EQ bars
    lit = int(round(f * 8))
    out = []
    for i in range(W):
        b = min(i * 8 // max(1, W), 7)
        if b < lit:
            h = min(1.0, (b + 1) / 8.0 * (1.0 + 0.18 * math.sin(2 * math.pi * t * 0.5 + b * 0.6)))
            out.append((_VBLK[min(8, int(h * 8))], _gp(i, W)))
        else:
            out.append((" ", DIM))
    return out


def _t_plasma(f, W, t):                                # lasers: jittery plasma fill
    n, ep = int(f * W), int(t * 15)
    out = []
    for i in range(W):
        if i < n:
            h = ((i * 7 + ep * 31) * 2654435761) & 0xFFFFFFFF
            out.append(("█" if (h % 1000) / 1000.0 > 0.5 else "▓", _gp(i, W)))
        else:
            out.append(("░", DIM))
    return out


def _t_spectrum(f, W, t):                              # waves: per-cell FFT amplitude
    out = []
    for i in range(W):
        fq = i + 1.0
        spec = (1.0 / math.sqrt(fq)) * (1 + 0.4 * math.sin(0.7 * fq)) * (1 + 0.3 * abs(math.sin(1.1 * fq + 2.5 * t)))
        filled = i < f * W
        lv = max(0, min(4, int((spec if filled else spec * 0.12) * 4)))
        out.append((_SHADE[lv], _gp(i, W) if filled and lv else DIM))
    return out


def _t_wave(f, W, t):                                  # sinewave: scrolling ripple in eighths
    n, k, ph = int(f * W), 2 * math.pi * 4 / max(1, W), t * 2 * math.pi * 0.6
    out = []
    for i in range(W):
        lv = max(0, min(8, int(round(abs(math.sin(k * i + ph)) * 8))))
        out.append((_VBLK[lv], _gp(i, W)) if i < n else (("░" if lv < 3 else _VBLK[lv]), DIM))
    return out


def _t_segmented(f, W, t):                             # classic LED meter: lit segments + gaps
    segs = max(6, W // 3)
    per = max(1, W // segs)
    lit = int(round(f * segs))
    out = []
    for i in range(W):
        if per > 1 and i % per == per - 1:
            out.append((" ", DIM))
        elif i // per < lit:
            out.append(("█", _gp(i, W)))
        else:
            out.append(("░", DIM))
    return out


def _t_pulse(f, W, t):                                 # neon: whole fill breathes ▓↔█
    n = int(f * W)
    ch = "█" if (0.5 + 0.5 * math.sin(t * 3.0)) > 0.5 else "▓"
    return [(ch, _gp(i, W)) if i < n else ("░", DIM) for i in range(W)]


def _t_double(f, W, t):                                # blocks: fill both ends → center
    e = f * W * 4.0                                    # eighths per side
    full, rem = int(e // 8), int(e % 8)
    out = []
    for i in range(W):
        m = min(i, W - 1 - i)                          # distance from nearest end
        if m < full:
            out.append(("█", _gp(i, W)))
        elif m == full and rem:
            ch = _EIGHTHS[rem] if i <= W - 1 - i else _SHADE[1 + min(2, rem // 3)]
            out.append((ch, _gp(i, W)))
        else:
            out.append(("░", DIM))
    return out


def _t_brick(f, W, t):                                 # blocks: masonry with mortar lines
    e = f * W
    out = []
    for i in range(W):
        d = e - i
        if d >= 1:
            out.append(("▉" if i % 3 == 2 else "█", _gp(i, W)))
        elif d > 0:
            out.append((_EIGHTHS[max(1, int(d * 8))], _gp(i, W)))
        else:
            out.append(("░", DIM))
    return out


def _t_rocket(f, W, t):                                # classic: tapered tail, bright nose
    n = int(f * W)
    out = []
    for i in range(W):
        if i >= n:
            out.append(("░", DIM))
        elif i >= n - 1:
            out.append(("█", _gp(i, W)))
        elif n > 4 and i < 3:
            out.append(("▂▄▆"[i], _gp(i, W)))
        else:
            out.append(("▆", _gp(i, W)))
    return out


_DITHER_ORDER = [0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15]  # bayer 1-D


def _t_dither(f, W, t):                                # blocks: ordered-dither reveal
    th = f * 16.0
    out = []
    for i in range(W):
        o = _DITHER_ORDER[i % 16]
        if o + 1 <= th:
            out.append(("█", _gp(i, W)))
        elif o < th:
            out.append(("▓", _gp(i, W)))
        else:
            out.append(("░", DIM))
    return out


def _t_typer(f, W, t):                                 # tech: terminal typer + blink cursor
    n = int(f * W)
    blink = (t * 2.0) % 1.0 < 0.5
    out = []
    for i in range(W):
        if i < n:
            out.append(("█", _gp(i, W)))
        elif i == n:
            out.append(("▌" if blink else " ", _gp(i, W)))
        else:
            out.append(("·", DIM))
    return out


def _t_comet(f, W, t):                                 # border runner: comet laps the fill
    n = int(f * W)
    if n <= 0:
        return [("·", DIM)] * W
    head = ((t * 0.6) % 1.0) * n
    out = []
    for i in range(W):
        if i >= n:
            out.append(("·", DIM))
        else:
            d = (head - i) % n                         # distance behind the head
            ch = "█" if d < 1 else "▓" if d < 2 else "▒" if d < 3.5 else "░"
            out.append((ch, _gp(i, W)))
    return out


def _t_heartbeat(f, W, t):                             # tech EKG: pulse rides the fill
    n = int(f * W)
    pos = ((t * 0.9) % 1.0) * max(1, n)
    out = []
    for i in range(W):
        if i >= n:
            out.append(("░", DIM))
            continue
        lv = 2                                         # flat baseline
        d = i - int(pos)
        if 0 <= d < 4:
            lv = (3, 8, 1, 6)[d]                       # the QRS spike
        out.append((_VBLK[lv], _gp(i, W)))
    return out


def _t_waterfall(f, W, t):                             # blocks: shades flow through fill
    n = int(f * W)
    out = []
    for i in range(W):
        wv = math.sin(i * 0.5 - t * 3.0) * 0.5 + 0.5
        if i < n:
            out.append((_SHADE[1 + min(3, int(wv * 3.5))], _gp(i, W)))
        else:
            out.append((_SHADE[1] if wv < 0.4 else " ", DIM))
    return out


# ── Tech themes (ported from dotmax src/progress/styles/tech.rs) ─────────────
def _h32(n):                                           # deterministic 32-bit hash
    return (n * 2654435761) & 0xFFFFFFFF


def _t_matrix(f, W, t):                                # tech: digital-rain code stream
    n = int(f * W)
    ep = int(t * 6)
    out = []
    for i in range(W):
        if i >= n:
            out.append(("·", DIM))
            continue
        hv = _h32(i * 13 + ep)
        head = (_h32(i * 7 + int(t * 3)) % 17) == 0    # occasional bright glyph
        out.append(("█" if head else "▓▒░▒"[hv % 4], _gp(i, W)))
    return out


def _t_packets(f, W, t):                               # tech: packets stream down a line
    n = int(f * W)
    out = [("─", _gp(i, W)) if i < n else ("·", DIM) for i in range(W)]
    if n > 0:
        np_ = max(1, n // 6)
        for k in range(np_):
            pos = int(((t * 0.7 + k / np_) % 1.0) * n)
            for dx in (0, 1):
                if pos + dx < n:
                    out[pos + dx] = ("█", _gp(pos + dx, W))
    return out


def _t_glitch(f, W, t):                                # tech: fill with glitch dropouts
    n = int(f * W)
    ep = int(t * 3)
    out = []
    for i in range(W):
        if i >= n:
            out.append(("░", DIM))
            continue
        r = _h32(i + ep * 97) % 23
        ch = "▒" if r == 0 else "▚" if r == 1 else " " if r == 2 else "█"
        out.append((ch, _gp(i, W)))
    return out


def _t_hex(f, W, t):                                   # tech: hex dump fills in place
    e = f * W
    blink = (t * 4.0) % 1.0 < 0.5
    out = []
    for i in range(W):
        ch = "0123456789ABCDEF"[_h32(i) % 16]
        if i + 1 <= e or (i < e and blink):            # in-progress digit flickers
            out.append((ch, _gp(i, W)))
        else:
            out.append(("·", DIM))
    return out


def _t_circuit(f, W, t):                               # tech: trace + junctions + pulse
    n = int(f * W)
    pulse = int(((t * 0.9) % 1.0) * max(1, n))
    out = []
    for i in range(W):
        if i >= n:
            out.append(("·", DIM))
        elif i == pulse:
            out.append(("█", _gp(i, W)))               # the travelling current pulse
        elif i % 5 == 2:
            out.append(("╪", _gp(i, W)))               # junction node
        else:
            out.append(("═", _gp(i, W)))
    return out


def _t_hazard(f, W, t):                                # eva: scrolling warning stripes
    n = int(f * W)
    off = int(t * 4)
    return [((("█", "▞", "▞")[(i + off) % 3], _gp(i, W)) if i < n else ("░", DIM))
            for i in range(W)]


BAR_THEMES = [
    ("gradient", _t_gradient), ("smooth", _t_smooth), ("shaded", _t_shaded),
    ("scanline", _t_scanline), ("signal", _t_signal), ("plasma", _t_plasma),
    ("spectrum", _t_spectrum), ("wave", _t_wave), ("segmented", _t_segmented),
    ("pulse", _t_pulse), ("double", _t_double), ("brick", _t_brick),
    ("rocket", _t_rocket), ("dither", _t_dither), ("typer", _t_typer),
    ("comet", _t_comet), ("heartbeat", _t_heartbeat), ("waterfall", _t_waterfall),
    ("matrix", _t_matrix), ("packets", _t_packets), ("glitch", _t_glitch),
    ("hex", _t_hex), ("circuit", _t_circuit), ("hazard", _t_hazard),
]
# Animated themes need a fast redraw; static ones stay calm (no needless full-screen
# repaints flooding the terminal — important over WSL / remote terminals).
_ANIMATED = {"scanline", "signal", "plasma", "spectrum", "wave", "pulse",
             "typer", "comet", "heartbeat", "waterfall",
             "matrix", "packets", "glitch", "hex", "circuit", "hazard"}


def theme_is_animated():
    return BAR_THEMES[THEME_IDX][0] in _ANIMATED


def cycle_theme(delta):
    global THEME_IDX
    THEME_IDX = (THEME_IDX + delta) % len(BAR_THEMES)
    save_prefs()


def set_theme(name):
    global THEME_IDX
    for i, (n, _) in enumerate(BAR_THEMES):
        if n == (name or "").lower():
            THEME_IDX = i
            return True
    return False


DUAL_GAUGE = True           # 'g' toggles per-connection dual GPU/CPU braille gauge


def toggle_dual():
    global DUAL_GAUGE
    DUAL_GAUGE = not DUAL_GAUGE
    save_prefs()


# ── Look preferences (persisted so the panel reopens how you left it) ───────
PREFS_PATH = os.path.join(CONFIG_DIR, "prefs.json")


def load_prefs():
    try:
        with open(PREFS_PATH) as f:
            p = json.load(f)
    except Exception:
        return
    global DUAL_GAUGE
    set_theme(p.get("theme"))
    set_scheme(p.get("scheme"))
    set_gauge(p.get("gauge"))
    set_field(p.get("field"))
    if isinstance(p.get("dual"), bool):
        DUAL_GAUGE = p["dual"]


def save_prefs():
    """Best-effort: never let a read-only $HOME break the panel."""
    try:
        os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
        with open(PREFS_PATH, "w") as f:
            json.dump({"theme": BAR_THEMES[THEME_IDX][0],
                       "scheme": SCHEMES[SCHEME_IDX]["name"],
                       "gauge": GAUGE_STYLES[GAUGE_IDX][0],
                       "field": FIELD_STYLES[FIELD_IDX][0],
                       "dual": DUAL_GAUGE}, f)
    except Exception:
        pass


def _set_anim(t):
    global ANIM_T
    ANIM_T = t


def bar_runs(used, total, width):
    """Render a horizontal bar with the currently-selected dotmax-derived theme."""
    width = max(1, width)
    f = 0.0 if (not total or used is None) else max(0.0, min(1.0, used / total))
    runs = BAR_THEMES[THEME_IDX][1](f, width, ANIM_T)
    return runs[:width] if len(runs) >= width else runs + [(" ", DIM)] * (width - len(runs))


def fmt_bytes_gib(kb):
    return None if kb is None else kb / 1024.0 / 1024.0


def fmt_uptime(s):
    if s is None:
        return "—"
    s = int(s)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d}d{h}h" if h else f"{d}d"
    if h:
        return f"{h}h{m}m" if m else f"{h}h"
    return f"{m}m"


def _pad(runs):
    """Pad/truncate a list of (text,key) runs to exactly CW display columns."""
    used = sum(len(t) for t, _ in runs)
    if used < CW:
        runs = runs + [(" " * (CW - used), TEXT)]
    elif used > CW:  # truncate from the end
        over = used - CW
        out = []
        for t, k in reversed(runs):
            if over <= 0:
                out.append((t, k))
            elif len(t) <= over:
                over -= len(t)
            else:
                out.append((t[:len(t) - over], k))
                over = 0
        runs = list(reversed(out))
    return runs


def metric_row(label, used, total, suffix, key=None):
    """' LABEL <gradient-bar>  suffix' padded to CW. The bar self-colors green→purple
    and the suffix value picks up the bar's fill color. `key` is accepted but unused."""
    head = f" {label} "
    suf = f" {suffix}"
    bw = max(3, CW - len(head) - len(suf))
    fill = 0.0 if (not total or used is None) else max(0.0, min(100.0, 100.0 * used / total))
    skey = grad_key(fill) if (used is not None and total) else DIM
    return _pad([(head, LABEL)] + bar_runs(used, total, bw) + [(suf, skey)])


CORE_BARS = "▁▂▃▄▅▆▇█"  # eighth-blocks: glyph height + color encode per-core load


def core_strip(pcts):
    """Per-core heat strip: one colored block-glyph per core, wrapped to tile width.
    Returns a list of padded rows ([] if no per-core data — e.g. warming or vast)."""
    if not pcts:
        return []
    glyphs = []
    for p in pcts:
        if p is None:
            glyphs.append((" ", DIM))
            continue
        idx = min(len(CORE_BARS) - 1, max(0, int(p / 100.0 * (len(CORE_BARS) - 1) + 0.5)))
        glyphs.append((CORE_BARS[idx], DIM if p < 5 else pct_key(p)))
    width = CW - 1  # one leading space of indent
    return [_pad([(" ", TEXT)] + glyphs[i:i + width]) for i in range(0, len(glyphs), width)]


# ── Braille car-speedometer (ported from dotmax src/progress/styles/meter.rs) ──
# 2×4 dots per braille cell; dot(col,row) -> bit in the U+2800 block.
_BRAILLE_BIT = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
                (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80}
GAUGE_H = 9  # tile rows the gauge occupies


class _Braille:
    """Tiny braille canvas: set dots in 2×4-per-cell dot space, emit colored cells."""
    def __init__(self, cells_w, cells_h):
        self.cw, self.ch = cells_w, cells_h
        self.dw, self.dh = cells_w * 2, cells_h * 4
        self.bits = [0] * (cells_w * cells_h)
        self.col = [None] * (cells_w * cells_h)

    def set(self, x, y, color=None):
        x, y = int(round(x)), int(round(y))
        if 0 <= x < self.dw and 0 <= y < self.dh:
            idx = (y // 4) * self.cw + (x // 2)
            self.bits[idx] |= _BRAILLE_BIT[(x % 2, y % 4)]
            if color:
                self.col[idx] = color

    def line(self, x0, y0, x1, y1, color=None):  # Bresenham (meter.rs:57)
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            self.set(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def arc(self, cx, cy, r, a0, a1, color=None):  # polar sampling (meter.rs:87)
        steps = max(8, int(r * abs(a1 - a0)) + 2)
        for i in range(steps + 1):
            a = a0 + (a1 - a0) * i / steps
            self.set(cx + r * math.cos(a), cy - r * math.sin(a), color)

    def rows(self):
        out = []
        for cy in range(self.ch):
            out.append([(chr(0x2800 + self.bits[cy * self.cw + cx]),
                         self.col[cy * self.cw + cx] or FRAME) for cx in range(self.cw)])
        return out


# ── Block canvas — the non-braille sibling of _Braille ──────────────────────
# 2×2 subpixels per cell rendered as quadrant block glyphs, so shapes come out
# as SOLID chunky slabs instead of dot lattices. Half the resolution of braille,
# twice the presence — the right material for the Eva-style angular gauges.
_QUAD = " ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█"   # index = TL·1 + TR·2 + BL·4 + BR·8


class _Blocks:
    """Quadrant-block canvas with the same set/line/rows interface as _Braille,
    so the polygon helpers and gauge code work on either material."""
    def __init__(self, cells_w, cells_h):
        self.cw, self.ch = cells_w, cells_h
        self.dw, self.dh = cells_w * 2, cells_h * 2
        self.bits = [0] * (cells_w * cells_h)
        self.col = [None] * (cells_w * cells_h)

    def set(self, x, y, color=None):
        x, y = int(round(x)), int(round(y))
        if 0 <= x < self.dw and 0 <= y < self.dh:
            idx = (y // 2) * self.cw + (x // 2)
            self.bits[idx] |= 1 << ((y % 2) * 2 + (x % 2))
            if color:
                self.col[idx] = color

    line = _Braille.line                               # Bresenham via self.set
    arc = _Braille.arc

    def rows(self):
        out = []
        for cy in range(self.ch):
            out.append([(_QUAD[self.bits[cy * self.cw + cx]],
                         self.col[cy * self.cw + cx] or FRAME) for cx in range(self.cw)])
        return out


def speedometer_rows(pct, cells_w, cells_h):
    """270° braille speedometer: arc + 10% ticks + purple redline (>=90%) + needle@pct."""
    b = _Braille(cells_w, cells_h)
    cx = b.dw // 2
    r = min(cx - 1, int((b.dh - 2) / 1.71))
    cy = r + 1
    a0, span = math.radians(225.0), math.radians(-270.0)  # 270° sweep, clockwise
    red = a0 + 0.9 * span                                  # 90% mark
    b.arc(cx, cy, r, a0, red, FRAME)                        # 0..90% body arc
    b.arc(cx, cy, r, red, a0 + span, CRIT_KEYS[-1])         # 90..100% redline
    for i in range(11):                                     # tick marks every 10%
        a = a0 + (i / 10.0) * span
        tl = (r // 3) if i % 5 == 0 else (r // 6)
        col = CRIT_KEYS[-1] if i >= 9 else FRAME
        b.line(cx + (r - tl) * math.cos(a), cy - (r - tl) * math.sin(a),
               cx + r * math.cos(a), cy - r * math.sin(a), col)
    if pct is not None:                                     # needle + hub
        a = a0 + max(0.0, min(1.0, pct / 100.0)) * span
        nk = grad_key(pct)
        b.line(cx, cy, cx + r * 0.88 * math.cos(a), cy - r * 0.88 * math.sin(a), nk)
        b.set(cx, cy, nk)
        b.set(cx + 1, cy, nk)
    return b.rows()


def build_meter_tile(node):
    """The macro 'TOTAL COMPUTE' tile: a braille speedometer of fleet-wide CPU load."""
    pct = node.get("pct")
    dkey = grad_key(pct) if pct is not None else DIM
    name = node.get("name", "compute")
    head, tail = f"┌─ {name} ", "● ┐"
    fill = max(1, TILE_W - len(head) - len(tail))
    top = [(head, FRAME), ("─" * fill, FRAME), ("●", dkey), (" ┐", FRAME)]
    bottom = "└" + "─" * CW + "┘"
    rows = [_pad(r) for r in speedometer_rows(pct, CW, GAUGE_H)]
    val = f"{pct:.0f}% {node.get('readout', 'compute')}" if pct is not None else "·· warming"
    rows.append(_pad([(" " * max(0, (CW - len(val)) // 2), TEXT), (val, dkey)]))
    return top, rows, bottom


# ── Dual concentric gauge (ported from dotmax meter.rs::DualGauge) ──────────
# Two full-circle braille rings sweeping clockwise from 12 o'clock: the OUTER
# ring tracks GPU, the INNER ring tracks CPU. 100% == a full revolution. The
# CPU% reads as the centre text; the GPU% reads on the line below the dial.
DUAL_H = 8          # full dial height (rows); kept ~square so the circle isn't oval
DUAL_H_COMPACT = 5  # shrunk dial when the layout is tight (drops legend too), so the
                    # auto "too tall → compact" pass can keep every node on screen


def node_gpu_util(node):
    """Aggregate GPU utilization for a connection: mean across its GPUs, or None."""
    us = [g["util"] for g in (node.get("gpus") or []) if g.get("util") is not None]
    return (sum(us) / len(us)) if us else None


def _frac(pct):
    return max(0.0, min(1.0, (pct or 0.0) / 100.0))


def _g_rings(gpu_pct, cpu_pct, cells_w, cells_h):
    """Concentric rings (meter.rs::DualGauge): outer = GPU, inner = CPU. Each ring =
    a dim dotted track + a bright fill arc proportional to its value (thick_arc)."""
    b = _Braille(cells_w, cells_h)
    cx, cy = b.dw // 2, b.dh // 2
    r_outer = max(3, min(cx - 1, cy - 1))
    r_inner = max(2, int(round(r_outer * 0.55)))
    a_top = math.pi / 2.0                              # 12 o'clock, sweep clockwise

    def ring(r, pct, fill_key):
        steps = max(8, int(r * 2 * math.pi))           # dim dotted track (every 3rd dot)
        for i in range(0, steps + 1, 3):
            a = a_top - (i / steps) * 2 * math.pi
            b.set(cx + r * math.cos(a), cy - r * math.sin(a), DIM)
        if pct is not None and pct > 0.1:              # bright fill: a..a-sweep, two radii
            sweep = _frac(pct) * 2 * math.pi
            b.arc(cx, cy, r, a_top, a_top - sweep, fill_key)
            if r - 1 >= 1:
                b.arc(cx, cy, r - 1, a_top, a_top - sweep, fill_key)

    ring(r_outer, gpu_pct, grad_key(gpu_pct))          # outer = GPU
    ring(r_inner, cpu_pct, grad_key(cpu_pct))          # inner = CPU
    return b.rows()


def _g_speedo(gpu_pct, cpu_pct, cells_w, cells_h):
    """meter.rs::Speedometer, dual-needle: 270° arc, ticks, redline; GPU = long
    needle, CPU = short needle."""
    b = _Braille(cells_w, cells_h)
    cx = b.dw // 2
    r = max(4, min(cx - 1, int((b.dh - 2) / 1.71)))
    cy = r + 1
    a0, span = math.radians(225.0), math.radians(-270.0)
    red = a0 + 0.9 * span
    b.arc(cx, cy, r, a0, red, FRAME)
    b.arc(cx, cy, r, red, a0 + span, CRIT_KEYS[-1])
    for i in range(11):
        a = a0 + (i / 10.0) * span
        tl = (r // 3) if i % 5 == 0 else (r // 6)
        col = CRIT_KEYS[-1] if i >= 9 else FRAME
        b.line(cx + (r - tl) * math.cos(a), cy - (r - tl) * math.sin(a),
               cx + r * math.cos(a), cy - r * math.sin(a), col)

    def needle(pct, ln):
        if pct is None:
            return
        a = a0 + _frac(pct) * span
        b.line(cx, cy, cx + r * ln * math.cos(a), cy - r * ln * math.sin(a),
               grad_key(pct))

    needle(cpu_pct, 0.55)
    needle(gpu_pct, 0.92)                              # GPU last so it wins overlaps
    b.set(cx, cy, FRAME)
    b.set(cx + 1, cy, FRAME)
    return b.rows()


def _g_half(gpu_pct, cpu_pct, cells_w, cells_h):
    """meter.rs::HalfGauge: 180° fuel-style semicircle; GPU sweeps the outer arc,
    CPU the inner, both left→right over a dotted track."""
    b = _Braille(cells_w, cells_h)
    cx = b.dw // 2
    cy = b.dh - 2
    r = max(4, min(cx - 1, cy - 1))
    ri = max(2, int(r * 0.62))
    b.line(cx - r, cy + 1, cx + r, cy + 1, FRAME)      # baseline
    for q in range(5):                                  # ticks at 0/25/50/75/100
        a = math.pi - (q / 4.0) * math.pi
        b.line(cx + r * math.cos(a), cy - r * math.sin(a),
               cx + (r + 2) * math.cos(a), cy - (r + 2) * math.sin(a), FRAME)
    for rr in (r, ri):                                  # dotted tracks
        steps = max(8, int(rr * math.pi))
        for i in range(0, steps + 1, 3):
            a = math.pi - (i / steps) * math.pi
            b.set(cx + rr * math.cos(a), cy - rr * math.sin(a), DIM)

    def sweep(rr, pct):
        if pct is None or pct <= 0.1:
            return
        k = grad_key(pct)
        b.arc(cx, cy, rr, math.pi, math.pi * (1.0 - _frac(pct)), k)
        if rr - 1 >= 1:
            b.arc(cx, cy, rr - 1, math.pi, math.pi * (1.0 - _frac(pct)), k)

    sweep(r, gpu_pct)
    sweep(ri, cpu_pct)
    return b.rows()


def _g_vu(gpu_pct, cpu_pct, cells_w, cells_h):
    """meter.rs::VuNeedle: 120° studio VU meter with a red zone past 80%; GPU =
    long needle, CPU = short needle."""
    b = _Braille(cells_w, cells_h)
    cx = b.dw // 2
    cy = b.dh - 2
    r = max(4, min(cx - 1, cy - 1))
    a0, a1 = math.radians(150.0), math.radians(30.0)
    red = a0 + 0.8 * (a1 - a0)
    b.arc(cx, cy, r, a0, red, FRAME)
    b.arc(cx, cy, r, red, a1, CRIT_KEYS[-1])
    for i in range(11):
        a = a0 + (i / 10.0) * (a1 - a0)
        tl = (r // 4) if i % 5 == 0 else (r // 7)
        col = CRIT_KEYS[-1] if i >= 8 else FRAME
        b.line(cx + (r - tl) * math.cos(a), cy - (r - tl) * math.sin(a),
               cx + r * math.cos(a), cy - r * math.sin(a), col)

    def needle(pct, ln):
        if pct is None:
            return
        a = a0 + _frac(pct) * (a1 - a0)
        b.line(cx, cy, cx + r * ln * math.cos(a), cy - r * ln * math.sin(a),
               grad_key(pct))

    needle(cpu_pct, 0.6)
    needle(gpu_pct, 0.95)
    b.set(cx, cy, FRAME)
    b.set(cx + 1, cy, FRAME)
    return b.rows()


def _g_donut(gpu_pct, cpu_pct, cells_w, cells_h):
    """meter.rs::Donut: GPU fills the outer band clockwise from 12 o'clock; CPU
    fills a solid pie-wedge core."""
    b = _Braille(cells_w, cells_h)
    cx, cy = b.dw // 2, b.dh // 2
    ro = max(4, min(cx, cy) - 1)
    rib = max(3, int(ro * 0.72))                       # inner edge of the GPU band
    rc = max(2, int(ro * 0.45))                        # CPU core radius
    a_top = math.pi / 2.0
    steps = max(8, int(ro * 2 * math.pi))              # dotted track on the rim
    for i in range(0, steps + 1, 3):
        a = a_top - (i / steps) * 2 * math.pi
        b.set(cx + ro * math.cos(a), cy - ro * math.sin(a), DIM)
    if gpu_pct is not None and gpu_pct > 0.1:
        k = grad_key(gpu_pct)
        for rr in range(rib, ro + 1):
            b.arc(cx, cy, rr, a_top, a_top - _frac(gpu_pct) * 2 * math.pi, k)
    if cpu_pct is not None and cpu_pct > 0.1:
        k = grad_key(cpu_pct)
        for rr in range(1, rc + 1):
            b.arc(cx, cy, rr, a_top, a_top - _frac(cpu_pct) * 2 * math.pi, k)
    return b.rows()


def _g_segments(gpu_pct, cpu_pct, cells_w, cells_h):
    """meter.rs::SegmentedRing: LED segments with gaps, lit clockwise; outer ring =
    GPU, inner = CPU. Lit segments take the position gradient (like the bars)."""
    b = _Braille(cells_w, cells_h)
    cx, cy = b.dw // 2, b.dh // 2
    ro = max(4, min(cx, cy) - 1)
    ri = max(2, int(ro * 0.6))
    n = 16
    seg = 2 * math.pi / n
    a_top = math.pi / 2.0

    def ring(rr, pct):
        lit = 0 if pct is None else int(round(_frac(pct) * n))
        for s in range(n):
            sa = a_top - s * seg
            sb = sa - seg * 0.7                        # 30% gap between segments
            if s < lit:
                k = grad_key((s + 1) / n * 100.0)
                b.arc(cx, cy, rr, sa, sb, k)
                if rr - 1 >= 1:
                    b.arc(cx, cy, rr - 1, sa, sb, k)
            else:
                b.arc(cx, cy, rr, sa, sb, DIM)

    ring(ro, gpu_pct)
    ring(ri, cpu_pct)
    return b.rows()


def _g_signal(gpu_pct, cpu_pct, cells_w, cells_h):
    """meter.rs::SignalArc, mirrored: cell-signal tiers fanning UP for GPU and
    DOWN for CPU, lit tiers ∝ value with the position gradient."""
    b = _Braille(cells_w, cells_h)
    cx, cy = b.dw // 2, b.dh // 2
    rmax = max(4, min(cx - 1, cy - 1))
    tiers = max(3, min(5, rmax // 2))

    def fan(pct, up):
        lit = 0 if pct is None else int(math.ceil(_frac(pct) * tiers))
        lo, hi = (math.radians(45.0), math.radians(135.0)) if up else \
                 (math.radians(225.0), math.radians(315.0))
        for t_ in range(1, tiers + 1):
            rr = 1 + t_ * (rmax - 1) / tiers
            b.arc(cx, cy, rr, lo, hi,
                  grad_key(t_ / tiers * 100.0) if t_ <= lit else DIM)

    fan(gpu_pct, True)
    fan(cpu_pct, False)
    b.set(cx, cy, FRAME)
    b.set(cx + 1, cy, FRAME)
    return b.rows()


def _g_thermo(gpu_pct, cpu_pct, cells_w, cells_h):
    """blocks.rs::Thermometer, twinned: two mercury tubes filling bottom-up — GPU
    left, CPU right — with 25% wall ticks and a height-gradient fill."""
    b = _Braille(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    top, bot = 1, dh - 2

    def tube(x0, pct):
        b.line(x0 - 2, top, x0 - 2, bot, FRAME)
        b.line(x0 + 2, top, x0 + 2, bot, FRAME)
        b.line(x0 - 2, top, x0 + 2, top, FRAME)
        b.line(x0 - 2, bot, x0 + 2, bot, FRAME)
        for q in range(1, 4):                          # 25/50/75% ticks on the walls
            y = bot - (bot - top) * q / 4.0
            b.set(x0 - 3, y, FRAME)
            b.set(x0 + 3, y, FRAME)
        if pct is not None:
            scale = max(1, bot - top - 1)
            fh = int(round(scale * _frac(pct)))
            for yy in range(bot - fh, bot):
                kk = grad_key((bot - yy) / scale * 100.0)
                for xx in (x0 - 1, x0, x0 + 1):
                    b.set(xx, yy, kk)

    tube(dw // 4, gpu_pct)
    tube(dw - 1 - dw // 4, cpu_pct)
    return b.rows()


def _g_pressure(gpu_pct, cpu_pct, cells_w, cells_h):
    """meter.rs::PressureDial: 240° dial with a hatched danger zone past 75%;
    GPU = long needle, CPU = short needle."""
    b = _Braille(cells_w, cells_h)
    cx = b.dw // 2
    r = max(4, min(cx - 1, int((b.dh - 2) / 1.5)))
    cy = r + 1
    a0, span = math.radians(210.0), math.radians(-240.0)
    danger = a0 + 0.75 * span
    b.arc(cx, cy, r, a0, danger, FRAME)
    b.arc(cx, cy, r, danger, a0 + span, CRIT_KEYS[-1])
    for q in range(6):                                 # radial hatching in the danger zone
        a = danger + (q / 5.0) * (a0 + span - danger)
        b.line(cx + (r - max(2, r // 4)) * math.cos(a), cy - (r - max(2, r // 4)) * math.sin(a),
               cx + r * math.cos(a), cy - r * math.sin(a), CRIT_KEYS[0])
    for q in range(5):                                 # ticks at 0/25/50/75/100
        a = a0 + (q / 4.0) * span
        tl = max(2, r // 5)
        b.line(cx + (r - tl) * math.cos(a), cy - (r - tl) * math.sin(a),
               cx + r * math.cos(a), cy - r * math.sin(a),
               CRIT_KEYS[-1] if q >= 3 else FRAME)

    def needle(pct, ln):
        if pct is None:
            return
        a = a0 + _frac(pct) * span
        b.line(cx, cy, cx + r * ln * math.cos(a), cy - r * ln * math.sin(a),
               grad_key(pct))

    needle(cpu_pct, 0.5)
    needle(gpu_pct, 0.9)
    b.set(cx, cy, FRAME)
    b.set(cx + 1, cy, FRAME)
    return b.rows()


def _g_clock(gpu_pct, cpu_pct, cells_w, cells_h):
    """meter.rs::ClockFace: 12 hour ticks; the BIG hand is GPU, the SMALL hand is
    CPU — each value is one full revolution from 12 o'clock."""
    b = _Braille(cells_w, cells_h)
    cx, cy = b.dw // 2, b.dh // 2
    r = max(4, min(cx, cy) - 1)
    b.arc(cx, cy, r, 0.0, 2 * math.pi, FRAME)
    for i in range(12):
        a = math.pi / 2 - i * math.pi / 6.0
        ln = (r // 4) if i % 3 == 0 else max(1, r // 6)
        b.line(cx + (r - ln) * math.cos(a), cy - (r - ln) * math.sin(a),
               cx + r * math.cos(a), cy - r * math.sin(a), FRAME)

    def hand(pct, ln):
        if pct is None:
            return
        a = math.pi / 2 - _frac(pct) * 2 * math.pi
        b.line(cx, cy, cx + r * ln * math.cos(a), cy - r * ln * math.sin(a),
               grad_key(pct))

    hand(cpu_pct, 0.5)                                 # small hand = CPU
    hand(gpu_pct, 0.85)                                # big hand = GPU
    b.set(cx, cy, FRAME)
    b.set(cx + 1, cy, FRAME)
    return b.rows()


# ── Square gauges (ported from dotmax src/progress/styles/border.rs) ─────────
def _rect_perim(x0, y0, x1, y1):
    """Clockwise perimeter dot path from the top-left corner."""
    pts = []
    for x in range(x0, x1):
        pts.append((x, y0))
    for y in range(y0, y1):
        pts.append((x1, y))
    for x in range(x1, x0, -1):
        pts.append((x, y1))
    for y in range(y1, y0, -1):
        pts.append((x0, y))
    return pts


def _g_frame(gpu_pct, cpu_pct, cells_w, cells_h):
    """border.rs::DrawOn, dual: the outer square frame draws clockwise from the
    top-left ∝ GPU, an inset frame ∝ CPU; dotted tracks mark the remainder."""
    b = _Braille(cells_w, cells_h)

    def frame(x0, y0, x1, y1, pct):
        if x1 - x0 < 2 or y1 - y0 < 2:
            return
        pts = _rect_perim(x0, y0, x1, y1)
        lit = 0 if pct is None else int(round(_frac(pct) * len(pts)))
        for i, (x, y) in enumerate(pts):
            if i < lit:
                b.set(x, y, grad_key((i + 1) / len(pts) * 100.0))
            elif i % 3 == 0:
                b.set(x, y, DIM)

    frame(1, 1, b.dw - 2, b.dh - 2, gpu_pct)
    frame(5, 4, b.dw - 6, b.dh - 5, cpu_pct)
    return b.rows()


def _g_inset(gpu_pct, cpu_pct, cells_w, cells_h):
    """border.rs::FillFrame: the square fills INWARD from the shell — GPU sets the
    band thickness; CPU grows a solid core square from the centre."""
    b = _Braille(cells_w, cells_h)
    x0, y0, x1, y1 = 1, 1, b.dw - 2, b.dh - 2
    if x1 - x0 < 2 or y1 - y0 < 2:
        return b.rows()
    max_in = max(1, min(x1 - x0, y1 - y0) // 2)

    def outline(d, key):
        b.line(x0 + d, y0 + d, x1 - d, y0 + d, key)
        b.line(x1 - d, y0 + d, x1 - d, y1 - d, key)
        b.line(x1 - d, y1 - d, x0 + d, y1 - d, key)
        b.line(x0 + d, y1 - d, x0 + d, y0 + d, key)

    outline(0, FRAME)                                  # the shell is always visible
    depth = 0 if gpu_pct is None else int(round(_frac(gpu_pct) * max_in))
    for d in range(1, depth + 1):                      # GPU: band thickening inward
        outline(d, grad_key(d / max_in * 100.0))
    if cpu_pct is not None and cpu_pct > 0.5:          # CPU: solid centred core
        cxm, cym = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        hs = _frac(cpu_pct) * (max_in - 1)
        ck = grad_key(cpu_pct)
        for yy in range(int(cym - hs), int(cym + hs) + 1):
            b.line(int(cxm - hs), yy, int(cxm + hs), yy, ck)
    return b.rows()


def _g_brackets(gpu_pct, cpu_pct, cells_w, cells_h):
    """border.rs::CornerBrackets: L-brackets grow from the corners toward the edge
    midpoints ∝ GPU; a centre crosshair grows ∝ CPU."""
    b = _Braille(cells_w, cells_h)
    x0, y0, x1, y1 = 1, 1, b.dw - 2, b.dh - 2
    if x1 - x0 < 2 or y1 - y0 < 2:
        return b.rows()
    pts = _rect_perim(x0, y0, x1, y1)
    for i in range(0, len(pts), 4):                    # sparse dotted outline
        b.set(pts[i][0], pts[i][1], DIM)
    if gpu_pct is not None and gpu_pct > 0.5:
        armx = _frac(gpu_pct) * ((x1 - x0) / 2.0)
        army = _frac(gpu_pct) * ((y1 - y0) / 2.0)
        k = grad_key(gpu_pct)
        for cxn, cyn, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                                 (x0, y1, 1, -1), (x1, y1, -1, -1)):
            b.line(cxn, cyn, cxn + sx * armx, cyn, k)
            b.line(cxn, cyn, cxn, cyn + sy * army, k)
    if cpu_pct is not None and cpu_pct > 0.5:
        cxm, cym = (x0 + x1) // 2, (y0 + y1) // 2
        ck = grad_key(cpu_pct)
        ax = _frac(cpu_pct) * (x1 - x0) / 2.0
        ay = _frac(cpu_pct) * (y1 - y0) / 2.0
        b.line(cxm - ax, cym, cxm + ax, cym, ck)
        b.line(cxm, cym - ay, cxm, cym + ay, ck)
    return b.rows()


def _g_grid(gpu_pct, cpu_pct, cells_w, cells_h):
    """lasers.rs::SecurityGrid: GPU lights horizontal beams top→down, CPU lights
    vertical beams left→right; crossings of live beams flare bright."""
    b = _Braille(cells_w, cells_h)
    x0, y0, x1, y1 = 1, 1, b.dw - 2, b.dh - 2
    if x1 - x0 < 4 or y1 - y0 < 4:
        return b.rows()
    pts = _rect_perim(x0, y0, x1, y1)
    for i in range(0, len(pts), 3):                    # dim outline shell
        b.set(pts[i][0], pts[i][1], DIM)
    nr = max(3, (y1 - y0) // 4)
    nc = max(3, (x1 - x0) // 6)
    rows_y = [y0 + 1 + round((y1 - y0 - 2) * i / (nr - 1)) for i in range(nr)]
    cols_x = [x0 + 1 + round((x1 - x0 - 2) * j / (nc - 1)) for j in range(nc)]
    glit = 0 if gpu_pct is None else int(round(_frac(gpu_pct) * nr))
    clit = 0 if cpu_pct is None else int(round(_frac(cpu_pct) * nc))
    for i, yy in enumerate(rows_y):
        if i < glit:
            b.line(x0 + 1, yy, x1 - 1, yy, grad_key((i + 1) / nr * 100.0))
        else:
            for xx in range(x0 + 1, x1, 4):
                b.set(xx, yy, DIM)
    for j, xx in enumerate(cols_x):
        if j < clit:
            b.line(xx, y0 + 1, xx, y1 - 1, grad_key((j + 1) / nc * 100.0))
        else:
            for yy in range(y0 + 1, y1, 4):
                b.set(xx, yy, DIM)
    for i in range(glit):                              # live-beam crossings flare
        for j in range(clit):
            b.set(cols_x[j], rows_y[i], LABEL)
    return b.rows()


# ── Eva-style angular gauges: polygon scanline fills, so a "block fill" can
# follow ANY slanted boundary (hexagons, trapezoids, diamonds) instead of a box.
def _poly_edges_x(pts, y):
    """Sorted x-intersections of the polygon's edges with horizontal dot-row y."""
    xs = []
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        if (y0 <= y < y1) or (y1 <= y < y0):
            xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
    xs.sort()
    return xs


def _poly_outline(b, pts, key):
    n = len(pts)
    for i in range(n):
        b.line(pts[i][0], pts[i][1], pts[(i + 1) % n][0], pts[(i + 1) % n][1], key)


def _poly_fill(b, pts, frac, hole=None):
    """Liquid block fill: fill the polygon bottom-up to `frac` of its height; the
    surface row's hue reads the level on the load ramp (dark base → hot top).
    The boundary follows the polygon's slanted walls. `hole` cuts out a region."""
    ys = [p[1] for p in pts]
    y0, y1 = min(ys), max(ys)
    if y1 <= y0 or frac <= 0.0:
        return
    level = y1 - frac * (y1 - y0)
    for y in range(max(0, int(math.ceil(level))), int(y1) + 1):
        key = grad_key((y1 - y) / (y1 - y0) * 100.0)
        xs = _poly_edges_x(pts, y + 1e-4)              # nudge: skip vertex doubles
        hx = _poly_edges_x(hole, y + 1e-4) if hole else []
        for j in range(0, len(xs) - 1, 2):
            for x in range(int(math.ceil(xs[j])), int(xs[j + 1]) + 1):
                if len(hx) >= 2 and hx[0] <= x <= hx[-1]:
                    continue
                b.set(x, y, key)


def _path_dots(pts):
    """Ordered dot path along the closed polygon outline (for draw-on sweeps)."""
    path = []
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        steps = max(1, int(max(abs(x1 - x0), abs(y1 - y0))))
        for s in range(steps):
            path.append((x0 + (x1 - x0) * s / steps, y0 + (y1 - y0) * s / steps))
    return path


def _poly_edges_y(pts, x):
    """Sorted y-intersections of the polygon's edges with vertical dot-column x."""
    ys = []
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        if (x0 <= x < x1) or (x1 <= x < x0):
            ys.append(y0 + (x - x0) * (y1 - y0) / (x1 - x0))
    ys.sort()
    return ys


def _poly_fill_h(b, pts, frac):
    """Horizontal liquid fill: left→right to `frac` of the polygon's width, the
    leading edge hue reading the level (the bars' position-gradient, in 2D)."""
    xs_ = [p[0] for p in pts]
    x0, x1 = min(xs_), max(xs_)
    if x1 <= x0 or frac <= 0.0:
        return
    limit = x0 + frac * (x1 - x0)
    for x in range(max(0, int(math.ceil(x0))), int(limit) + 1):
        key = grad_key((x - x0) / (x1 - x0) * 100.0)
        ys = _poly_edges_y(pts, x + 1e-4)
        for j in range(0, len(ys) - 1, 2):
            for y in range(int(math.ceil(ys[j])), int(ys[j + 1]) + 1):
                b.set(x, y, key)


def _g_nerv(gpu_pct, cpu_pct, cells_w, cells_h):
    """NERV hexagon in solid blocks (no braille): GPU is a liquid fill whose
    surface follows the slanted hex walls, leaving a hollow core hexagon where
    the CPU level rises on its own."""
    b = _Blocks(cells_w, cells_h)
    cx, cy = b.dw / 2.0, b.dh / 2.0
    rx = max(3.0, b.dw / 2.0 - 1.0)
    ry = max(2.0, b.dh / 2.0 - 1.0)

    def hexpts(sx, sy):
        return [(cx - sx, cy), (cx - sx * 0.5, cy - sy), (cx + sx * 0.5, cy - sy),
                (cx + sx, cy), (cx + sx * 0.5, cy + sy), (cx - sx * 0.5, cy + sy)]

    outer, inner = hexpts(rx, ry), hexpts(rx * 0.45, ry * 0.45)
    _poly_outline(b, outer, FRAME)
    if gpu_pct is not None:
        _poly_fill(b, outer, _frac(gpu_pct), hole=inner)
    _poly_outline(b, inner, DIM)
    if cpu_pct is not None:
        _poly_fill(b, inner, _frac(cpu_pct))
    return b.rows()


def _g_magi(gpu_pct, cpu_pct, cells_w, cells_h):
    """MAGI tri-core in solid blocks: three angular slabs with slanted inner
    edges — GPU fills MELCHIOR (left), CPU fills BALTHASAR (right), the bottom
    core takes the combined load. Data conduits join the cores at the centre."""
    b = _Blocks(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw < 12 or dh < 6:                              # too small for the layout
        return b.rows()
    w, h = dw - 1.0, dh - 1.0
    ymid = h * 0.55
    left = [(1, 1), (dw * 0.44, 1), (dw * 0.30, ymid), (1, ymid)]
    right = [(w - 1, 1), (dw * 0.56, 1), (dw * 0.70, ymid), (w - 1, ymid)]
    core = [(dw * 0.30, h * 0.66), (dw * 0.70, h * 0.66),
            (dw * 0.60, h - 1), (dw * 0.40, h - 1)]
    vals = [v for v in (gpu_pct, cpu_pct) if v is not None]
    comb = (sum(vals) / len(vals)) if vals else None
    for pts, val in ((left, gpu_pct), (right, cpu_pct), (core, comb)):
        _poly_outline(b, pts, FRAME)
        if val is not None:
            _poly_fill(b, pts, _frac(val))
    hub = (dw / 2.0, h * 0.61)                         # data conduits to the hub
    b.line(dw * 0.30, ymid, hub[0], hub[1], DIM)
    b.line(dw * 0.70, ymid, hub[0], hub[1], DIM)
    b.line(dw / 2.0, h * 0.66, hub[0], hub[1], DIM)
    return b.rows()


def _g_octa(gpu_pct, cpu_pct, cells_w, cells_h):
    """Eva targeting octagon in solid blocks: the chamfered frame draws on
    clockwise ∝ GPU over a dotted track; inside, a diamond core block-fills
    bottom-up ∝ CPU."""
    b = _Blocks(cells_w, cells_h)
    x0, y0, x1, y1 = 0.0, 0.0, b.dw - 1.0, b.dh - 1.0
    if x1 - x0 < 4 or y1 - y0 < 4:
        return b.rows()
    chx, chy = (x1 - x0) * 0.25, (y1 - y0) * 0.25
    octa = [(x0 + chx, y0), (x1 - chx, y0), (x1, y0 + chy), (x1, y1 - chy),
            (x1 - chx, y1), (x0 + chx, y1), (x0, y1 - chy), (x0, y0 + chy)]
    path = _path_dots(octa)
    lit = 0 if gpu_pct is None else int(round(_frac(gpu_pct) * len(path)))
    for i, (x, y) in enumerate(path):
        if i < lit:
            b.set(x, y, grad_key((i + 1) / len(path) * 100.0))
        elif i % 3 == 0:
            b.set(x, y, DIM)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rx, ry = (x1 - x0) * 0.30, (y1 - y0) * 0.32
    diamond = [(cx, cy - ry), (cx + rx, cy), (cx, cy + ry), (cx - rx, cy)]
    _poly_outline(b, diamond, DIM)
    if cpu_pct is not None:
        _poly_fill(b, diamond, _frac(cpu_pct))
    return b.rows()


def _g_delta(gpu_pct, cpu_pct, cells_w, cells_h):
    """Warning delta in solid blocks: a big triangle block-fills bottom-up ∝ GPU
    around an inverted core triangle that holds the CPU level."""
    b = _Blocks(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw < 8 or dh < 6:
        return b.rows()
    cx = dw / 2.0
    tri = [(cx, 0.0), (dw - 1.0, dh - 1.0), (1.0, dh - 1.0)]
    irx = dw * 0.16
    inner = [(cx - irx, dh * 0.55), (cx + irx, dh * 0.55), (cx, dh * 0.92)]
    _poly_outline(b, tri, FRAME)
    if gpu_pct is not None:
        _poly_fill(b, tri, _frac(gpu_pct), hole=inner)
    _poly_outline(b, inner, DIM)
    if cpu_pct is not None:
        _poly_fill(b, inner, _frac(cpu_pct))
    return b.rows()


def _g_wings(gpu_pct, cpu_pct, cells_w, cells_h):
    """HUD wings in solid blocks: two slanted parallelogram slabs with angled
    ends — the top wing fills left→right ∝ GPU, the bottom wing (opposite
    slant) ∝ CPU — the bars family stretched into 2-D angled slabs."""
    b = _Blocks(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw < 10 or dh < 4:
        return b.rows()
    sl = dw * 0.12                                     # slant of the cut ends
    gap = dh * 0.5
    top = [(1 + sl, 0.0), (dw - 1.0, 0.0), (dw - 1.0 - sl, gap - 1.0), (1.0, gap - 1.0)]
    bot = [(1.0, gap + 1.0), (dw - 1.0 - sl, gap + 1.0), (dw - 1.0, dh - 1.0), (1 + sl, dh - 1.0)]
    for pts, val in ((top, gpu_pct), (bot, cpu_pct)):
        _poly_outline(b, pts, FRAME)
        if val is not None:
            _poly_fill_h(b, pts, _frac(val))
    return b.rows()


def _g_pylon(gpu_pct, cpu_pct, cells_w, cells_h):
    """Entry-plug pylons in solid blocks: twin columns with angled caps
    block-fill bottom-up — GPU left, CPU right."""
    b = _Blocks(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw < 10 or dh < 5:
        return b.rows()
    hw = dw * 0.11                                     # column half-width
    cap = dh * 0.22                                    # angled-cap height

    def pylon(xm, pct):
        pts = [(xm - hw, dh - 1.0), (xm - hw, cap), (xm, 0.0),
               (xm + hw, cap), (xm + hw, dh - 1.0)]
        _poly_outline(b, pts, FRAME)
        if pct is not None:
            _poly_fill(b, pts, _frac(pct))
        for q in range(1, 4):                          # 25/50/75% wall ticks
            y = (dh - 1.0) - (dh - 1.0 - cap) * q / 4.0
            b.set(xm - hw - 1, y, DIM)
            b.set(xm + hw + 1, y, DIM)

    pylon(dw * 0.30, gpu_pct)
    pylon(dw * 0.70, cpu_pct)
    return b.rows()


# ── The clean series: hard angles with NO diagonals. Every edge is horizontal
# or vertical and lands exactly on a half-cell boundary, so full/half blocks
# render it pixel-perfect — solid corners, zero stair-step seams. Angles come
# from notches and steps instead of slants.
def _rect_box(b, x0, y0, x1, y1, key):
    b.line(x0, y0, x1, y0, key)
    b.line(x1, y0, x1, y1, key)
    b.line(x1, y1, x0, y1, key)
    b.line(x0, y1, x0, y0, key)


def _ortho_path(b, path, key):
    """Closed outline along H/V-only segments."""
    n = len(path)
    for i in range(n):
        x0, y0 = path[i]
        x1, y1 = path[(i + 1) % n]
        b.line(x0, y0, x1, y1, key)


def _zone_fill(b, rects, frac, hole=None):
    """Liquid fill of a rectilinear zone (union of axis-aligned rects), rising
    bottom-up to `frac` of the zone's total height. Row hue reads the level on
    the load ramp; `hole` = (x0, y0, x1, y1) stays empty."""
    if frac <= 0.0 or not rects:
        return
    ymin = min(r[1] for r in rects)
    ymax = max(r[3] for r in rects)
    if ymax <= ymin:
        return
    start = max(0, int(math.ceil(ymax - frac * (ymax - ymin))))
    for x0, y0, x1, y1 in rects:
        for y in range(max(y0, start), y1 + 1):
            key = grad_key((ymax - y) / (ymax - ymin) * 100.0)
            for x in range(x0, x1 + 1):
                if hole and hole[0] <= x <= hole[2] and hole[1] <= y <= hole[3]:
                    continue
                b.set(x, y, key)


def _zone_fill_h(b, rects, frac):
    """Horizontal liquid fill of a rectilinear zone, left→right, hue = position
    (the bars' gradient in 2-D)."""
    if frac <= 0.0 or not rects:
        return
    xmin = min(r[0] for r in rects)
    xmax = max(r[2] for r in rects)
    if xmax <= xmin:
        return
    limit = int(xmin + frac * (xmax - xmin))
    for x0, y0, x1, y1 in rects:
        for x in range(x0, min(x1, limit) + 1):
            key = grad_key((x - xmin) / (xmax - xmin) * 100.0)
            for y in range(y0, y1 + 1):
                b.set(x, y, key)


def _g_bastion(gpu_pct, cpu_pct, cells_w, cells_h):
    """Corner-notched vessel: an octagon built from right angles only. GPU is
    the liquid fill following the notched walls; CPU rises in a square core."""
    b = _Blocks(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw < 10 or dh < 6:
        return b.rows()
    W, H = dw - 1, dh - 1
    nx, ny = max(2, dw // 6), max(1, dh // 5)
    _ortho_path(b, [(nx, 0), (W - nx, 0), (W - nx, ny), (W, ny), (W, H - ny),
                    (W - nx, H - ny), (W - nx, H), (nx, H), (nx, H - ny),
                    (0, H - ny), (0, ny), (nx, ny)], FRAME)
    body = [(nx + 1, 1, W - nx - 1, ny), (1, ny + 1, W - 1, H - ny - 1),
            (nx + 1, H - ny, W - nx - 1, H - 1)]
    core = (int(dw * 0.32), int(dh * 0.34), int(dw * 0.68), int(dh * 0.66))
    if gpu_pct is not None:
        _zone_fill(b, body, _frac(gpu_pct), hole=core)
    _rect_box(b, core[0], core[1], core[2], core[3], DIM)
    if cpu_pct is not None:
        _zone_fill(b, [(core[0] + 1, core[1] + 1, core[2] - 1, core[3] - 1)],
                   _frac(cpu_pct))
    return b.rows()


def _g_ziggurat(gpu_pct, cpu_pct, cells_w, cells_h):
    """Stepped pyramid: stacked tiers instead of a sloped triangle. GPU floods
    the tiers bottom-up; CPU rises in a central well."""
    b = _Blocks(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw < 12 or dh < 6:
        return b.rows()
    tiers = 4 if dh >= 12 else 3
    cx, th = dw // 2, dh // tiers
    rects = []
    for i in range(tiers):                             # 0 = narrow top tier
        hw = int((dw - 2) * (i + 1) / (2 * tiers))
        rects.append((cx - hw, i * th,
                      cx + hw, dh - 1 if i == tiers - 1 else (i + 1) * th - 1))
    for r in rects:
        _rect_box(b, r[0], r[1], r[2], r[3], FRAME)    # layered tier outlines
    well = (cx - max(2, dw // 9), int(dh * 0.5), cx + max(2, dw // 9), dh - 2)
    if gpu_pct is not None:
        _zone_fill(b, [(r[0] + 1, r[1] + 1, r[2] - 1, r[3] - 1) for r in rects],
                   _frac(gpu_pct), hole=well)
    _rect_box(b, well[0], well[1], well[2], well[3], DIM)
    if cpu_pct is not None:
        _zone_fill(b, [(well[0] + 1, well[1] + 1, well[2] - 1, well[3] - 1)],
                   _frac(cpu_pct))
    return b.rows()


def _g_citadel(gpu_pct, cpu_pct, cells_w, cells_h):
    """The MAGI layout in pure rectangles: GPU fills the left core, CPU the
    right, the keep below takes the combined load; orthogonal conduits join
    them over a data bus."""
    b = _Blocks(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw < 12 or dh < 6:
        return b.rows()
    W, H = dw - 1, dh - 1
    ymid = int(dh * 0.52)
    left = (1, 0, int(dw * 0.42), ymid)
    right = (int(dw * 0.58), 0, W - 1, ymid)
    keep = (int(dw * 0.32), int(dh * 0.68), int(dw * 0.68), H)
    vals = [v for v in (gpu_pct, cpu_pct) if v is not None]
    comb = sum(vals) / len(vals) if vals else None
    for r, val in ((left, gpu_pct), (right, cpu_pct), (keep, comb)):
        _rect_box(b, r[0], r[1], r[2], r[3], FRAME)
        if val is not None:
            _zone_fill(b, [(r[0] + 1, r[1] + 1, r[2] - 1, r[3] - 1)], _frac(val))
    bus = int(dh * 0.60)                               # orthogonal data conduits
    xl, xr = (left[0] + left[2]) // 2, (right[0] + right[2]) // 2
    b.line(xl, ymid + 1, xl, bus, DIM)
    b.line(xr, ymid + 1, xr, bus, DIM)
    b.line(xl, bus, xr, bus, DIM)
    b.line(dw // 2, bus, dw // 2, keep[1] - 1, DIM)
    return b.rows()


def _g_bays(gpu_pct, cpu_pct, cells_w, cells_h):
    """Twin equipment bays: slabs with a stepped notch at the outer corner (no
    slants), filling left→right — GPU top bay, CPU bottom bay."""
    b = _Blocks(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw < 12 or dh < 6:
        return b.rows()
    W = dw - 1
    nx, ny = max(2, dw // 7), max(1, dh // 6)
    gap = dh // 2

    def bay(y0, y1, notch_top, pct):
        xn = W - nx
        if notch_top:                                  # notch at the top-right
            _ortho_path(b, [(0, y0 + ny), (xn, y0 + ny), (xn, y0), (W, y0),
                            (W, y1), (0, y1)], FRAME)
            zone = [(1, y0 + ny + 1, xn - 1, y1 - 1), (xn, y0 + 1, W - 1, y1 - 1)]
        else:                                          # notch at the bottom-right
            _ortho_path(b, [(0, y0), (W, y0), (W, y1), (xn, y1),
                            (xn, y1 - ny), (0, y1 - ny)], FRAME)
            zone = [(1, y0 + 1, xn - 1, y1 - ny - 1), (xn, y0 + 1, W - 1, y1 - 1)]
        if pct is not None:
            _zone_fill_h(b, zone, _frac(pct))

    bay(0, gap - 2, False, gpu_pct)
    bay(gap + 1, dh - 1, True, cpu_pct)
    return b.rows()


# (name, render fn, legend line shown under non-compact tiles)
GAUGE_STYLES = [
    ("rings", _g_rings, "cpu ● ◯ gpu"),
    ("speedo", _g_speedo, "gpu long · cpu short"),
    ("half", _g_half, "gpu outer · cpu inner"),
    ("vu", _g_vu, "gpu long · cpu short"),
    ("donut", _g_donut, "gpu ring · cpu core"),
    ("segments", _g_segments, "gpu outer · cpu inner"),
    ("signal", _g_signal, "gpu ▲ fan · cpu ▼ fan"),
    ("thermo", _g_thermo, "gpu left · cpu right"),
    ("pressure", _g_pressure, "gpu long · cpu short"),
    ("clock", _g_clock, "gpu big · cpu small hand"),
    ("frame", _g_frame, "gpu outer · cpu inner"),
    ("inset", _g_inset, "gpu band · cpu core"),
    ("brackets", _g_brackets, "gpu corners · cpu cross"),
    ("grid", _g_grid, "gpu rows · cpu cols"),
    ("nerv", _g_nerv, "gpu hex · cpu core"),
    ("magi", _g_magi, "gpu L · cpu R · Σ core"),
    ("octa", _g_octa, "gpu frame · cpu diamond"),
    ("delta", _g_delta, "gpu delta · cpu core"),
    ("wings", _g_wings, "gpu top · cpu low wing"),
    ("pylon", _g_pylon, "gpu left · cpu right"),
    ("bastion", _g_bastion, "gpu vessel · cpu core"),
    ("ziggurat", _g_ziggurat, "gpu tiers · cpu well"),
    ("citadel", _g_citadel, "gpu L · cpu R · Σ keep"),
    ("bays", _g_bays, "gpu top · cpu low bay"),
]
GAUGE_IDX = 0


def dual_gauge_rows(gpu_pct, cpu_pct, cells_w, cells_h):
    """Render the selected gauge style. Every style shows BOTH values (GPU + CPU);
    the caller overlays the centre CPU% text and the GPU% line below."""
    return GAUGE_STYLES[GAUGE_IDX][1](gpu_pct, cpu_pct, cells_w, cells_h)


def cycle_gauge(delta):
    global GAUGE_IDX
    GAUGE_IDX = (GAUGE_IDX + delta) % len(GAUGE_STYLES)
    save_prefs()


def set_gauge(name):
    global GAUGE_IDX
    for i, (n, _, _) in enumerate(GAUGE_STYLES):
        if n == (name or "").lower():
            GAUGE_IDX = i
            return True
    return False


def _overlay_center(row, text, key):
    """Stamp `text` centered onto a braille cell row, overwriting those cells."""
    row = list(row)
    start = max(0, (len(row) - len(text)) // 2)
    for i, ch in enumerate(text):
        if 0 <= start + i < len(row):
            row[start + i] = (ch, key)
    return row


def _center_row(text, key):
    """A full-width content row with `text` horizontally centered."""
    pad = max(0, (CW - len(text)) // 2)
    return _pad([(" " * pad, TEXT), (text, key)])


def build_dual_rows(node, compact=False):
    """The per-connection dial: GPU (outer ring) + CPU (inner ring), CPU% in the
    centre, GPU% on the line below. `compact` shrinks the dial and drops the legend
    so a crowded fleet still fits on screen. Returns a list of padded content rows."""
    h = DUAL_H_COMPACT if compact else DUAL_H
    gpct, cpct = node_gpu_util(node), node.get("cpu_pct")
    dial = dual_gauge_rows(gpct, cpct, CW, h)
    dial[h // 2] = _overlay_center(dial[h // 2],
                                   f"{cpct:.0f}%" if cpct is not None else "··%",
                                   grad_key(cpct))
    rows = [_pad(r) for r in dial]
    rows.append(_center_row(f"GPU {gpct:.0f}%" if gpct is not None else "GPU ··",
                            grad_key(gpct)))
    if not compact:
        rows.append(_center_row(GAUGE_STYLES[GAUGE_IDX][2], DIM))  # per-style legend
    return rows


def gpu_row_compact(i, g):
    """One dense line per GPU — for tiles with many cards (e.g. 8x-GPU Vast pods),
    so a tile doesn't balloon to 4 rows × N GPUs and blow out the layout."""
    u, mu, mt, t = g.get("util"), g.get("mem_used"), g.get("mem_total"), g.get("temp")
    vram = f" {mu/1024:.0f}/{mt/1024:.0f}G" if (mu is not None and mt) else ""
    temp = f" {int(t)}°" if t is not None else ""
    suf = (f" {int(u):>3}%" if u is not None else "  ?%") + vram + temp
    head = f"#{i} "
    bw = max(3, CW - len(head) - len(suf))
    return _pad([(head, LABEL)] + bar_runs(u, 100, bw) + [(suf, TEXT)])


def build_tile(node, compact=False):
    """Return (top_border_runs, [content rows], bottom_border) for a node.
    compact drops the per-core strip to claw back vertical space when tight."""
    if node.get("kind") == "meter":
        return build_meter_tile(node)
    name = node["name"]
    status = node.get("status", "offline")
    is_vast = node.get("source") == "vast"
    dot = ("●" if status == "ok" else "◐" if status in ("api", "nossh", "unreachable")
           else "◌" if status == "scanning" else "○")
    dot_key = {"ok": OK, "api": WARN, "nossh": WARN, "unreachable": CRIT}.get(status, DIM)

    # Top border with embedded name + status dot, total width == TILE_W.
    head = f"┌─ {name} "
    tail = f"{dot} ┐"
    fill = TILE_W - len(head) - len(tail)
    if fill < 1:
        head = f"┌─ {name[:max(1, len(name) + fill - 1)]} "
        fill = TILE_W - len(head) - len(tail)
    top = [(head, FRAME), ("─" * fill, FRAME), (dot, dot_key), (" ┐", FRAME)]
    bottom = "└" + "─" * CW + "┘"

    rows = []
    if status == "offline":
        if is_vast:
            rows.append(_pad([(f" ░ {node.get('vast_state') or 'not running'}", DIM)]))
            rows.append(_pad([(f" {node['num_gpus']}x {node.get('model') or 'GPU'}", DIM)]))
        else:
            rows.append(_pad([(" ░ offline", DIM)]))
            rows.append(_pad([(f" last seen via {node.get('os') or '—'}", DIM)]))
    elif status == "nossh":
        rows.append(_pad([(" online · not probed", DIM)]))
        rows.append(_pad([(f" {node.get('ip') or ''}", DIM)]))
    elif status == "unreachable":
        rows.append(_pad([(" online · no reply", WARN)]))
        rows.append(_pad([(f" {node.get('err') or 'ssh failed'}", DIM)]))
    elif status == "scanning":
        rows.append(_pad([(" scanning…", DIM)]))
        rows.append(_pad([(" ", DIM)]))
    elif DUAL_GAUGE:  # ok/api, dual-gauge view: one GPU(outer)/CPU(inner) dial per tile
        rows.extend(build_dual_rows(node, compact))
    else:  # ok or api -> render metrics
        gpus = node.get("gpus") or []
        if gpus and (compact or len(gpus) >= 3):
            # Many GPUs (or tight): one dense line each (model on the tile title / count).
            if len(gpus) > 1:
                rows.append(_pad([(f" {len(gpus)}x {_short_gpu(gpus[0].get('name') or 'GPU')}"[:CW], TEXT)]))
            for i, g in enumerate(gpus):
                rows.append(gpu_row_compact(i, g))
        elif gpus:
            multi = len(gpus) > 1
            for i, g in enumerate(gpus):
                tag = f"#{i} " if multi else ""
                rows.append(_pad([(" " + (tag + (g["name"] or "GPU"))[:CW - 1], TEXT)]))
                rows.append(metric_row("GPU ", g["util"], 100,
                                       f"{int(g['util']):>3}%" if g["util"] is not None else "  ?",
                                       pct_key(g["util"])))
                gu, gt = g.get("mem_used"), g.get("mem_total")
                if gu is not None and gt:
                    rows.append(metric_row("VRAM", gu, gt, f"{gu/1024:.1f}/{gt/1024:.0f}G",
                                           pct_key(100 * gu / gt)))
                else:
                    rows.append(_pad([(" VRAM shared (unified)", DIM)]))
                t, p = g.get("temp"), g.get("power")
                tcell = f"{int(t)}°C" if t is not None else "—°C"
                pcell = f"{p:.0f}W" if p is not None else "—W"
                rows.append(_pad([(" ", TEXT), (f"{tcell:<7}", temp_key(t)),
                                  (f"{pcell:>6}", TEXT)]))
        else:  # no GPU metrics (Intel Arc w/o tooling, or CPU-only node)
            label = node.get("gpu_label")
            if label:
                rows.append(_pad([(" " + label, TEXT)]))
                rows.append(_pad([(" no GPU metrics", DIM)]))
            else:
                rows.append(_pad([(" no GPU", DIM)]))

        cpu = node.get("cpu_pct")
        nc = node.get("ncpu")
        load1 = node["load"][0] if node.get("load") else None
        cpu_suffix = (f"{nc or '?'}c ld{load1:.1f}" if load1 is not None else f"{nc or '?'}c")
        rows.append(metric_row("CPU ", cpu, 100,
                               (f"{int(cpu):>2}% " if cpu is not None else "·· ") + cpu_suffix,
                               pct_key(cpu)))
        if not compact:
            rows.extend(core_strip(node.get("core_pct")))  # per-core heat strip (fleet only)
        mt, ma = node.get("mem_total_kb"), node.get("mem_avail_kb")
        mu = (mt - ma) if (mt is not None and ma is not None) else None
        rlabel = (f"{fmt_bytes_gib(mu):.0f}/{fmt_bytes_gib(mt):.0f}G up{fmt_uptime(node.get('uptime_s'))}"
                  if mu is not None and mt else f"up{fmt_uptime(node.get('uptime_s'))}")
        rows.append(metric_row("RAM ", mu, mt, rlabel,
                               pct_key(100 * mu / mt if mu is not None and mt else None)))

    # Vast footer: cost + location + data source (so it's never mistaken for live fleet).
    if is_vast:
        dph = node.get("dph")
        geo = (node.get("geo") or "").split(",")[0]
        cost = f"${dph:.2f}/hr" if dph is not None else "$?/hr"
        rows.append(_pad([(" " + cost, LABEL), ("  " + geo, DIM)]))
        if node.get("detail"):
            rows.append(_pad([(" " + node["detail"], DIM)]))
    return top, rows, bottom


def fleet_compute(snapshot):
    """Cores-weighted total CPU utilization across online fleet nodes (0..100), or None."""
    num = den = 0.0
    for n in snapshot.values():
        if (n.get("source") == "fleet" and n.get("status") == "ok"
                and n.get("cpu_pct") is not None and n.get("ncpu")):
            num += n["cpu_pct"] * n["ncpu"]
            den += n["ncpu"]
    return (num / den) if den else None


def fleet_gpu(snapshot):
    """Average GPU utilization across all fleet GPUs (0..100), or None."""
    utils = [g["util"] for n in snapshot.values()
             if n.get("source") == "fleet" and n.get("status") == "ok"
             for g in (n.get("gpus") or []) if g.get("util") is not None]
    return (sum(utils) / len(utils)) if utils else None


def vast_compute(snapshot):
    """Cores-weighted CPU across Vast pods with real (/proc) CPU (status ok), or None."""
    num = den = 0.0
    for n in snapshot.values():
        if (n.get("source") == "vast" and n.get("status") == "ok"
                and n.get("cpu_pct") is not None and n.get("ncpu")):
            num += n["cpu_pct"] * n["ncpu"]
            den += n["ncpu"]
    return (num / den) if den else None


def vast_gpu(snapshot):
    """Average GPU utilization across all Vast pod GPUs (0..100), or None."""
    utils = [g["util"] for n in snapshot.values()
             if n.get("source") == "vast" and n.get("status") in ("ok", "api")
             for g in (n.get("gpus") or []) if g.get("util") is not None]
    return (sum(utils) / len(utils)) if utils else None


def grouped(snapshot):
    """[(section_title, [nodes])] — FLEET, VAST PODS, then the TOTAL COMPUTE meter."""
    fleet = [snapshot[n] for n in ORDER if n in snapshot and snapshot[n].get("source") != "vast"]
    fleet += sorted((v for k, v in snapshot.items()
                     if v.get("source") != "vast" and k not in ORDER),
                    key=lambda d: d["name"])
    vast = sorted((v for v in snapshot.values() if v.get("source") == "vast"),
                  key=lambda d: d.get("id") or 0)
    out = []
    if fleet:
        out.append(("FLEET", fleet))
    if vast:
        out.append(("VAST PODS", vast))
    return out  # the CPU/GPU dials are drawn as a fixed bottom-right overlay, not here


def _centered_runs(s, w, key):
    s = s[:w]                                        # never overflow the panel width
    pad = max(0, (w - len(s)) // 2)
    return [(" " * pad, TEXT), (s, key), (" " * max(0, w - pad - len(s)), TEXT)]


def gauge_groups(snapshot):
    """Dual-gauge groups for the TOTALS tower: (label, gpu_pct, cpu_pct) — outer ring
    = GPU, inner ring = CPU. Fleet always; rental (Vast) added when pods exist."""
    groups = [("FLEET", fleet_gpu(snapshot), fleet_compute(snapshot))]
    if any(n.get("source") == "vast" for n in snapshot.values()):
        groups.append(("VAST", vast_gpu(snapshot), vast_compute(snapshot)))
    return groups


TOWER_GW = 13   # dial width inside the right-edge tower


def tower_geom(snapshot, rows_h, cols_w):
    """Right-edge TOTALS tower: (gw, gh, tower_w, dials_to_show), or None (no room).
    Reserves a full-height column on the right; dials stack vertically inside it."""
    if not any(n.get("source") == "fleet" for n in snapshot.values()):
        return None
    gw = TOWER_GW
    tower_w = gw + 2
    if cols_w - tower_w < TILE_W or rows_h < 8:       # no room for tiles -> no tower
        return None
    groups = gauge_groups(snapshot)
    content_h = rows_h - 3                            # title row + 2 borders inside rows 1..rows_h-1
    gh = 3
    for g in (7, 6, 5, 4, 3):                         # each group: label + dial(g) + GPU% = g+2 rows
        if len(groups) * (g + 2) <= content_h:
            gh = g
            break
    fit = max(1, content_h // (gh + 2))               # if even gh=3 won't fit all, clip
    return gw, gh, tower_w, groups[:fit]


# ── Beat-frequency backdrop — a vertical port of dotmax waves::BeatFrequency ─────
# A faithful copy of the loading_bars `beat-frequency` style, rotated 90°: the wave
# val = sin(f1·θ)+sin(f2·θ) displaces left/right (not up/down) as you travel the
# column, bounded by the ±|cos(Δf·θ/2)| beat envelope, tinted with the load gradient
# along its length. The GPU signal hangs from the top and the CPU signal stands up
# from the base; each reaches the centre at 100%, so they meet in the middle. The
# beat lumps (the energy pulse) travel toward the centre — DOWN for the top GPU
# signal, UP for the bottom CPU signal. The concentric dials composite on top.
_BEAT_BLANK = "⠀"


def beat_field_rows(gpu_pct, cpu_pct, cells_w, cells_h, t):
    """Vertical beat-frequency for the TOTALS column. Returns braille cell rows whose
    blank cells (chr 0x2800) let the caller's dials show through — it's a backdrop."""
    b = _Braille(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw == 0 or dh == 0:
        return b.rows()
    cx = dw / 2.0                                      # the wave swings left/right about here
    mid = dh / 2.0
    amp = max(1.0, cx - 1.0)                           # horizontal swing spans the column width
    f1 = 3.0                                           # carrier cycles (lowered from BeatFrequency's
    twopi = 2 * math.pi                                # 8 so the wave stays smooth on a short axis)

    def trace(pct, y0, y1, down):
        """One vertical beat-frequency signal over dot-rows y0→y1: val = sin(f1·θ)+
        sin(f2·θ) displaced horizontally, ±|cos(Δf·θ/2)| envelope walls, gradient tint
        along its length. `down` sends the beat lumps travelling toward the centre."""
        if pct is None or pct <= 0.0:
            return
        frac = max(0.0, min(1.0, pct / 100.0))
        df = 0.05 + frac * 1.8                          # beat Δf eased by load (orig: eased·2+.05)
        f2 = f1 + df
        phase = (-1.0 if down else 1.0) * t * twopi * 0.4   # sign sets the pulse travel direction
        ys = list(range(int(round(y0)), int(round(y1)), 1 if y1 >= y0 else -1))
        span = max(1, len(ys))
        prev = None
        for i, yi in enumerate(ys):
            if not (0 <= yi < dh):
                continue
            theta = (yi / dh) * twopi + phase
            val = 0.5 * (math.sin(f1 * theta) + math.sin(f2 * theta))   # [-1,1]; beat built in
            dx = cx + val * amp
            key = grad_key(pct * (i / span))            # green at the outer end → load hue at the front
            b.set(dx, yi, key)
            if prev is not None and abs(prev - dx) <= amp:   # connect into a continuous curve
                for xx in range(int(min(prev, dx)), int(max(prev, dx)) + 1):
                    b.set(xx, yi, key)
            prev = dx
            env = abs(math.cos((f2 - f1) * theta / 2.0)) * amp          # the beat envelope walls
            b.set(cx - env, yi, FRAME)
            b.set(cx + env, yi, FRAME)

    gfrac = 0.0 if gpu_pct is None else max(0.0, min(1.0, gpu_pct / 100.0))
    cfrac = 0.0 if cpu_pct is None else max(0.0, min(1.0, cpu_pct / 100.0))
    greach = gfrac * mid                               # each signal reaches the centre at 100%
    creach = cfrac * mid
    trace(gpu_pct, 0, greach, down=True)               # GPU hangs from the top; lumps pulse DOWN
    trace(cpu_pct, dh - 1, dh - 1 - creach, down=False)  # CPU stands from the base; lumps pulse UP
    if gpu_pct and cpu_pct and greach >= (dh - creach) - 1.0:   # fronts meet → the beat flash
        flash = grad_key(max(gpu_pct, cpu_pct))
        for yi in range(max(0, int(dh - creach)), min(dh, int(greach) + 1)):
            for xx in range(dw):
                if (xx + yi) % 2 == 0:
                    b.set(xx, yi, flash)
    return b.rows()


# ── More TOTALS field styles — same growth contract as the beat field: the GPU
# signal grows DOWN from the top, the CPU signal UP from the base, each reaching
# the centre at 100%; blank cells stay transparent so the dials float on top.
def field_helix(gpu_pct, cpu_pct, cells_w, cells_h, t):
    """DNA double helix: two counter-phase strands + base-pair rungs, rotating
    with time. Strand reach ∝ load."""
    b = _Braille(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw == 0 or dh == 0:
        return b.rows()
    cx = dw / 2.0
    amp = max(1.0, cx - 2.0)
    mid = dh / 2.0

    def helix(pct, ys, down):
        if pct is None or pct <= 0:
            return
        span = max(1, len(ys))
        for i, yi in enumerate(ys):
            th = yi * 0.45 + (t * 2.2 if down else -t * 2.2)
            xa = cx + math.sin(th) * amp
            xb = cx + math.sin(th + math.pi) * amp
            key = grad_key(pct * (i / span))           # hue advances toward the front
            b.set(xa, yi, key)
            b.set(xb, yi, key)
            if yi % 5 == 0:                            # base-pair rung
                for xx in range(int(min(xa, xb)) + 1, int(max(xa, xb))):
                    b.set(xx, yi, FRAME)

    helix(gpu_pct, list(range(0, int(_frac(gpu_pct) * mid))), True)
    helix(cpu_pct, list(range(dh - 1, dh - 1 - int(_frac(cpu_pct) * mid), -1)), False)
    return b.rows()


def field_rain(gpu_pct, cpu_pct, cells_w, cells_h, t):
    """Code rain: GPU drops fall from the top, CPU bubbles rise from the base.
    Column depth ∝ load; bright heads, dim tails, hash-staggered speeds."""
    b = _Braille(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw == 0 or dh == 0:
        return b.rows()
    mid = dh / 2.0

    def streams(pct, down):
        depth = _frac(pct) * mid
        if depth < 1.0:
            return
        key = grad_key(pct)
        for x in range(0, dw, 2):
            hv = _h32(x * 31 + (0 if down else 7))
            speed = 0.25 + (hv % 100) / 100.0 * 0.6
            ph = ((hv >> 8) & 0xFF) / 255.0
            head = ((t * speed + ph) % 1.0) * depth
            yh = head if down else dh - 1 - head
            b.set(x, yh, key)
            for k in range(1, 5):                      # fading tail behind the head
                yt = head - k * 1.5
                if yt >= 0:
                    b.set(x, yt if down else dh - 1 - yt, FRAME if k < 3 else DIM)

    streams(gpu_pct, True)
    streams(cpu_pct, False)
    return b.rows()


def field_wave(gpu_pct, cpu_pct, cells_w, cells_h, t):
    """Standing waves (waves.rs::StandingWave, vertical): horizontal displacement
    A·sin(k·y)·cos(ωt) with fixed nodes marked on the axis; mode count ∝ load."""
    b = _Braille(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw == 0 or dh == 0:
        return b.rows()
    cx = dw / 2.0
    amp = max(1.0, cx - 1.5)
    mid = dh / 2.0
    breathe = math.cos(t * 2.4)

    def standing(pct, ys, _down):
        if pct is None or pct <= 0:
            return
        span = max(1, len(ys))
        mode = 1 + int(_frac(pct) * 4)
        prev = None
        for i, yi in enumerate(ys):
            ph = math.sin(mode * math.pi * i / span)
            x = cx + ph * breathe * amp
            key = grad_key(pct * (i / span))
            b.set(x, yi, key)
            if prev is not None and abs(prev - x) <= amp:
                for xx in range(int(min(prev, x)), int(max(prev, x)) + 1):
                    b.set(xx, yi, key)
            prev = x
            if abs(ph) < 0.08:                         # fixed node on the axis
                b.set(cx - 1, yi, FRAME)
                b.set(cx + 1, yi, FRAME)

    standing(gpu_pct, list(range(0, int(_frac(gpu_pct) * mid))), True)
    standing(cpu_pct, list(range(dh - 1, dh - 1 - int(_frac(cpu_pct) * mid), -1)), False)
    return b.rows()


def field_sync(gpu_pct, cpu_pct, cells_w, cells_h, t):
    """Hazard stripes in solid blocks (no braille): diagonal warning bands sweep
    the loaded region — GPU's descend from the top, CPU's rise from the base,
    slopes opposed — with a dotted front line marking each level."""
    b = _Blocks(cells_w, cells_h)
    dw, dh = b.dw, b.dh
    if dw == 0 or dh == 0:
        return b.rows()
    mid = dh / 2.0
    drift = int(t * 6)

    def stripes(pct, down):
        depth = int(_frac(pct) * mid)
        if depth < 1:
            return
        key = grad_key(pct)
        for rel in range(depth):
            yi = rel if down else dh - 1 - rel
            for x in range(dw):
                d = (x + (yi + drift if down else -yi - drift)) % 7
                if d < 3:
                    b.set(x, yi, key if d < 2 else FRAME)
        yf = depth if down else dh - 1 - depth         # the level's front line
        for x in range(0, dw, 2):
            b.set(x, yf, FRAME)

    stripes(gpu_pct, True)
    stripes(cpu_pct, False)
    return b.rows()


FIELD_STYLES = [
    ("beat", beat_field_rows), ("helix", field_helix),
    ("rain", field_rain), ("wave", field_wave), ("sync", field_sync),
]
FIELD_IDX = 0


def field_rows(gpu_pct, cpu_pct, cells_w, cells_h, t):
    return FIELD_STYLES[FIELD_IDX][1](gpu_pct, cpu_pct, cells_w, cells_h, t)


def cycle_field(delta):
    global FIELD_IDX
    FIELD_IDX = (FIELD_IDX + delta) % len(FIELD_STYLES)
    save_prefs()


def set_field(name):
    global FIELD_IDX
    for i, (n, _) in enumerate(FIELD_STYLES):
        if n == (name or "").lower():
            FIELD_IDX = i
            return True
    return False


def draw_tower(put, rows_h, cols_w, gw, gh, tower_w, groups, t):
    """Draw the right-edge TOTALS column. A live beat-frequency interference field
    fills the whole interior (GPU falling from the top, CPU rising from the base,
    meeting mid-column); the dual concentric gauges (outer=GPU, inner=CPU, CPU% in
    the centre, GPU% below) are then composited on top, floating in the motion."""
    x0, bottom = cols_w - tower_w, rows_h - 1
    put(1, x0, "┌" + " TOTALS " + "─" * max(0, tower_w - 10) + "┐", FRAME)
    interior_h = bottom - 2                            # interior = absolute rows 2..bottom-1
    if interior_h < 1:
        put(bottom, x0, "└" + "─" * (tower_w - 2) + "┘", FRAME)
        return

    # 1) the backdrop — driven by the FLEET totals (groups[0]) as the ambient signal.
    g_all, c_all = (groups[0][1], groups[0][2]) if groups else (None, None)
    bg = field_rows(g_all, c_all, gw, interior_h, t)

    # 2) the foreground — labels + dials laid out exactly as before, but captured into
    #    a sparse grid so blank cells let the backdrop bleed through.
    fg = [[None] * gw for _ in range(interior_h)]

    def stamp(ri, runs):
        if not (0 <= ri < interior_h):
            return
        cx = 0
        for txt, k in runs:
            for chh in txt:
                if 0 <= cx < gw:
                    fg[ri][cx] = (chh, k)
                cx += 1

    n = len(groups)
    per = gh + 2                                       # label + dial + GPU% line
    gap = max(0, (interior_h - n * per) // (n + 1))    # even spacing above each gauge
    y = 0                                              # interior-relative row index
    for label, gpu_pct, cpu_pct in groups:
        y += gap
        stamp(y, _centered_runs(label, gw, LABEL))     # group header (FLEET / VAST)
        y += 1
        dial = dual_gauge_rows(gpu_pct, cpu_pct, gw, gh)
        dial[gh // 2] = _overlay_center(dial[gh // 2],
                                        f"{cpu_pct:.0f}%" if cpu_pct is not None else "··%",
                                        grad_key(cpu_pct))
        for drow in dial:
            stamp(y, drow)
            y += 1
        gtxt = f"GPU {gpu_pct:.0f}%" if gpu_pct is not None else "GPU ··"
        stamp(y, _centered_runs(gtxt, gw, grad_key(gpu_pct)))
        y += 1

    # 3) composite top-down: a foreground cell wins unless it is blank (space or the
    #    empty braille pattern), in which case the live field shows through.
    for ri in range(interior_h):
        put(2 + ri, x0, "│", FRAME)
        cx = x0 + 1
        for ci in range(gw):
            cell = fg[ri][ci]
            if cell is None or cell[0] == " " or cell[0] == _BEAT_BLANK:
                ch, k = bg[ri][ci]
            else:
                ch, k = cell
            put(2 + ri, cx, ch, k)
            cx += 1
        put(2 + ri, x0 + tower_w - 1, "│", FRAME)
    put(bottom, x0, "└" + "─" * (tower_w - 2) + "┘", FRAME)


def summary(snapshot):
    fleet = [n for n in snapshot.values() if n.get("source") != "vast"]
    vast = [n for n in snapshot.values() if n.get("source") == "vast"]
    fleet_ok = sum(1 for n in fleet if n.get("status") == "ok")
    burn = sum((n.get("dph") or 0) for n in vast if n.get("status") in ("ok", "api"))
    return fleet_ok, len(fleet), len(vast), burn


# A monitor must never crash — render failures degrade to an error tile + a logged
# traceback (~/.cache/fleet-overwatch-error.log) and an on-screen hint, never an exit.
LAST_ERR = None
ERR_LOG = os.path.expanduser("~/.cache/fleet-overwatch-error.log")


def _safe(fn, *a, **k):
    """Call fn, swallowing any exception (returns None on failure). The buffer that
    keeps every curses/terminal interaction from feeding a failure up the stack."""
    try:
        return fn(*a, **k)
    except Exception:
        return None


def _log_err(label, exc):
    global LAST_ERR
    LAST_ERR = f"{label}: {type(exc).__name__}: {exc}"[:200]
    try:
        os.makedirs(os.path.dirname(ERR_LOG), exist_ok=True)
        with open(ERR_LOG, "a") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {label}\n")
            f.write(traceback.format_exc())
    except OSError:
        pass


def safe_tile(node, compact=False):
    """build_tile, but a single bad node renders an error tile instead of crashing."""
    try:
        return build_tile(node, compact)
    except Exception as e:
        _log_err(f"tile {node.get('name', '?')}", e)
        nm = str(node.get("name", "?"))
        fill = max(1, TILE_W - len(nm) - 7)
        top = [(f"┌─ {nm} ", FRAME), ("─" * fill, FRAME), ("✗", CRIT), (" ┐", FRAME)]
        rows = [_pad([(" render error", CRIT)]), _pad([(" " + str(e)[:CW - 2], DIM)])]
        return top, rows, "└" + "─" * CW + "┘"


# ── Curses UI ───────────────────────────────────────────────────────────────
def ui(stdscr, interval, filter_names, want_fleet, want_vast, allow_attach):
    _safe(locale.setlocale, locale.LC_ALL, "")
    _safe(curses.curs_set, 0)
    has_color = False
    try:
        if curses.has_colors():
            curses.start_color()
            has_color = True
    except Exception:
        has_color = False
    bg = -1
    if has_color and _safe(curses.use_default_colors) is None:
        bg = curses.COLOR_BLACK  # terminal lacks default-color (-1) support
    use256 = has_color and curses.COLORS >= 256
    pairs = {}

    def setpair(key, col256, col8):
        if not has_color:
            return
        idx = pairs.get(key)                 # stable index per key: a scheme switch
        if idx is None:                      # re-inits the SAME pair, recoloring live
            idx = len(pairs) + 1
            pairs[key] = idx
        try:
            curses.init_pair(idx, col256 if use256 else col8, bg)
        except Exception:
            pass

    def apply_scheme():
        s = SCHEMES[SCHEME_IDX]
        b8, c8 = s["base8"], s["crit8"]
        setpair(TEXT, s["text"], b8)
        setpair(LABEL, s["label"], b8)       # bright accent (bold)
        setpair(FRAME, s["frame"], b8)       # dim borders
        setpair(DIM, 240, curses.COLOR_WHITE)  # muted grey, scheme-independent
        setpair(OK, s["ok"], b8)
        setpair(WARN, s["warn"], c8)
        setpair(CRIT, s["critc"], c8)
        for k, c in zip(RAMP_KEYS, s["ramp"]):   # body gradient 0..CRIT_AT
            setpair(k, c, b8)
        for k, c in zip(CRIT_KEYS, s["crit"]):   # redline at the top
            setpair(k, c, c8)

    apply_scheme()

    def attr(key):
        idx = pairs.get(key)
        a = curses.color_pair(idx) if idx else curses.A_NORMAL  # no-color -> plain
        if key == LABEL:
            a |= curses.A_BOLD
        if key in (DIM, FRAME):
            a |= curses.A_DIM
        return a

    def put(y, x, text, key):
        try:
            stdscr.addstr(y, x, text, attr(key))
        except Exception:   # curses.error AND e.g. UnicodeEncodeError on odd locales
            pass

    # Independent pollers: fleet at the chosen interval, Vast on a slower cadence
    # (it's network-bound + API-lagged). Each touches only its own tiles, so a slow
    # Vast scan can't stall or flicker the fleet, and vice-versa.
    stop = threading.Event()
    if want_fleet:
        threading.Thread(target=fleet_poller, args=(stop, interval, filter_names),
                         daemon=True).start()
    if want_vast:
        threading.Thread(target=vast_poller, args=(stop, max(8.0, interval), allow_attach),
                         daemon=True).start()
    def draw(snapshot, pinfo, rows_h, cols_w):
        fleet_ok, fleet_total, pods, burn = summary(snapshot)
        ts = time.strftime("%H:%M:%S", time.localtime(pinfo.get("ts") or time.time()))
        put(0, 0, "❬ FLEET OVERWATCH ❭", LABEL)
        dup = f" · {len(DUP_HIDDEN)}dup" if DUP_HIDDEN else ""
        gauge = f" ◔ {GAUGE_STYLES[GAUGE_IDX][0]} ◂v▸" if DUAL_GAUGE else ""
        status = (f"{fleet_ok}/{fleet_total} fleet · {pods} pods · ${burn:.2f}/hr{dup}"
                  f"   ▞ {BAR_THEMES[THEME_IDX][0]} ◂+/-▸ ▦ {SCHEMES[SCHEME_IDX]['name']} ◂c▸{gauge}"
                  f" ∿ {FIELD_STYLES[FIELD_IDX][0]} ◂f▸")
        clock_x = max(20, cols_w - 26)
        put(0, 20, status[:max(0, clock_x - 21)], TEXT)   # clip so the clock never clobbers it
        put(0, clock_x, f"{ts}  g:{'gauge' if DUAL_GAUGE else 'bars'} q:quit", DIM)
        if not snapshot:
            put(2, 0, "scanning fleet…  (first sweep may take a few seconds)", DIM)
        # Reserve a full-height column on the right for the dial tower; tiles fill only
        # the left region, so they can never collide with it.
        tg = tower_geom(snapshot, rows_h, cols_w)
        left_w = (cols_w - tg[2] - 1) if tg else cols_w     # tg[2] = tower_w (+1 gutter)
        per_row = max(1, (left_w + 1) // (TILE_W + 1))
        sec_w = max(0, min(left_w, per_row * (TILE_W + 1) - 1))

        def plan(compact):
            secs, h = [], 2
            for title, members in grouped(snapshot):
                rows = [[safe_tile(n, compact) for n in members[i:i + per_row]]
                        for i in range(0, len(members), per_row)]
                secs.append((title, rows))
                h += 1 + sum(max(len(c) + 2 for _, c, _ in r) for r in rows) + 1
            return secs, h

        secs, total_h = plan(False)
        if total_h > rows_h:                 # too tall to fit -> drop per-core strips
            secs, _ = plan(True)

        def draw_row(built, y):
            rowh = max(len(c) + 2 for _, c, _ in built)
            for col, (top, content, bottom) in enumerate(built):
                gx = col * (TILE_W + 1)
                cx = gx
                for t, k in top:
                    put(y, cx, t, k)
                    cx += len(t)
                for j in range(rowh - 2):              # uniform height: pad tiles to rowh
                    put(y + 1 + j, gx, "│", FRAME)
                    if j < len(content):
                        cx = gx + 1
                        for t, k in content[j]:
                            put(y + 1 + j, cx, t, k)
                            cx += len(t)
                    put(y + 1 + j, gx + TILE_W - 1, "│", FRAME)
                put(y + rowh - 1, gx, bottom, FRAME)
            return rowh

        def draw_section(title, rows, y, limit):
            if y + 1 > limit:
                return y
            put(y, 0, f"── {title} " + "─" * max(0, sec_w - len(title) - 4), DIM)
            y += 1
            for built in rows:
                rowh = max(len(c) + 2 for _, c, _ in built)
                if y + rowh > limit:
                    break
                draw_row(built, y)
                y += rowh
            return y + 1

        # GUARANTEE the VAST PODS section is on screen: reserve its height at the bottom
        # and render fleet above it. If anything has to clip, it's fleet — never the pods.
        def sec_h(s):
            return 1 + sum(max(len(c) + 2 for _, c, _ in r) for r in s[1]) + 1
        vast_sec = next((s for s in secs if s[0] == "VAST PODS"), None)
        vast_h = 0
        if vast_sec:
            # Reserve what vast needs, but NEVER so much that fleet collapses: always
            # keep the fleet header + its first tile-row on screen. (vast clips before
            # it's allowed to swallow the whole view.)
            fleet_secs = [s for s in secs if s[0] != "VAST PODS" and s[1]]
            fleet_keep = 0
            if fleet_secs:                            # global header(2) + section header(1) + first row
                fleet_keep = 3 + max(len(c) + 2 for _, c, _ in fleet_secs[0][1][0])
            vast_h = min(sec_h(vast_sec), max(3, rows_h - fleet_keep))
        y = 2
        for s in secs:
            if s[0] == "VAST PODS":
                continue
            y = draw_section(s[0], s[1], y, rows_h - vast_h)
        if vast_sec:
            draw_section(vast_sec[0], vast_sec[1], max(y, rows_h - vast_h), rows_h)
        global LIVE_TOWER
        LIVE_TOWER = bool(tg)
        if tg:
            draw_tower(put, rows_h, cols_w, *tg, ANIM_T)
        if LAST_ERR:
            put(rows_h - 1, 0, ("⚠ " + LAST_ERR)[:max(0, cols_w - 1 - (tg[2] if tg else 0))], CRIT)

    try:
        while True:
            # Fast redraw only while an animated theme is active; otherwise stay calm so
            # we don't flood the terminal with full-screen repaints (can choke WSL/remote).
            _safe(stdscr.timeout, 110 if (theme_is_animated() or LIVE_TOWER) else 500)
            rows_h, cols_w = 24, 80
            try:
                _set_anim(time.time())
                with LOCK:
                    snapshot = dict(LATEST)
                    pinfo = dict(POLL_INFO)
                rows_h, cols_w = stdscr.getmaxyx()
                stdscr.erase()
                draw(snapshot, pinfo, rows_h, cols_w)
            except Exception as e:                  # never let a frame kill the panel
                _log_err("render", e)
                _safe(stdscr.addstr, 0, 0,
                      ("overwatch render error (logged): " + str(e))[:max(1, cols_w - 1)])
            ch = _safe(stdscr.getch)
            if ch in (ord("q"), ord("Q"), 27):
                break
            if ch in (ord("+"), ord("=")):
                cycle_theme(1)
            elif ch in (ord("-"), ord("_")):
                cycle_theme(-1)
            elif ch == ord("c"):
                cycle_scheme(1)
                apply_scheme()
            elif ch == ord("C"):
                cycle_scheme(-1)
                apply_scheme()
            elif ch == ord("v"):
                cycle_gauge(1)
            elif ch == ord("V"):
                cycle_gauge(-1)
            elif ch == ord("f"):
                cycle_field(1)
            elif ch == ord("F"):
                cycle_field(-1)
            elif ch in (ord("g"), ord("G")):
                toggle_dual()
            _safe(stdscr.refresh)
    finally:
        stop.set()


# ── Plain-text one-shot (debug / verification) ──────────────────────────────
def render_plain(snapshot):
    lines = []
    fleet_ok, fleet_total, pods, burn = summary(snapshot)
    lines.append(f"❬ FLEET OVERWATCH ❭  {fleet_ok}/{fleet_total} fleet · {pods} pods · "
                 f"${burn:.2f}/hr  {time.strftime('%H:%M:%S')}")
    for title, members in grouped(snapshot):
        lines.append("")
        lines.append(f"── {title} ──")
        for node in members:
            top, content, bottom = safe_tile(node)
            lines.append("".join(t for t, _ in top))
            for line in content:
                lines.append("│" + "".join(t for t, _ in line) + "│")
            lines.append(bottom)
    if any(n.get("source") == "fleet" for n in snapshot.values()):
        lines.append("")
        lines.append("── TOTALS ──")
        for label, gpu, cpu in gauge_groups(snapshot):
            g = f"{gpu:.1f}%" if gpu is not None else "··"
            c = f"{cpu:.1f}%" if cpu is not None else "··"
            lines.append(f"  {label:<6} GPU {g:>7}   CPU {c:>7}")
    return "\n".join(lines)


def plain_loop(interval, filter_names, want_fleet, want_vast, allow_attach):
    """Non-curses fallback: ANSI clear + render_plain on a timer. Never touches curses,
    so it survives terminals where the TUI can't init. Ctrl-C to quit."""
    try:
        while True:
            try:
                poll_once(filter_names, want_fleet, want_vast, allow_attach)
                with LOCK:
                    snap = dict(LATEST)
                sys.stdout.write("\033[2J\033[H" + render_plain(snap) + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                break                       # output went away -> exit, don't spin
            except Exception as e:
                _log_err("plain", e)
            time.sleep(max(1.0, interval))
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="Fleet Overwatch — tailnet + Vast GPU/CPU panel")
    ap.add_argument("--config", default=None,
                    help="JSON config path (default: $FLEET_OVERWATCH_CONFIG, "
                         "./overwatch.config.json, ~/.config/fleet-overwatch/config.json)")
    ap.add_argument("--interval", type=float, default=2.5, help="refresh seconds (default 2.5)")
    ap.add_argument("--once", action="store_true", help="print one plain frame and exit")
    ap.add_argument("--nodes", default="", help="comma-separated subset of fleet node names")
    ap.add_argument("--only", choices=["fleet", "vast"], default=None, help="show only one source")
    ap.add_argument("--no-vast", action="store_true", help="hide Vast pods")
    ap.add_argument("--theme", default=None,
                    help="bar theme (also +/- in the TUI): " + ", ".join(n for n, _ in BAR_THEMES))
    ap.add_argument("--scheme", default=None,
                    help="color scheme (also c in the TUI): " + ", ".join(s["name"] for s in SCHEMES))
    ap.add_argument("--gauge", default=None,
                    help="gauge style (also v in the TUI): " + ", ".join(n for n, _, _ in GAUGE_STYLES))
    ap.add_argument("--field", default=None,
                    help="TOTALS field style (also f in the TUI): " + ", ".join(n for n, _ in FIELD_STYLES))
    ap.add_argument("--no-vast-attach", action="store_true", help="disable auto-heal key attach")
    ap.add_argument("--vast-attach", action="store_true",
                    help="attach fleet key to all running pods, then exit")
    args = ap.parse_args()
    if args.config:
        if not os.path.exists(args.config):
            print(f"error: config not found: {args.config}", file=sys.stderr)
            return 1
        apply_config(args.config)
    load_prefs()                              # restore last theme/scheme/gauge choice
    if args.theme and not set_theme(args.theme):
        print(f"error: unknown theme {args.theme!r}; available: "
              + ", ".join(n for n, _ in BAR_THEMES), file=sys.stderr)
        return 1
    if args.scheme and not set_scheme(args.scheme):
        print(f"error: unknown scheme {args.scheme!r}; available: "
              + ", ".join(s["name"] for s in SCHEMES), file=sys.stderr)
        return 1
    if args.gauge and not set_gauge(args.gauge):
        print(f"error: unknown gauge {args.gauge!r}; available: "
              + ", ".join(n for n, _, _ in GAUGE_STYLES), file=sys.stderr)
        return 1
    if args.field and not set_field(args.field):
        print(f"error: unknown field {args.field!r}; available: "
              + ", ".join(n for n, _ in FIELD_STYLES), file=sys.stderr)
        return 1
    filter_names = set(n.strip().lower() for n in args.nodes.split(",") if n.strip()) or None
    want_fleet = args.only != "vast"
    want_vast = (not args.no_vast) and args.only != "fleet"
    allow_attach = not args.no_vast_attach

    # Standalone heal pass: attach the fleet key to every running pod, then exit.
    if args.vast_attach:
        if not find_vastai():
            print("error: vastai CLI not found", file=sys.stderr)
            return 1
        pub = fleet_pubkey()
        if not pub:
            print(f"error: no fleet key at {VAST_PUB} — run ./vast-setup.sh first", file=sys.stderr)
            return 1
        pods = [p for p in (vast_discover() or []) if p["online"]]
        if not pods:
            print("no running pods to attach")
            return 0
        for p in pods:
            vast_attach(p["id"])
            print(f"attached fleet key to pod {p['id']} ({p['num_gpus']}x {p['model']})")
        return 0

    if want_fleet and not find_tailscale():
        print("warning: tailscale CLI not found; fleet section will be empty "
              "(set \"tailscale_paths\" in the config if it lives somewhere unusual).",
              file=sys.stderr)

    if args.once:
        # Two polls so CPU% has a delta to report on the second frame.
        poll_once(filter_names, want_fleet, want_vast, allow_attach)
        time.sleep(min(1.5, args.interval))
        poll_once(filter_names, want_fleet, want_vast, allow_attach)
        with LOCK:
            snapshot = dict(LATEST)
        print(render_plain(snapshot))
        return 0

    # Try the curses TUI; if the terminal can't init it, fall back to plain
    # auto-refresh rather than crashing. Either way the cause is logged.
    try:
        curses.wrapper(ui, args.interval, filter_names, want_fleet, want_vast, allow_attach)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        _log_err("curses", e)
        sys.stderr.write(
            f"overwatch: curses UI failed ({type(e).__name__}: {e}); falling back to plain "
            f"auto-refresh (Ctrl-C to quit). Traceback logged to {ERR_LOG}\n")
        time.sleep(1.5)
        plain_loop(args.interval, filter_names, want_fleet, want_vast, allow_attach)
    return 0


if __name__ == "__main__":
    sys.exit(main())
