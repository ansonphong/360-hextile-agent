"""Hotkeys MCP wire, approval provenance and uncertain-write contract."""
import http.client
import io
import json
import socket
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from hextile_client import Client
from hextile_mcp import HextileMcpServer, TOOLS

CASES = {
    "list_hotkeys": ("list", {}),
    "inspect_hotkey": ("inspect", {"id": "app.settings", "bindings": [{"key": "slash", "meta_key": True}]}),
    "set_hotkey_bindings": ("set", {"id": "app.settings", "bindings": [{"key": "slash", "meta_key": True}], "revision": "rev-one"}),
    "remove_hotkey_binding": ("remove", {"id": "app.settings", "binding": {"key": "slash", "meta_key": True}, "revision": "rev-one"}),
    "restore_hotkey": ("restore", {"id": "app.settings", "revision": "rev-one"}),
    "reset_hotkeys": ("reset", {"revision": "rev-one"}),
}


class Response:
    status = 200

    def __init__(self, data):
        self.raw = json.dumps(data).encode()

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


def payload(result):
    return json.loads(result["content"][0]["text"])


def test_catalog_has_closed_model_safe_bindings_and_approval_annotations():
    tools = {row["name"]: row for row in TOOLS}
    for name, (op, args) in CASES.items():
        row = tools[name]
        assert row["annotations"]["readOnlyHint"] == (op in {"list", "inspect"})
        assert row["annotations"]["destructiveHint"] == (op in {"remove", "reset"})
        schema = row["inputSchema"]
        assert schema["additionalProperties"] is False
        assert set(args) <= schema["properties"].keys()
        assert '"meta"' not in json.dumps(schema)
        if op not in {"list", "inspect"}:
            assert "revision" in schema["required"]
    binding = tools["set_hotkey_bindings"]["inputSchema"]["properties"]["bindings"]
    assert binding["maxItems"] == 8
    assert binding["items"]["additionalProperties"] is False
    assert "meta_key" in binding["items"]["properties"]


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("internal", [False, True])
def test_exact_wire_receipt_and_private_or_external_authority(name, internal):
    seen = []
    receipt = {"revision": "rev-two", "id": "app.settings", "effective_bindings": [{"key": "slash", "meta_key": True}]}

    def opener(req, timeout=None):
        if req.full_url.endswith("/api/agent/hotkeys/requests"):
            seen.append((req, timeout))
        return Response({"ok": True, "data": receipt})

    server = HextileMcpServer(client=Client(opener=opener, internal_mode=internal), child_token="child" if internal else "")
    op, args = CASES[name]
    result = server.call_tool(name, args, operation_id="approved-op" if internal else None)
    assert result["isError"] is False
    assert payload(result) == receipt
    assert len(seen) == 1
    req, timeout = seen[0]
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {"op": op, "args": args}
    assert timeout >= 25
    assert req.get_header("X-hextile-copilot-token") == ("child" if internal else None)
    assert req.get_header("X-hextile-op") == ("approved-op" if internal else None)
    assert req.get_header("X-hextile-agent") == (None if internal else "mcp")


@pytest.mark.parametrize("failure", ["timeout", "disconnect", "delivered", "malformed"])
def test_uncertain_write_never_replays(failure):
    calls = []

    def opener(req, timeout=None):
        if not req.full_url.endswith("/api/agent/hotkeys/requests"):
            return Response({})
        calls.append(req)
        if failure == "timeout":
            raise socket.timeout("lost reply")
        if failure == "disconnect":
            raise http.client.RemoteDisconnected("lost reply")
        if failure == "delivered":
            raise urllib.error.HTTPError(req.full_url, 504, "Timeout", {}, io.BytesIO(b'{"detail":{"code":"outcome_unknown"}}'))
        return Response({})

    result = HextileMcpServer(client=Client(opener=opener)).call_tool("reset_hotkeys", {"revision": "rev-one"})
    assert result["isError"] is True
    assert payload(result)["kind"] == "hotkeys_outcome_unknown"
    assert len(calls) == 1


def test_conflict_details_and_stable_code_survive_without_raw_body():
    conflicts = [{"id": f"action-with-a-long-but-valid-catalog-identifier-{i}", "binding": {"key": "slash", "meta_key": True}} for i in range(8)]

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(json.dumps({"detail": {"code": "hotkey_conflict", "conflicts": conflicts}}).encode()))

    result = payload(HextileMcpServer(client=Client(opener=opener)).call_tool("set_hotkey_bindings", CASES["set_hotkey_bindings"][1]))
    assert result["kind"] == "http"
    assert result["code"] == "hotkey_conflict"
    assert result["conflicts"] == conflicts
    assert "body" not in result


def test_list_continuation_schema_and_opaque_cursor_round_trip():
    schema = next(row["inputSchema"] for row in TOOLS if row["name"] == "list_hotkeys")
    assert not schema.get("required")
    assert schema["properties"]["cursor"]["type"] == "string"
    assert schema["properties"]["cursor"]["maxLength"] == 128
    assert schema["additionalProperties"] is False
    seen = []
    page = {"revision": "rev-one", "actions": [], "total": 109, "next_cursor": "rev-one:40"}

    def opener(req, timeout=None):
        if req.full_url.endswith("/api/agent/hotkeys/requests"):
            seen.append(json.loads(req.data))
        return Response({"ok": True, "data": page})

    server = HextileMcpServer(client=Client(opener=opener))
    assert payload(server.call_tool("list_hotkeys", {"cursor": "rev-one:20"})) == page
    assert seen == [{"op": "list", "args": {"cursor": "rev-one:20"}}]
