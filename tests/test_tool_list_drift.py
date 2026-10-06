"""Local drift + handshake tests for hextile-agent (plugin repo only).

- Tool names mentioned in SKILL.md ⊆ MCP TOOL_NAMES
- Tool count matches TOOL_NAMES; OPEN-4 annotation sets partition
- Client maps connection refusal → app-down wording
- Probe maps 404 on workflows/run → upgrade
"""

from __future__ import annotations

import ast
import http.client
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
import jsonschema

ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = ROOT / "mcp"
sys.path.insert(0, str(MCP_DIR))

from hextile_client import (  # noqa: E402
    APP_DOWN_MSG,
    BATCH_UNAVAILABLE_MSG,
    UPGRADE_MSG,
    Client,
    HextileClientError,
    error_payload,
)
from hextile_mcp import (  # noqa: E402
    GUIDE_NAMES,
    TOOL_NAMES,
    TOOLS,
    HextileMcpServer,
    _BATCH_TOOLS,
    _DESTRUCTIVE,
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


# P5 copies this name list as a frozenset tripwire in APP backend/api/routes/agent_events.py.
EXPECTED_TOOLS = (
    "list_hotkeys",
    "inspect_hotkey",
    "set_hotkey_bindings",
    "remove_hotkey_binding",
    "restore_hotkey",
    "reset_hotkeys",
    "list_workflows",
    "get_workflow",
    "get_capabilities",
    "describe_image",
    "get_live_context",
    "apply_config_delta",
    "save_workflow",
    "delete_workflow",
    "run_workflow",
    "validate_config",
    "get_status",
    "get_gpu_diagnostics",
    "get_seed_memory_advice",
    "get_render_config",
    "get_logs",
    "list_runs",
    "search_library_prompts",
    "cancel_run",
    "retry_run",
    "generate_seed",
    "get_seed_job",
    "list_seed_history",
    "get_seed_batch",
    "cancel_seed",
    "list_360_loras",
    "list_installed_models",
    "get_model_readiness",
    "get_model_download_queue",
    "install_model",
    "repair_model",
    "cancel_model_download",
    "get_guide",
    "extract_sequence_video",
    "create_sequence",
    "start_sequence",
    "get_sequence",
    "list_sequences",
    "stop_sequence",
    "preflight_batch",
    "get_batch_preflight",
    "start_batch",
    "list_batches",
    "get_batch",
    "get_batch_items",
    "pause_batch",
    "resume_batch",
    "cancel_batch",
    "retry_batch",
    "import_batch_outputs",
    "get_unreal_export_catalog",
    "preflight_unreal_export",
    "export_to_unreal_project",
    "preflight_file_export",
    "export_render_file",
    "get_layer_draft",
    "generate_layer",
    "get_layer_generation",
    "cancel_layer_generation",
    "rematte_layer",
    "land_generated_layer",
    "commit_layer_draft",
    "preflight_spot_clone",
    "clone_spot",
    "get_spot_clone",
    "get_shader_context",
    "propose_shader",
    "open_shader_workspace",
    "register_shader_update",
    "get_shader_update",
)


def test_tool_names_match_surface() -> None:
    assert tuple(TOOL_NAMES) == EXPECTED_TOOLS
    assert len(TOOLS) == len(EXPECTED_TOOLS)
    assert {t["name"] for t in TOOLS} == set(EXPECTED_TOOLS)
    assert _BATCH_TOOLS <= set(EXPECTED_TOOLS)
    assert len(_BATCH_TOOLS) == 11


def test_open4_annotations_partition() -> None:
    assert _READ_ONLY | _MUTATING == set(TOOL_NAMES)
    assert _READ_ONLY.isdisjoint(_MUTATING)
    for t in TOOLS:
        ann = t.get("annotations") or {}
        name = t["name"]
        if name in _READ_ONLY:
            assert ann.get("readOnlyHint") is True
            assert ann.get("destructiveHint") is False
        else:
            assert ann.get("readOnlyHint") is False
        if name in _DESTRUCTIVE:
            assert ann.get("destructiveHint") is True
        else:
            assert ann.get("destructiveHint") is False
    for name in (
        "get_batch_preflight",
        "list_batches",
        "get_batch",
        "get_batch_items",
    ):
        assert name in _READ_ONLY
    for name in (
        "preflight_batch",
        "start_batch",
        "pause_batch",
        "resume_batch",
        "cancel_batch",
        "retry_batch",
        "import_batch_outputs",
    ):
        assert name in _MUTATING
    assert "cancel_batch" in _DESTRUCTIVE
    assert "get_unreal_export_catalog" in _READ_ONLY
    assert "preflight_unreal_export" in _READ_ONLY
    assert "export_to_unreal_project" in _MUTATING
    assert "preflight_file_export" in _READ_ONLY
    assert "export_render_file" in _MUTATING
    assert "describe_image" in _MUTATING
    assert "get_gpu_diagnostics" in _READ_ONLY
    assert "search_library_prompts" in _READ_ONLY
    assert "get_seed_memory_advice" in _READ_ONLY
    assert "retry_run" in _MUTATING
    assert {"get_model_readiness", "get_model_download_queue"} <= _READ_ONLY
    assert {"install_model", "repair_model", "cancel_model_download"} <= _MUTATING
    assert "cancel_model_download" in _DESTRUCTIVE
    assert {"preflight_spot_clone", "get_spot_clone"} <= _READ_ONLY
    assert "clone_spot" in _MUTATING
    assert "get_shader_context" in _READ_ONLY
    assert "propose_shader" in _MUTATING
    assert "propose_shader" not in _DESTRUCTIVE
    assert "get_shader_update" in _READ_ONLY
    assert {"open_shader_workspace", "register_shader_update"} <= _MUTATING
    assert not {"open_shader_workspace", "register_shader_update"} & _DESTRUCTIVE


def _names_from_assign(source: str, target: str) -> set[str]:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == target for t in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset":
            if value.args and isinstance(value.args[0], (ast.Set, ast.Tuple, ast.List)):
                value = value.args[0]
        if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            continue
        names: set[str] = set()
        for elt in value.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                names.add(elt.value)
        return names
    raise AssertionError(f"{target} assign not found")


def test_app_tool_names_ast_equals() -> None:
    app_path = ROOT.parent / "360-HEXTILE-APP" / "backend" / "api" / "routes" / "agent_events.py"
    assert app_path.is_file(), f"missing sibling APP inventory: {app_path}"
    app_names = _names_from_assign(app_path.read_text(encoding="utf-8"), "TOOL_NAMES")
    assert set(TOOL_NAMES) == app_names


def test_gpu_diagnostics_and_retry_client_routes() -> None:
    seen: list[tuple[str, str, Any]] = []

    class Response:
        status = 200

        def read(self) -> bytes:
            return b'{"device_index": 1, "accounting": {"nvml_sample_age_ms": 123}}'

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def opener(req: Any, timeout: Any = None) -> Response:
        seen.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
        return Response()

    client = Client(opener=opener)
    result = client.get_gpu_diagnostics()
    assert result["accounting"]["nvml_sample_age_ms"] == 123
    client.retry_run("render-1")
    client.retry_run("render-1", vram_recovery=False)
    client.retry_run("render-1", vram_recovery=True)
    assert seen == [
        ("GET", "http://127.0.0.1:8000/api/processors/vram-status", None),
        ("POST", "http://127.0.0.1:8000/api/renders/render-1/retry", None),
        ("POST", "http://127.0.0.1:8000/api/renders/render-1/retry", None),
        ("POST", "http://127.0.0.1:8000/api/renders/render-1/retry", {"vram_recovery": True}),
    ]


def test_gpu_diagnostics_tool_and_retry_mapping() -> None:
    client = mock.Mock()
    client.get_gpu_diagnostics.return_value = {"device_index": 1}
    server = HextileMcpServer(client=client)
    assert server._get_gpu_diagnostics({}) == {"device_index": 1}
    client.get_gpu_diagnostics.assert_called_once_with()
    server._retry_run({"run_id": "render-1"})
    server._retry_run({"run_id": "render-1", "vram_recovery": False})
    server._retry_run({"run_id": "render-1", "vram_recovery": True})
    assert client.retry_run.call_args_list == [
        mock.call("render-1", vram_recovery=False),
        mock.call("render-1", vram_recovery=False),
        mock.call("render-1", vram_recovery=True),
    ]
    assert _tool_schema("get_gpu_diagnostics")["properties"] == {}
    assert _tool_schema("retry_run")["properties"]["vram_recovery"]["type"] == "boolean"


def test_search_library_prompts_forwards_page_without_paths_or_retry() -> None:
    page = {
        "hits": [{"kind": "render", "id": "render-1", "snippet": "moonlight"}],
        "next_cursor": "opaque-next-page",
        "scanned": 3,
        "skipped": {"oversized": 0, "unreadable": 0},
    }
    args = {
        "query": "moonlight",
        "kinds": ["render", "sequence"],
        "lifecycle_status": "archived",
        "limit": 4,
        "cursor": "opaque-prior-page",
    }
    response = mock.MagicMock(status=200)
    response.read.return_value = json.dumps(page).encode("utf-8")
    response.__enter__.return_value = response
    opener = mock.Mock(return_value=response)
    server = HextileMcpServer(client=Client(opener=opener))
    assert server._search_library_prompts(args) == page
    opener.assert_called_once()
    request = opener.call_args.args[0]
    assert request.get_method() == "POST"
    assert request.full_url == "http://127.0.0.1:8000/api/renders/library-prompts/search"
    assert json.loads(request.data) == args
    schema = _tool_schema("search_library_prompts")
    assert schema["required"] == ["query"]
    assert set(schema["properties"]) == set(args)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["kinds"]["items"]["enum"] == ["render", "sequence"]
    assert schema["properties"]["kinds"]["maxItems"] == 2
    assert schema["properties"]["lifecycle_status"]["enum"] == ["active", "archived", "trashed"]
    assert schema["properties"]["limit"]["maximum"] == 50
    assert schema["properties"]["cursor"]["maxLength"] == 1024


def test_seed_memory_advice_keeps_separate_samples_and_typed_errors() -> None:
    seen: list[str] = []
    gpu = {"card": {"free_gb": 8}, "accounting": {"nvml_sample_age_ms": 1200}}
    advice = {"risk": "caution", "free_gb": 7.5, "total_gb": 16}

    class Response:
        status = 200

        def __init__(self, data: Any) -> None:
            self.data = data

        def read(self) -> bytes:
            return json.dumps(self.data).encode()

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def opener(req: Any, timeout: Any = None) -> Response:
        seen.append(req.full_url)
        return Response(gpu if len(seen) == 1 else advice)

    result = HextileMcpServer(client=Client(opener=opener))._get_seed_memory_advice({
        "lora_path": "my lora", "width": 1600, "height": 800, "cpu_offload": True,
    })
    assert result == {"sampling": "separate", "gpu": gpu, "advice": advice}
    assert result["gpu"]["card"]["free_gb"] != result["advice"]["free_gb"]
    assert "nvml_sample_age_ms" not in result["advice"]
    assert seen == [
        "http://127.0.0.1:8000/api/processors/vram-status",
        "http://127.0.0.1:8000/api/360-lora/memory-advice?lora_path=my+lora&width=1600&height=800&cpu_offload=True",
    ]
    assert _tool_schema("get_seed_memory_advice")["required"] == ["lora_path", "width", "height"]

    def missing(_req: Any, timeout: Any = None) -> Any:
        raise urllib.error.HTTPError("/memory-advice", 422, "bad preset", {}, io.BytesIO(b"bad preset"))

    with pytest.raises(HextileClientError) as caught:
        Client(opener=missing).get_seed_memory_advice("lora", 1600, 800)
    assert caught.value.status_code == 422
    assert caught.value.kind == "http"


def test_model_tool_schemas_are_closed_and_targeted() -> None:
    for name in ("get_model_readiness", "get_model_download_queue", "install_model",
                 "repair_model", "cancel_model_download"):
        assert _tool_schema(name)["additionalProperties"] is False
    assert _tool_schema("get_model_download_queue")["properties"] == {}
    override = _tool_schema("get_model_readiness")["properties"]["overrides"]
    assert override["maxItems"] == 32
    assert override["items"]["additionalProperties"] is False
    assert override["items"]["required"] == ["pipeline_id", "model_id"]
    assert _tool_schema("install_model")["required"] == ["pipeline_id", "model_id", "bundle"]
    assert _tool_schema("cancel_model_download")["required"] == [
        "pipeline_id", "model_id", "expected_queue_entry_id"
    ]
    for name in ("install_model", "repair_model", "cancel_model_download"):
        assert "approved" not in _tool_schema(name)["properties"]
        assert "path" not in _tool_schema(name)["properties"]


def test_model_client_routes_and_unknown_outcome() -> None:
    seen: list[tuple[str, str, Any]] = []

    class Response:
        status = 200

        def __init__(self, bundle: bool = False) -> None:
            self.bundle = bundle

        def read(self) -> bytes:
            result: dict[str, Any] = {"success": True, "message": "Queued"}
            if self.bundle:
                result.update(model_dispositions={}, failed_model_ids=[])
            return json.dumps(result).encode("utf-8")

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def opener(req: Any, timeout: Any = None) -> Response:
        seen.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
        return Response("/queue/pipeline/" in req.full_url)

    client = Client(opener=opener)
    client.get_model_readiness([{"pipeline_id": "sdxl", "model_id": "base"}])
    client.get_model_download_queue()
    client.install_model_single("sdxl", "base")
    client.install_model_bundle("sdxl", "base", "fingerprint")
    client.repair_model("sdxl", "base/part")
    client.cancel_model_download("sdxl", "base/part", "entry-id")
    assert seen == [
        ("POST", "http://127.0.0.1:8000/api/models/selection-catalog",
         {"overrides": [{"pipeline_id": "sdxl", "model_id": "base"}]}),
        ("GET", "http://127.0.0.1:8000/api/models/queue", None),
        ("POST", "http://127.0.0.1:8000/api/models/queue/add",
         {"pipeline_id": "sdxl", "model_id": "base"}),
        ("POST", "http://127.0.0.1:8000/api/models/queue/pipeline/sdxl?selected_model_id=base&expected_selection_fingerprint=fingerprint", None),
        ("POST", "http://127.0.0.1:8000/api/models/sdxl/base%2Fpart/repair", None),
        ("POST", "http://127.0.0.1:8000/api/models/sdxl/base%2Fpart/cancel?expected_queue_entry_id=entry-id", None),
    ]

    def lost(_req: Any, timeout: Any = None) -> None:
        raise urllib.error.URLError(ConnectionResetError("lost acknowledgement"))

    with pytest.raises(HextileClientError) as ei:
        Client(opener=lost).repair_model("sdxl", "base")
    assert ei.value.kind == "model_outcome_unknown"
    assert "read the download queue" in str(ei.value)


@pytest.mark.parametrize("raw", [b"", b"{", b'"ok"', b'{"success":true}'])
def test_model_ambiguous_2xx_acknowledgement_is_unknown(raw: bytes) -> None:
    calls = 0

    class Response:
        status = 200

        def read(self) -> bytes:
            return raw

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def opener(_req: Any, timeout: Any = None) -> Response:
        nonlocal calls
        if not _req.full_url.endswith("/api/agent/events"):
            calls += 1
        return Response()

    result = HextileMcpServer(client=Client(opener=opener)).call_tool(
        "repair_model", {"pipeline_id": "sdxl", "model_id": "base"}
    )
    assert calls == 1
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["kind"] == "model_outcome_unknown"
    assert "read the download queue" in payload["error"]


def test_model_truncated_2xx_acknowledgement_is_unknown() -> None:
    class Response:
        status = 200

        def read(self) -> bytes:
            raise http.client.IncompleteRead(b'{"success":true', 3)

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    result = HextileMcpServer(client=Client(opener=lambda *_args, **_kwargs: Response())).call_tool(
        "install_model", {"pipeline_id": "sdxl", "model_id": "base", "bundle": False}
    )
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["kind"] == "model_outcome_unknown"


def test_model_action_rejection_retains_app_details_and_success_stays_success() -> None:
    queue_status = {"queued": [{"pipeline_id": "sdxl", "model_id": "base",
                                "queue_entry_id": "entry"}]}
    single_reject = {"success": False, "message": "Cannot queue", "queue_status": queue_status}
    partial_bundle = {
        "success": False, "message": "Added 1 required models to queue (1 rejected)",
        "queue_status": queue_status,
        "model_dispositions": {
            "base": {"disposition": "newly_queued", "reason": None},
            "refiner": {"disposition": "rejected", "reason": "Not enough disk"},
        },
        "failed_model_ids": ["refiner"],
    }
    admitted = {"success": True, "message": "Queued", "queue_status": queue_status}
    responses = [single_reject, partial_bundle, admitted]

    class Response:
        status = 200

        def __init__(self, data: dict[str, Any]) -> None:
            self.data = data

        def read(self) -> bytes:
            return json.dumps(self.data).encode("utf-8")

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def opener(req: Any, timeout: Any = None) -> Response:
        if req.full_url.endswith("/api/agent/events"):
            return Response({"ok": True})
        if req.full_url.endswith("/selection-catalog"):
            return Response({"models": [{"pipeline_id": "sdxl", "id": "base",
                                        "selection_fingerprint": "fp"}]})
        return Response(responses.pop(0))

    server = HextileMcpServer(client=Client(opener=opener))
    single = server.call_tool("install_model", {
        "pipeline_id": "sdxl", "model_id": "base", "bundle": False,
    })
    bundle = server.call_tool("install_model", {
        "pipeline_id": "sdxl", "model_id": "base", "bundle": True,
        "expected_selection_fingerprint": "fp",
    })
    success = server.call_tool("repair_model", {"pipeline_id": "sdxl", "model_id": "base"})

    assert single["isError"] is True
    assert json.loads(single["content"][0]["text"]) == single_reject
    assert bundle["isError"] is True
    assert json.loads(bundle["content"][0]["text"]) == partial_bundle
    assert success["isError"] is False
    assert json.loads(success["content"][0]["text"]) == admitted
    assert responses == []


def test_model_handlers_require_current_preconditions_and_exact_target() -> None:
    client = mock.Mock()
    server = HextileMcpServer(client=client)
    client.get_model_readiness.return_value = {"models": [{
        "pipeline_id": "sdxl", "id": "base", "selection_fingerprint": "fp",
    }]}
    client.get_model_download_queue.return_value = {"queued": [{
        "pipeline_id": "sdxl", "model_id": "base", "queue_entry_id": "entry",
    }], "downloads": []}
    server._install_model({"pipeline_id": "sdxl", "model_id": "base", "bundle": True,
                           "expected_selection_fingerprint": "fp"})
    client.get_model_readiness.assert_called_once_with([])
    client.install_model_bundle.assert_called_once_with("sdxl", "base", "fp")
    server._install_model({"pipeline_id": "sdxl", "model_id": "base", "bundle": False})
    client.install_model_single.assert_called_once_with("sdxl", "base")
    server._repair_model({"pipeline_id": "sdxl", "model_id": "base"})
    client.repair_model.assert_called_once_with("sdxl", "base")
    server._cancel_model_download({"pipeline_id": "sdxl", "model_id": "base",
                                   "expected_queue_entry_id": "entry"})
    client.cancel_model_download.assert_called_once_with("sdxl", "base", "entry")

    client.get_model_readiness.return_value = {"models": [{"pipeline_id": "sdxl", "id": "base"}]}
    with pytest.raises(HextileClientError):
        server._install_model({"pipeline_id": "sdxl", "model_id": "base", "bundle": True,
                               "expected_selection_fingerprint": "fp"})
    client.get_model_readiness.return_value = {"models": [{
        "pipeline_id": "sdxl", "id": "base", "selection_fingerprint": "new-fp",
    }]}
    with pytest.raises(HextileClientError) as stale_selection:
        server._install_model({"pipeline_id": "sdxl", "model_id": "base", "bundle": True,
                               "expected_selection_fingerprint": "fp"})
    assert stale_selection.value.status_code == 409
    client.get_model_download_queue.return_value = {"queued": [{
        "pipeline_id": "sdxl", "model_id": "base",
    }], "downloads": []}
    with pytest.raises(HextileClientError):
        server._cancel_model_download({"pipeline_id": "sdxl", "model_id": "base",
                                       "expected_queue_entry_id": "entry"})
    client.get_model_download_queue.return_value = {"queued": [{
        "pipeline_id": "sdxl", "model_id": "base", "queue_entry_id": "new-entry",
    }], "downloads": []}
    with pytest.raises(HextileClientError) as stale_entry:
        server._cancel_model_download({"pipeline_id": "sdxl", "model_id": "base",
                                       "expected_queue_entry_id": "entry"})
    assert stale_entry.value.status_code == 409
    with pytest.raises(HextileClientError):
        server._repair_model({"pipeline_id": "sdxl", "model_id": "base", "path": "/tmp/x"})
    with pytest.raises(HextileClientError):
        server._install_model({"pipeline_id": "sdxl", "model_id": "base", "bundle": False,
                               "approved": True})
    client.install_model_bundle.assert_called_once()
    client.cancel_model_download.assert_called_once()


@pytest.mark.parametrize("status", [403, 404, 409, 422])
def test_model_http_errors_remain_tool_errors(status: int) -> None:
    def rejected(req: Any, timeout: Any = None) -> None:
        raise urllib.error.HTTPError(
            url=req.full_url, code=status, msg="rejected", hdrs=None,
            fp=io.BytesIO(b'{"detail":"rejected"}'),
        )

    result = HextileMcpServer(client=Client(opener=rejected)).call_tool(
        "repair_model", {"pipeline_id": "sdxl", "model_id": "base"}
    )
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["status_code"] == status
    assert payload["kind"] == "http"


def test_skill_tool_names_subset_of_mcp() -> None:
    skill = (ROOT / "skills" / "360-hextile" / "SKILL.md").read_text(encoding="utf-8")
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
    skill = (ROOT / "skills" / "360-hextile" / "SKILL.md").read_text(encoding="utf-8")
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
    assert set(server._handlers) == set(EXPECTED_TOOLS)
    discovered = {tool["name"]: tool for tool in resp["result"]["tools"]}
    opened = discovered["open_shader_workspace"]["inputSchema"]
    got = discovered["get_shader_update"]["inputSchema"]
    registered = discovered["register_shader_update"]["inputSchema"]
    assert opened["properties"]["workspace_schema"] == {"type": "integer", "const": 2}
    assert "workspace_schema" not in opened["required"]
    assert got["properties"]["include_source_diagnostics"] == {"type": "boolean", "default": False}
    legacy = {"shader": {"origin": "project", "shaderId": "test"}, "project_context_witness": "wit",
              "studio_surface_id": "surface", "expected_working_revision": 1,
              "expected_document_hash": "sha256:" + "ab" * 32, "expected_source_hash": "sha256:" + "cd" * 32}
    absent = {key: value for key, value in {**legacy, "workspace_schema": 2}.items() if not key.startswith("expected_")}
    for args in (legacy, {**legacy, "workspace_schema": 2}, absent):
        jsonschema.validate(args, opened)
    for schema in (opened, got, registered):
        assert schema["additionalProperties"] is False
        assert not {"apply", "approved", "git", "source", "path", "complete_candidate"} & schema["properties"].keys()
    for args in ({**absent, "workspace_schema": 3}, {**absent, "expected_working_revision": 1}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(args, opened)
    update = {"handle": "sw_test", "change_id": "sw_test:1", "project_context_witness": "wit"}
    for args in (update, {**update, "include_source_diagnostics": False}, {**update, "include_source_diagnostics": True}):
        jsonschema.validate(args, got)
    for flag in (1, "true", None):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**update, "include_source_diagnostics": flag}, got)
    assert not any("git" in name or name in {"apply_shader", "compile_shader", "read_resource"} for name in names)
    assert discovered["get_shader_update"]["annotations"]["readOnlyHint"] is True
    assert discovered["open_shader_workspace"]["annotations"]["readOnlyHint"] is False
    assert discovered["register_shader_update"]["annotations"]["readOnlyHint"] is False
    for method in ("resources/list", "resources/read"):
        response = server.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": method, "params": {}})
        assert response["error"]["code"] == -32601


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
    assert resp["result"]["serverInfo"]["version"] == "0.5.2"
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in resp["result"]["capabilities"]
    instructions = resp["result"]["instructions"]
    for term in ("current_surface", "executable_capabilities", "execution_ready", "conventions/1", "modulation/2", "keyboard/mouse", "MIDI/OSC/gamepad"):
        assert term in instructions


