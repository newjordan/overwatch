import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from overwatch_control.config import load_config, require_bulk_root
from overwatch_control.hub import make_handler
from overwatch_control.mcp import MCPServer
from overwatch_control.state import HubState, safe_artifact_name


class ControlPlaneTests(unittest.TestCase):
    def test_config_loading_and_required_bulk_root(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            bulk = Path(td) / "bulk"
            cfg_path.write_text(json.dumps({"storage": {"bulk_root": str(bulk)}}), encoding="utf-8")
            cfg, used = load_config(str(cfg_path))
            self.assertEqual(used, str(cfg_path))
            self.assertEqual(require_bulk_root(cfg), str(bulk))
            self.assertTrue(bulk.is_dir())

    def test_target_selection_and_job_status(self):
        with tempfile.TemporaryDirectory() as td:
            state = HubState(str(Path(td) / "hub.sqlite3"), str(Path(td) / "bulk"))
            state.register_runner({"runner_id": "r1", "name": "alpha", "tags": ["gpu", "local"]})
            state.register_runner({"runner_id": "r2", "name": "beta", "tags": ["cpu"]})
            job = state.submit_job({"command": "true", "target_tags": ["gpu"]})
            self.assertEqual(job["status"], "queued")
            self.assertEqual([r["runner_id"] for r in job["runs"]], ["r1"])
            nxt = state.next_job_for_runner("r1")
            self.assertEqual(nxt["job_id"], job["id"])
            done = state.complete_run(job["id"], "r1", "succeeded", 0, None)
            self.assertEqual(done["status"], "succeeded")
            state.close()

    def test_artifact_path_safety(self):
        self.assertEqual(safe_artifact_name("./logs/out.txt"), "logs/out.txt")
        with self.assertRaises(ValueError):
            safe_artifact_name("../secret")
        with tempfile.TemporaryDirectory() as td:
            state = HubState(str(Path(td) / "hub.sqlite3"), str(Path(td) / "bulk"))
            state.register_runner({"runner_id": "r1", "tags": []})
            job = state.submit_job({"command": "true", "target_rigs": ["r1"]})
            art = state.save_artifact(job["id"], "r1", "logs/out.txt", base64.b64encode(b"ok").decode("ascii"))
            self.assertEqual(art["size"], 2)
            self.assertTrue((Path(td) / "bulk" / art["rel_path"]).is_file())
            state.close()

    def test_mcp_tool_request_handling(self):
        with running_hub() as (base_url, state):
            state.register_runner({"runner_id": "r1", "name": "alpha", "tags": ["gpu"]})
            server = MCPServer(base_url)
            content = server.call_tool("list_rigs", {})[0]["text"]
            self.assertIn("alpha", content)
            job_text = server.call_tool("submit_job", {"command": "true", "target_rigs": ["r1"]})[0]["text"]
            self.assertIn("job-", job_text)

    def test_local_hub_with_two_runner_processes(self):
        with tempfile.TemporaryDirectory() as td, running_hub({"runner": {"work_dir": td}}) as (base_url, state):
            state.register_runner({"runner_id": "r1", "tags": ["gpu"]})
            state.register_runner({"runner_id": "r2", "tags": ["gpu"]})
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(
                json.dumps({"hub": {"url": base_url}, "runner": {"work_dir": td}, "storage": {"bulk_root": str(Path(td) / "bulk")}}),
                encoding="utf-8",
            )
            job = state.submit_job(
                {
                    "command": [sys.executable, "-c", "from pathlib import Path; Path('artifact.txt').write_text('ok')"],
                    "target_tags": ["gpu"],
                    "artifact_globs": ["artifact.txt"],
                    "timeout": 20,
                }
            )
            for runner_id in ("r1", "r2"):
                subprocess.run(
                    [sys.executable, "-m", "overwatch_control.runner", "--config", str(cfg_path), "--hub-url", base_url, "--id", runner_id, "--tag", "gpu", "--once"],
                    cwd=os.getcwd(),
                    check=True,
                    timeout=30,
                )
            deadline = time.time() + 5
            final = state.job(job["id"])
            while final["status"] not in ("succeeded", "failed") and time.time() < deadline:
                time.sleep(0.1)
                final = state.job(job["id"])
            self.assertEqual(final["status"], "succeeded")
            self.assertEqual(len(state.artifacts(job["id"])), 2)


class running_hub:
    def __init__(self, cfg_extra=None):
        self.cfg_extra = cfg_extra or {}

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        cfg = {"suites": {}, **self.cfg_extra}
        self.state = HubState(str(Path(self.td.name) / "hub.sqlite3"), str(Path(self.td.name) / "bulk"))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state, cfg))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address
        return f"http://{host}:{port}", self.state

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()
        self.state.close()
        self.td.cleanup()


if __name__ == "__main__":
    unittest.main()
