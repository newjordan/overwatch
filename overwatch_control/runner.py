"""Pull-based Overwatch runner for fleet rigs."""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import platform
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import load_config


def request_json(base_url: str, method: str, path: str, payload=None, timeout=30):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {e.code} {body}") from e


def default_runner_id():
    return socket.gethostname().split(".")[0].lower()


def checkpoint_path(cfg: dict, runner_id: str) -> str:
    configured = (cfg.get("runner") or {}).get("checkpoint_path")
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    root = Path(os.path.expanduser("~/.cache/fleet-overwatch"))
    root.mkdir(parents=True, exist_ok=True)
    return str(root / f"runner-{runner_id}.json")


class Runner:
    def __init__(self, cfg: dict, hub_url: str | None = None, runner_id: str | None = None, tags=None, aliases=None):
        self.cfg = cfg
        runner_cfg = cfg.get("runner") or {}
        self.hub_url = hub_url or (cfg.get("hub") or {}).get("url") or "http://127.0.0.1:8765"
        self.runner_id = runner_id or runner_cfg.get("id") or default_runner_id()
        self.name = runner_cfg.get("name") or self.runner_id
        self.tags = list(tags if tags is not None else (runner_cfg.get("tags") or []))
        configured_aliases = runner_cfg.get("aliases") or []
        self.aliases = sorted({str(a) for a in list(configured_aliases) + list(aliases or []) if str(a).strip()})
        self.work_dir = os.path.abspath(os.path.expanduser(runner_cfg.get("work_dir") or "."))
        self.poll_interval = float(runner_cfg.get("poll_interval") or 2.0)
        self.artifact_limit = int(float(runner_cfg.get("artifact_limit_mb") or 256) * 1024 * 1024)
        self.checkpoint_file = checkpoint_path(cfg, self.runner_id)
        self.active_job_id = None

    def register(self):
        info = {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "aliases": self.aliases,
        }
        return request_json(
            self.hub_url,
            "POST",
            "/api/runners/register",
            {"runner_id": self.runner_id, "name": self.name, "tags": self.tags, "info": info},
        )

    def heartbeat(self, status="online"):
        return request_json(
            self.hub_url,
            "POST",
            f"/api/runners/{self.runner_id}/heartbeat",
            {"status": status, "active_job_id": self.active_job_id},
            timeout=10,
        )

    def poll_once(self):
        self.heartbeat()
        data = request_json(self.hub_url, "POST", f"/api/runners/{self.runner_id}/next-job", {}, timeout=30)
        job = data.get("job")
        if job:
            self.execute_job(job)

    def run_forever(self):
        self.register()
        recovered = self.load_checkpoint()
        if recovered:
            self.log(recovered["job_id"], "stderr", "runner restarted after checkpoint; marking prior run failed\n")
            self.complete(recovered["job_id"], "failed", None, "runner restarted before completion")
            self.clear_checkpoint()
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"runner: {e}", file=sys.stderr)
            time.sleep(max(0.2, self.poll_interval))

    def execute_job(self, job: dict):
        job_id = job["job_id"]
        spec = job["spec"]
        self.active_job_id = job_id
        self.write_checkpoint(job)
        status = "failed"
        exit_code = None
        message = None
        try:
            status, exit_code, message = self._run_process(job_id, spec)
            if status == "succeeded":
                self.upload_artifacts(job_id, spec)
        finally:
            self.complete(job_id, status, exit_code, message)
            self.clear_checkpoint()
            self.active_job_id = None
            self.heartbeat()

    def _run_process(self, job_id: str, spec: dict):
        cwd = os.path.abspath(os.path.join(self.work_dir, os.path.expanduser(spec.get("cwd") or ".")))
        command = spec.get("command")
        if isinstance(command, str):
            shell = True
            args = command
        else:
            shell = False
            args = [str(x) for x in command]
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
        timeout = float(spec.get("timeout") or 3600)
        expected = {int(x) for x in (spec.get("expected_exit") or [0])}
        self.log(job_id, "stderr", f"starting command in {cwd}: {command}\n")
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        q: queue.Queue[tuple[str, str]] = queue.Queue()
        threads = [
            threading.Thread(target=self._pump, args=("stdout", proc.stdout, q), daemon=True),
            threading.Thread(target=self._pump, args=("stderr", proc.stderr, q), daemon=True),
        ]
        for t in threads:
            t.start()
        deadline = time.time() + timeout
        while True:
            try:
                stream, text = q.get(timeout=0.2)
                self.log(job_id, stream, text)
            except queue.Empty:
                pass
            if proc.poll() is not None:
                while True:
                    try:
                        stream, text = q.get_nowait()
                        self.log(job_id, stream, text)
                    except queue.Empty:
                        break
                code = proc.returncode
                if code in expected:
                    return "succeeded", code, None
                return "failed", code, f"exit code {code} not in expected {sorted(expected)}"
            if time.time() > deadline:
                proc.kill()
                return "timed_out", None, f"timeout after {timeout:.1f}s"
            try:
                status = request_json(self.hub_url, "GET", f"/api/jobs/{job_id}", timeout=5).get("job", {}).get("status")
                if status == "canceled":
                    proc.terminate()
                    return "canceled", None, "job canceled by hub"
            except Exception:
                pass

    def _pump(self, stream, pipe, q):
        try:
            for line in iter(pipe.readline, ""):
                q.put((stream, line))
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def log(self, job_id, stream, text):
        if not text:
            return
        request_json(
            self.hub_url,
            "POST",
            f"/api/jobs/{job_id}/logs",
            {"runner_id": self.runner_id, "stream": stream, "text": text},
            timeout=10,
        )

    def complete(self, job_id, status, exit_code, message):
        return request_json(
            self.hub_url,
            "POST",
            f"/api/jobs/{job_id}/complete",
            {"runner_id": self.runner_id, "status": status, "exit_code": exit_code, "message": message},
            timeout=30,
        )

    def upload_artifacts(self, job_id, spec):
        cwd = os.path.abspath(os.path.join(self.work_dir, os.path.expanduser(spec.get("cwd") or ".")))
        for pattern in spec.get("artifact_globs") or []:
            for path in glob.glob(os.path.join(cwd, pattern), recursive=True):
                if not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
                rel = os.path.relpath(path, cwd)
                if size > self.artifact_limit:
                    self.log(job_id, "stderr", f"skipping artifact over limit: {rel}\n")
                    continue
                with open(path, "rb") as f:
                    content_b64 = base64.b64encode(f.read()).decode("ascii")
                request_json(
                    self.hub_url,
                    "POST",
                    f"/api/jobs/{job_id}/artifacts",
                    {"runner_id": self.runner_id, "name": rel, "content_b64": content_b64},
                    timeout=60,
                )

    def write_checkpoint(self, job):
        Path(os.path.dirname(self.checkpoint_file)).mkdir(parents=True, exist_ok=True)
        with open(self.checkpoint_file, "w", encoding="utf-8") as f:
            json.dump(job, f)

    def load_checkpoint(self):
        try:
            with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def clear_checkpoint(self):
        try:
            os.unlink(self.checkpoint_file)
        except FileNotFoundError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a Fleet Overwatch polling runner")
    ap.add_argument("--config", default=None, help="JSON config path")
    ap.add_argument("--hub-url", default=None, help="hub base URL")
    ap.add_argument("--id", default=None, help="runner id")
    ap.add_argument("--tag", action="append", default=None, help="runner tag; may be repeated")
    ap.add_argument("--alias", action="append", default=None, help="alternate rig name; may be repeated")
    ap.add_argument("--once", action="store_true", help="register, poll once, then exit")
    args = ap.parse_args(argv)
    cfg, _ = load_config(args.config)
    runner = Runner(cfg, args.hub_url, args.id, args.tag, args.alias)
    if args.once:
        runner.register()
        runner.poll_once()
        return 0
    runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
