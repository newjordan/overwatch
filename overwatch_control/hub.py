"""Spark-hosted Overwatch hub HTTP service."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .config import load_config, require_bulk_root, suite_spec
from .state import HubState


def json_bytes(data, status=200):
    body = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    return status, {"Content-Type": "application/json", "Content-Length": str(len(body))}, body


def text_bytes(text, status=200, content_type="text/plain; charset=utf-8"):
    body = text.encode("utf-8")
    return status, {"Content-Type": content_type, "Content-Length": str(len(body))}, body


def make_handler(state: HubState, cfg: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OverwatchHub/0.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("hub: " + fmt % args + "\n")

        def _read_json(self):
            size = int(self.headers.get("Content-Length") or "0")
            if not size:
                return {}
            return json.loads(self.rfile.read(size).decode("utf-8"))

        def _send(self, response):
            status, headers, body = response
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _error(self, status, message):
            self._send(json_bytes({"error": message}, status))

        def do_GET(self):
            try:
                self._route_get()
            except KeyError as e:
                self._error(404, str(e))
            except Exception as e:
                self._error(500, str(e))

        def do_POST(self):
            try:
                self._route_post()
            except KeyError as e:
                self._error(404, str(e))
            except ValueError as e:
                self._error(400, str(e))
            except Exception as e:
                self._error(500, str(e))

        def _route_get(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)
            parts = [unquote(p) for p in path.strip("/").split("/") if p]
            if path == "/":
                self._send(text_bytes(render_dashboard(state, cfg), 200, "text/html; charset=utf-8"))
            elif path == "/api/health":
                self._send(json_bytes({"ok": True, "service": "overwatch-hub"}))
            elif path == "/api/rigs":
                self._send(json_bytes({"rigs": state.rigs()}))
            elif len(parts) == 3 and parts[:2] == ["api", "rigs"]:
                self._send(json_bytes({"rig": state.rig(parts[2])}))
            elif path == "/api/jobs":
                self._send(json_bytes({"jobs": state.jobs()}))
            elif len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                self._send(json_bytes({"job": state.job(parts[2])}))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "logs":
                runner = (qs.get("runner_id") or [None])[0]
                self._send(json_bytes({"logs": state.logs(parts[2], runner)}))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "artifacts":
                self._send(json_bytes({"artifacts": state.artifacts(parts[2])}))
            elif len(parts) >= 5 and parts[:2] == ["api", "artifacts"]:
                rel = "/".join(parts[2:])
                self._send_artifact(rel)
            elif path == "/api/suites":
                self._send(json_bytes({"suites": cfg.get("suites") or {}}))
            elif path == "/api/fleet-snapshot":
                self._send(json_bytes({"rigs": state.rigs(), "jobs": state.jobs(20), "suites": cfg.get("suites") or {}}))
            else:
                self._error(404, "not found")

        def _route_post(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            parts = [unquote(p) for p in path.strip("/").split("/") if p]
            payload = self._read_json()
            if path == "/api/runners/register":
                self._send(json_bytes({"rig": state.register_runner(payload)}))
            elif len(parts) == 4 and parts[:2] == ["api", "runners"] and parts[3] == "heartbeat":
                self._send(json_bytes({"rig": state.heartbeat(parts[2], payload)}))
            elif len(parts) == 4 and parts[:2] == ["api", "runners"] and parts[3] == "next-job":
                self._send(json_bytes({"job": state.next_job_for_runner(parts[2])}))
            elif path == "/api/jobs":
                self._send(json_bytes({"job": state.submit_job(payload)}, 201))
            elif path == "/api/suites":
                name = payload.get("suite") or payload.get("name")
                spec = suite_spec(cfg, name)
                spec.update({k: v for k, v in payload.items() if k not in ("suite", "name")})
                self._send(json_bytes({"job": state.submit_job(spec)}, 201))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "logs":
                state.append_log(parts[2], payload.get("runner_id"), payload.get("stream") or "stdout", payload.get("text") or "")
                self._send(json_bytes({"ok": True}))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "complete":
                self._send(json_bytes({"job": state.complete_run(parts[2], payload.get("runner_id"), payload.get("status"), payload.get("exit_code"), payload.get("message"))}))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                self._send(json_bytes({"job": state.cancel_job(parts[2])}))
            elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "artifacts":
                self._send(json_bytes({"artifact": state.save_artifact(parts[2], payload.get("runner_id"), payload.get("name"), payload.get("content_b64"))}, 201))
            else:
                self._error(404, "not found")

        def _send_artifact(self, rel_path):
            clean = rel_path.replace("\\", "/").lstrip("/")
            if ".." in clean.split("/"):
                self._error(400, "unsafe artifact path")
                return
            path = os.path.join(state.bulk_root, clean)
            if not os.path.isfile(path):
                self._error(404, "artifact not found")
                return
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{html.escape(os.path.basename(path))}"')
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)

    return Handler


def render_dashboard(state: HubState, cfg: dict) -> str:
    rigs = state.rigs()
    jobs = state.jobs(50)
    suites = cfg.get("suites") or {}

    def esc(value):
        return html.escape(str(value if value is not None else ""))

    rig_rows = "\n".join(
        "<tr><td>{name}</td><td>{id}</td><td>{status}</td><td>{age:.1f}s</td><td>{tags}</td><td>{aliases}</td><td>{active}</td></tr>".format(
            name=esc(r["name"]),
            id=esc(r["id"]),
            status=esc(r["status"]),
            age=r["online_age"],
            tags=esc(", ".join(r.get("tags") or [])),
            aliases=esc(", ".join(r.get("aliases") or [])),
            active=esc(r.get("active_job_id") or ""),
        )
        for r in rigs
    ) or '<tr><td colspan="7">No runners registered.</td></tr>'
    job_rows = "\n".join(
        "<tr><td>{id}</td><td>{name}</td><td>{status}</td><td>{runs}</td><td><a href=\"/api/jobs/{id}/logs\">logs</a></td><td><a href=\"/api/jobs/{id}/artifacts\">artifacts</a></td></tr>".format(
            id=esc(j["id"]),
            name=esc(j.get("name") or ""),
            status=esc(j["status"]),
            runs=esc(", ".join(f"{r['runner_id']}:{r['status']}" for r in j.get("runs") or [])),
        )
        for j in jobs
    ) or '<tr><td colspan="6">No jobs submitted.</td></tr>'
    suite_rows = "\n".join(
        f"<tr><td>{esc(name)}</td><td>{esc(spec.get('command'))}</td><td>{esc(', '.join(spec.get('target_tags') or spec.get('tags') or []))}</td></tr>"
        for name, spec in sorted(suites.items())
    ) or '<tr><td colspan="3">No named suites configured.</td></tr>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fleet Overwatch Hub</title>
  <style>
    body {{ margin: 0; font: 14px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: #080b0e; color: #d9f7e7; }}
    header {{ padding: 18px 24px; border-bottom: 1px solid #1d3129; background: #0d1414; }}
    main {{ padding: 20px 24px 42px; display: grid; gap: 22px; }}
    h1, h2 {{ margin: 0; font-weight: 700; letter-spacing: 0; }}
    h1 {{ font-size: 20px; }}
    h2 {{ font-size: 15px; color: #76e2aa; margin-bottom: 10px; }}
    table {{ width: 100%; border-collapse: collapse; background: #0b1112; border: 1px solid #1d3129; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #14221f; text-align: left; vertical-align: top; }}
    th {{ color: #a8eecb; background: #101b19; }}
    a {{ color: #76c7ff; }}
    .sub {{ color: #7f998d; margin-top: 4px; }}
  </style>
</head>
<body>
  <header><h1>Fleet Overwatch Hub</h1><div class="sub">Pull-based runner control plane over Tailscale HTTP</div></header>
  <main>
    <section><h2>Fleet</h2><table><thead><tr><th>Name</th><th>ID</th><th>Status</th><th>Age</th><th>Tags</th><th>Aliases</th><th>Active Job</th></tr></thead><tbody>{rig_rows}</tbody></table></section>
    <section><h2>Jobs</h2><table><thead><tr><th>ID</th><th>Name</th><th>Status</th><th>Runs</th><th>Logs</th><th>Artifacts</th></tr></thead><tbody>{job_rows}</tbody></table></section>
    <section><h2>Suites</h2><table><thead><tr><th>Name</th><th>Command</th><th>Tags</th></tr></thead><tbody>{suite_rows}</tbody></table></section>
  </main>
</body>
</html>"""


def build_state(cfg: dict) -> HubState:
    bulk_root = require_bulk_root(cfg)
    sqlite_path = os.path.abspath(os.path.expanduser((cfg.get("metadata") or {}).get("sqlite_path") or "./overwatch-hub.sqlite3"))
    return HubState(sqlite_path, bulk_root)


def serve(cfg: dict):
    state = build_state(cfg)
    hub_cfg = cfg.get("hub") or {}
    host = hub_cfg.get("host") or "127.0.0.1"
    port = int(hub_cfg.get("port") or 8765)
    httpd = ThreadingHTTPServer((host, port), make_handler(state, cfg))
    print(f"overwatch hub listening on http://{host}:{port}", file=sys.stderr)
    print(f"bulk artifacts: {state.bulk_root}", file=sys.stderr)
    try:
        httpd.serve_forever()
    finally:
        state.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the Fleet Overwatch hub")
    ap.add_argument("--config", default=None, help="JSON config path")
    args = ap.parse_args(argv)
    cfg, used = load_config(args.config)
    if used:
        print(f"using config: {used}", file=sys.stderr)
    serve(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
