"""Local drift + handshake tests for hextile-agent (plugin repo only).

- Tool names mentioned in SKILL.md ⊆ MCP TOOL_NAMES
- Tool count matches TOOL_NAMES; OPEN-4 annotation sets partition
- Client maps connection refusal → app-down wording
- Probe maps 404 on workflows/run → upgrade
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
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
    GUIDE_NAMES,
    TOOL_NAMES,
    TOOLS,
    HextileMcpServer,
    _MUTATING,
    _READ_ONLY,
    load_guide,
)

INSTALL_SPEC = importlib.util.spec_from_file_location(
    "hextile_codex_install", ROOT / "codex" / "install.py"
)
assert INSTALL_SPEC is not None and INSTALL_SPEC.loader is not None
INSTALL_MODULE = importlib.util.module_from_spec(INSTALL_SPEC)
INSTALL_SPEC.loader.exec_module(INSTALL_MODULE)


EXPECTED_TOOLS = (
    "list_workflows",
    "get_workflow",
    "get_capabilities",
    "save_workflow",
    "delete_workflow",
    "run_workflow",
    "validate_config",
    "get_status",
    "get_render_config",
    "get_logs",
    "list_runs",
    "cancel_run",
    "retry_run",
    "generate_seed",
    "list_360_loras",
    "get_guide",
)


def test_tool_names_match_surface() -> None:
    assert tuple(TOOL_NAMES) == EXPECTED_TOOLS
    assert len(TOOLS) == len(EXPECTED_TOOLS)
    assert {t["name"] for t in TOOLS} == set(EXPECTED_TOOLS)


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
    tools_section = re.search(
        r"^## Tools$(.*?)(?=^## |\Z)", skill, re.MULTILINE | re.DOTALL
    )
    assert tools_section is not None, "SKILL.md tools section missing"
    toolish = set(
        re.findall(
            r"^\|\s*`([a-z_][a-z0-9_]*)`\s*\|",
            tools_section.group(1),
            re.MULTILINE,
        )
    )
    # Every canonical tool must appear in the skill.
    missing_in_skill = set(TOOL_NAMES) - toolish
    assert not missing_in_skill, f"SKILL.md missing tools: {missing_in_skill}"
    # No toolish names outside the MCP list.
    extra = toolish - set(TOOL_NAMES)
    assert not extra, f"SKILL.md mentions unknown tools: {extra}"


def test_codex_installer_removes_complete_bare_mcp_section() -> None:
    config = """\
[mcp_servers.hextile]
command = "python3"
args = ["/old/mcp.py", "--legacy"]
env = { HEXTILE_TEST = "1" }

[features]
apps = true
"""
    cleaned = INSTALL_MODULE._remove_hextile_section(config)
    assert "mcp_servers.hextile" not in cleaned
    assert "/old/mcp.py" not in cleaned
    assert cleaned.strip() == '[features]\napps = true'


def test_codex_installer_toml_escapes_mcp_script_path(tmp_path: Path) -> None:
    script = tmp_path / 'quoted "dir"' / "hextile_mcp.py"
    block = INSTALL_MODULE.mcp_block(script)
    escaped = json.dumps(str(script.resolve()))
    assert f"args = [{escaped}]" in block


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
    assert names == list(EXPECTED_TOOLS)


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


def test_save_workflow_rejects_builtin() -> None:
    client = Client(opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HTTP")))
    with pytest.raises(HextileClientError) as ei:
        client.save_workflow("builtin", "x", {"pipeline": "sd21"})
    assert ei.value.status_code == 403
    assert "immutable" in str(ei.value).lower()


def test_delete_workflow_rejects_builtin() -> None:
    client = Client(opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HTTP")))
    with pytest.raises(HextileClientError) as ei:
        client.delete_workflow("builtin", "quick-scout")
    assert ei.value.status_code == 403


def test_list_runs_sends_lifecycle_query() -> None:
    seen: dict[str, str] = {}

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return json.dumps({"data": {"renders": []}}).encode()

        def getcode(self) -> int:
            return 200

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def opener(req, timeout=None):  # noqa: ANN001
        seen["url"] = req.full_url
        return _Resp()

    client = Client(opener=opener)
    client.list_runs("archived")
    assert "lifecycle_status=archived" in seen["url"]
    assert seen["url"].rstrip("/").endswith("renders") or "/renders/" in seen["url"]


def test_get_guide_reads_real_markdown() -> None:
    listed = load_guide("index")
    assert set(listed["guides"]) == set(GUIDE_NAMES)
    schema = load_guide("workflow-schema")
    body = schema["markdown"]
    assert "HextileConfig" in body
    assert "input.source" in body or "`file` or `render`" in body
    site = load_guide("website-index")
    assert "360hextile.com/docs/hextile/" in site["markdown"]
    assert "/docs/automation" in site["markdown"]  # says there is NO such page


def test_mcp_get_guide_and_save_builtin() -> None:
    server = HextileMcpServer()
    ok = server.call_tool("get_guide", {"name": "recipes"})
    assert ok["isError"] is False
    assert "list_360_loras" in ok["content"][0]["text"]
    err = server.call_tool(
        "save_workflow",
        {"origin": "builtin", "id": "nope", "document": {"pipeline": "sd21"}},
    )
    assert err["isError"] is True
    assert "immutable" in err["content"][0]["text"].lower()


def test_codex_install_copies_references(tmp_path: Path) -> None:
    INSTALL_MODULE.ensure_skills(tmp_path, dry_run=False)
    dest = tmp_path / ".agents" / "skills" / "hextile" / "references" / "best-practices.md"
    assert dest.is_file()
    assert "Authority" in dest.read_text(encoding="utf-8")


def test_py_files_compile() -> None:
    import py_compile

    for rel in (
        "mcp/hextile_client.py",
        "mcp/hextile_mcp.py",
        "codex/install.py",
        "tests/test_tool_list_drift.py",
    ):
        py_compile.compile(str(ROOT / rel), doraise=True)
