"""Critical Spot Clone MCP authority, wire, and uncertain-write contract."""

from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from hextile_client import Client  # noqa: E402
from hextile_mcp import HextileMcpServer, TOOLS  # noqa: E402


NODE = "bbbbbbbbbbbb"
PARENT = "aaaaaaaaaaaa"
SAVED = {
    "target": "saved", "render_id": "render one", "node_id": NODE,
    "expected_head": PARENT, "parent_id": PARENT,
    "destination_id": "spot-copy-1", "source_pose": {"yaw": 10, "pitch": 0},
    "destination_pose": {"yaw": 30, "pitch": 10, "roll": 0, "fov": 45},
    "mask": {"source": "shape", "shape": "hexagon"},
}
RECEIPT = {"node_id": NODE, "parent_id": PARENT, "head_advanced": True,
           "recipe_digest": "d" * 64, "mask_sha256": "e" * 64, "state": "complete"}


class Response:
    status = 200

    def __init__(self, data: Any) -> None:
        self.raw = json.dumps(data).encode()

    def read(self) -> bytes:
        return self.raw

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["content"][0]["text"])


def test_catalog_is_closed_by_saved_live_target_and_mask_source() -> None:
    tools = {tool["name"]: tool for tool in TOOLS}
    assert {"preflight_spot_clone", "clone_spot", "get_spot_clone"} <= tools.keys()
    assert tools["preflight_spot_clone"]["annotations"]["readOnlyHint"] is True
    assert tools["get_spot_clone"]["annotations"]["readOnlyHint"] is True
    assert tools["clone_spot"]["annotations"]["readOnlyHint"] is False
    saved, live = tools["clone_spot"]["inputSchema"]["oneOf"]
    assert saved["additionalProperties"] is live["additionalProperties"] is False
    assert saved["properties"]["target"] == {"const": "saved"}
    assert live["properties"]["target"] == {"const": "live"}
    assert "mask" not in live["properties"]
    assert not {"path", "pixels", "force", "retry_with_new_id"} & set(saved["properties"])
    for variant in saved["properties"]["mask"]["oneOf"]:
        assert variant["additionalProperties"] is False
        assert not {"path", "pixels"} & set(variant["properties"])


def test_saved_preflight_clone_and_exact_receipt_wire() -> None:
    seen: list[tuple[str, str, Any]] = []

    def opener(req: Any, timeout: Any = None) -> Response:
        data = json.loads(req.data) if req.data else None
        if not req.full_url.endswith("/api/agent/events"):
            seen.append((req.get_method(), req.full_url, data))
        if "agent-preflight" in req.full_url:
            return Response({"head": PARENT, "parent_id": PARENT, "parent_final": True,
                             "templates": [], "render_rasters": []})
        return Response(RECEIPT)

    server = HextileMcpServer(client=Client(opener=opener))
    assert payload(server.call_tool("preflight_spot_clone", {"render_id": "render one", "parent_id": PARENT}))["head"] == PARENT
    assert payload(server.call_tool("clone_spot", SAVED))["node_id"] == NODE
    assert payload(server.call_tool("get_spot_clone", {"render_id": "render one", "node_id": NODE}))["recipe_digest"] == "d" * 64
    assert seen == [
        ("GET", f"http://127.0.0.1:8000/api/renders/render%20one/spot-clone/agent-preflight?parent_id={PARENT}", None),
        ("POST", "http://127.0.0.1:8000/api/renders/render%20one/spot-clone/agent",
         {key: value for key, value in SAVED.items() if key not in {"target", "render_id"}}),
        ("GET", f"http://127.0.0.1:8000/api/renders/render%20one/spot-clone/agent/{NODE}", None),
    ]


