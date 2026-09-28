"""Mocked HTTP: export MCP tools proxy the APP door."""
from __future__ import annotations

import http.client
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
    LONG_OPERATION_TIMEOUT_S,
    Client,
    HextileClientError,
)
from hextile_mcp import HextileMcpServer, TOOL_NAMES, TOOLS  # noqa: E402

BASE = "http://127.0.0.1:8000"


class _Resp:
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
        seen.setdefault("urls", []).append(req.full_url)
        seen.setdefault("requests", []).append((req.full_url, req.data, timeout))
        seen["url"] = req.full_url
        seen["data"] = req.data
        seen["timeout"] = timeout
        seen["method"] = req.get_method()
        seen["calls"] = seen.get("calls", 0) + 1
        return _Resp(payload)

    return opener


def _timeout_opener(seen: dict[str, Any]):
    def opener(req, timeout=None):  # noqa: ANN001
        seen["url"] = req.full_url
        seen.setdefault("requests", []).append((req.full_url, req.data, timeout))
        seen["timeout"] = timeout
        seen["calls"] = seen.get("calls", 0) + 1
        raise TimeoutError("timed out")

    return opener


def test_catalog_get_url() -> None:
    seen: dict[str, Any] = {}
    Client(base_url=BASE, opener=_recorder(seen, {"parents": []})).get_unreal_export_catalog()
    assert seen["method"] == "GET"
    assert seen["url"] == f"{BASE}/api/renders/unreal-export/catalog"
    assert seen["timeout"] == 30.0


def test_preflight_posts_quoted_id() -> None:
    seen: dict[str, Any] = {}
    body = {
        "projection": "equirectangular",
        "encoding": "hdr",
        "unreal_project": {"uproject_path": r"C:\Work\Sky.uproject"},
    }
    Client(base_url=BASE, opener=_recorder(seen)).preflight_unreal_export("r/a", body)
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/renders/{urllib.parse.quote('r/a', safe='')}/export/preflight"
    assert json.loads(seen["data"]) == body
    assert seen["timeout"] == 30.0


def test_export_keeps_windows_path_and_300s_timeout() -> None:
    seen: dict[str, Any] = {}
    win = r"D:\Projects\Game.uproject"
    Client(base_url=BASE, opener=_recorder(seen, {"outcome": "complete"})).export_to_unreal_project(
        "abc",
        {
            "projection": "equirectangular",
            "encoding": "hdr",
            "destination": "should-omit",
            "unreal_project": {"uproject_path": win},
        },
    )
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/renders/abc/export"
    posted = json.loads(seen["data"])
    assert "destination" not in posted
    assert posted["unreal_project"]["uproject_path"] == win
    assert "\\mnt\\" not in posted["unreal_project"]["uproject_path"]
    assert seen["timeout"] == LONG_OPERATION_TIMEOUT_S == 300.0


def test_export_timeout_is_one_attempt_unknown() -> None:
    seen: dict[str, Any] = {}
    with pytest.raises(HextileClientError) as exc:
        Client(base_url=BASE, opener=_timeout_opener(seen)).export_to_unreal_project(
            "abc",
            {"unreal_project": {"uproject_path": r"C:\a.uproject"}},
        )
    assert seen["calls"] == 1
    assert exc.value.kind == "ue_outcome_unknown"


def test_mcp_handlers_proxy_and_forward_outcome() -> None:
    seen: dict[str, Any] = {}
    server = HextileMcpServer(
        client=Client(base_url=BASE, opener=_recorder(seen, {"outcome": "partial"}))
    )
    result = server.call_tool(
        "export_to_unreal_project",
        {
            "render_id": "r1",
            "projection": "equirectangular",
            "encoding": "hdr",
            "unreal_project": {"uproject_path": r"C:\Work\Sky.uproject"},
        },
    )
    assert "partial" in json.dumps(result)
    assert any(u.endswith("/api/renders/r1/export") for u in seen.get("urls", []))


def test_tool_names_include_export() -> None:
    assert "get_unreal_export_catalog" in TOOL_NAMES
    assert "preflight_unreal_export" in TOOL_NAMES
    assert "export_to_unreal_project" in TOOL_NAMES
    assert "preflight_file_export" in TOOL_NAMES
    assert "export_render_file" in TOOL_NAMES
    assert len(TOOL_NAMES) == len(TOOLS)


