<img width="1672" height="941" alt="gpu_overwatch" src="https://github.com/user-attachments/assets/335b2aa5-bbf9-48d1-ad7b-045413098c01" />
# Fleet Overwatch

Author: Frosty40

A terminal "overwatch" panel showing live **GPU/CPU activity across a
Tailscale network** plus optional **Vast.ai GPU pods**. It is built for
operators juggling local Linux machines and ephemeral cloud rentals.

One process discovers tailnet devices (from `tailscale status`) and Vast pods
(from the vastai API), polls each over SSH in parallel, and draws a
refreshing grid of per-machine tiles grouped into **FLEET** and **VAST PODS**
sections, with a fleet-wide **TOTALS** dial column. Offline / unreachable nodes
render greyed. Pure Python **stdlib** — no pip, no daemons on the other machines.

```
❬ FLEET OVERWATCH ❭   5/7 fleet · 2 pods · $0.61/hr   ▞ smooth ◂+/-▸ ▦ phosphor ◂c▸

┌─ node-a ─────────────● ┐ ┌─ node-b ─────────────● ┐
│ RTX 3090               │ │ RTX 4080               │
│ GPU  ██░░░░░░░░   22%   │ │ GPU  ░░░░░░░░░░    0%   │
│ VRAM █░░░░░  1.9/24G    │ │ VRAM █░░░░░  0.1/16G    │
│ 58°C     44W           │ │ 34°C      4W           │
│ CPU  4% 32c ld0.1      │ │ CPU 18% 24c ld4.3      │
│ RAM  2/47G up23h       │ │ RAM  4/15G up3h         │
└────────────────────────┘ └────────────────────────┘
```
<img width="1920" height="1032" alt="fleet_ops" src="https://github.com/user-attachments/assets/4a472368-5041-4505-b534-0e3e69216474" />
<img width="1672" height="941" alt="overwatch_1" src="https://github.com/user-attachments/assets/8a87a717-3dfb-4639-8b9d-fedd5d53f957" />
<img width="1672" height="941" alt="gpu_2" src="https://github.com/user-attachments/assets/f0cf86bc-6431-43a4-ad5a-3fdbffb7caf7" />

## Looks

The renderer uses braille/block terminal graphics. Everything is cycled live
and persists across runs (`~/.config/fleet-overwatch/prefs.json`):

- **24 bar themes** (`+`/`-`): gradient, smooth (⅛-cell edges), shaded,
  scanline, signal, plasma, spectrum, wave, segmented, pulse, double, brick,
  rocket, dither, typer, comet, heartbeat, waterfall, a tech set —
  matrix, packets, glitch, hex, circuit — and hazard warning stripes
- **8 color schemes** (`c`/`C`): phosphor (default), ember, cyber, ice,
  rainbow, amber, vapor, mono — each keeps the "calm body, redline ≥90%" ramp
- **24 gauge dial styles** (`v`/`V`): braille dials — rings, speedo, half, vu,
  donut, segments, signal, thermo, pressure, clock — a square family with
  inset fills — frame, inset, brackets, grid — an angular Eva-HUD set
  rendered in SOLID quadrant blocks (no braille) with polygon liquid fills —
  nerv (hexagon), magi (tri-core), octa (targeting octagon), delta (warning
  triangle), wings (slanted HUD slabs), pylon (twin angled columns) — and a
  pixel-perfect CLEAN series (hard angles, zero diagonals: every edge lands
  on a half-cell boundary) — bastion (corner-notched vessel), ziggurat
  (stepped pyramid), citadel (rectilinear MAGI), bays (stepped twin slabs).
  Every dial shows GPU + CPU together
- **5 TOTALS field styles** (`f`/`F`) animating the right-hand tower backdrop:
  beat (beat-frequency interference), helix (rotating DNA), rain (code rain),
  wave (standing waves), sync (hazard stripes) — GPU grows down from the top,
  CPU up from the base
- `g` toggles between the gauge view and the classic metric-bars view

Start with a chosen look:
`./overwatch.py --theme circuit --scheme cyber --gauge grid --field helix`

## Quickstart

```bash
git clone <this-repo> && cd overwatch
./overwatch.py --once        # one plain-text frame — verify discovery/probing
./overwatch.py               # live TUI (q quits)
```

Optional local install:

```bash
install -Dm755 overwatch ~/.local/bin/overwatch
overwatch --once
```

With **zero configuration** it probes every Linux tailnet device as the current
login user. Normal SSH configuration still applies. To pin per-node SSH users
and display preferences, copy the safe starter config:

```bash
mkdir -p ~/.config/fleet-overwatch
cp config.example.json ~/.config/fleet-overwatch/config.json
$EDITOR ~/.config/fleet-overwatch/config.json
```

Additional boilerplate templates are available:

```bash
cp templates/fleet-basic.json ~/.config/fleet-overwatch/config.json
cp templates/vast-only.json ~/.config/fleet-overwatch/config.json
```

Config search order: `--config PATH` → `$FLEET_OVERWATCH_CONFIG` →
`./overwatch.config.json` → `~/.config/fleet-overwatch/config.json`.
All keys are optional:

