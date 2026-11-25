"""Parsers fed with captured nvidia-smi, rocm-smi, and tailscale output."""

from __future__ import annotations

import json
import subprocess

import pytest

import overwatch
from fleet_overwatch.parsing import (
    classify_probe_error,
    devices_from_tailscale_status,
    metrics_from_sections,
    parse_nvidia_smi,
    parse_probe_sections,
    parse_rocm_smi,
)

NVIDIA_CSV = """\
NVIDIA GeForce RTX 4090, 72, 8192, 24576, 67, 280.50
NVIDIA A100-SXM4-80GB, [N/A], 1024, 81920, 41, [N/A]
name, utilization.gpu, memory.used, memory.total, temperature.gpu, power.draw
"""

ROCM_CSV = """\
================================= ROCm System Management Interface =================================
device,GPU use (%),GPU memory use (%),Temperature (Sensor edge) (C),Average Graphics Package Power (W),Card series,Card model,VRAM Total Memory (B),VRAM Total Used Memory (B)
card0,22,8,58.0,44.0,Radeon RX,7900 XTX,25769803776,2147483648
card1,3,1,41.0,36.0,Instinct,MI210,68719476736,1073741824
========================================== End of ROCm SMI Log ====================================
"""

ROCM_KV = """\
GPU[0] : GPU use (%): 22
GPU[0] : Temperature (Sensor edge) (C): 58.0
GPU[0] : Average Graphics Package Power (W): 44.0
GPU[0] : VRAM Total Used Memory (B): 2147483648
GPU[0] : VRAM Total Memory (B): 25769803776
GPU[0] : Card series: Radeon RX
GPU[0] : Card model: 7900 XTX
"""

ROCM_CONCISE = """\
======================================== Concise Info ====================================
GPU  Temp (DieEdge)  AvgPwr  SCLK    MCLK     Fan  Perf  PwrCap  VRAM%  GPU%
0    45.0c           32.0W   500Mhz  800Mhz   0%   auto  300.0W  12%    5%
1    41.0c           28.0W   500Mhz  800Mhz   0%   auto  300.0W  3%     0%
==========================================================================================
"""

NVIDIA_PROBE = """\
==GPUKIND==
nvidia
==GPU==
NVIDIA GeForce RTX 4090, 72, 8192, 24576, 67, 280.5
==CPU==
cpu  100 0 50 800 50 0 0 0 0 0
cpu0 40 0 20 400 20 0 0 0 0 0
cpu1 60 0 30 400 30 0 0 0 0 0
==NPROC==
2
==LOAD==
0.40 0.20 0.10 2/80 123
==MEM==
MemTotal:       16384000 kB
MemAvailable:   8000000 kB
==UPTIME==
3600.50
==END==
"""

ROCM_PROBE = """\
==GPUKIND==
rocm
==GPU==
""" + ROCM_CSV + """\
==CPU==
cpu  10 0 10 80 0 0 0 0 0 0
==NPROC==
4
==LOAD==
0.10 0.10 0.10 1/40 9
==MEM==
MemTotal:       32000000 kB
MemAvailable:   16000000 kB
==UPTIME==
120.0
==END==
"""

TAILSCALE_STATUS = {
    "Self": {
        "DNSName": "node-a.example.invalid.",
        "HostName": "node-a",
        "TailscaleIPs": ["192.0.2.10"],
        "OS": "linux",
        "Online": True,
    },
    "Peer": {
        "b": {
            "DNSName": "node-b.example.invalid.",
            "HostName": "node-b",
            "TailscaleIPs": ["192.0.2.11"],
            "OS": "linux",
            "Online": True,
        },
        "off": {
            "DNSName": "node-off.example.invalid.",
            "HostName": "node-off",
            "TailscaleIPs": ["192.0.2.14"],
            "OS": "linux",
            "Online": False,
        },
        "phone": {
            "DNSName": "phone.example.invalid.",
            "HostName": "phone",
            "TailscaleIPs": ["192.0.2.15"],
            "OS": "android",
            "Online": True,
        },
        "dup": {
            "DNSName": "node-a-1.example.invalid.",
            "HostName": "node-a",
            "TailscaleIPs": ["192.0.2.16"],
            "OS": "windows",
            "Online": True,
        },
        "broken": None,
        "weird": "not-a-device",
    },
}


def test_nvidia_smi_csv_parses_cards_and_blanks() -> None:
    gpus = parse_nvidia_smi(NVIDIA_CSV)
    assert [gpu["name"] for gpu in gpus] == ["RTX 4090", "A100-SXM4-80GB"]
    assert gpus[0]["util"] == 72
    assert gpus[0]["mem_used"] == 8192
    assert gpus[0]["mem_total"] == 24576
    assert gpus[0]["temp"] == 67
    assert gpus[0]["power"] == 280.50
    assert gpus[1]["util"] is None
    assert gpus[1]["power"] is None
    assert gpus[1]["mem_used"] == 1024


def test_rocm_smi_csv_converts_bytes_to_mebibytes() -> None:
    gpus = parse_rocm_smi(ROCM_CSV)
    assert [gpu["name"] for gpu in gpus] == ["Radeon RX 7900 XTX", "Instinct MI210"]
    assert gpus[0]["util"] == 22
    assert gpus[0]["temp"] == 58.0
    assert gpus[0]["power"] == 44.0
    assert gpus[0]["mem_used"] == pytest.approx(2048)
    assert gpus[0]["mem_total"] == pytest.approx(24576)
    assert gpus[1]["util"] == 3
    assert gpus[1]["mem_total"] == pytest.approx(65536)


