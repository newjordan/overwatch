"""Typed helpers for Fleet Overwatch.

The curses panel lives in ``overwatch``. This package holds the pieces that
can run without a terminal: probe parsing, Tailscale status decoding, probe
failure classification, and the fictional demo fleet.
"""

from fleet_overwatch.demo import build_demo_nodes
from fleet_overwatch.parsing import (
    classify_probe_error,
    devices_from_tailscale_status,
    metrics_from_sections,
    parse_nvidia_smi,
    parse_probe_sections,
    parse_rocm_smi,
    short_gpu_name,
)

__all__ = [
    "build_demo_nodes",
    "classify_probe_error",
    "devices_from_tailscale_status",
    "metrics_from_sections",
    "parse_nvidia_smi",
    "parse_probe_sections",
    "parse_rocm_smi",
    "short_gpu_name",
]
