"""Local drift + handshake tests for hextile-agent (plugin repo only).

- Tool names mentioned in SKILL.md ⊆ MCP TOOL_NAMES
- Exactly 7 tools; OPEN-4 annotation sets partition
- Client maps connection refusal → app-down wording
- Probe maps 404 on workflows/run → upgrade
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import socket
import sys
import urllib.error
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = ROOT / "mcp"
sys.path.insert(0, str(MCP_DIR))

from hextile_client import (  # noqa: E402
    APP_DOWN_MSG,
    UPGRADE_MSG,
    Client,
    HextileClientError,
)
from hextile_mcp import (  # noqa: E402
    TOOL_NAMES,
    TOOLS,
    HextileMcpServer,
    _MUTATING,
    _READ_ONLY,
)


EXPECTED_SEVEN = (
    "list_workflows",
    "get_workflow",
    "run_workflow",
    "validate_config",
    "get_status",
    "cancel_run",
    "generate_seed",
)


def test_exactly_seven_tools() -> None:
    assert tuple(TOOL_NAMES) == EXPECTED_SEVEN
    assert len(TOOLS) == 7
    assert {t["name"] for t in TOOLS} == set(EXPECTED_SEVEN)


def test_open4_annotations_partition() -> None:
    assert _READ_ONLY | _MUTATING == set(TOOL_NAMES)
    assert _READ_ONLY.isdisjoint(_MUTATING)
    for t in TOOLS:
        ann = t.get("annotations") or {}
        name = t["name"]
        if name in _READ_ONLY:
            assert ann.get("readOnlyHint") is True
        else:
            assert ann.get("readOnlyHint") is False


def test_skill_tool_names_subset_of_mcp() -> None:
    skill = (ROOT / "skills" / "hextile" / "SKILL.md").read_text(encoding="utf-8")
    # Backtick-fenced tool names and bare mentions in the tools table.
    mentioned = set(re.findall(r"`([a-z_]+)`", skill))
    # Only care about identifiers that look like our tools.
    toolish = {m for m in mentioned if m in set(TOOL_NAMES) or m in EXPECTED_SEVEN}
    # Every canonical tool must appear in the skill.
    missing_in_skill = set(TOOL_NAMES) - toolish
    assert not missing_in_skill, f"SKILL.md missing tools: {missing_in_skill}"
    # No toolish names outside the MCP list.
    extra = toolish - set(TOOL_NAMES)
    assert not extra, f"SKILL.md mentions unknown tools: {extra}"


def test_skill_has_no_stale_spellings() -> None:
    skill = (ROOT / "skills" / "hextile" / "SKILL.md").read_text(encoding="utf-8")
    # Stale spellings must not appear as positive instructions.
    # "not `/api/lora-360`" is allowed once as a negation — flag raw path usage.
    assert ".workflow.json" not in skill
    assert "hextile mcp" not in skill.lower()
    # Forbid positive /api/lora-360 endpoint (allow "not … lora-360").
    for m in re.finditer(r"/api/lora-360", skill):
        start = max(0, m.start() - 20)
        window = skill[start : m.end() + 5]
        assert "not" in window.lower(), f"stale positive path near: {window!r}"


def test_client_connection_refused_message() -> None:
    def boom(_req, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError(ConnectionRefusedError("Connection refused"))

    client = Client(opener=boom)
    with pytest.raises(HextileClientError) as ei:
        client.list_workflows()
    err = ei.value
    assert err.kind == "app_down"
    assert "isn't running" in str(err)
    assert "Launch 360 Hextile" in str(err)
    assert APP_DOWN_MSG in str(err)


def test_client_404_run_is_upgrade() -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"detail":"Not Found"}'),
        )

    client = Client(opener=boom)
    with pytest.raises(HextileClientError) as ei:
        client.run_workflow(workflow_id="quick-scout", dry_run=True)
    err = ei.value
    assert err.kind == "upgrade"
    assert "Upgrade 360 Hextile" in str(err)
    assert UPGRADE_MSG in str(err)


def test_probe_422_means_ok() -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=422,
            msg="Unprocessable",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"detail":"workflow_id is required"}'),
        )

    client = Client(opener=boom)
    result = client.probe()
    assert result["ok"] is True


def test_mcp_list_workflows_app_down_is_tool_error() -> None:
    def boom(_req, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError(ConnectionRefusedError("Connection refused"))

    server = HextileMcpServer(client=Client(opener=boom))
    result = server.call_tool("list_workflows", {})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "isn't running" in text or "Launch 360 Hextile" in text


def test_mcp_tools_list_rpc() -> None:
    server = HextileMcpServer(client=Client(opener=lambda *a, **k: None))
    resp = server.handle_rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert resp is not None
    names = [t["name"] for t in resp["result"]["tools"]]
    assert names == list(EXPECTED_SEVEN)


def test_mcp_initialize() -> None:
    server = HextileMcpServer()
    resp = server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
    )
    assert resp is not None
    assert resp["result"]["serverInfo"]["name"] == "hextile"
    assert "tools" in resp["result"]["capabilities"]


def test_py_files_compile() -> None:
    import py_compile

    for rel in (
        "mcp/hextile_client.py",
        "mcp/hextile_mcp.py",
        "codex/install.py",
        "tests/test_tool_list_drift.py",
    ):
        py_compile.compile(str(ROOT / rel), doraise=True)
