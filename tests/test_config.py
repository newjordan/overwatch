"""The example config is valid JSON and loads through the real config path."""

from __future__ import annotations

import json
from pathlib import Path

import overwatch

ROOT = Path(__file__).resolve().parents[1]


def test_example_and_templates_load() -> None:
    paths = [
        ROOT / "config.example.json",
        ROOT / "templates" / "fleet-basic.json",
        ROOT / "templates" / "vast-only.json",
    ]
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        used = overwatch.apply_config(str(path))
        assert used == str(path)
    overwatch.apply_config(str(ROOT / "config.example.json"))
    assert overwatch.SSH_USER["node-a"] == "operator"
    assert overwatch.SSH_USER["node-b"] == "service"
    assert "phone" in overwatch.EXCLUDE
    assert overwatch.ORDER[:3] == ["node-a", "node-b", "node-c"]
    assert overwatch.GPU_LABEL["node-c"] == "Example GPU"
    assert overwatch.VAST_USER == "root"


def test_pyproject_declares_installable_panel() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "fleet-overwatch"' in text
    assert 'fleet-overwatch = "overwatch:main"' in text
    assert "pytest" in text
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pytest" in workflow
    assert "pip install -e" in workflow