| key               | meaning                                                              |
|-------------------|----------------------------------------------------------------------|
| `ssh_users`       | `{node-name: login}` — the one fact Tailscale can't supply            |
| `default_user`    | login for unlisted nodes (`null` = ssh's own default)                |
| `gpu_labels`      | `{node-name: label}` cosmetic GPU name when no tool is queryable      |
| `exclude`         | tailnet device names to never show                                    |
| `order`           | preferred tile order; unknown nodes appended alphabetically           |
| `vast_key`        | SSH key for Vast pods (default `~/.ssh/fleet_overwatch_ed25519`)      |
| `vast_user`       | SSH login for Vast pods (usually `root`)                              |
| `tailscale_paths` | candidate tailscale binaries (default covers PATH + WSL exe paths)    |
| `vastai_paths`    | candidate vastai binaries (default: PATH, `~/.local/bin/vastai`)      |

## Flags

```
--once               one plain-text frame, then exit (debug/verify)
--interval N         refresh seconds (default 2.5)
--nodes a,b          subset of fleet nodes
--only fleet|vast    one source only        --no-vast    hide pods
--theme/--scheme/--gauge/--field NAME   start with a chosen look
--config PATH        explicit config file
--vast-attach        attach the fleet key to all running pods, then exit
--no-vast-attach     disable the auto-heal attach pass
```

## How it works

- **Tailscale-based, end to end.** Discovery, liveness and addressing all come
  from `tailscale status --json`; each node's SSH host is its tailnet IP, read
  live. No LAN IPs, no hardcoded hostnames. WSL setups work out of the box —
  the Windows `tailscale.exe` is found automatically.
- **Collection (pull).** For each online device, one SSH round-trip runs a
  small POSIX-shell probe (`nvidia-smi --query-gpu`, `/proc/stat`,
  `/proc/loadavg`, `/proc/meminfo`, `/proc/uptime`) in a thread pool with
  per-node timeouts — one slow/dead node never freezes the panel. The local
  machine probes itself directly; Windows peers are shown but not probed.
- **CPU% is a delta** between consecutive `/proc/stat` reads, never a
  since-boot average; a per-core heat strip shows every core's real delta.
- **GPU support:** NVIDIA via `nvidia-smi` (util/VRAM/temp/power, multi-GPU).
  Intel Arc/Xe via a built-in sysfs reader (RC6 idle-residency -> util, hwmon
  energy -> power, BAR2 -> VRAM total; no elevated privileges or extra tools).
  Machines with neither show CPU/RAM and an honest "no GPU metrics" label.
- **Vast.ai pods:** discovered via `vastai show instances-v1` ($/hr, model,
  location, burn-rate total in the header). Live per-GPU stats come over SSH
  using Vast's direct connection; pod-scoped CPU/RAM come from the API
  (inside Vast containers `/proc` is host-wide and would lie). Tiles are
  labeled with their data source so API-lagged numbers are never mistaken for
  live ones.
- **Never crashes.** Render failures degrade to an error tile and a logged
  traceback (`~/.cache/fleet-overwatch-error.log`); terminals that can't init
  curses get a plain auto-refreshing fallback.

## Vast.ai one-time key setup — `./vast-setup.sh`

Vast does not auto-inject account SSH keys into new pods, so fresh pods may be
unreachable by default. `vast-setup.sh` generates one passphrase-less fleet key,
registers it with Vast, optionally distributes it to nodes listed in local
config, and attaches it to running pods. After that, overwatch can auto-heal:
any pod it cannot SSH into gets the key attached automatically on the next scan.

> **Security tradeoff:** the fleet key is passphrase-less and can be copied to
> configured machines so the panel can probe non-interactively. Treat it as a
> low-value credential for ephemeral rentals and do not reuse it elsewhere.
> Host-key checking is disabled for Vast pods only because they are ephemeral.

## Requirements

- Python 3.8+ on the machine running the panel; plain POSIX shell + `/proc`
  on the probed machines (any Linux).
- Non-interactive SSH to each node — Tailscale SSH or keys/agent.
- Optional: `vastai` CLI (`pipx install vastai`) with an API key for the
  pods section; `tailscale` for the fleet section.
- A monospace font with box-drawing (`─│┌┐└┘`), block (`█▉▆░`) and braille
  (`⠿`) glyphs.

## License

MIT — see [LICENSE](LICENSE).

```text
⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⠤⠚⠓⠤⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⡾⣅⠀⠀⠀⠀⣨⢷⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⣀⡤⡦⣄⡀⠀⡇⠀⠙⢢⡔⠋⠀⢸⠀⢀⣠⢴⢤⣀⠀⠀
⡴⠊⠁⠀⢷⠀⠉⠲⣇⠀⠀⢸⡇⠀⠀⣸⠖⠉⠀⡼⠀⠈⠑⢦
⣧⠀⠀⢀⣸⡀⠀⠀⢿⠙⠲⢼⣧⠖⠋⡿⠀⠀⢀⣇⡀⠀⠀⣸
⢹⡴⠚⠉⠀⠈⠑⠦⣼⠀⠀⢸⡇⠀⠀⣧⡴⠊⠁⠀⠉⠓⢦⡏
⠀⠈⠓⢤⣀⡤⠖⠋⠁⠙⠲⣼⣧⠖⠋⠈⠙⠲⢤⣀⡤⠚⠁⠀
⢀⡠⠖⠉⠀⠉⠓⠦⣄⠴⠚⢹⡏⠓⠦⣠⡴⠚⠉⠀⠉⠲⢄⡀
⣼⠙⠲⢤⣀⡠⠔⠋⢹⠀⠀⣸⣇⠀⠀⣏⠙⠲⢄⣀⡤⠖⠋⣧
⡏⠀⠀⠀⢸⠀⠀⠀⣿⠴⠚⢹⡏⠓⠦⣿⠀⠀⠀⡇⠀⠀⠀⢸
⠙⠢⣄⡀⡟⣀⡤⠚⡇⠀⠀⢸⡇⠀⠀⢸⠓⠤⣀⢹⢀⣠⠔⠋
⠀⠀⠀⠉⠋⠁⠀⠀⡇⣀⠴⠊⠑⠦⣀⢸⠀⠀⠈⠙⠉⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠻⣅⠀⠀⠀⠀⣨⠟⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠲⠖⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
```