def test_private_headers_are_request_local() -> None:
    seen: list[dict[str, str]] = []

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return b"{}"

        def getcode(self) -> int:
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def opener(req, timeout=None):  # noqa: ANN001
        seen.append({k.lower(): v for k, v in req.header_items()})
        return _Resp()

    client = Client(opener=opener, internal_mode=True)
    with client.request_headers(
        {"X-Hextile-Copilot-Token": "secret", "X-Hextile-Op": "op-a"}
    ):
        client.run_workflow(workflow_id="quick-scout")
    client.run_workflow(workflow_id="quick-scout")
    Client(opener=opener).run_workflow(workflow_id="quick-scout")

    assert seen[0]["x-hextile-copilot-token"] == "secret"
    assert seen[0]["x-hextile-op"] == "op-a"
    assert "x-hextile-agent" not in seen[0]
    assert "x-hextile-copilot-token" not in seen[1]
    assert "x-hextile-op" not in seen[1]
    assert seen[2]["x-hextile-agent"] == "mcp"


def test_seed_client_uses_exact_job_routes_and_preserves_result() -> None:
    seen: list[tuple[str, str, Any, float | None]] = []
    request_id = "b7519ceb-e344-464d-923c-ef16e02050e6"
    receipt = {
        "job_id": request_id,
        "status": "partial",
        "completed_variations": [
            {"index": 2, "id": "v2", "path": "D:/seeds/v2.png", "seed": 42}
        ],
        "variation_errors": [{"index": 0, "message": "failed"}],
        "error": {"code": "generation_failed", "message": "one variation failed"},
    }

    class _Resp:
        status = 200

        def __init__(self, body: Any) -> None:
            self.body = body

        def read(self) -> bytes:
            return json.dumps(self.body).encode()

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def opener(req, timeout=None):  # noqa: ANN001
        seen.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None, timeout))
        if req.get_method() == "GET":
            return _Resp(receipt)
        return _Resp({"job_id": request_id, "status": "accepted"})

    client = Client(opener=opener)
    ack = client.generate_seed("look", request_id=request_id, lora_path="lora", base_model="sdxl", n=4)
    assert ack == {"job_id": request_id, "status": "accepted"}
    assert seen[0][0:2] == ("POST", "http://127.0.0.1:8000/api/360-lora/jobs")
    assert seen[0][2] == {
        "request_id": request_id, "prompt": "look", "lora_path": "lora",
        "base_model": "sdxl", "num_variations": 4,
    }
    assert seen[0][3] == 30.0
    client.generate_seed(
        "look", request_id=request_id, lora_path="lora", base_model="sdxl", n=4,
        width=1600, height=800, resolution_preset="1600x800", cpu_offload=True,
        allow_low_vram=True,
    )
    assert seen[1][2] == {
        **seen[0][2], "width": 1600, "height": 800,
        "resolution_preset": "1600x800", "cpu_offload": True, "allow_low_vram": True,
    }
    assert client.get_seed_job(request_id) == receipt
    assert seen[2][0:2] == ("GET", f"http://127.0.0.1:8000/api/360-lora/jobs/{request_id}")
    client.cancel_seed(request_id)
    assert seen[3][0:2] == ("POST", f"http://127.0.0.1:8000/api/360-lora/jobs/{request_id}/cancel")
    with pytest.raises(HextileClientError, match="canonical UUID"):
        client.generate_seed("look", request_id="bad-id", lora_path="lora", base_model="sdxl")
    assert len(seen) == 4


