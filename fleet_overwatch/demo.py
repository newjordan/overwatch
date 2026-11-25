"""A fictional fleet so the panel can run with no tailnet, SSH, or GPUs.

Every name is a placeholder. Every address is in TEST-NET-1 (192.0.2.0/24,
RFC 5737), which is reserved for documentation and is not a real network.
"""

from __future__ import annotations

import math
from typing import Any

# RFC 5737 documentation range. These addresses are not routable hosts.
_DOC_NET = "192.0.2."


def _blank(**fields: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "status": "offline",
        "gpus": [],
        "cpu_pct": None,
        "core_pct": None,
        "ncpu": None,
        "load": None,
        "mem_total_kb": None,
        "mem_avail_kb": None,
        "uptime_s": None,
        "gpukind": "none",
        "gpu_label": None,
        "detail": None,
        "err": None,
        "source": "fleet",
        "host": None,
        "ip": "",
        "os": "linux",
        "online": False,
        "is_self": False,
    }
    node.update(fields)
    return node


def _gib(amount: float) -> int:
    return int(amount * 1024 * 1024)


def _wave(base: float, tick: int, phase: float) -> float:
    """Hold the published sample on tick 0; later ticks drift so the TUI moves."""
    if tick <= 0:
        return base
    return max(0.0, min(100.0, base + 7.0 * math.sin(tick / 2.5 + phase)))


def _gpu(name: str, util: float, used_mib: float, total_mib: float,
         temp: float, power: float) -> dict[str, Any]:
    return {
        "name": name,
        "util": util,
        "mem_used": used_mib,
        "mem_total": total_mib,
        "temp": temp,
        "power": power,
    }


def build_demo_nodes(tick: int = 0) -> dict[str, dict[str, Any]]:
    """Return a full draw snapshot of a fictional fleet.

    Includes a busy NVIDIA box, a ROCm box, a CPU-only box, a host that is
    listed but not answering SSH, a tailnet peer that is offline, one running
    rental, and one stopped rental. Nothing here is probed.
    """
    gpu_a = _wave(72.0, tick, 0.2)
    gpu_b = _wave(22.0, tick, 1.4)
    cpu_a = _wave(34.0, tick, 0.6)
    cpu_b = _wave(11.0, tick, 2.1)
    cpu_c = _wave(6.0, tick, 0.9)
    nodes = {
        "node-a": _blank(
            name="node-a", status="ok", online=True, is_self=True, os="linux",
            host="node-a", ip=f"{_DOC_NET}10", gpukind="nvidia",
            gpus=[_gpu("RTX 4090", gpu_a, 8192, 24576, 67, 280)],
            cpu_pct=cpu_a, ncpu=16, load=[1.2, 0.9, 0.4],
            core_pct=[_wave(value, tick, index / 3) for index, value in enumerate(
                (12, 40, 55, 22, 8, 63, 18, 44))],
            mem_total_kb=_gib(48), mem_avail_kb=_gib(36), uptime_s=23 * 3600,
        ),
        "node-b": _blank(
            name="node-b", status="ok", online=True, os="linux",
            host="node-b", ip=f"{_DOC_NET}11", gpukind="rocm",
            gpus=[_gpu("Radeon RX 7900 XTX", gpu_b, 2048, 24576, 48, 90)],
            cpu_pct=cpu_b, ncpu=24, load=[0.8, 0.7, 0.5],
            core_pct=[_wave(18, tick, index) for index in range(8)],
            mem_total_kb=_gib(64), mem_avail_kb=_gib(50), uptime_s=5 * 3600,
        ),
        "node-c": _blank(
            name="node-c", status="ok", online=True, os="linux",
            host="node-c", ip=f"{_DOC_NET}12", gpukind="none",
            cpu_pct=cpu_c, ncpu=8, load=[0.2, 0.2, 0.1],
            mem_total_kb=_gib(16), mem_avail_kb=_gib(11), uptime_s=2 * 86400,
        ),
        "node-down": _blank(
            name="node-down", status="down", online=True, os="linux",
            host="node-down", ip=f"{_DOC_NET}13",
            err="connection refused",
        ),
        "node-off": _blank(
            name="node-off", status="offline", online=False, os="linux",
            host="node-off", ip=f"{_DOC_NET}14",
        ),
        "vast 1001": _blank(
            name="vast 1001", source="vast", status="ok", online=True,
            id=1001, num_gpus=1, model="RTX 4090", dph=0.42,
            geo="Example Region", vast_state="running",
            detail="gpu:pod cpu:host", gpukind="nvidia",
            gpus=[_gpu("RTX 4090", _wave(41.0, tick, 0.4), 6144, 24576, 61, 210)],
            cpu_pct=_wave(18.0, tick, 1.1), ncpu=8, load=[0.6, 0.5, 0.4],
            mem_total_kb=_gib(32), mem_avail_kb=_gib(20), uptime_s=90 * 60,
            ip=f"{_DOC_NET}40",
        ),
        "vast 1002": _blank(
            name="vast 1002", source="vast", status="offline", online=False,
            id=1002, num_gpus=2, model="RTX 3090", dph=0.19,
            geo="Example Region", vast_state="exited",
            ip=f"{_DOC_NET}41",
        ),
    }
    return nodes
