"""Isolate panel globals so tests never read a real tailnet config or home prefs."""

from __future__ import annotations

import json

import pytest

import overwatch


@pytest.fixture(autouse=True)
def isolate_panel(monkeypatch: pytest.MonkeyPatch, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.delenv("FLEET_OVERWATCH_CONFIG", raising=False)
    monkeypatch.setattr(overwatch, "PREFS_PATH", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(overwatch, "ERR_LOG", str(tmp_path / "error.log"))
    overwatch.apply_config(str(empty))
    overwatch.LATEST.clear()
    overwatch.PREV_CPU.clear()
    overwatch.LAST_SEEN.clear()
    overwatch.DUP_HIDDEN.clear()
    overwatch.ATTEMPTED_ATTACH.clear()
    overwatch.DEMO_MODE = False
    overwatch.LAST_ERR = None
    overwatch.DUAL_GAUGE = True
    overwatch.THEME_IDX = 0
    overwatch.SCHEME_IDX = 0
    overwatch.GAUGE_IDX = 0
    overwatch.FIELD_IDX = 0
    yield
