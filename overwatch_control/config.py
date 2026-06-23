"""Configuration helpers for the Fleet Overwatch control plane."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path


CONFIG_DIR = Path(os.path.expanduser("~/.config/fleet-overwatch"))

DEFAULT_CONFIG = {
    "ssh_users": {},
    "default_user": None,
    "gpu_labels": {},
    "exclude": [],
    "order": [],
    "vast_key": "~/.ssh/fleet_overwatch_ed25519",
    "vast_user": "root",
    "tailscale_paths": None,
    "vastai_paths": None,
    "hub": {
        "host": "127.0.0.1",
        "port": 8765,
        "url": "http://127.0.0.1:8765",
    },
    "storage": {
        "bulk_root": None,
    },
    "metadata": {
        "sqlite_path": "./overwatch-hub.sqlite3",
    },
    "runner": {
        "id": None,
        "name": None,
        "tags": [],
        "work_dir": ".",
        "poll_interval": 2.0,
        "checkpoint_path": None,
        "artifact_limit_mb": 256,
    },
    "suites": {},
}


def config_path(cli_path: str | None = None) -> str | None:
    for candidate in (
        cli_path,
        os.environ.get("FLEET_OVERWATCH_CONFIG"),
        "overwatch.config.json",
        str(CONFIG_DIR / "config.json"),
    ):
        if candidate and os.path.exists(os.path.expanduser(candidate)):
            return os.path.expanduser(candidate)
    return None


def _merge(base, override):
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | None = None) -> tuple[dict, str | None]:
    used = config_path(path)
    data = {}
    if used:
        with open(used, "r", encoding="utf-8") as f:
            data = json.load(f)
    return _merge(DEFAULT_CONFIG, data), used


def require_bulk_root(cfg: dict) -> str:
    bulk_root = ((cfg.get("storage") or {}).get("bulk_root") or "").strip()
    if not bulk_root:
        raise ValueError('hub requires config key "storage.bulk_root"')
    bulk_root = os.path.abspath(os.path.expanduser(bulk_root))
    os.makedirs(bulk_root, exist_ok=True)
    return bulk_root


def suite_spec(cfg: dict, name: str) -> dict:
    suites = cfg.get("suites") or {}
    if name not in suites:
        raise KeyError(f"unknown suite: {name}")
    spec = copy.deepcopy(suites[name])
    spec.setdefault("name", name)
    spec.setdefault("kind", "suite")
    return spec
