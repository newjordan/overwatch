#!/usr/bin/env python3
"""modeld — on/off model-generation surface for the rig fleet.

Lightweight always-listening HTTP endpoint (stdlib only, runs in system python).
The heavy model lives in a *worker* subprocess inside its own conda env; modeld
spawns it on the first request (the "on") and kills it after idle (the "off"),
so VRAM is only held while work is flowing. Outputs are written to a blob dir and
returned as ARTIFACT POINTERS (path + sha256), never inline bytes — agents fetch
the bytes over tailscale-ssh.

Endpoints
  GET  /v1/models      -> {"object":"list","data":[{"id":<model>,"ready":<bool>}]}  (Hydra health probe)
  GET  /healthz        -> {"ok":true,"model":...,"worker":"up|down","idle_s":N}
  POST /generate       -> {"params":{...}} -> {"ok":true,"artifact":{path,sha256,size,mime,fetch_hint}}

Config via env:
  MODELD_MODEL     surface model id            (default: from --model)
  MODELD_PORT      bind port (127.0.0.1 only)  (default: 18080)
  MODELD_ENV       conda env for the worker    (e.g. trellis2 / flux2)
  MODELD_WORKER    worker script path          (e.g. worker_trellis.py)
  MODELD_OUT       blob output dir             (default: ~/models/out)
  MODELD_IDLE      idle seconds before unload  (default: 600)
  CONDA_SH         path to conda profile.d/conda.sh
"""
from __future__ import annotations
import json, os, sys, time, threading, subprocess, hashlib, socket, signal, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL   = os.environ.get("MODELD_MODEL", "unknown")
PORT    = int(os.environ.get("MODELD_PORT", "18080"))
ENV     = os.environ.get("MODELD_ENV", "")
WORKER  = os.environ.get("MODELD_WORKER", "")
OUT     = Path(os.environ.get("MODELD_OUT", str(Path.home() / "models" / "out"))).expanduser()
IDLE    = int(os.environ.get("MODELD_IDLE", "600"))
CONDA_SH= os.environ.get("CONDA_SH", str(Path.home() / "miniconda3/etc/profile.d/conda.sh"))
MIN_FREE_GB = float(os.environ.get("MODELD_MIN_FREE_GB", "0"))  # refuse to load if < this free
HOSTID  = socket.gethostname()


def mem_available_gb():
    """Free RAM (MemAvailable) in GiB — host-protection preflight. On GB10 the GPU
    shares this unified memory, so this also gauges VRAM headroom."""
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except Exception:
        return None
    return None

OUT.mkdir(parents=True, exist_ok=True)


class Worker:
    """A long-lived model process in its conda env, speaking JSONL on stdin/stdout.
    Protocol: modeld writes one request JSON line; worker replies with one JSON line.
    Worker prints {"ready":true} once when the model is loaded."""
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.last = time.time()
        self.lock = threading.Lock()

    def up(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self) -> tuple[bool, str]:
        with self.lock:
            if self.up():
                return True, "already-up"
            if not (ENV and WORKER):
                return False, "no worker configured (MODELD_ENV/MODELD_WORKER unset)"
            if MIN_FREE_GB > 0:
                avail = mem_available_gb()
                if avail is not None and avail < MIN_FREE_GB:
                    return False, (f"insufficient free memory: {avail:.1f}GB available, "
                                   f"need >={MIN_FREE_GB}GB — refusing to load (host protected)")
            # spawn: bash -lc 'source conda.sh; conda activate ENV; exec python -u WORKER MODEL'
            cmd = ["bash", "-lc",
                   f"source {CONDA_SH} && conda activate {ENV} && exec python -u {WORKER} {MODEL}"]
            try:
                # stderr merged into stdout: one pipe, always drained -> no deadlock.
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True, bufsize=1)
            except Exception as exc:
                return False, f"spawn failed: {exc}"
            # wait for the worker's ready/error line (model load can take a while)
            deadline = time.time() + 180
            tail: list[str] = []
            while time.time() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    self.proc = None
                    return False, "worker exited during load: " + " | ".join(tail[-3:])
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    tail.append(line[:200]); continue  # non-json worker/conda log
                if msg.get("ready"):
                    return True, "loaded"
                if msg.get("error"):
                    self.kill()
                    return False, f"worker load error: {msg['error']}"
            self.kill()
            return False, "timeout loading model: " + " | ".join(tail[-3:])

    def run(self, params: dict) -> dict:
        ok, detail = self.ensure()
        if not ok:
            return {"ok": False, "error": detail, "stage": "load"}
        with self.lock:
            self.last = time.time()
            try:
                self.proc.stdin.write(json.dumps({"params": params}) + "\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
            except Exception as exc:
                self.kill()
                return {"ok": False, "error": f"worker io: {exc}", "stage": "run"}
            self.last = time.time()
        if not line:
            self.kill()
            return {"ok": False, "error": "worker produced no result (died mid-run)", "stage": "run"}
        try:
            return json.loads(line)
        except Exception as exc:
            return {"ok": False, "error": f"bad worker reply: {exc}: {line[:200]}", "stage": "run"}

    def kill(self):
        with self.lock:
            if self.proc:
                try:
                    self.proc.send_signal(signal.SIGTERM)
                    self.proc.wait(timeout=10)
                except Exception:
                    try: self.proc.kill()
                    except Exception: pass
                self.proc = None

    def idle_seconds(self) -> float:
        return time.time() - self.last


W = Worker()


def _reaper():
    while True:
        time.sleep(15)
        if W.up() and W.idle_seconds() > IDLE:
            W.kill()  # unload GPU after idle ("off")


def _artifact(path: Path, mime: str) -> dict:
    h = hashlib.sha256(); size = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            size += len(chunk); h.update(chunk)
    return {"path": str(path), "sha256": h.hexdigest(), "size": size, "mime": mime,
            "host": HOSTID, "fetch_hint": "tailscale-ssh"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        body = (json.dumps(obj) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            return self._send(200, {"object": "list",
                "data": [{"id": MODEL, "object": "model", "ready": W.up()}]})
        if self.path == "/healthz":
            avail = mem_available_gb()
            return self._send(200, {"ok": True, "model": MODEL, "host": HOSTID,
                "worker": "up" if W.up() else "down",
                "idle_s": round(W.idle_seconds()) if W.up() else None, "out": str(OUT),
                "mem_free_gb": round(avail, 1) if avail is not None else None,
                "min_free_gb": MIN_FREE_GB})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/generate":
            return self._send(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as exc:
            return self._send(400, {"ok": False, "error": f"bad json: {exc}"})
        params = dict(req.get("params") or {})
        params.setdefault("job_id", f"{MODEL}-{uuid.uuid4().hex[:10]}")
        params.setdefault("out_dir", str(OUT))
        res = W.run(params)
        if not res.get("ok"):
            return self._send(503, res)
        # worker returns {"ok":true,"output":"<abs path>","mime":"..."}
        out = Path(res["output"])
        if not out.exists():
            return self._send(500, {"ok": False, "error": f"worker output missing: {out}"})
        return self._send(200, {"ok": True, "model": MODEL, "host": HOSTID,
                                "artifact": _artifact(out, res.get("mime", "application/octet-stream"))})


def main():
    if len(sys.argv) > 1:
        globals()["MODEL"] = sys.argv[1]
    threading.Thread(target=_reaper, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"modeld: model={MODEL} port=127.0.0.1:{PORT} env={ENV or '-'} idle={IDLE}s out={OUT}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        W.kill()


if __name__ == "__main__":
    main()
