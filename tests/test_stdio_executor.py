"""Stdio executor: sequential tools/call replies are never dropped."""

from __future__ import annotations

import json
import select
import subprocess
import sys
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
