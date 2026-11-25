"""Demo mode renders a fictional fleet and never touches the network."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import overwatch
from fleet_overwatch.demo import build_demo_nodes

ROOT = Path(__file__).resolve().parents[1]
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DOC = ("192.0.2.", "198.51.100.", "203.0.113.")


def test_demo_nodes_are_documentation_only() -> None:
    nodes = build_demo_nodes(0)
    assert "node-a" in nodes
    assert nodes["node-down"]["status"] == "down"
    assert nodes["node-off"]["status"] == "offline"
    assert nodes["node-b"]["gpukind"] == "rocm"
    assert nodes["vast 1002"]["status"] == "offline"
    for node in nodes.values():
        for field in ("ip", "ssh_host"):
            value = str(node.get(field) or "")
            if value:
                assert value.startswith("192.0.2."), value
        assert "192.0.2." not in node["name"]


def test_demo_once_makes_no_subprocess_calls(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("demo mode must not spawn processes")

    monkeypatch.setattr(overwatch.subprocess, "run", boom)
    monkeypatch.setattr(overwatch.shutil, "which", lambda *_a, **_k: None)
    assert overwatch.main(["--demo", "--once"]) == 0
    text = capsys.readouterr().out
    assert "FLEET OVERWATCH · DEMO" in text
    assert "3/5 fleet" in text
    assert "2 pods" in text
    assert "$0.42/hr" in text
    assert "node-a" in text
    assert "node-down" in text
    assert "host down" in text
    assert "node-off" in text
    assert "offline" in text
    assert "vast 1001" in text
    assert "exited" in text


def test_demo_snapshot_second_tick_stays_in_range() -> None:
    overwatch.apply_demo_snapshot(3)
    for node in overwatch.LATEST.values():
        cpu = node.get("cpu_pct")
        if cpu is not None:
            assert 0.0 <= cpu <= 100.0
        for gpu in node.get("gpus") or []:
            if gpu["util"] is not None:
                assert 0.0 <= gpu["util"] <= 100.0


def test_readme_includes_the_demo_screenshot() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/demo.png" in readme
    assert (ROOT / "docs" / "demo.png").is_file()
    assert "--demo" in readme


def test_source_tree_has_no_live_addresses() -> None:
    roots = [ROOT / "overwatch.py", ROOT / "fleet_overwatch", ROOT / "tests",
             ROOT / "templates", ROOT / "config.example.json", ROOT / "vast-setup.sh",
             ROOT / "README.md", ROOT / "scripts"]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(path for path in root.rglob("*") if path.is_file())
    for path in files:
        if path.suffix not in {".py", ".json", ".sh", ".md", ".yml", ".toml"} and path.name != "config.example.json":
            continue
        for found in _IPV4.findall(path.read_text(encoding="utf-8")):
            assert found.startswith(_DOC), f"{path} contains non-documentation address {found}"