def test_external_live_and_unlisted_saved_inputs_never_reach_app() -> None:
    seen: list[str] = []

    def opener(req: Any, timeout: Any = None) -> Response:
        if not req.full_url.endswith("/api/agent/events"):
            seen.append(req.full_url)
        return Response(RECEIPT)

    server = HextileMcpServer(client=Client(opener=opener))
    live = {"target": "live", "render_id": "r", "node_id": NODE,
            "expected_head": PARENT, "prepared_operation_id": "prepared-1",
            "expected_mask_fingerprint": "f" * 64}
    denied = payload(server.call_tool("clone_spot", live))
    assert denied["status_code"] == 403
    assert server.call_tool("clone_spot", {**SAVED, "force": True})["isError"] is True
    assert server.call_tool("clone_spot", {**SAVED, "mask": {"source": "from_raster", "raster_id": "ok.png", "path": "/tmp/a"}})["isError"] is True
    assert server.call_tool("clone_spot", {**SAVED, "mask": {"source": "from_template", "template_id": "m"}})["isError"] is True
    assert seen == []


def test_private_live_requires_child_and_operation_headers() -> None:
    seen: list[tuple[str, Any, str | None, str | None]] = []

    def opener(req: Any, timeout: Any = None) -> Response:
        seen.append((req.full_url, json.loads(req.data), req.get_header("X-hextile-copilot-token"), req.get_header("X-hextile-op")))
        return Response({"ok": True, "ticket_id": "ticket-1", "status": "pending"})

    server = HextileMcpServer(client=Client(opener=opener, internal_mode=True), child_token="private-child")
    live = {"target": "live", "render_id": "r", "node_id": NODE,
            "expected_head": PARENT, "prepared_operation_id": "prepared-1",
            "expected_mask_fingerprint": "f" * 64}
    assert payload(server.call_tool("clone_spot", live))["status_code"] == 403
    assert payload(server.call_tool("clone_spot", live, operation_id="approved-op"))["status"] == "pending"
    assert seen == [("http://127.0.0.1:8000/api/agent/creative-live",
                     {"tool_name": "clone_spot", "arguments": live}, "private-child", "approved-op")]


def test_lost_write_is_unknown_and_409_is_preserved() -> None:
    calls: list[str] = []

    def lost(req: Any, timeout: Any = None) -> Response:
        if req.full_url.endswith("/api/agent/events"):
            return Response({})
        calls.append(req.full_url)
        raise socket.timeout("reply lost")

    result = payload(HextileMcpServer(client=Client(opener=lost)).call_tool("clone_spot", SAVED))
    assert result["kind"] == "spot_clone_outcome_unknown"
    assert result["receipt"] == {"state": "unknown", "render_id": "render one",
                                 "node_id": NODE, "reconcile": "get_spot_clone"}
    assert len(calls) == 1

    def collision(req: Any, timeout: Any = None) -> Response:
        if req.full_url.endswith("/api/agent/events"):
            return Response({})
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {},
                                     io.BytesIO(b'{"detail":{"reason":"node_id_collision"}}'))

    refusal = payload(HextileMcpServer(client=Client(opener=collision)).call_tool("clone_spot", SAVED))
    assert refusal["status_code"] == 409
    assert refusal["kind"] == "http"


def test_incomplete_success_is_unknown_not_a_completed_node() -> None:
    def incomplete(_req: Any, timeout: Any = None) -> Response:
        return Response({})

    saved = payload(HextileMcpServer(client=Client(opener=incomplete)).call_tool("clone_spot", SAVED))
    assert saved["kind"] == "spot_clone_outcome_unknown"
    assert saved["receipt"]["node_id"] == NODE

    live = {"target": "live", "render_id": "r", "node_id": NODE,
            "expected_head": PARENT, "prepared_operation_id": "prepared-1",
            "expected_mask_fingerprint": "f" * 64}
    internal = HextileMcpServer(client=Client(opener=incomplete, internal_mode=True), child_token="private-child")
    pending = payload(internal.call_tool("clone_spot", live, operation_id="approved-op"))
    assert pending["kind"] == "spot_clone_outcome_unknown"
    assert pending["receipt"]["node_id"] == NODE
