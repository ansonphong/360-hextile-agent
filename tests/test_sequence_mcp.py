"""Fake-opener HTTP tests for sequence Client methods and MCP tools/list.

No live APP, no GPU, no network.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = ROOT / "mcp"
sys.path.insert(0, str(MCP_DIR))

from hextile_client import (  # noqa: E402
    GENERATE_TIMEOUT_S,
    Client,
    HextileClientError,
)
from hextile_mcp import HextileMcpServer  # noqa: E402

SEQUENCE_TOOLS = (
    "extract_sequence_video",
    "create_sequence",
    "start_sequence",
    "get_sequence",
    "list_sequences",
    "stop_sequence",
)

# Full HextileConfig-shaped object (not a pipeline-only stub). Nested keys
# prove create_sequence emits the complete object, not a subset.
FULL_CONFIG: dict[str, Any] = {
    "pipeline": "realesrgan",
    "hextile": {
        "template": "Hextile_20_2K",
        "rendertiles": "all",
        "tile_input_width": 512,
        "tile_output_width": 512,
    },
    "diffusion": {
        "model": "realesrgan-x4plus",
        "seed": -1,
        "strength": 0.45,
        "guidance_scale": 7.5,
        "num_inference_steps": 20,
    },
    "input": {
        "format": "equirectangular",
        "mode": "sequence",
        "source": "file",
        "path": "/frames/stills",
        "width": 2048,
        "height": 1024,
    },
    "upscaling": {
        "model": "realesrgan-x4plus",
        "scale_factor": 4,
        "tile_size": 0,
        "template": "",
    },
    "output": {
        "format": "equirectangular",
        "width": 8192,
        "height": 4096,
        "file_format": "PNG",
    },
    "prompt": {
        "global": "identity-check",
        "negative": "blurry",
        "directional": {"mode": "none", "points": []},
    },
    "post_processing": [],
    "sequence": {"enabled": True},
}

BASE = "http://hextile.test"


class _Resp:
    status = 200

    def __init__(self, payload: Any = None) -> None:
        self._payload = {} if payload is None else payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def getcode(self) -> int:
        return 200

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _recorder(seen: dict[str, Any], payload: Any = None):
    def opener(req, timeout=None):  # noqa: ANN001
        seen["url"] = req.full_url
        seen["data"] = req.data
        seen["timeout"] = timeout
        seen["method"] = req.get_method()
        return _Resp(payload)

    return opener


def _client(seen: dict[str, Any], payload: Any = None) -> Client:
    return Client(base_url=BASE, opener=_recorder(seen, payload))


def _http_error_opener(code: int, msg: str):
    def opener(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=code,
            msg=msg,
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(json.dumps({"detail": msg}).encode()),
        )

    return opener


def test_extract_sequence_video_posts_path_and_generate_timeout() -> None:
    seen: dict[str, Any] = {}
    _client(seen).extract_sequence_video("/clips/scene.mp4")
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/sequences/extract-video"
    assert json.loads(seen["data"]) == {"video_path": "/clips/scene.mp4"}
    assert seen["timeout"] == GENERATE_TIMEOUT_S
    assert GENERATE_TIMEOUT_S == 300.0


def test_create_sequence_posts_full_config_lossless() -> None:
    seen: dict[str, Any] = {}
    _client(seen).create_sequence("/frames/stills", FULL_CONFIG, name="upres-folder")
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/sequences/create"
    body = json.loads(seen["data"])
    assert body["folder_path"] == "/frames/stills"
    assert body["name"] == "upres-folder"
    assert body["config"] == FULL_CONFIG
    assert set(body["config"]) == set(FULL_CONFIG)
    assert body["config"]["hextile"]["template"] == "Hextile_20_2K"
    assert body["config"]["diffusion"]["model"] == "realesrgan-x4plus"
    assert body["config"]["input"] == FULL_CONFIG["input"]
    assert body["config"]["upscaling"] == FULL_CONFIG["upscaling"]


def test_start_sequence_quotes_id() -> None:
    seen: dict[str, Any] = {}
    sequence_id = "seq_a/b"
    _client(seen).start_sequence(sequence_id)
    quoted = urllib.parse.quote(sequence_id, safe="")
    assert quoted == "seq_a%2Fb"
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/sequences/{quoted}/start"
    assert "/seq_a/b/" not in seen["url"]


def test_stop_sequence_quotes_id() -> None:
    seen: dict[str, Any] = {}
    sequence_id = "seq id"
    _client(seen).stop_sequence(sequence_id)
    quoted = urllib.parse.quote(sequence_id, safe="")
    assert quoted == "seq%20id"
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/sequences/{quoted}/stop"


def test_get_sequence_gets_quoted_id() -> None:
    seen: dict[str, Any] = {}
    sequence_id = "seq_a/b"
    _client(seen).get_sequence(sequence_id)
    quoted = urllib.parse.quote(sequence_id, safe="")
    assert seen["method"] == "GET"
    assert seen["url"] == f"{BASE}/api/sequences/{quoted}"
    assert seen["data"] is None


def test_list_sequences_default_lifecycle_active() -> None:
    seen: dict[str, Any] = {}
    _client(seen).list_sequences()
    assert seen["method"] == "GET"
    assert seen["url"] == f"{BASE}/api/sequences/?lifecycle_status=active"


def test_list_sequences_passes_lifecycle_status() -> None:
    seen: dict[str, Any] = {}
    _client(seen).list_sequences("archived")
    assert seen["method"] == "GET"
    assert seen["url"] == f"{BASE}/api/sequences/?lifecycle_status=archived"


def test_extract_sequence_video_402_is_hextile_client_error() -> None:
    client = Client(
        base_url=BASE,
        opener=_http_error_opener(402, "Payment Required"),
    )
    with pytest.raises(HextileClientError) as ei:
        client.extract_sequence_video("/v.mp4")
    assert ei.value.status_code == 402


def test_create_sequence_400_is_hextile_client_error() -> None:
    client = Client(
        base_url=BASE,
        opener=_http_error_opener(400, "Bad Request"),
    )
    with pytest.raises(HextileClientError) as ei:
        client.create_sequence("/frames/stills", FULL_CONFIG)
    assert ei.value.status_code == 400


def test_mcp_tools_list_includes_sequence_names() -> None:
    server = HextileMcpServer(client=Client(opener=lambda *a, **k: None))
    resp = server.handle_rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert resp is not None
    names = [t["name"] for t in resp["result"]["tools"]]
    for name in SEQUENCE_TOOLS:
        assert name in names
