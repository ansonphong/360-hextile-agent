"""Stdio executor: sequential tools/call replies are never dropped."""

from __future__ import annotations

import io
import json
import select
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "mcp" / "hextile_mcp.py"


def test_sequential_tools_call_replies_not_dropped() -> None:
    expected = list(range(1, 13))
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT)],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdin is not None and proc.stdout is not None
    seen: list[int] = []
    try:
        for call_id in expected:
            proc.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": call_id,
                        "method": "tools/call",
                        "params": {
                            "name": "get_guide",
                            "arguments": {"name": "index"},
                        },
                    }
                )
                + "\n"
            )
            proc.stdin.flush()
            ready, _, _ = select.select([proc.stdout], [], [], 5)
            assert ready, f"no reply for id={call_id}"
            row = json.loads(proc.stdout.readline())
            seen.append(row.get("id"))
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)
    assert seen == expected


def test_control_lane_replies_while_ordinary_call_is_blocked(monkeypatch) -> None:  # noqa: ANN001
    sys.path.insert(0, str(ROOT / "mcp"))
    import hextile_mcp

    class FakeServer:
        notify = None

        def handle_rpc(self, msg):  # noqa: ANN001
            name = msg["params"]["name"]
            if name == "list_workflows":
                time.sleep(0.2)
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {"name": name}}

    rows = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_workflows", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_status", "arguments": {"run_id": "run_1"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_seed_job", "arguments": {"job_id": "seed_1"}}},
    ]
    stdin = io.StringIO("".join(json.dumps(row) + "\n" for row in rows))
    stdout = io.StringIO()
    monkeypatch.setattr(hextile_mcp, "HextileMcpServer", FakeServer)
    monkeypatch.setattr(hextile_mcp.sys, "stdin", stdin)
    monkeypatch.setattr(hextile_mcp.sys, "stdout", stdout)

    assert hextile_mcp.main() == 0
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [row["id"] for row in replies] == [2, 3, 1]


def test_internal_meta_headers_and_activity_suppression() -> None:
    # Kept in the agent-focused file because this is the canonical proxy contract.
    sys.path.insert(0, str(ROOT / "mcp"))
    from hextile_mcp import HextileMcpServer

    seen: list[tuple[str, str | None, str | None]] = []

    class FakeClient:
        internal_mode = True

        def request_headers(self, headers):  # noqa: ANN001
            class C:
                def __enter__(self_nonlocal):
                    seen.append(("headers", headers.get("X-Hextile-Copilot-Token"), headers.get("X-Hextile-Op")))

                def __exit__(self_nonlocal, *args):
                    return None

            return C()

        def get_status(self, run_id):
            seen.append(("status", run_id, None))
            return {"run_id": run_id, "status": "running"}

        def post_activity(self, _envelope):
            raise AssertionError("internal mode must suppress activity")

    server = HextileMcpServer(client=FakeClient(), child_token="child-secret")
    response = server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "get_status",
                "arguments": {"run_id": "run_1", "actor": "external"},
                "_meta": {"hextile_op": "op_1"},
            },
        }
    )
    assert response is not None and response["result"]["isError"] is False
    assert seen == [("headers", "child-secret", "op_1"), ("status", "run_1", None)]


