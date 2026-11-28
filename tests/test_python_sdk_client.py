"""Verify the thin Apache-2.0 Python client speaks HTTP without importing core engine code."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

SDK_DIR = Path(__file__).resolve().parents[1] / "sdk" / "python"


def _load_sdk():
    sys.path.insert(0, str(SDK_DIR))
    try:
        spec = importlib.util.spec_from_file_location("unimem_client", SDK_DIR / "unimem_client" / "__init__.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["unimem_client"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if str(SDK_DIR) in sys.path:
            sys.path.remove(str(SDK_DIR))


def test_thin_python_sdk_has_zero_engine_or_hub_imports():
    for source in (SDK_DIR / "unimem_client").glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top not in {"unimem", "hub"}, f"thin SDK must stay decoupled: {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                assert top not in {"unimem", "hub"}, f"thin SDK must stay decoupled: {node.module}"


def test_thin_python_sdk_remote_calls_send_bearer_and_container_tag_and_block_redirects():
    sdk = _load_sdk()
    recorded: list[tuple[str, str, dict, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return None

        def do_GET(self):
            if self.path.startswith("/v1/learning/capabilities"):
                recorded.append(("GET", self.path, dict(self.headers), {}))
                payload = {"schema": "unimem.learning.v1", "operations": ["capabilities", "outcome"]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
                return
            if self.path == "/v1/health/redirect":
                recorded.append(("GET", self.path, dict(self.headers), {}))
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/leak")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            recorded.append(("POST", self.path, dict(self.headers), body))
            if self.path == "/v1/memory/reflect":
                reply = {"occasion_id": body["occasion_id"], "context": "[fact:1] test", "evidence": []}
            elif self.path == "/v1/learning/outcome":
                reply = {"schema": "unimem.learning.v1", "accepted": True}
            else:
                reply = {"ok": True}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = sdk.RemoteMemoryClient(
            f"http://127.0.0.1:{server.server_port}",
            token="secret-token",
            agent_id="bot-7",
            context={"tenant": "acme"},
            container_tag="fleet-eu",
        )
        res = client.reflect("recover actuator", occasion_id="occ-1")
        assert res["occasion_id"] == "occ-1"
        out = client.learning.outcome("occ-1", succeeded=True)
        assert out["accepted"] is True
        with pytest.raises(Exception):
            client._open_json(
                __import__("urllib.request").request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/health/redirect",
                    headers=client._headers(content_type=False),
                )
            )
    finally:
        server.shutdown()

    post_reflect = next(r for r in recorded if r[1] == "/v1/memory/reflect")
    assert post_reflect[2]["Authorization"] == "Bearer secret-token"
    assert post_reflect[2]["X-Container-Tag"] == "fleet-eu"
    assert post_reflect[3]["context"] == {"tenant": "acme", "agent": "bot-7"}