def test_seed_tools_require_exact_ids_and_activity_keeps_seed_out_of_run() -> None:
    posted: list[dict[str, Any]] = []
    request_id = "b7519ceb-e344-464d-923c-ef16e02050e6"

    class FakeClient:
        def generate_seed(self, _prompt: str, **kwargs: Any) -> dict[str, Any]:
            return {"job_id": kwargs["request_id"], "run_id": "wrong-run", "status": "accepted"}

        def get_seed_job(self, job_id: str) -> dict[str, Any]:
            return {"job_id": job_id, "run_id": "wrong-run", "status": "partial", "completed_variations": [{"index": 2, "path": "D:/v2.png", "seed": 42}]}

        def cancel_seed(self, job_id: str) -> dict[str, Any]:
            return {"job_id": job_id, "run_id": "wrong-run", "status": "cancelling"}

        def post_activity(self, envelope: dict[str, Any]) -> None:
            posted.append(envelope)

    server = HextileMcpServer(client=FakeClient())  # type: ignore[arg-type]
    for name in ("generate_seed", "get_seed_job", "cancel_seed"):
        assert "job_id" in _tool_schema(name)["properties"] or name == "generate_seed"
        required = "request_id" if name == "generate_seed" else "job_id"
        assert required in _tool_schema(name)["required"]
        assert server.call_tool(name, {})["isError"] is True
    posted.clear()
    generated = server.call_tool("generate_seed", {
        "request_id": request_id, "prompt": "look", "lora_path": "lora", "base_model": "sdxl",
    })
    assert generated["isError"] is False
    got = server.call_tool("get_seed_job", {"job_id": request_id})
    assert got["isError"] is False
    assert json.loads(got["content"][0]["text"])["completed_variations"][0]["index"] == 2
    cancelled = server.call_tool("cancel_seed", {"job_id": request_id})
    assert cancelled["isError"] is False
    assert {event["seed"]["job_id"] for event in posted} == {request_id}
    assert not any("run" in event for event in posted)
    assert [event["seed"].get("status") for event in posted if event["phase"] != "started"] == ["accepted", "partial", "cancelling"]


