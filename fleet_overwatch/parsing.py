"""Parsers for nvidia-smi, rocm-smi, and `tailscale status --json`.

The remote probe is a POSIX shell script. It prints tool stdout between
markers and returns. All interpretation happens here, on the machine running
the panel, so tests can feed captured command output without GPUs or a tailnet.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

# Documentation-only sentinels nvidia-smi and rocm-smi print for unsupported fields.
_NA = {
    "",
    "n/a",
    "na",
    "[n/a]",
    "[not supported]",
    "not supported",
    "none",
    "-",
    "unknown",
}

_DOWN_MARKERS = (
    "timed out",
    "timeout",
    "time out",
    "no route to host",
    "no route",
    "network is unreachable",
    "network unreachable",
    "connection refused",
    "connection reset",
    "connection closed",
    "host is down",
    "host down",
    "broken pipe",
    "operation timed out",
    "could not resolve",
    "name or service not known",
    "temporary failure in name resolution",
)

_GPU_KV = re.compile(r"GPU\[(\d+)\]\s*:\s*(.*?)\s*:\s*(.*?)\s*$")


def short_gpu_name(name: str) -> str:
    """Drop the noisy vendor prefix nvidia-smi puts on GeForce cards."""
    text = re.sub(r"^NVIDIA\s+GeForce\s+", "", (name or "").strip())
    text = re.sub(r"^NVIDIA\s+", "", text)
    return re.sub(r"\s+GPU$", "", text).strip()


def _num(value: str | None) -> float | None:
    """Parse a metric cell. Unsupported / blank cells become None, never an exception."""
    if value is None:
        return None
    text = value.strip().strip("[]")
    text = re.sub(r"(?i)\s*(mib|mb|gib|gb|%|w|c)$", "", text).strip()
    if text.lower() in _NA:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_nvidia_smi(text: str) -> list[dict[str, Any]]:
    """Parse `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits`.

    Memory values are mebibytes, temperature is Celsius, power is watts.
    `[N/A]` and blank cells are missing data, not zeros.
    """
    gpus: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("="):
            continue
        if line.lower().startswith("name,"):
            continue
        fields = [cell.strip() for cell in line.split(",")]
        if len(fields) < 6:
            continue
        if fields[1].lower() in {"utilization.gpu", "gpu"}:
            continue
        gpus.append({
            "name": short_gpu_name(fields[0]) or "GPU",
            "util": _num(fields[1]),
            "mem_used": _num(fields[2]),
            "mem_total": _num(fields[3]),
            "temp": _num(fields[4]),
            "power": _num(fields[5]),
        })
    return gpus


def _split_csv(line: str) -> list[str]:
    return [cell.strip() for cell in line.split(",")]


def _column(header: dict[str, int], *keys: str) -> int | None:
    for key in keys:
        if key in header:
            return header[key]
    for key in keys:
        for name, idx in header.items():
            if key in name:
                return idx
    return None


def _cell(header: dict[str, int], cells: Sequence[str], *keys: str) -> str:
    idx = _column(header, *keys)
    if idx is None or idx >= len(cells):
        return ""
    return cells[idx].strip()


def _named_gpu(series: str, model: str) -> str:
    parts = []
    for piece in (series, model):
        text = piece.strip()
        if text and text.lower() not in _NA:
            parts.append(text)
    return short_gpu_name(" ".join(parts)) or "AMD GPU"


def _gpu_from_rocm_row(header: dict[str, int], cells: Sequence[str]) -> dict[str, Any] | None:
    name = _named_gpu(
        _cell(header, cells, "card series"),
        _cell(header, cells, "card model"),
    )
    util = _num(_cell(header, cells, "gpu use (%)"))
    temp = _num(_cell(
        header, cells,
        "temperature (sensor edge) (c)",
        "temperature (sensor junction) (c)",
        "temperature (sensor memory) (c)",
    ))
    power = _num(_cell(
        header, cells,
        "average graphics package power (w)",
        "current socket graphics package power (w)",
        "average graphics package power",
    ))
    used_b = _num(_cell(header, cells, "vram total used memory (b)"))
    total_b = _num(_cell(header, cells, "vram total memory (b)"))
    mem_used = used_b / 1048576.0 if used_b is not None else None
    mem_total = total_b / 1048576.0 if total_b is not None else None
    if name == "AMD GPU" and util is None and temp is None and power is None and mem_total is None:
        return None
    return {
        "name": name,
        "util": util,
        "mem_used": mem_used,
        "mem_total": mem_total,
        "temp": temp,
        "power": power,
    }


def _parse_rocm_csv(text: str) -> list[dict[str, Any]] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header_at: int | None = None
    for index, line in enumerate(lines):
        lower = line.lower()
        if "," in line and "gpu use" in lower:
            header_at = index
            break
    if header_at is None:
        return None
    header = {name: index for index, name in enumerate(_split_csv(lines[header_at].lower()))}
    gpus: list[dict[str, Any]] = []
    for line in lines[header_at + 1:]:
        lower = line.lower()
        if line.startswith("=") or "end of rocm" in lower:
            break
        if "," not in line:
            continue
        parsed = _gpu_from_rocm_row(header, _split_csv(line))
        if parsed is not None:
            gpus.append(parsed)
    return gpus


def _parse_rocm_key_value(text: str) -> list[dict[str, Any]] | None:
    grouped: dict[int, dict[str, str]] = {}
    for line in text.splitlines():
        match = _GPU_KV.search(line.strip())
        if not match:
            continue
        index = int(match.group(1))
        grouped.setdefault(index, {})[match.group(2).strip().lower()] = match.group(3).strip()
    if not grouped:
        return None
    gpus: list[dict[str, Any]] = []
    for index in sorted(grouped):
        fields = grouped[index]
        used_b = _num(fields.get("vram total used memory (b)", ""))
        total_b = _num(fields.get("vram total memory (b)", ""))
        gpus.append({
            "name": _named_gpu(fields.get("card series", ""), fields.get("card model", "")),
            "util": _num(fields.get("gpu use (%)", "")),
            "mem_used": used_b / 1048576.0 if used_b is not None else None,
            "mem_total": total_b / 1048576.0 if total_b is not None else None,
            "temp": _num(
                fields.get("temperature (sensor edge) (c)")
                or fields.get("temperature (sensor junction) (c)")
                or ""
            ),
            "power": _num(
                fields.get("average graphics package power (w)")
                or fields.get("current socket graphics package power (w)")
                or ""
            ),
        })
    return gpus


def _parse_rocm_concise(text: str) -> list[dict[str, Any]] | None:
    """The default `rocm-smi` table: index, temp, power, and a trailing GPU%."""
    lines = text.splitlines()
    header_at: int | None = None
    for index, line in enumerate(lines):
        if "GPU%" in line and "VRAM%" in line and "Temp" in line:
            header_at = index
            break
    if header_at is None:
        return None
    gpus: list[dict[str, Any]] = []
    for line in lines[header_at + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("="):
            break
        parts = stripped.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        percents = [part for part in parts if part.endswith("%")]
        watts = next((part for part in parts if part.lower().endswith("w") and _num(part) is not None), "")
        temp = next((part for part in parts if part.lower().endswith("c") and _num(part) is not None), "")
        gpus.append({
            "name": f"AMD GPU {parts[0]}",
            "util": _num(percents[-1]) if percents else None,
            "mem_used": None,
            "mem_total": None,
            "temp": _num(temp),
            "power": _num(watts),
        })
    return gpus or None


def parse_rocm_smi(text: str) -> list[dict[str, Any]]:
    """Parse rocm-smi CSV, `GPU[n] : key: value` output, or the concise table.

    Byte memory counters are converted to mebibytes so a ROCm card matches the
    nvidia-smi records the rest of the panel already draws.
    """
    if not text or not text.strip():
        return []
    csv_gpus = _parse_rocm_csv(text)
    if csv_gpus:
        return csv_gpus
    kv_gpus = _parse_rocm_key_value(text)
    if kv_gpus:
        return kv_gpus
    concise = _parse_rocm_concise(text)
    return concise or []


def parse_probe_sections(text: str) -> dict[str, list[str]]:
    """Split marker-delimited probe stdout into named sections."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        match = re.match(r"^==(\w+)==$", line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _cpu_busy_total(cpu_line: str) -> tuple[int, int] | None:
    # "cpu  user nice system idle iowait irq softirq steal guest guest_nice"
    parts = cpu_line.split()
    values = [int(piece) for piece in parts[1:] if piece.isdigit()]
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    total = sum(values)
    return total - idle, total


