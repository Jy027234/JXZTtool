"""Exercise the OpenAI-compatible embedding path against a local HTTP gateway.

The gateway is intentionally deterministic and test-only.  The purpose is to
verify ParseCore's real HTTP request, bearer authentication, batching,
embedding coverage and semantic search wiring without claiming that an online
production gateway or business hit-rate has been approved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from parsecore.bootstrap import build_runtime
from parsecore.models import ParseRequest


GATEWAY_KEY = "local-gateway-key"


def _vector(text: str, dimensions: int = 16) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 8) for value in values]


class _GatewayState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.request_count = 0
        self.batch_sizes: list[int] = []
        self.models: list[str] = []
        self.auth_failures = 0


def _handler(state: _GatewayState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return None

        def _write(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP hook
            if self.path != "/v1/embeddings":
                self._write(404, {"error": {"message": "not_found"}})
                return
            if self.headers.get("Authorization") != f"Bearer {GATEWAY_KEY}":
                with state.lock:
                    state.auth_failures += 1
                self._write(401, {"error": {"message": "invalid_api_key"}})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                inputs = payload.get("input")
                if isinstance(inputs, str):
                    inputs = [inputs]
                if not isinstance(inputs, list) or not all(isinstance(item, str) for item in inputs):
                    raise ValueError("input_must_be_string_list")
                model = str(payload.get("model") or "")
                data = [
                    {"object": "embedding", "index": index, "embedding": _vector(text)}
                    for index, text in enumerate(inputs)
                ]
                with state.lock:
                    state.request_count += 1
                    state.batch_sizes.append(len(inputs))
                    state.models.append(model)
                self._write(200, {"object": "list", "data": data, "model": model})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._write(400, {"error": {"message": str(exc)}})

    return Handler


def _config_text(base_url: str) -> str:
    return f'''[project]
name = "embedding-compat-gateway-smoke"
mode = "embedded-sdk"

[storage]
database_url = "memory://"

[index]
mode = "memory"

[providers.embedding]
enabled = true
provider = "openai-compatible"
model = "local-compat-embedding-v1"
base_url = "{base_url}"
api_key_env = "PARSECORE_EMBEDDING_API_KEY"
timeout_seconds = 5.0
max_retries = 0
batch_size = 2

[[parsers]]
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt"]
'''


def run_smoke() -> dict[str, Any]:
    state = _GatewayState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, name="embedding-compat-gateway", daemon=True)
    thread.start()
    previous_key = os.environ.get("PARSECORE_EMBEDDING_API_KEY")
    os.environ["PARSECORE_EMBEDDING_API_KEY"] = GATEWAY_KEY
    try:
        with tempfile.TemporaryDirectory(prefix="parsecore-embedding-gateway-") as tmp:
            root = Path(tmp)
            config = root / "parsecore.toml"
            config.write_text(_config_text(f"http://127.0.0.1:{server.server_port}/v1"), encoding="utf-8")
            source = root / "gateway-smoke.txt"
            source.write_text(
                "Hydraulic Pressure Warning Manual\n"
                "WARNING: Release hydraulic pressure before removal.\n"
                "NOTE: Verify line caps after maintenance.\n"
                "Procedure: inspect the pump and record pressure values.\n",
                encoding="utf-8",
            )
            runtime = build_runtime(config)
            outcome = runtime.submit(
                ParseRequest(
                    doc_id="embedding-compat-gateway-smoke",
                    file_path=str(source),
                    media_type="text/plain",
                    options={"source": "embedding-compat-gateway-smoke"},
                )
            )
            hits = runtime.search_document(
                doc_id="embedding-compat-gateway-smoke",
                query="hydraulic pressure warning",
                limit=3,
            )
            embedded_count = sum(1 for chunk in outcome.chunks if chunk.embedding is not None)
            with state.lock:
                gateway = {
                    "request_count": state.request_count,
                    "batch_sizes": list(state.batch_sizes),
                    "models": list(state.models),
                    "auth_failures": state.auth_failures,
                }
            return {
                "schema_version": "2026-07-embedding-compat-gateway-smoke",
                "status": "ok" if embedded_count == len(outcome.chunks) and gateway["auth_failures"] == 0 else "failed",
                "scope": "local_openai_compatible_gateway_transport_only",
                "chunks": len(outcome.chunks),
                "embedded_chunks": embedded_count,
                "embedded_chunk_ratio": round(embedded_count / len(outcome.chunks), 4) if outcome.chunks else 1.0,
                "embedding_dimension": len(outcome.chunks[0].embedding or ()) if outcome.chunks else 0,
                "search_hits": [hit.chunk_id for hit in hits],
                "gateway": gateway,
            }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if previous_key is None:
            os.environ.pop("PARSECORE_EMBEDDING_API_KEY", None)
        else:
            os.environ["PARSECORE_EMBEDDING_API_KEY"] = previous_key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-json")
    args = parser.parse_args()
    result = run_smoke()
    if args.out_json:
        output = Path(args.out_json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