def test_seed_tool_forwards_only_explicit_options() -> None:
    client = mock.Mock()
    server = HextileMcpServer(client=client)
    base = {
        "request_id": "b7519ceb-e344-464d-923c-ef16e02050e6",
        "prompt": "look", "lora_path": "lora", "base_model": "sdxl",
    }
    server._generate_seed(base)
    assert client.generate_seed.call_args == mock.call(
        "look", request_id=base["request_id"], lora_path="lora", base_model="sdxl", n=4,
    )
    server._generate_seed({
        **base, "width": 1600, "height": 800, "resolution_preset": "1600x800",
        "cpu_offload": False, "allow_low_vram": True, "approved": True,
    })
    assert client.generate_seed.call_args == mock.call(
        "look", request_id=base["request_id"], lora_path="lora", base_model="sdxl", n=4,
        width=1600, height=800, resolution_preset="1600x800",
        cpu_offload=False, allow_low_vram=True,
    )
    props = _tool_schema("generate_seed")["properties"]
    assert {"resolution_preset", "cpu_offload", "allow_low_vram"} <= set(props)
    assert "approved" not in props


def test_uncertain_seed_ack_names_recoverable_job_id() -> None:
    request_id = "b7519ceb-e344-464d-923c-ef16e02050e6"

    def timeout(_req: Any, timeout: Any = None) -> Any:
        raise urllib.error.URLError(TimeoutError("timed out"))

    client = Client(opener=timeout)
    with pytest.raises(HextileClientError) as caught:
        client.generate_seed("look", request_id=request_id, lora_path="lora", base_model="sdxl")
    assert caught.value.kind == "app_down"
    assert request_id in str(caught.value)
    assert "get_seed_job" in str(caught.value)


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
    guides = {name: load_guide(name)["markdown"] for name in GUIDE_NAMES}
    assert "workspace_schema:2" in guides["best-practices"]
    assert "include_source_diagnostics:true" in guides["best-practices"]
    assert "unused helpers" in guides["best-practices"]
    assert "mixed legacy Projects" in guides["best-practices"]
    assert "user's instruction" in guides["best-practices"]
    assert "accepted snapshot only" in guides["best-practices"]
    assert "source_files" in guides["recipes"]
    assert "register a new canonical change ID" in guides["recipes"]
    assert "only the human Applies" in guides["recipes"]
    assert "metadata/config" in guides["workflow-schema"]
    assert "No GLSL bodies belong in durable JSON" in guides["workflow-schema"]
    assert "source-modules/1" in guides["workflow-schema"]
    for name in ("gitignore.txt", "gitattributes.txt"):
        url = f"https://360hextile.com/static/templates/shaders/{name}"
        assert url in guides["best-practices"]
        assert url in guides["website-index"]
    assert "https://360hextile.com/docs/create/shader" in guides["website-index"]
    assert "https://360hextile.com/docs/user-guide/agent-mode" in guides["website-index"]
    schema = load_guide("workflow-schema")
    body = schema["markdown"]
    assert "HextileConfig" in body
    assert "input.source" in body or "`file` or `render`" in body
    site = load_guide("website-index")
    assert "360hextile.com/docs/hextile/" in site["markdown"]
    assert "/docs/automation" in site["markdown"]  # says there is NO such page
    client = Client(opener=lambda *_a, **_k: pytest.fail("guides must be local"))
    client.post_activity = mock.Mock()  # APP telemetry is independent of local guide reads.
    server = HextileMcpServer(client=client)
    for name in ("index", *GUIDE_NAMES):
        response = server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "get_guide", "arguments": {"name": name}}})
        result = response["result"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"]) == load_guide(name)
        if name != "index":
            assert load_guide(name)["markdown"].strip()
    refused = server.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                "params": {"name": "get_guide", "arguments": {"name": "../../private"}}})
    assert refused["result"]["isError"] is True
    assert "Unknown guide" in refused["result"]["content"][0]["text"]


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
    dest = tmp_path / ".agents" / "skills" / "360-hextile" / "references" / "best-practices.md"
    assert dest.is_file()
    assert "Authority" in dest.read_text(encoding="utf-8")