def metrics_from_sections(sections: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """Turn one probe's sections into GPU samples plus CPU and memory fields."""
    kind = ((sections.get("GPUKIND") or ["none"])[0] or "none").strip()
    raw_gpu = "\n".join(sections.get("GPU") or [])
    if kind.lower() == "rocm":
        gpus = parse_rocm_smi(raw_gpu)
    else:
        gpus = parse_nvidia_smi(raw_gpu)

    aggregate: tuple[int, int] | None = None
    cores: list[tuple[int, int]] = []
    for line in sections.get("CPU") or []:
        parts = line.split()
        if not parts or not parts[0].startswith("cpu"):
            continue
        sample = _cpu_busy_total(line)
        if parts[0] == "cpu":
            aggregate = sample
        elif parts[0][3:].isdigit() and sample:
            cores.append(sample)

    ncpu = ((sections.get("NPROC") or [""])[0] or "").strip()
    load = ((sections.get("LOAD") or [""])[0] or "").split()[:3]
    memory: dict[str, int] = {}
    for line in sections.get("MEM") or []:
        match = re.match(r"(\w+):\s+(\d+)", line)
        if match:
            memory[match.group(1)] = int(match.group(2))
    uptime = ((sections.get("UPTIME") or [""])[0] or "").strip()
    return {
        "gpukind": kind or "none",
        "gpus": gpus,
        "cpu_bt": aggregate,
        "core_bt": cores,
        "ncpu": int(ncpu) if ncpu.isdigit() else None,
        "load": [float(piece) for piece in load] if load else None,
        "mem_total_kb": memory.get("MemTotal"),
        "mem_avail_kb": memory.get("MemAvailable"),
        "uptime_s": float(uptime) if re.match(r"^[\d.]+$", uptime) else None,
    }


def _device_name(node: Mapping[str, Any]) -> str:
    dns = str(node.get("DNSName") or "").strip(".")
    if dns:
        return dns.split(".")[0].lower()
    return str(node.get("HostName") or "?").lower()


def _tailscale_ip(node: Mapping[str, Any]) -> str:
    ips = node.get("TailscaleIPs")
    if isinstance(ips, (list, tuple)) and ips:
        return str(ips[0])
    if isinstance(ips, str):
        return ips
    return ""


def devices_from_tailscale_status(
    data: Mapping[str, Any],
    *,
    exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Decode `tailscale status --json` into fleet device records.

    A peer that is present but offline stays in the list with ``online`` false
    so the panel can draw it as down instead of dropping it. Names in
    ``exclude`` (matched on the short DNS label) are omitted. Malformed peer
    records are skipped.
    """
    skipped = {str(name).lower() for name in (exclude or set())}
    self_node = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    self_name = _device_name(self_node) if self_node else None
    peers = data.get("Peer") if isinstance(data.get("Peer"), dict) else {}
    devices: list[dict[str, Any]] = []
    nodes: list[Any] = [self_node, *peers.values()]
    for node in nodes:
        if not isinstance(node, dict) or not node:
            continue
        name = _device_name(node)
        if name in skipped:
            continue
        devices.append({
            "name": name,
            "source": "fleet",
            "host": node.get("HostName"),
            "ip": _tailscale_ip(node),
            "os": node.get("OS"),
            "online": bool(node.get("Online")) or (name == self_name),
            "is_self": name == self_name,
        })
    return devices


def classify_probe_error(exc: BaseException) -> dict[str, str]:
    """Map an SSH or local-probe failure to a short tile status.

    Connection failures become ``down`` (the machine is not answering).
    Authentication and unexpected failures become ``unreachable``. The returned
    ``err`` is a fixed phrase so a host's address or a long stderr line never
    lands on the tile.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        return {"status": "down", "err": "timeout"}
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in text for marker in _DOWN_MARKERS):
        if "refused" in text:
            return {"status": "down", "err": "connection refused"}
        if "timed out" in text or "timeout" in text or "time out" in text:
            return {"status": "down", "err": "timeout"}
        return {"status": "down", "err": "host down"}
    if "permission denied" in text or "authentication" in text:
        return {"status": "unreachable", "err": "auth failed"}
    if isinstance(exc, FileNotFoundError):
        return {"status": "unreachable", "err": "ssh missing"}
    return {"status": "unreachable", "err": "no reply"}
