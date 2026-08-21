#!/usr/bin/env python3
"""stdio MCP proxy for 360 Hextile (stdlib only — no pip).

JSON-RPC 2.0 over newline-delimited stdin/stdout.
Talks only to http://127.0.0.1:8000. No app logic, no local merge authority.

v0.2.1 tools (22): catalog + persist + run + monitor + config + seed + models + guides + live context + live apply.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

# Allow `python3 mcp/hextile_mcp.py` from package root or elsewhere.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from hextile_client import (  # noqa: E402
    APP_DOWN_MSG,
    Client,
    HextileClientError,
    error_payload,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "hextile"
SERVER_VERSION = "0.2.1"
ACTIVITY_SCHEMA = "hextile.agent.activity.v1"

# Canonical tool names — drift tests assert SKILL.md ⊆ this list.
TOOL_NAMES = (
    "list_workflows",
    "get_workflow",
    "get_capabilities",
    "get_live_context",
    "apply_config_delta",
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
    "list_seed_history",
    "get_seed_batch",
    "cancel_seed",
    "list_360_loras",
    "list_installed_models",
    "get_guide",
)

# OPEN-4 annotations (read-only vs mutating).
_READ_ONLY = frozenset(
    {
        "list_workflows",
        "get_workflow",
        "get_capabilities",
        "get_live_context",
        "validate_config",
        "get_status",
        "get_render_config",
        "get_logs",
        "list_runs",
        "list_seed_history",
        "get_seed_batch",
        "list_360_loras",
        "list_installed_models",
        "get_guide",
    }
)
_MUTATING = frozenset(
    {
        "apply_config_delta",
        "save_workflow",
        "delete_workflow",
        "run_workflow",
        "generate_seed",
        "cancel_run",
        "retry_run",
        "cancel_seed",
    }
)

GUIDE_NAMES = (
    "workflow-schema",
    "best-practices",
    "website-index",
    "recipes",
)
_GUIDE_ROOT = Path(__file__).resolve().parent.parent / "skills" / "hextile" / "references"


def _annotations(name: str) -> dict[str, bool]:
    if name in _READ_ONLY:
        return {"readOnlyHint": True, "destructiveHint": False}
    if name in {"cancel_run", "cancel_seed", "delete_workflow"}:
        return {"readOnlyHint": False, "destructiveHint": True}
    return {"readOnlyHint": False, "destructiveHint": False}


def _map_apply_live_refuse(exc: HextileClientError) -> str:
    blob = f"{exc.body or ''} {exc}"
    if "studio_not_present" in blob:
        return "Studio isn't present. Open 360 Hextile with a document."
    if "follow_off" in blob:
        return (
            "Studio isn't following. Turn on MCP follow to apply this to the "
            "open file, or use run_workflow to queue a render."
        )
    if "stale_snapshot" in blob:
        return "The open file changed. Call get_live_context again and retry."
    if "identity_key" in blob:
        return "I can't change the document name, file name, or current render id."
    if "pipeline_key" in blob:
        return (
            "I can't change the pipeline on the open file. "
            "Use run_workflow if you need a different pipeline on a render."
        )
    if "empty_partial" in blob:
        return "config_partial must be a non-empty object."
    return str(exc)


def _tool_def(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: Optional[list[str]] = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": _annotations(name),
    }


TOOLS: list[dict[str, Any]] = [
    _tool_def(
        "list_workflows",
        "List workflow templates on all shelves (builtin / user / project). "
        "A workflow is a full .hextile.json config document.",
        {},
    ),
    _tool_def(
        "get_workflow",
        "Read one workflow template before overriding fields.",
        {
            "origin": {
                "type": "string",
                "description": "Shelf: builtin | user | project",
                "default": "builtin",
            },
            "id": {
                "type": "string",
                "description": "Workflow id (e.g. quick-scout)",
            },
        },
        required=["id"],
    ),
    _tool_def(
        "get_capabilities",
        "Handshake: GET /api/workflows/capabilities "
        "(workflow_api_version + features).",
        {},
    ),
    _tool_def(
        "get_live_context",
        "Read-only studio snapshot (K1). Follow state, doc_generation FNV hex, "
        "and identity-stripped live export when the studio is present. "
        "403 studio_not_present if the FE RAM slot is empty. "
        "Does not merge. include_nav and include_active_tool default false.",
        {
            "include_nav": {
                "type": "boolean",
                "description": "Include descriptive nav manifest. Default false.",
                "default": False,
            },
            "include_active_tool": {
                "type": "boolean",
                "description": "Include compact active viewer tool. Default false.",
                "default": False,
            },
        },
    ),
    _tool_def(
        "apply_config_delta",
        "Apply a live delta to the open studio document when MCP follow is ON. "
        "Never queues GPU. Requires config_partial + doc_generation (FNV hex). "
        "Forbidden: confirm, dry_run, workflow_id, document, queue. "
        "Follow OFF → follow_off. Pipeline / identity keys refused.",
        {
            "config_partial": {
                "type": "object",
                "description": (
                    "Delta only. Arrays replace wholesale. Never identity keys "
                    "(id, name, configFileName, currentRenderId) or pipeline."
                ),
            },
            "doc_generation": {
                "type": "string",
                "description": "FNV-1a hex from get_live_context.doc_generation",
            },
            "explanation": {
                "type": "string",
                "description": "Short reason for the live apply",
            },
        },
        required=["config_partial", "doc_generation"],
    ),
    _tool_def(
        "save_workflow",
        "Create-only persist of a .hextile.json document on the user or "
        "project shelf. Builtin is immutable. Existing id → HTTP 409; "
        "use a new id or delete_workflow first.",
        {
            "origin": {
                "type": "string",
                "description": "user | project (not builtin)",
                "default": "user",
            },
            "id": {
                "type": "string",
                "description": "New workflow id (alphanumeric, hyphen, underscore)",
            },
            "document": {
                "type": "object",
                "description": "Full .hextile.json document to save",
            },
        },
        required=["id", "document"],
    ),
    _tool_def(
        "delete_workflow",
        "Delete a user or project workflow. Cannot delete builtin.",
        {
            "origin": {
                "type": "string",
                "description": "user | project",
                "default": "user",
            },
            "id": {"type": "string", "description": "Workflow id to delete"},
        },
        required=["id"],
    ),
    _tool_def(
        "run_workflow",
        "Merge overrides into a workflow, validate, and queue a render. "
        "Server owns deep-merge + HextileConfig validation — send overrides only. "
        "Requires workflow_id XOR document (exactly one). "
        "Arrays replace wholesale (add a LoRA ≠ append). No confirm argument.",
        {
            "workflow_id": {
                "type": "string",
                "description": "Template id when not sending document",
            },
            "origin": {
                "type": "string",
                "description": "Shelf for workflow_id",
                "default": "builtin",
            },
            "document": {
                "type": "object",
                "description": "Full config document (preferred over workflow_id when set)",
            },
            "overrides": {
                "type": "object",
                "description": (
                    "Partial deep-merge onto the template (server-side). "
                    "Arrays replace wholesale."
                ),
            },
            "output": {
                "type": "object",
                "description": "Optional output block merge (dir, name, …)",
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, validate only (same as validate_config)",
                "default": False,
            },
        },
    ),
    _tool_def(
        "validate_config",
        "Terraform-plan: merge + validate without queueing (dry_run: true).",
        {
            "workflow_id": {"type": "string"},
            "origin": {"type": "string", "default": "builtin"},
            "document": {"type": "object"},
            "overrides": {"type": "object"},
            "output": {"type": "object"},
        },
    ),
    _tool_def(
        "get_status",
        "Poll render/job status by run_id from run_workflow.",
        {
            "run_id": {
                "type": "string",
                "description": "Render id returned by run_workflow",
            },
        },
        required=["run_id"],
    ),
    _tool_def(
        "get_render_config",
        "Read the producing .hextile.json for a render (GET /api/renders/{id}/config). Read-only.",
        {
            "render_id": {
                "type": "string",
                "description": "Render id whose producing config to read",
            },
        },
        required=["render_id"],
    ),
    _tool_def(
        "get_logs",
        "Fetch failed-run logs (GET /api/renders/{id}/logs). Read-only.",
        {
            "run_id": {
                "type": "string",
                "description": "Render id whose logs to read",
            },
        },
        required=["run_id"],
    ),
    _tool_def(
        "list_runs",
        "List library renders (monitor without a stored run_id). "
        "Response data.renders[] includes status, progress, output_path.",
        {
            "lifecycle_status": {
                "type": "string",
                "description": "active | archived | trashed",
                "default": "active",
            },
        },
    ),
    _tool_def(
        "cancel_run",
        "Stop a running render (status → cancelled).",
        {
            "run_id": {"type": "string", "description": "Render id to stop"},
        },
        required=["run_id"],
    ),
    _tool_def(
        "retry_run",
        "Retry a crashed/failed render (POST /api/renders/{id}/retry). "
        "APP returns 400 otherwise. Uses APP tile-reuse policy.",
        {
            "run_id": {"type": "string", "description": "Render id to retry"},
        },
        required=["run_id"],
    ),
    _tool_def(
        "generate_seed",
        "Generate 360-LoRA equirect seed image(s). Returns variation paths. "
        "Two-step: pick a path, then run_workflow with overrides "
        "{input: {path, source: 'file'}}. Requires lora_path + base_model.",
        {
            "prompt": {"type": "string", "description": "Generation prompt"},
            "lora_path": {
                "type": "string",
                "description": "Relative LoRA path known to the app",
            },
            "base_model": {
                "type": "string",
                "description": "sdxl | sd15 | flux_schnell | qwen_image",
            },
            "n": {
                "type": "integer",
                "description": "Number of variations (1–8)",
                "default": 4,
                "minimum": 1,
                "maximum": 8,
            },
            "trigger_word": {
                "type": "string",
                "description": "Optional LoRA trigger word",
            },
            "width": {
                "type": "integer",
                "description": "Output width (APP default 1600)",
            },
            "height": {
                "type": "integer",
                "description": "Output height (APP default 800)",
            },
            "seed": {
                "type": "integer",
                "description": "Generation seed (−1 = random)",
            },
            "num_inference_steps": {
                "type": "integer",
                "description": "Inference steps (1–100)",
            },
            "guidance_scale": {
                "type": "number",
                "description": "CFG / guidance scale",
            },
            "negative_prompt": {
                "type": "string",
                "description": "Negative prompt",
            },
            "seamless_x": {
                "type": "boolean",
                "description": "Circular X-padding for horizontal seam",
            },
        },
        required=["prompt", "lora_path", "base_model"],
    ),
    _tool_def(
        "list_seed_history",
        "List 360-LoRA seed batches (GET /api/360-lora/history). "
        "Recover variation paths after the 300s generate_seed timeout. "
        "Always sends offset+limit so APP returns {batches, total}.",
        {
            "offset": {
                "type": "integer",
                "description": "Pagination offset",
                "default": 0,
            },
            "limit": {
                "type": "integer",
                "description": "Page size (1–100)",
                "default": 50,
            },
            "status": {
                "type": "string",
                "description": "active | archived | trashed",
                "default": "active",
            },
        },
    ),
    _tool_def(
        "get_seed_batch",
        "Read one 360-LoRA seed batch (GET /api/360-lora/history/{batch_id}). "
        "Recover after the 300s generate_seed timeout.",
        {
            "batch_id": {
                "type": "string",
                "description": "Batch id from generate_seed or list_seed_history",
            },
        },
        required=["batch_id"],
    ),
    _tool_def(
        "cancel_seed",
        "Stop the live 360-LoRA job (POST /api/360-lora/cancel). "
        "Cancels whatever seed is live — no batch id, global. "
        "Not a render — use cancel_run for renders.",
        {},
    ),
    _tool_def(
        "list_360_loras",
        "List installed/known 360-LoRAs. Use path + base_model on generate_seed.",
        {},
    ),
    _tool_def(
        "list_installed_models",
        "List installed weights for a pipeline "
        "(GET /api/models/{pipeline_id}?installed_only=true). "
        "pipeline_id is required. Dry-run is Pydantic only — this tool is how "
        "the agent sees installed weights.",
        {
            "pipeline_id": {
                "type": "string",
                "description": "Pipeline id (e.g. sdxl).",
            },
        },
        required=["pipeline_id"],
    ),
    _tool_def(
        "get_guide",
        "Read bundled agent documentation (schema, best practices, "
        "website index, recipes). Pass name, or omit to list guides.",
        {
            "name": {
                "type": "string",
                "description": (
                    "workflow-schema | best-practices | website-index | "
                    "recipes | index"
                ),
            },
        },
    ),
]

assert {t["name"] for t in TOOLS} == set(TOOL_NAMES)
assert _READ_ONLY | _MUTATING == set(TOOL_NAMES)


def load_guide(name: str) -> dict[str, Any]:
    """Read an allowlisted bundled guide. Unknown/empty name lists titles."""
    requested = (name or "index").strip().lower()
    if requested in ("", "index", "list"):
        return {
            "guides": list(GUIDE_NAMES),
            "hint": "Call get_guide with name= one of these titles.",
        }
    if requested not in GUIDE_NAMES:
        raise HextileClientError(
            f"Unknown guide {requested!r}. Available: {', '.join(GUIDE_NAMES)}",
            status_code=None,
            kind="other",
        )
    path = _GUIDE_ROOT / f"{requested}.md"
    if not path.is_file():
        raise HextileClientError(
            f"Guide file missing: {path.name}",
            status_code=None,
            kind="other",
        )
    return {
        "name": requested,
        "path": str(path),
        "markdown": path.read_text(encoding="utf-8"),
    }


def _overrides_keys(name: str, args: Mapping[str, Any]) -> Optional[list[str]]:
    """Names only of args.overrides / config_partial. Never values."""
    if name == "apply_config_delta":
        partial = args.get("config_partial")
        if not isinstance(partial, Mapping):
            return None
        return [str(k) for k in partial.keys()]
    if name not in ("validate_config", "run_workflow"):
        return None
    overrides = args.get("overrides")
    if not isinstance(overrides, Mapping):
        return None
    return [str(k) for k in overrides.keys()]


def _run_from_payload(payload: Any) -> Optional[dict[str, Any]]:
    """Pull run.run_id/status from a handler payload when present."""
    if not isinstance(payload, Mapping):
        return None
    candidates: list[Mapping[str, Any]] = [payload]
    inner = payload.get("data")
    if isinstance(inner, Mapping):
        candidates.append(inner)
    for src in candidates:
        run_id = src.get("run_id")
        if run_id is None:
            continue
        run: dict[str, Any] = {"run_id": str(run_id)}
        if src.get("status") is not None:
            run["status"] = str(src["status"])
        return run
    return None


def _ok_result(data: Any) -> dict[str, Any]:
    text = data if isinstance(data, str) else json.dumps(data, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err_result(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": True}


class HextileMcpServer:
    def __init__(
        self,
        client: Optional[Client] = None,
        notify: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self.client = client or Client()
        self.notify = notify
        self.session_id: Optional[str] = None
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "list_workflows": self._list_workflows,
            "get_workflow": self._get_workflow,
            "get_capabilities": self._get_capabilities,
            "get_live_context": self._get_live_context,
            "apply_config_delta": self._apply_config_delta,
            "save_workflow": self._save_workflow,
            "delete_workflow": self._delete_workflow,
            "run_workflow": self._run_workflow,
            "validate_config": self._validate_config,
            "get_status": self._get_status,
            "get_render_config": self._get_render_config,
            "get_logs": self._get_logs,
            "list_runs": self._list_runs,
            "cancel_run": self._cancel_run,
            "retry_run": self._retry_run,
            "generate_seed": self._generate_seed,
            "list_seed_history": self._list_seed_history,
            "get_seed_batch": self._get_seed_batch,
            "cancel_seed": self._cancel_seed,
            "list_360_loras": self._list_360_loras,
            "list_installed_models": self._list_installed_models,
            "get_guide": self._get_guide,
        }

    def handle_rpc(self, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Handle one JSON-RPC message. Returns response or None for notifications."""
        method = msg.get("method")
        msg_id = msg.get("id", None)
        params = msg.get("params") or {}

        # Notifications have no id.
        is_notification = "id" not in msg

        if method == "initialize":
            self._ensure_session_id()
            return self._response(
                msg_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                    "instructions": (
                        "Call get_capabilities then get_live_context before compose; "
                        "validate_config before run_workflow."
                    ),
                },
            )

        if method == "notifications/initialized" or method == "initialized":
            return None

        if method == "ping":
            return self._response(msg_id, {})

        if method == "tools/list":
            return self._response(msg_id, {"tools": TOOLS})

        if method == "tools/call":
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            progress_token = meta.get("progressToken")
            try:
                result = self.call_tool(
                    name, arguments, progress_token=progress_token
                )
            except Exception as exc:  # noqa: BLE001 — surface to agent
                return self._response(
                    msg_id,
                    _err_result(error_payload(exc)),
                )
            return self._response(msg_id, result)

        if is_notification:
            return None

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": -32601,
                "message": f"Method not found: {method}",
            },
        }

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        progress_token: Any = None,
    ) -> dict[str, Any]:
        call_id = str(uuid.uuid4())
        args = arguments if isinstance(arguments, dict) else {}
        overrides_keys = _overrides_keys(name, args)
        self._emit_activity(
            tool=name, call_id=call_id, phase="started",
            overrides_keys=overrides_keys,
        )
        handler = self._handlers.get(name)
        if handler is None:
            self._emit_activity(
                tool=name,
                call_id=call_id,
                phase="failed",
                error={
                    "kind": "other",
                    "status_code": None,
                    "message": f"Unknown tool: {name}",
                },
                overrides_keys=overrides_keys,
            )
            return _err_result(
                {"ok": False, "error": f"Unknown tool: {name}", "kind": "other"}
            )
        try:
            if name == "generate_seed":
                self._notify_progress(progress_token, 0)
            data = handler(args)
            phase = (
                "cancelled"
                if name in ("cancel_run", "cancel_seed")
                else "succeeded"
            )
            self._emit_activity(
                tool=name,
                call_id=call_id,
                phase=phase,
                run=_run_from_payload(data),
                overrides_keys=overrides_keys,
            )
            return _ok_result(data)
        except HextileClientError as exc:
            self._emit_activity(
                tool=name,
                call_id=call_id,
                phase="failed",
                error={
                    "kind": exc.kind,
                    "status_code": exc.status_code,
                    "message": str(exc),
                },
                overrides_keys=overrides_keys,
            )
            return _err_result(error_payload(exc))
        except Exception as exc:  # noqa: BLE001
            self._emit_activity(
                tool=name,
                call_id=call_id,
                phase="failed",
                error={
                    "kind": "other",
                    "status_code": None,
                    "message": str(exc),
                },
                overrides_keys=overrides_keys,
            )
            return _err_result(
                {
                    "ok": False,
                    "error": str(exc),
                    "kind": "other",
                    "trace": traceback.format_exc()[-500:],
                }
            )

    def _ensure_session_id(self) -> str:
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        return self.session_id

    def _emit_activity(
        self,
        *,
        tool: str,
        call_id: str,
        phase: str,
        run: Optional[Mapping[str, Any]] = None,
        error: Optional[Mapping[str, Any]] = None,
        overrides_keys: Optional[list[str]] = None,
    ) -> None:
        # ids/phase only — never config/nav/tool_op/args. Timeout 1s; swallow.
        envelope: dict[str, Any] = {
            "schema": ACTIVITY_SCHEMA,
            "session_id": self._ensure_session_id(),
            "call_id": call_id,
            "tool": tool,
            "phase": phase,
        }
        if run:
            run_body: dict[str, Any] = {}
            if run.get("run_id") is not None:
                run_body["run_id"] = str(run["run_id"])
            if run.get("status") is not None:
                run_body["status"] = str(run["status"])
            if run_body:
                envelope["run"] = run_body
        if error:
            status = error.get("status_code")
            # Body snippets echo config/override/prompt values — never on the bus.
            envelope["error"] = {
                "kind": error.get("kind", "other"),
                "status_code": status,
                "message": (
                    f"HTTP {status}"
                    if status is not None
                    else str(error.get("kind") or "other")
                ),
            }
        if overrides_keys is not None:
            envelope["overrides_keys"] = list(overrides_keys)
        try:
            self.client.post_activity(envelope)
        except Exception:
            pass

    def _notify_progress(
        self,
        progress_token: Any,
        progress: float,
        message: Optional[str] = None,
    ) -> None:
        if progress_token is None or self.notify is None:
            return
        params: dict[str, Any] = {
            "progressToken": progress_token,
            "progress": progress,
        }
        if message is not None:
            params["message"] = message
        try:
            self.notify(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": params,
                }
            )
        except Exception:
            pass

    # ── tool handlers ───────────────────────────────────────────────────

    def _list_workflows(self, _args: dict[str, Any]) -> Any:
        return self.client.list_workflows()

    def _get_workflow(self, args: dict[str, Any]) -> Any:
        origin = args.get("origin") or "builtin"
        wid = args.get("id") or args.get("workflow_id")
        if not wid:
            raise HextileClientError(
                "id is required", status_code=None, kind="other"
            )
        return self.client.get_workflow(str(origin), str(wid))

    def _get_capabilities(self, _args: dict[str, Any]) -> Any:
        return self.client.get_capabilities()

    def _get_live_context(self, args: dict[str, Any]) -> Any:
        try:
            return self.client.get_live_context(
                include_nav=bool(args.get("include_nav", False)),
                include_active_tool=bool(args.get("include_active_tool", False)),
            )
        except HextileClientError as exc:
            body = exc.body or ""
            if exc.status_code == 403 and "studio_not_present" in body:
                raise HextileClientError(
                    "Studio isn't present. Open 360 Hextile with a document.",
                    status_code=403,
                    body=body,
                    kind="http",
                ) from exc
            raise

    def _apply_config_delta(self, args: dict[str, Any]) -> Any:
        if "config_partial" not in args:
            raise HextileClientError(
                "config_partial is required",
                status_code=None,
                kind="other",
            )
        partial = args.get("config_partial")
        if not isinstance(partial, dict):
            raise HextileClientError(
                "config_partial must be an object",
                status_code=None,
                kind="other",
            )
        if not partial:
            raise HextileClientError(
                "config_partial must be a non-empty object.",
                status_code=422,
                kind="http",
            )
        gen = args.get("doc_generation")
        if not isinstance(gen, str) or not gen:
            raise HextileClientError(
                "doc_generation is required",
                status_code=None,
                kind="other",
            )
        for forbidden in (
            "confirm",
            "dry_run",
            "workflow_id",
            "document",
            "queue",
            "force",
            "skip_follow",
        ):
            if forbidden in args:
                raise HextileClientError(
                    f"{forbidden} is not allowed on apply_config_delta",
                    status_code=None,
                    kind="other",
                )
        try:
            return self.client.apply_config_delta(
                config_partial=partial,
                doc_generation=gen,
                explanation=str(args.get("explanation") or ""),
            )
        except HextileClientError as exc:
            raise HextileClientError(
                _map_apply_live_refuse(exc),
                status_code=exc.status_code,
                body=exc.body,
                kind=exc.kind,
            ) from exc

    def _save_workflow(self, args: dict[str, Any]) -> Any:
        document = args.get("document")
        if not isinstance(document, dict):
            raise HextileClientError(
                "document is required and must be a JSON object",
                status_code=None,
                kind="other",
            )
        return self.client.save_workflow(
            str(args.get("origin") or "user"),
            str(args.get("id") or args.get("workflow_id") or ""),
            document,
        )

    def _delete_workflow(self, args: dict[str, Any]) -> Any:
        return self.client.delete_workflow(
            str(args.get("origin") or "user"),
            str(args.get("id") or args.get("workflow_id") or ""),
        )

    def _run_workflow(self, args: dict[str, Any]) -> Any:
        workflow_id = args.get("workflow_id")
        document = args.get("document")
        has_id = isinstance(workflow_id, str) and bool(workflow_id)
        has_doc = isinstance(document, dict)
        if has_id == has_doc:
            raise HextileClientError(
                "run_workflow requires workflow_id XOR document",
                status_code=None,
                kind="other",
            )
        return self.client.run_workflow(
            workflow_id=workflow_id if has_id else None,
            origin=str(args.get("origin") or "builtin"),
            document=document if has_doc else None,
            overrides=args.get("overrides"),
            output=args.get("output"),
            dry_run=bool(args.get("dry_run", False)),
        )

    def _validate_config(self, args: dict[str, Any]) -> Any:
        return self.client.run_workflow(
            workflow_id=args.get("workflow_id"),
            origin=str(args.get("origin") or "builtin"),
            document=args.get("document"),
            overrides=args.get("overrides"),
            output=args.get("output"),
            dry_run=True,
        )

    def _get_status(self, args: dict[str, Any]) -> Any:
        run_id = args.get("run_id")
        if not run_id:
            raise HextileClientError(
                "run_id is required", status_code=None, kind="other"
            )
        return self.client.get_status(str(run_id))

    def _get_render_config(self, args: dict[str, Any]) -> Any:
        render_id = args.get("render_id")
        if not render_id:
            raise HextileClientError(
                "render_id is required", status_code=None, kind="other"
            )
        return self.client.get_render_config(str(render_id))

    def _get_logs(self, args: dict[str, Any]) -> Any:
        run_id = args.get("run_id")
        if not run_id:
            raise HextileClientError(
                "run_id is required", status_code=None, kind="other"
            )
        return self.client.get_logs(str(run_id))

    def _list_runs(self, args: dict[str, Any]) -> Any:
        return self.client.list_runs(
            str(args.get("lifecycle_status") or "active")
        )

    def _cancel_run(self, args: dict[str, Any]) -> Any:
        run_id = args.get("run_id")
        if not run_id:
            raise HextileClientError(
                "run_id is required", status_code=None, kind="other"
            )
        return self.client.cancel_run(str(run_id))

    def _retry_run(self, args: dict[str, Any]) -> Any:
        run_id = args.get("run_id")
        if not run_id:
            raise HextileClientError(
                "run_id is required", status_code=None, kind="other"
            )
        return self.client.retry_run(str(run_id))

    def _generate_seed(self, args: dict[str, Any]) -> Any:
        prompt = args.get("prompt")
        lora_path = args.get("lora_path")
        base_model = args.get("base_model")
        if not prompt or not lora_path or not base_model:
            raise HextileClientError(
                "prompt, lora_path, and base_model are required",
                status_code=None,
                kind="other",
            )
        n = int(args.get("n") or 4)
        # Pass through optional known fields only when present.
        extra = {}
        for key in (
            "trigger_word",
            "width",
            "height",
            "seed",
            "num_inference_steps",
            "guidance_scale",
            "negative_prompt",
            "seamless_x",
        ):
            if key in args:
                extra[key] = args[key]
        return self.client.generate_seed(
            str(prompt),
            lora_path=str(lora_path),
            base_model=str(base_model),
            n=n,
            **extra,
        )

    def _list_seed_history(self, args: dict[str, Any]) -> Any:
        offset = 0 if args.get("offset") is None else int(args["offset"])
        limit = 50 if args.get("limit") is None else int(args["limit"])
        status = args.get("status")
        return self.client.list_seed_history(
            offset=offset,
            limit=limit,
            status=str(status) if status else None,
        )

    def _get_seed_batch(self, args: dict[str, Any]) -> Any:
        batch_id = args.get("batch_id")
        if not batch_id:
            raise HextileClientError(
                "batch_id is required", status_code=None, kind="other"
            )
        return self.client.get_seed_batch(str(batch_id))

    def _cancel_seed(self, _args: dict[str, Any]) -> Any:
        return self.client.cancel_seed()

    def _list_360_loras(self, _args: dict[str, Any]) -> Any:
        return self.client.list_360_loras()

    def _list_installed_models(self, args: dict[str, Any]) -> Any:
        raw = args.get("pipeline_id")
        pipeline_id = str(raw).strip() if raw else ""
        if not pipeline_id:
            raise HextileClientError(
                "pipeline_id is required",
                status_code=None,
                kind="other",
            )
        return self.client.list_installed_models(pipeline_id)

    def _get_guide(self, args: dict[str, Any]) -> Any:
        return load_guide(str(args.get("name") or "index"))

    @staticmethod
    def _response(msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _read_message(stdin) -> Optional[dict[str, Any]]:
    """Read one NDJSON JSON-RPC message from stdin.

    Also accepts optional Content-Length framed messages (LSP-style) for
    clients that prefer that framing.
    """
    # Peek strategy: read line; if Content-Length, read body; else parse line.
    line = stdin.readline()
    if not line:
        return None
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    stripped = line.strip()
    if not stripped:
        return _read_message(stdin)

    if stripped.lower().startswith("content-length:"):
        length = int(stripped.split(":", 1)[1].strip())
        # Consume headers until blank line.
        while True:
            hdr = stdin.readline()
            if not hdr:
                return None
            if isinstance(hdr, bytes):
                hdr = hdr.decode("utf-8")
            if hdr in ("\r\n", "\n", ""):
                break
        body = stdin.read(length)
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        return json.loads(body)

    return json.loads(stripped)


_WRITE_LOCK = threading.Lock()


def _write_message(stdout, msg: dict[str, Any]) -> None:
    line = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
    with _WRITE_LOCK:
        stdout.write(line + "\n")
        stdout.flush()


def main() -> int:
    # Keep process alive even if app is down — tools return clean errors.
    server = HextileMcpServer()
    # Binary-safe-ish: use text mode with UTF-8.
    stdin = sys.stdin
    stdout = sys.stdout
    # Reconfigure if possible (Python 3.7+).
    try:
        stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    server.notify = lambda msg: _write_message(stdout, msg)

    # tools/call on one worker so generate_seed cannot starve ping.
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hextile-mcp")
    pending_lock = threading.Lock()
    pending: list[Future[Any]] = []

    reply_mark = threading.Lock()

    def write_reply(fut: Future[Any], resp: Optional[dict[str, Any]]) -> None:
        # Mark the Future itself — never id(fut), which CPython reuses.
        with reply_mark:
            if getattr(fut, "_hextile_written", False):
                return
            setattr(fut, "_hextile_written", True)
        if resp is not None:
            _write_message(stdout, resp)

    def run_rpc(msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            return server.handle_rpc(msg)
        except Exception as exc:  # noqa: BLE001
            if "id" in msg:
                return {
                    "jsonrpc": "2.0",
                    "id": msg.get("id"),
                    "error": {
                        "code": -32603,
                        "message": f"Internal error: {exc}",
                    },
                }
            return None

    def on_done(fut: Future[Any]) -> None:
        try:
            resp = fut.result()
        except Exception:
            resp = None
        write_reply(fut, resp)
        with pending_lock:
            try:
                pending.remove(fut)
            except ValueError:
                pass

    def submit_tool(msg: dict[str, Any]) -> None:
        fut = executor.submit(run_rpc, msg)
        with pending_lock:
            pending.append(fut)
        fut.add_done_callback(on_done)

    def join_inflight() -> None:
        with pending_lock:
            futs = list(pending)
        for fut in futs:
            try:
                resp = fut.result()
            except Exception:
                resp = None
            # Backup write if the done-callback has not run yet.
            write_reply(fut, resp)

    try:
        while True:
            try:
                msg = _read_message(stdin)
            except Exception as exc:  # noqa: BLE001
                _write_message(
                    stdout,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32700,
                            "message": f"Parse error: {exc}",
                        },
                    },
                )
                continue
            if msg is None:
                join_inflight()
                break
            if not isinstance(msg, dict):
                continue
            # Ignore bare responses.
            if "method" not in msg:
                continue
            if msg.get("method") == "tools/call":
                submit_tool(msg)
                continue
            resp = run_rpc(msg)
            if resp is not None:
                _write_message(stdout, resp)
    finally:
        executor.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    # Quiet reminder on stderr only — never pollute stdout (MCP channel).
    sys.stderr.write(
        f"{SERVER_NAME} MCP {SERVER_VERSION} — proxy to 127.0.0.1:8000\n"
    )
    sys.stderr.flush()
    raise SystemExit(main())
