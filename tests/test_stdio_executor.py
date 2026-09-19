"""Stdio executor: sequential tools/call replies are never dropped."""

from __future__ import annotations

import io
import json
import select
import subprocess
import sys
import time
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
    ]
    stdin = io.StringIO("".join(json.dumps(row) + "\n" for row in rows))
    stdout = io.StringIO()
    monkeypatch.setattr(hextile_mcp, "HextileMcpServer", FakeServer)
    monkeypatch.setattr(hextile_mcp.sys, "stdin", stdin)
    monkeypatch.setattr(hextile_mcp.sys, "stdout", stdout)

    assert hextile_mcp.main() == 0
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [row["id"] for row in replies] == [2, 1]


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
