"""SQLite-backed hub state for runners, jobs, logs, and artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


TERMINAL = {"succeeded", "failed", "canceled", "timed_out"}


def now() -> float:
    return time.time()


def job_id() -> str:
    return "job-" + uuid.uuid4().hex[:12]


def safe_artifact_name(name: str) -> str:
    clean = (name or "").replace("\\", "/").lstrip("/")
    parts = []
    for part in clean.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("artifact paths may not contain '..'")
        parts.append(part)
    if not parts:
        raise ValueError("artifact path is empty")
    return "/".join(parts)


class HubState:
    def __init__(self, sqlite_path: str, bulk_root: str):
        self.sqlite_path = sqlite_path
        self.bulk_root = os.path.abspath(os.path.expanduser(bulk_root))
        os.makedirs(os.path.dirname(os.path.abspath(sqlite_path)) or ".", exist_ok=True)
        os.makedirs(self.bulk_root, exist_ok=True)
        self.db = sqlite3.connect(sqlite_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS rigs (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                info_json TEXT NOT NULL,
                status TEXT NOT NULL,
                last_seen REAL NOT NULL,
                active_job_id TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                name TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                message TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                runner_id TEXT NOT NULL REFERENCES rigs(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                assigned_at REAL,
                started_at REAL,
                finished_at REAL,
                exit_code INTEGER,
                message TEXT,
                UNIQUE(job_id, runner_id)
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                runner_id TEXT NOT NULL,
                ts REAL NOT NULL,
                stream TEXT NOT NULL,
                text TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                runner_id TEXT NOT NULL,
                name TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def register_runner(self, payload: dict) -> dict:
        rid = str(payload.get("runner_id") or payload.get("id") or "").strip()
        if not rid:
            raise ValueError("runner_id is required")
        name = str(payload.get("name") or rid)
        tags = sorted({str(t) for t in (payload.get("tags") or [])})
        info = payload.get("info") or {}
        ts = now()
        self.db.execute(
            """
            INSERT INTO rigs(id, name, tags_json, info_json, status, last_seen, active_job_id)
            VALUES(?, ?, ?, ?, 'online', ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              tags_json=excluded.tags_json,
              info_json=excluded.info_json,
              status='online',
              last_seen=excluded.last_seen
            """,
            (rid, name, json.dumps(tags), json.dumps(info), ts),
        )
        self.db.commit()
        return self.rig(rid)

    def heartbeat(self, runner_id: str, payload: dict) -> dict:
        self.db.execute(
            "UPDATE rigs SET status=?, last_seen=?, active_job_id=? WHERE id=?",
            (
                str(payload.get("status") or "online"),
                now(),
                payload.get("active_job_id"),
                runner_id,
            ),
        )
        self.db.commit()
        return self.rig(runner_id)

    def rig(self, runner_id: str) -> dict:
        row = self.db.execute("SELECT * FROM rigs WHERE id=?", (runner_id,)).fetchone()
        if not row:
            raise KeyError(runner_id)
        return self._rig_dict(row)

    def rigs(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM rigs ORDER BY name, id").fetchall()
        return [self._rig_dict(r) for r in rows]

    def _rig_dict(self, row) -> dict:
        data = dict(row)
        data["tags"] = json.loads(data.pop("tags_json") or "[]")
        data["info"] = json.loads(data.pop("info_json") or "{}")
        data["aliases"] = sorted({str(a) for a in (data["info"].get("aliases") or [])})
        data["online_age"] = max(0.0, now() - float(data["last_seen"]))
        return data

    def _matches(self, rig: dict, spec: dict) -> bool:
        targets = {str(x).lower() for x in (spec.get("target_rigs") or spec.get("targets") or [])}
        tags = {str(x).lower() for x in (spec.get("target_tags") or spec.get("tags") or [])}
        aliases = {str(x).lower() for x in (rig.get("aliases") or [])}
        names = {rig["id"].lower(), rig["name"].lower(), *aliases}
        if targets and not names.intersection(targets):
            return False
        rig_tags = {str(x).lower() for x in rig.get("tags") or []}
        if tags and not tags.issubset(rig_tags):
            return False
        return True

    def select_targets(self, spec: dict) -> list[dict]:
        candidates = self.rigs()
        selected = [r for r in candidates if self._matches(r, spec)]
        if spec.get("scope") == "one" and selected:
            return selected[:1]
        return selected

    def submit_job(self, spec: dict) -> dict:
        spec = dict(spec or {})
        if "command" not in spec:
            raise ValueError("job spec requires command")
        spec.setdefault("kind", "ad_hoc")
        spec.setdefault("name", spec.get("suite") or spec.get("kind") or "job")
        spec.setdefault("cwd", ".")
        spec.setdefault("timeout", 3600)
        spec.setdefault("artifact_globs", [])
        spec.setdefault("expected_exit", [0])
        spec.setdefault("concurrency_limit", 0)
        jid = job_id()
        ts = now()
        targets = self.select_targets(spec)
        status = "queued" if targets else "failed"
        message = None if targets else "no registered rigs matched the target selector"
        with self.db:
            self.db.execute(
                "INSERT INTO jobs(id, name, kind, status, spec_json, created_at, updated_at, message) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (jid, spec.get("name"), spec.get("kind"), status, json.dumps(spec), ts, ts, message),
            )
            for rig in targets:
                self.db.execute(
                    "INSERT INTO runs(job_id, runner_id, status) VALUES(?, ?, 'queued')",
                    (jid, rig["id"]),
                )
        return self.job(jid)

    def next_job_for_runner(self, runner_id: str) -> dict | None:
        rig = self.rig(runner_id)
        rows = self.db.execute(
            """
            SELECT runs.*, jobs.spec_json, jobs.status AS job_status
            FROM runs JOIN jobs ON jobs.id=runs.job_id
            WHERE runs.runner_id=? AND runs.status='queued' AND jobs.status IN ('queued', 'running')
            ORDER BY jobs.created_at, runs.id
            """,
            (runner_id,),
        ).fetchall()
        for row in rows:
            spec = json.loads(row["spec_json"])
            limit = int(spec.get("concurrency_limit") or 0)
            if limit > 0:
                active = self.db.execute(
                    "SELECT COUNT(*) FROM runs WHERE job_id=? AND status='running'",
                    (row["job_id"],),
                ).fetchone()[0]
                if active >= limit:
                    continue
            ts = now()
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status='running', assigned_at=?, started_at=? WHERE id=? AND status='queued'",
                    (ts, ts, row["id"]),
                )
                self.db.execute(
                    "UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='queued'",
                    (ts, row["job_id"]),
                )
                self.db.execute(
                    "UPDATE rigs SET active_job_id=? WHERE id=?",
                    (row["job_id"], runner_id),
                )
            return {"job_id": row["job_id"], "runner_id": rig["id"], "spec": spec}
        return None

    def append_log(self, job_id: str, runner_id: str, stream: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO logs(job_id, runner_id, ts, stream, text) VALUES(?, ?, ?, ?, ?)",
            (job_id, runner_id, now(), stream or "stdout", text),
        )
        self.db.commit()

    def complete_run(self, job_id: str, runner_id: str, status: str, exit_code=None, message=None) -> dict:
        if status not in TERMINAL:
            raise ValueError("run completion status must be terminal")
        ts = now()
        with self.db:
            self.db.execute(
                "UPDATE runs SET status=?, finished_at=?, exit_code=?, message=? WHERE job_id=? AND runner_id=?",
                (status, ts, exit_code, message, job_id, runner_id),
            )
            self.db.execute("UPDATE rigs SET active_job_id=NULL WHERE id=?", (runner_id,))
        self._refresh_job_status(job_id)
        return self.job(job_id)

    def _refresh_job_status(self, job_id: str) -> None:
        job = self.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] == "canceled":
            return
        rows = self.db.execute("SELECT status FROM runs WHERE job_id=?", (job_id,)).fetchall()
        statuses = [r["status"] for r in rows]
        if not statuses:
            return
        if all(s in TERMINAL for s in statuses):
            final = "succeeded" if all(s == "succeeded" for s in statuses) else "failed"
        elif any(s == "running" for s in statuses):
            final = "running"
        else:
            final = "queued"
        self.db.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (final, now(), job_id))
        self.db.commit()

    def cancel_job(self, job_id: str) -> dict:
        ts = now()
        with self.db:
            self.db.execute("UPDATE jobs SET status='canceled', updated_at=? WHERE id=?", (ts, job_id))
            self.db.execute("UPDATE runs SET status='canceled', finished_at=? WHERE job_id=? AND status='queued'", (ts, job_id))
        return self.job(job_id)

    def job(self, job_id: str) -> dict:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise KeyError(job_id)
        data = dict(row)
        data["spec"] = json.loads(data.pop("spec_json") or "{}")
        runs = self.db.execute("SELECT * FROM runs WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        data["runs"] = [dict(r) for r in runs]
        return data

    def jobs(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self.job(r["id"]) for r in rows]

    def logs(self, job_id: str, runner_id: str | None = None, limit: int = 500) -> list[dict]:
        if runner_id:
            rows = self.db.execute(
                "SELECT * FROM logs WHERE job_id=? AND runner_id=? ORDER BY id DESC LIMIT ?",
                (job_id, runner_id, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM logs WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def save_artifact(self, job_id: str, runner_id: str, name: str, content_b64: str) -> dict:
        rel_name = safe_artifact_name(name)
        blob = base64.b64decode(content_b64.encode("ascii"), validate=True)
        digest = hashlib.sha256(blob).hexdigest()
        rel_path = f"{job_id}/{runner_id}/{rel_name}"
        dest = Path(self.bulk_root) / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        self.db.execute(
            "INSERT INTO artifacts(job_id, runner_id, name, rel_path, size, sha256, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (job_id, runner_id, rel_name, rel_path, len(blob), digest, now()),
        )
        self.db.commit()
        return {"name": rel_name, "rel_path": rel_path, "size": len(blob), "sha256": digest}

    def artifacts(self, job_id: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        return [dict(r) for r in rows]
