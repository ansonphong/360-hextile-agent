"""Mocked HTTP: Unreal export MCP tools proxy the APP door."""
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
from hextile_mcp import HextileMcpServer, TOOL_NAMES  # noqa: E402

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
    assert seen["timeout"] == GENERATE_TIMEOUT_S == 300.0


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


def test_tool_names_include_unreal() -> None:
    assert "get_unreal_export_catalog" in TOOL_NAMES
    assert "preflight_unreal_export" in TOOL_NAMES
    assert "export_to_unreal_project" in TOOL_NAMES
    assert len(TOOL_NAMES) == 42