def _tool_schema(name: str) -> dict[str, Any]:
    for tool in TOOLS:
        if tool["name"] == name:
            return tool["inputSchema"]
    raise AssertionError(f"missing tool {name}")


def test_batch_tool_schemas_forbid_additional_properties() -> None:
    for name in sorted(_BATCH_TOOLS):
        schema = _tool_schema(name)
        assert schema.get("additionalProperties") is False
        for prop in (schema.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("type") == "object" and "properties" in prop:
                assert prop.get("additionalProperties") is False, name


def test_batch_unavailable_is_capability_error() -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=409,
            msg="Conflict",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"code":"batch_unavailable","message":"Batch workflows are disabled"}'),
        )

    client = Client(opener=boom)
    with pytest.raises(HextileClientError) as ei:
        client.list_batches()
    err = ei.value
    assert err.kind == "capability"
    assert err.status_code == 409
    assert BATCH_UNAVAILABLE_MSG in str(err)
    payload = error_payload(err)
    assert payload["ok"] is False
    assert payload["kind"] == "capability"
    assert payload["code"] == "batch_unavailable"

    server = HextileMcpServer(client=Client(opener=boom))
    result = server.call_tool("list_batches", {})
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "batch_unavailable" in text
    assert "unavailable" in text.lower()


def test_batch_missing_route_is_capability_error() -> None:
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
        client.preflight_batch(
            {
                "idempotency_key": "k",
                "workflow": {"kind": "catalog", "id": "upres-still", "origin": "builtin"},
                "input": {"kind": "folder", "path": "D:/in"},
                "output": {"directory": "D:/out", "format": "png"},
            }
        )
    assert ei.value.kind == "capability"