def test_mcp_activity_minilog_projects_only_confirmed_safe_facts() -> None:
    sys.path.insert(0, str(ROOT / "mcp"))
    from hextile_mcp import HextileMcpServer

    class FakeClient:
        def __init__(self):
            self.events = []

        def request_json(self, method, path):  # noqa: ANN001
            assert (method, path) == ("GET", "/api/agent/mcp-status")
            return {"enabled": True}

        def post_activity(self, envelope):  # noqa: ANN001
            self.events.append(envelope)

    client = FakeClient()
    server = HextileMcpServer(client=client, child_token="")
    server.client_name = "Codex <private-host-name>"
    server._handlers["get_status"] = lambda _args: {"run_id": "run_1", "status": "running"}
    server._handlers["set_shader_parameter"] = lambda _args: {"status": "pending", "ticket_id": "ticket_1"}
    server._handlers["save_workflow"] = lambda _args: {"id": "new_workflow", "document": "secret-document"}

    assert server.call_tool("get_status", {"run_id": "run_1"})["isError"] is False
    assert server.call_tool("set_shader_parameter", {"witness": {"shader": {"shaderId": "aurora"}},
                                                     "value": "secret-value"})["isError"] is False
    assert server.call_tool("save_workflow", {"id": "new_workflow", "document": {"prompt": "secret-prompt"}})["isError"] is False
    assert [row.get("outcome") for row in client.events if row["phase"] == "succeeded"] == [None, "pending", "saved"]
    assert client.events[0]["subject"] == {"kind": "run", "id": "run_1"}
    assert client.events[2]["subject"] == {"kind": "shader", "id": "aurora"}
    assert client.events[3]["ticket_id"] == "ticket_1"
    assert client.events[4]["subject"] == {"kind": "workflow", "id": "new_workflow"}
    assert all(row["client_name"] == "Codex" for row in client.events)
    assert all("secret" not in json.dumps(row) and "private" not in json.dumps(row) for row in client.events)


def test_describe_image_exact_body_timeout_and_text_only_result() -> None:
    sys.path.insert(0, str(ROOT / "mcp"))
    from hextile_client import Client
    from hextile_mcp import HextileMcpServer

    seen = []

    class Response:
        status = 200

        def read(self):  # noqa: ANN201
            return json.dumps({
                "prompt": "A mountain at dusk", "mode": "crop",
                "image_signature": "sha256-test", "image_base64": "SHOULD_NOT_LEAK",
            }).encode()

        def __enter__(self):  # noqa: ANN201
            return self

        def __exit__(self, *args):  # noqa: ANN002, ANN201
            return None

    def opener(req, timeout):  # noqa: ANN001, ANN201
        seen.append((req.get_method(), req.full_url, json.loads(req.data), timeout))
        return Response()

    server = HextileMcpServer(client=Client(opener=opener), child_token="test")
    source = {"kind": "render_node", "render_id": "render_1", "node_id": "node_2"}
    crop = {"yaw": 15, "pitch": -10, "fov": 70}
    result = server.call_tool("describe_image", {"source": source, "crop": crop})
    assert result["isError"] is False
    assert seen == [(
        "POST", "http://127.0.0.1:8000/api/prompts/describe-source",
        {"source": source, "crop": crop}, 300.0,
    )]
    assert len(result["content"]) == 1 and result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "prompt": "A mountain at dusk", "source": source, "mode": "crop",
        "crop": crop, "image_signature": "sha256-test",
    }
    assert "SHOULD_NOT_LEAK" not in result["content"][0]["text"]

    for rejected in (
        {"source": {**source, "image_base64": "AA=="}},
        {"source": {"kind": "local_file", "path": "https://example.com/image.jpg"}},
        {"source": source, "crop": {**crop, "bitmap": "AA=="}},
        {"source": source, "image_base64": "AA=="},
    ):
        assert server.call_tool("describe_image", rejected)["isError"] is True
    assert len(seen) == 1


