"""Collection and discovery with mocked SSH, including hosts that are down."""

from __future__ import annotations

import subprocess

import pytest

import overwatch

NVIDIA_PROBE = """\
==GPUKIND==
nvidia
==GPU==
NVIDIA GeForce RTX 4090, 72, 8192, 24576, 67, 280.5
==CPU==
cpu  100 0 50 800 50 0 0 0 0 0
cpu0 40 0 20 400 20 0 0 0 0 0
==NPROC==
2
==LOAD==
0.40 0.20 0.10 1/10 1
==MEM==
MemTotal:       16384000 kB
MemAvailable:   8000000 kB
==UPTIME==
100.0
==END==
"""

ROCM_PROBE = """\
==GPUKIND==
rocm
==GPU==
device,GPU use (%),Temperature (Sensor edge) (C),Average Graphics Package Power (W),Card series,Card model,VRAM Total Memory (B),VRAM Total Used Memory (B)
card0,22,58.0,44.0,Radeon RX,7900 XTX,25769803776,2147483648
==CPU==
cpu  10 0 10 80 0 0 0 0 0 0
==NPROC==
4
==LOAD==
0.10 0.10 0.10 1/10 1
==MEM==
MemTotal:       1000 kB
MemAvailable:   500 kB
==UPTIME==
10.0
==END==
"""


def _linux(name: str, ip: str) -> dict:
    return {
        "name": name,
        "source": "fleet",
        "host": name,
        "ip": ip,
        "os": "linux",
        "online": True,
        "is_self": False,
    }


def test_collect_mocked_nvidia_and_rocm(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = {"192.0.2.10": NVIDIA_PROBE, "192.0.2.11": ROCM_PROBE}
    seen: list[str] = []

    def fake_probe(target, port=None, opts=None):
        seen.append(target)
        return scripts[target]

    monkeypatch.setattr(overwatch, "_run_probe", fake_probe)
    nvidia = overwatch.collect(_linux("node-a", "192.0.2.10"))
    rocm = overwatch.collect(_linux("node-b", "192.0.2.11"))
    assert seen == ["192.0.2.10", "192.0.2.11"]
    assert nvidia["status"] == "ok"
    assert nvidia["gpus"][0]["name"] == "RTX 4090"
    assert nvidia["gpus"][0]["util"] == 72
    assert rocm["status"] == "ok"
    assert rocm["gpukind"] == "rocm"
    assert rocm["gpus"][0]["name"] == "Radeon RX 7900 XTX"
    assert rocm["gpus"][0]["util"] == 22


def test_collect_down_host_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(target, port=None, opts=None):
        if target == "192.0.2.13":
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=10)
        if target == "192.0.2.17":
            raise RuntimeError("ssh: connect to host 192.0.2.17 port 22: Connection refused")
        if target == "192.0.2.18":
            raise RuntimeError("Permission denied (publickey).")
        raise AssertionError(target)

    monkeypatch.setattr(overwatch, "_run_probe", fake_probe)
    down = overwatch.collect(_linux("node-down", "192.0.2.13"))
    refused = overwatch.collect(_linux("node-refused", "192.0.2.17"))
    denied = overwatch.collect(_linux("node-locked", "192.0.2.18"))
    assert down == {"status": "down", "err": "timeout"}
    assert refused["status"] == "down"
    assert refused["err"] == "connection refused"
    assert "192.0.2" not in refused["err"]
    assert denied == {"status": "unreachable", "err": "auth failed"}


def test_windows_peer_is_not_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(*_args, **_kwargs):
        raise AssertionError("windows peers are not probed")

    monkeypatch.setattr(overwatch, "_run_probe", fake_probe)
    device = _linux("windows-peer", "192.0.2.19")
    device["os"] = "windows"
    assert overwatch.ssh_target(device) is None
    assert overwatch.collect(device) == {"status": "nossh"}


def test_one_down_host_does_not_drop_the_others() -> None:
    def collect_fn(device):
        if device["name"] == "node-down":
            raise RuntimeError("ssh: connect to host 192.0.2.13 port 22: No route to host")
        return {
            "status": "ok",
            "gpus": [{"name": "RTX 4090", "util": 10, "mem_used": 1, "mem_total": 2,
                      "temp": 40, "power": 30}],
            "gpukind": "nvidia",
            "ncpu": 2,
        }

    items = [
        _linux("node-a", "192.0.2.10"),
        _linux("node-down", "192.0.2.13"),
        {**_linux("node-off", "192.0.2.14"), "online": False},
    ]
    overwatch.refresh_source("fleet", items, True, collect_fn)
    assert overwatch.LATEST["node-a"]["status"] == "ok"
    assert overwatch.LATEST["node-a"]["gpus"][0]["name"] == "RTX 4090"
    assert overwatch.LATEST["node-down"]["status"] == "down"
    assert overwatch.LATEST["node-down"]["err"] == "probe failed"
    assert overwatch.LATEST["node-off"]["status"] == "offline"
    plain = overwatch.render_plain(dict(overwatch.LATEST))
    assert "node-a" in plain
    assert "host down" in plain
    assert "offline" in plain


def test_cpu_percent_is_a_delta_between_polls() -> None:
    assert overwatch._pct_delta((150, 200), (100, 100)) == 50.0
    assert overwatch._pct_delta((10, 100), None) is None
    assert overwatch._pct_delta((10, 100), (10, 100)) is None
    node: dict = {}
    overwatch._apply_cpu_delta(node, "node-a", (100, 1000), [(40, 400)])
    assert node["cpu_pct"] is None  # first sample has no baseline
    overwatch._apply_cpu_delta(node, "node-a", (150, 1100), [(90, 500)])
    assert node["cpu_pct"] == pytest.approx(50.0)
    assert node["core_pct"] == [pytest.approx(50.0)]


def test_vast_ssh_failure_stays_on_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=10)

    attached: list[int] = []
    monkeypatch.setattr(overwatch, "_run_probe", fake_probe)
    monkeypatch.setattr(overwatch, "vast_attach", lambda pod_id: attached.append(pod_id))
    pod = {
        "name": "vast 1001",
        "online": True,
        "id": 1001,
        "ssh_host": "192.0.2.40",
        "ssh_port": 2222,
        "num_gpus": 1,
        "model": "RTX 4090",
        "uptime_mins": 5,
        "api": {
            "gpu_util": None,
            "cpu_util": None,
            "gpu_temp": None,
            "gpu_totalram": None,
            "vmem_usage": None,
            "cpu_cores": None,
            "mem_usage": None,
            "mem_limit": None,
        },
    }
    result = overwatch.collect_vast(pod, allow_attach=True)
    assert result["status"] == "down"
    assert result["err"] == "timeout"
    assert attached == [1001]

    pod["id"] = 1002
    pod["api"]["gpu_util"] = 15
    lagged = overwatch.collect_vast(pod, allow_attach=False)
    assert lagged["status"] == "api"
    assert lagged["gpus"][0]["util"] == 15