def test_unknown_batch_job_stays_http() -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"code":"unknown_job","message":"unknown job"}'),
        )

    client = Client(opener=boom)
    with pytest.raises(HextileClientError) as ei:
        client.get_batch("01Jmissing")
    assert ei.value.kind == "http"
    assert ei.value.status_code == 404


def test_get_batch_preflight_polls_scanning(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class FakeClient:
        def get_batch_preflight(self, preflight_id: str, **kwargs: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] < 3:
                return {"status": "scanning", "can_start": False, "preflight_id": preflight_id}
            return {
                "status": "ready",
                "can_start": True,
                "spec_hash": "sha256:abc",
                "preflight_id": preflight_id,
                "issues": [],
            }

    sleeps: list[float] = []
    monkeypatch.setattr("hextile_mcp.time.sleep", lambda s: sleeps.append(s))
    server = HextileMcpServer(client=FakeClient())  # type: ignore[arg-type]
    result = server.call_tool("get_batch_preflight", {"preflight_id": "pf1"})
    assert result["isError"] is False
    body = json.loads(result["content"][0]["text"])
    assert body["status"] == "ready"
    assert body["spec_hash"] == "sha256:abc"
    assert calls["n"] == 3
    assert sleeps == [2.0, 4.0]


def test_batch_activity_omits_run_id() -> None:
    posted: list[dict[str, Any]] = []

    class FakeClient:
        def start_batch(self, **kwargs: Any) -> dict[str, Any]:
            return {"job_id": "01Jbatch", "run_id": "01Jbatch", "status": "queued"}

        def post_activity(self, envelope: dict[str, Any]) -> None:
            posted.append(envelope)

        def cancel_batch(self, job_id: str, **kwargs: Any) -> dict[str, Any]:
            return {"job_id": job_id, "run_id": job_id, "status": "cancelled"}

    server = HextileMcpServer(client=FakeClient())  # type: ignore[arg-type]
    ok = server.call_tool(
        "start_batch",
        {
            "preflight_id": "pf1",
            "spec_hash": "sha256:abc",
            "idempotency_key": "k1",
        },
    )
    assert ok["isError"] is False
    phases = [e["phase"] for e in posted]
    assert "started" in phases and "succeeded" in phases
    for envelope in posted:
        assert envelope["tool"] == "start_batch"
        assert "run" not in envelope
    posted.clear()
    cancelled = server.call_tool(
        "cancel_batch",
        {"job_id": "01Jbatch", "idempotency_key": "k2", "expected_revision": 1},
    )
    assert cancelled["isError"] is False
    assert any(e["phase"] == "cancelled" for e in posted)
    for envelope in posted:
        assert "run" not in envelope


def test_py_files_compile() -> None:
    import py_compile

    for rel in (
        "mcp/hextile_client.py",
        "mcp/hextile_mcp.py",
        "codex/install.py",
        "tests/test_tool_list_drift.py",
    ):
        py_compile.compile(str(ROOT / rel), doraise=True)