def _file_args(folder: Path) -> dict[str, Any]:
    return {
        "render_id": "render/1", "source_node_id": "node-1",
        "folder_path": str(folder), "output_name": "sky.jpg",
        "projection": "equirectangular", "encoding": "jpg",
        "include_360_metadata": False,
    }


def _export_requests(seen: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    return [request for request in seen.get("requests", [])
            if "/api/renders/" in request[0]]


def test_file_preflight_and_export_proxy_exact_body(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    receipt = {"outcome": "complete", "paths": [str(tmp_path.resolve() / "sky.jpg")]}
    server = HextileMcpServer(client=Client(base_url=BASE, opener=_recorder(seen, receipt)))
    args = _file_args(tmp_path)
    assert not server.call_tool("preflight_file_export", args)["isError"]
    preflight_url, preflight_data, preflight_timeout = _export_requests(seen)[0]
    assert preflight_url == f"{BASE}/api/renders/render%2F1/export/file-preflight"
    preflight_body = json.loads(preflight_data)
    assert preflight_body == {
        "source_node_id": "node-1", "destination": str(tmp_path.resolve() / "sky.jpg"),
        "projection": "equirectangular", "encoding": "jpg", "include_360_metadata": False,
    }
    assert preflight_timeout == 30.0
    args.update(source_revision="source-digest", output_fingerprint="approved-fingerprint")
    assert not server.call_tool("export_render_file", args)["isError"]
    export_url, export_data, export_timeout = _export_requests(seen)[1]
    assert export_url == f"{BASE}/api/renders/render%2F1/export"
    assert json.loads(export_data) == {
        **preflight_body, "create_only": True,
        "expected_source_revision": "source-digest",
        "expected_output_fingerprint": "approved-fingerprint",
    }
    assert export_timeout == LONG_OPERATION_TIMEOUT_S
    assert len(_export_requests(seen)) == 2


def test_file_export_schema_and_direct_handler_reject_extra_fields(tmp_path: Path) -> None:
    schemas = {tool["name"]: tool for tool in TOOLS}
    for name in ("preflight_file_export", "export_render_file"):
        schema = schemas[name]["inputSchema"]
        assert schema["additionalProperties"] is False
        assert not {"hdri", "unreal_project", "destination", "overwrite"} & set(schema["properties"])
    assert schemas["preflight_file_export"]["annotations"]["readOnlyHint"] is True
    assert schemas["export_render_file"]["annotations"]["readOnlyHint"] is False
    assert "output_fingerprint" in schemas["export_render_file"]["inputSchema"]["required"]
    seen: dict[str, Any] = {}
    server = HextileMcpServer(client=Client(base_url=BASE, opener=_recorder(seen)))
    args = _file_args(tmp_path) | {"source_revision": "rev", "output_fingerprint": "fingerprint"}
    for field in ("hdri", "unreal_project", "destination", "overwrite", "create_only"):
        assert server.call_tool("export_render_file", args | {field: "forbidden"})["isError"]
    assert server.call_tool("export_render_file", _file_args(tmp_path))["isError"]
    assert _export_requests(seen) == []


@pytest.mark.parametrize("leaf", ["../sky.jpg", r"C:\sky.jpg", "sky:stream.jpg", "CON.jpg", "LPT¹.jpg", "sky.jpg ", "sky.jpg."])
def test_file_export_rejects_unsafe_leaf_before_dispatch(tmp_path: Path, leaf: str) -> None:
    seen: dict[str, Any] = {}
    server = HextileMcpServer(client=Client(base_url=BASE, opener=_recorder(seen)))
    args = _file_args(tmp_path) | {"output_name": leaf}
    assert server.call_tool("preflight_file_export", args)["isError"]
    assert _export_requests(seen) == []


def test_file_export_transport_loss_is_one_attempt_unknown(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    server = HextileMcpServer(client=Client(base_url=BASE, opener=_timeout_opener(seen)))
    args = _file_args(tmp_path) | {"source_revision": "rev", "output_fingerprint": "fingerprint"}
    result = server.call_tool("export_render_file", args)
    assert result["isError"]
    assert "file_outcome_unknown" in json.dumps(result)
    assert len(_export_requests(seen)) == 1
    assert _export_requests(seen)[0][2] == 300.0


def test_file_export_truncated_response_is_one_attempt_unknown(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class TruncatedResponse(_Resp):
        def read(self) -> bytes:
            raise http.client.IncompleteRead(b'{"paths":', 1)

    def opener(req, timeout=None):  # noqa: ANN001
        seen.setdefault("requests", []).append((req.full_url, req.data, timeout))
        return TruncatedResponse()

    server = HextileMcpServer(client=Client(base_url=BASE, opener=opener))
    args = _file_args(tmp_path) | {"source_revision": "rev", "output_fingerprint": "fingerprint"}
    result = server.call_tool("export_render_file", args)
    assert result["isError"]
    assert "file_outcome_unknown" in json.dumps(result)
    assert len(_export_requests(seen)) == 1
    assert _export_requests(seen)[0][2] == LONG_OPERATION_TIMEOUT_S


@pytest.mark.parametrize("raw", [b"", b"{bad json", b"{}", b'{"outcome":"complete","paths":[]}'])
def test_file_export_unusable_2xx_is_one_attempt_unknown(tmp_path: Path, raw: bytes) -> None:
    seen: dict[str, Any] = {}

    class RawResponse(_Resp):
        def read(self) -> bytes:
            return raw

    def opener(req, timeout=None):  # noqa: ANN001
        seen.setdefault("requests", []).append((req.full_url, req.data, timeout))
        return RawResponse()

    args = _file_args(tmp_path) | {"source_revision": "rev", "output_fingerprint": "fingerprint"}
    result = HextileMcpServer(client=Client(base_url=BASE, opener=opener)).call_tool("export_render_file", args)
    assert result["isError"]
    assert "file_outcome_unknown" in json.dumps(result)
    assert "inspect the destination" in json.dumps(result)
    assert len(_export_requests(seen)) == 1


@pytest.mark.parametrize("paths", [
    ["/other/sky.jpg"],
    ["relative/sky.jpg"],
    ["/other/sky.jpg", "/other/twice.jpg"],
])
def test_file_export_wrong_single_receipt_is_unknown(tmp_path: Path, paths: list[str]) -> None:
    seen: dict[str, Any] = {}
    receipt = {"outcome": "complete", "paths": paths}
    args = _file_args(tmp_path) | {"source_revision": "rev", "output_fingerprint": "fingerprint"}
    result = HextileMcpServer(client=Client(base_url=BASE, opener=_recorder(seen, receipt))).call_tool(
        "export_render_file", args
    )
    assert result["isError"]
    assert "file_outcome_unknown" in json.dumps(result)
    assert len(_export_requests(seen)) == 1


def test_file_export_valid_six_face_receipt(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    folder = tmp_path.resolve() / "faces"
    paths = [str(folder / f"sky_{face}.png") for face in ("px", "nx", "py", "ny", "pz", "nz")]
    receipt = {"outcome": "complete", "paths": paths}
    args = _file_args(tmp_path) | {
        "output_name": "faces", "projection": "cubemap_cross", "encoding": "png",
        "layout": "six_faces", "source_revision": "rev", "output_fingerprint": "fingerprint",
    }
    result = HextileMcpServer(client=Client(base_url=BASE, opener=_recorder(seen, receipt))).call_tool(
        "export_render_file", args
    )
    assert not result["isError"]
    assert len(_export_requests(seen)) == 1


def test_file_export_six_face_escape_is_unknown(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    folder = tmp_path.resolve() / "faces"
    paths = [str(folder / f"sky_{face}.png") for face in ("px", "nx", "py", "ny", "pz")]
    receipt = {"outcome": "complete", "paths": paths + [str(tmp_path.resolve() / "sky_nz.png")]}
    args = _file_args(tmp_path) | {
        "output_name": "faces", "projection": "cubemap_cross", "encoding": "png",
        "layout": "six_faces", "source_revision": "rev", "output_fingerprint": "fingerprint",
    }
    result = HextileMcpServer(client=Client(base_url=BASE, opener=_recorder(seen, receipt))).call_tool(
        "export_render_file", args
    )
    assert result["isError"]
    assert "file_outcome_unknown" in json.dumps(result)
    assert len(_export_requests(seen)) == 1


def test_file_export_http_refusal_stays_explicit(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def opener(req, timeout=None):  # noqa: ANN001
        seen.setdefault("requests", []).append((req.full_url, req.data, timeout))
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(b'{"detail":"destination_exists"}'))

    args = _file_args(tmp_path) | {"source_revision": "rev", "output_fingerprint": "fingerprint"}
    result = HextileMcpServer(client=Client(base_url=BASE, opener=opener)).call_tool("export_render_file", args)
    assert result["isError"]
    assert "destination_exists" in json.dumps(result)
    assert "file_outcome_unknown" not in json.dumps(result)
    assert len(_export_requests(seen)) == 1