def test_external_gate_checks_each_call_before_activity_or_dispatch() -> None:
    sys.path.insert(0, str(ROOT / "mcp"))
    from hextile_client import Client
    from hextile_mcp import HextileMcpServer

    enabled = False
    paths: list[str] = []

    class Response:
        status = 200

        def __init__(self, payload):  # noqa: ANN001
            self.payload = payload

        def read(self):  # noqa: ANN201
            return json.dumps(self.payload).encode()

        def __enter__(self):  # noqa: ANN201
            return self

        def __exit__(self, *args):  # noqa: ANN002, ANN201
            return None

    def opener(req, timeout):  # noqa: ANN001, ANN201
        nonlocal enabled
        assert req.get_header("X-hextile-agent") == "mcp"
        paths.append(req.full_url.removeprefix("http://127.0.0.1:8000"))
        if paths[-1] == "/api/agent/mcp-status":
            return Response({"enabled": enabled})
        if paths[-1] == "/api/agent/events":
            return Response({"ok": True})
        assert paths[-1] == "/api/renders/run_1"
        return Response({"run_id": "run_1", "status": "running"})

    server = HextileMcpServer(client=Client(opener=opener), child_token="")
    listed = server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert listed is not None and listed["result"]["tools"]
    assert paths == []
    denied = server.call_tool("get_status", {"run_id": "run_1"})
    assert denied["isError"] is True
    assert "disabled" in denied["content"][0]["text"]
    assert paths == ["/api/agent/mcp-status"]

    enabled = True
    allowed = server.call_tool("get_status", {"run_id": "run_1"})
    assert allowed["isError"] is False
    assert paths[1] == "/api/agent/mcp-status"
    assert "/api/renders/run_1" in paths[2:]

    paths.clear()
    enabled = False
    assert server.call_tool("get_status", {"run_id": "run_1"})["isError"] is True
    assert paths == ["/api/agent/mcp-status"]


def test_external_gate_fails_closed_on_bad_status() -> None:
    sys.path.insert(0, str(ROOT / "mcp"))
    from hextile_client import Client
    from hextile_mcp import HextileMcpServer

    class Response:
        status = 200

        def __init__(self, raw):  # noqa: ANN001
            self.raw = raw

        def read(self):  # noqa: ANN201
            return self.raw

        def __enter__(self):  # noqa: ANN201
            return self

        def __exit__(self, *args):  # noqa: ANN002, ANN201
            return None

    for raw in (b"{}", b'{"enabled": 1}', b'{"enabled": "true"}', b"[]", b"not json", None, 404, 503):
        paths = []

        def opener(req, timeout):  # noqa: ANN001, ANN201
            paths.append(req.full_url)
            assert req.full_url.endswith("/api/agent/mcp-status")
            if raw is None:
                raise urllib.error.URLError("offline")
            if isinstance(raw, int):
                raise urllib.error.HTTPError(req.full_url, raw, "unavailable", {}, io.BytesIO(b""))
            return Response(raw)

        server = HextileMcpServer(client=Client(opener=opener), child_token="")
        result = server.call_tool("get_status", {"run_id": "run_1"})
        assert result["isError"] is True
        assert len(paths) == 1


def test_request_marker_is_unoverridable_and_private_token_is_backend_checked() -> None:
    sys.path.insert(0, str(ROOT / "mcp"))
    from hextile_client import Client
    from hextile_mcp import HextileMcpServer

    seen = []

    class Response:
        status = 200

        def read(self):  # noqa: ANN201
            return b'{"run_id":"run_1","status":"running"}'

        def __enter__(self):  # noqa: ANN201
            return self

        def __exit__(self, *args):  # noqa: ANN002, ANN201
            return None

    def opener(req, timeout):  # noqa: ANN001, ANN201
        headers = {key.lower(): value for key, value in req.header_items()}
        seen.append((req.full_url, headers))
        assert headers["x-hextile-agent"] == "mcp"
        if headers.get("x-hextile-copilot-token") == "fake":
            raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b'{"detail":"mcp_disabled"}'))
        return Response()

    client = Client(opener=opener, internal_mode=True)
    with client.request_headers({"X-Hextile-Agent": "unmarked"}):
        client.request_json("GET", "/test", headers={"x-hextile-agent": "unmarked"})

    real = HextileMcpServer(client=client, child_token="real")
    assert real.call_tool("get_status", {"run_id": "run_1"})["isError"] is False
    assert seen[-1][1]["x-hextile-copilot-token"] == "real"
    assert all(not url.endswith("/api/agent/mcp-status") for url, _ in seen)

    fake = HextileMcpServer(client=client, child_token="fake")
    denied = fake.call_tool("get_status", {"run_id": "run_1"})
    assert denied["isError"] is True
    assert seen[-1][1]["x-hextile-copilot-token"] == "fake"
    assert seen[-1][1]["x-hextile-agent"] == "mcp"