def test_rocm_smi_key_value_and_concise_table() -> None:
    kv = parse_rocm_smi(ROCM_KV)
    assert len(kv) == 1
    assert kv[0]["name"] == "Radeon RX 7900 XTX"
    assert kv[0]["util"] == 22
    assert kv[0]["mem_total"] == pytest.approx(24576)

    concise = parse_rocm_smi(ROCM_CONCISE)
    assert [gpu["util"] for gpu in concise] == [5, 0]
    assert concise[0]["temp"] == 45.0
    assert concise[0]["power"] == 32.0
    assert concise[0]["name"] == "AMD GPU 0"
    assert concise[1]["power"] == 28.0


def test_probe_transcripts_for_nvidia_and_rocm() -> None:
    nvidia = metrics_from_sections(parse_probe_sections(NVIDIA_PROBE))
    assert nvidia["gpukind"] == "nvidia"
    assert nvidia["gpus"][0]["name"] == "RTX 4090"
    assert nvidia["cpu_bt"] == (150, 1000)
    assert len(nvidia["core_bt"]) == 2
    assert nvidia["ncpu"] == 2
    assert nvidia["load"] == [0.40, 0.20, 0.10]
    assert nvidia["mem_total_kb"] == 16384000
    assert nvidia["mem_avail_kb"] == 8000000
    assert nvidia["uptime_s"] == 3600.50

    rocm = metrics_from_sections(parse_probe_sections(ROCM_PROBE))
    assert rocm["gpukind"] == "rocm"
    assert rocm["gpus"][0]["name"] == "Radeon RX 7900 XTX"
    assert rocm["ncpu"] == 4


def test_probe_script_calls_both_gpu_tools() -> None:
    assert "nvidia-smi" in overwatch.PROBE
    assert "rocm-smi" in overwatch.PROBE
    assert "rocm" in overwatch.PROBE


def test_intel_csv_still_parses() -> None:
    text = "==GPUKIND==\nintel\n==GPU==\nIntel Arc, 40, 512, 16384, 51, 70\n"
    metrics = metrics_from_sections(parse_probe_sections(text))
    assert metrics["gpukind"] == "intel"
    assert metrics["gpus"][0]["name"] == "Intel Arc"
    assert metrics["gpus"][0]["util"] == 40


def test_tailscale_status_skips_junk_and_honors_exclude() -> None:
    devices = devices_from_tailscale_status(TAILSCALE_STATUS, exclude={"Phone"})
    names = [device["name"] for device in devices]
    assert names == ["node-a", "node-b", "node-off", "node-a-1"]
    by_name = {device["name"]: device for device in devices}
    assert by_name["node-a"]["is_self"] is True
    assert by_name["node-a"]["online"] is True
    assert by_name["node-a"]["ip"] == "192.0.2.10"
    assert by_name["node-off"]["online"] is False
    assert by_name["node-a-1"]["os"] == "windows"


def test_discover_uses_mocked_tailscale_status(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))

        class Proc:
            stdout = json.dumps(TAILSCALE_STATUS)
            stderr = ""
            returncode = 0

        return Proc()

    monkeypatch.setattr(overwatch, "find_tailscale", lambda: "tailscale")
    monkeypatch.setattr(overwatch.subprocess, "run", fake_run)
    overwatch.EXCLUDE = {"phone"}
    devices = overwatch.discover()
    assert calls == [["tailscale", "status", "--json"]]
    assert devices is not None
    names = [device["name"] for device in devices]
    # Same HostName as node-a: the Windows peer is collapsed, not shown as its own tile.
    assert "node-a-1" not in names
    assert "phone" not in names
    assert "node-off" in names
    assert any(name == "node-a-1" for name, _host in overwatch.DUP_HIDDEN)


def test_discover_keeps_last_view_when_tailscale_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(overwatch, "find_tailscale", lambda: None)
    assert overwatch.discover() is None

    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="tailscale", timeout=10)

    monkeypatch.setattr(overwatch, "find_tailscale", lambda: "tailscale")
    monkeypatch.setattr(overwatch.subprocess, "run", boom)
    assert overwatch.discover() is None

    def bad_json(*_args, **_kwargs):
        class Proc:
            stdout = "not-json"
            stderr = ""
            returncode = 1

        return Proc()

    monkeypatch.setattr(overwatch.subprocess, "run", bad_json)
    assert overwatch.discover() is None


def test_classify_probe_error_hides_the_address() -> None:
    timeout = classify_probe_error(subprocess.TimeoutExpired(cmd="ssh", timeout=10))
    assert timeout == {"status": "down", "err": "timeout"}
    refused = classify_probe_error(
        RuntimeError("ssh: connect to host 192.0.2.13 port 22: Connection refused")
    )
    assert refused == {"status": "down", "err": "connection refused"}
    assert "192.0.2" not in refused["err"]
    unreachable = classify_probe_error(RuntimeError("Permission denied (publickey)."))
    assert unreachable == {"status": "unreachable", "err": "auth failed"}
    assert classify_probe_error(FileNotFoundError("ssh"))["err"] == "ssh missing"
