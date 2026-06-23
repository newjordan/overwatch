"""Minimal stdio MCP server that talks to the Overwatch hub API."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import load_config


TOOLS = [
    {
        "name": "list_rigs",
        "description": "List registered Overwatch runners and their online age.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rig_status",
        "description": "Get one rig status by runner id.",
        "inputSchema": {"type": "object", "properties": {"runner_id": {"type": "string"}}, "required": ["runner_id"]},
    },
    {
        "name": "submit_job",
        "description": "Submit a controlled ad hoc shell/list command job to target rigs or tags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"description": "String shell command or argv list"},
                "target_rigs": {"type": "array", "items": {"type": "string"}},
                "target_tags": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "env": {"type": "object"},
                "timeout": {"type": "number"},
                "concurrency_limit": {"type": "integer"},
                "artifact_globs": {"type": "array", "items": {"type": "string"}},
                "expected_exit": {"type": "array", "items": {"type": "integer"}},
                "metadata": {"type": "object"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "submit_suite",
        "description": "Submit a named suite from hub config, optionally overriding target rigs/tags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "suite": {"type": "string"},
                "target_rigs": {"type": "array", "items": {"type": "string"}},
                "target_tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["suite"],
        },
    },
    {
        "name": "job_status",
        "description": "Get job status, runs, and metadata.",
        "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    },
    {
        "name": "job_logs",
        "description": "Read job logs.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}, "runner_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "job_artifacts",
        "description": "List artifacts uploaded for a job.",
        "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    },
    {
        "name": "cancel_job",
        "description": "Cancel a queued or running job.",
        "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    },
    {
        "name": "collect_fleet_snapshot",
        "description": "Collect rigs, recent jobs, and suite definitions from the hub.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def http_json(base_url: str, method: str, path: str, payload=None):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"hub HTTP {e.code}: {raw}") from e


class MCPServer:
    def __init__(self, hub_url: str):
        self.hub_url = hub_url.rstrip("/")

    def call_tool(self, name: str, args: dict):
        args = args or {}
        if name == "list_rigs":
            data = http_json(self.hub_url, "GET", "/api/rigs")
        elif name == "rig_status":
            data = http_json(self.hub_url, "GET", f"/api/rigs/{args['runner_id']}")
        elif name == "submit_job":
            payload = dict(args)
            payload.setdefault("kind", "ad_hoc")
            data = http_json(self.hub_url, "POST", "/api/jobs", payload)
        elif name == "submit_suite":
            data = http_json(self.hub_url, "POST", "/api/suites", args)
        elif name == "job_status":
            data = http_json(self.hub_url, "GET", f"/api/jobs/{args['job_id']}")
        elif name == "job_logs":
            qs = ""
            if args.get("runner_id"):
                qs = "?" + urlencode({"runner_id": args["runner_id"]})
            data = http_json(self.hub_url, "GET", f"/api/jobs/{args['job_id']}/logs{qs}")
        elif name == "job_artifacts":
            data = http_json(self.hub_url, "GET", f"/api/jobs/{args['job_id']}/artifacts")
        elif name == "cancel_job":
            data = http_json(self.hub_url, "POST", f"/api/jobs/{args['job_id']}/cancel", {})
        elif name == "collect_fleet_snapshot":
            data = http_json(self.hub_url, "GET", "/api/fleet-snapshot")
        else:
            raise KeyError(f"unknown tool: {name}")
        return [{"type": "text", "text": json.dumps(data, indent=2, sort_keys=True)}]

    def handle(self, msg: dict):
        method = msg.get("method")
        req_id = msg.get("id")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "overwatch-mcp", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                result = {"content": self.call_tool(params.get("name"), params.get("arguments") or {})}
            elif method == "notifications/initialized":
                return None
            else:
                raise KeyError(f"unsupported method: {method}")
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(e)}}


def read_message(stdin):
    headers = {}
    while True:
        line = stdin.buffer.readline()
        if line == b"":
            return None
        line = line.decode("ascii", "replace").strip()
        if not line:
            break
        key, _, value = line.partition(":")
        headers[key.lower()] = value.strip()
    size = int(headers.get("content-length") or "0")
    if size <= 0:
        return None
    return json.loads(stdin.buffer.read(size).decode("utf-8"))


def write_message(stdout, msg):
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stdout.buffer.flush()


def serve_stdio(hub_url: str):
    server = MCPServer(hub_url)
    while True:
        msg = read_message(sys.stdin)
        if msg is None:
            return
        response = server.handle(msg)
        if response is not None and msg.get("id") is not None:
            write_message(sys.stdout, response)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the Fleet Overwatch MCP stdio server")
    ap.add_argument("--config", default=None, help="JSON config path")
    ap.add_argument("--hub-url", default=None, help="hub base URL")
    args = ap.parse_args(argv)
    cfg, _ = load_config(args.config)
    hub_url = args.hub_url or (cfg.get("hub") or {}).get("url") or "http://127.0.0.1:8765"
    serve_stdio(hub_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
