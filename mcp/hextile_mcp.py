#!/usr/bin/env python3
"""stdio MCP proxy for 360 Hextile (stdlib only — no pip).

JSON-RPC 2.0 over newline-delimited stdin/stdout.
Talks only to http://127.0.0.1:8000. No app logic, no local merge authority.

v0.2.1 tools (14): catalog + persist + run + monitor + config + seed + guides.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

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

# Canonical tool names — drift tests assert SKILL.md ⊆ this list.
TOOL_NAMES = (
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
    "generate_seed",
    "list_360_loras",
    "get_guide",
)

# OPEN-4 annotations (read-only vs mutating).
_READ_ONLY = frozenset(
    {
        "list_workflows",
        "get_workflow",
        "get_capabilities",
        "validate_config",
        "get_status",
        "get_render_config",
        "get_logs",
        "list_runs",
        "list_360_loras",
        "get_guide",
    }
)
_MUTATING = frozenset(
    {
        "save_workflow",
        "delete_workflow",
        "run_workflow",
        "generate_seed",
        "cancel_run",
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
    if name in {"cancel_run", "delete_workflow"}:
        return {"readOnlyHint": False, "destructiveHint": True}
    return {"readOnlyHint": False, "destructiveHint": False}


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
        "Arrays replace wholesale (add a LoRA ≠ append).",
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
                "description": "Partial deep-merge onto the template (server-side)",
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
                "description": "sdxl | sd15 | flux_schnell",
            },
            "n": {
                "type": "integer",
                "description": "Number of variations (1–8)",
                "default": 4,
            },
        },
        required=["prompt", "lora_path", "base_model"],
    ),
    _tool_def(
        "list_360_loras",
        "List installed/known 360-LoRAs. Use path + base_model on generate_seed.",
        {},
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


def _ok_result(data: Any) -> dict[str, Any]:
    text = data if isinstance(data, str) else json.dumps(data, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err_result(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}], "isError": True}


class HextileMcpServer:
    def __init__(self, client: Optional[Client] = None) -> None:
        self.client = client or Client()
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "list_workflows": self._list_workflows,
            "get_workflow": self._get_workflow,
            "get_capabilities": self._get_capabilities,
            "save_workflow": self._save_workflow,
            "delete_workflow": self._delete_workflow,
            "run_workflow": self._run_workflow,
            "validate_config": self._validate_config,
            "get_status": self._get_status,
            "get_render_config": self._get_render_config,
            "get_logs": self._get_logs,
            "list_runs": self._list_runs,
            "cancel_run": self._cancel_run,
            "generate_seed": self._generate_seed,
            "list_360_loras": self._list_360_loras,
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
            return self._response(
                msg_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
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
            try:
                result = self.call_tool(name, arguments)
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

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return _err_result(
                {"ok": False, "error": f"Unknown tool: {name}", "kind": "other"}
            )
        try:
            data = handler(arguments if isinstance(arguments, dict) else {})
            return _ok_result(data)
        except HextileClientError as exc:
            return _err_result(error_payload(exc))
        except Exception as exc:  # noqa: BLE001
            return _err_result(
                {
                    "ok": False,
                    "error": str(exc),
                    "kind": "other",
                    "trace": traceback.format_exc()[-500:],
                }
            )

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

    def _save_workflow(self, args: dict[str, Any]) -> Any:
        return self.client.save_workflow(
            str(args.get("origin") or "user"),
            str(args.get("id") or args.get("workflow_id") or ""),
            args.get("document") or {},
        )

    def _delete_workflow(self, args: dict[str, Any]) -> Any:
        return self.client.delete_workflow(
            str(args.get("origin") or "user"),
            str(args.get("id") or args.get("workflow_id") or ""),
        )

    def _run_workflow(self, args: dict[str, Any]) -> Any:
        return self.client.run_workflow(
            workflow_id=args.get("workflow_id"),
            origin=str(args.get("origin") or "builtin"),
            document=args.get("document"),
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

    def _list_360_loras(self, _args: dict[str, Any]) -> Any:
        return self.client.list_360_loras()

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


def _write_message(stdout, msg: dict[str, Any]) -> None:
    line = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
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
            break
        if not isinstance(msg, dict):
            continue
        # Ignore bare responses.
        if "method" not in msg:
            continue
        try:
            resp = server.handle_rpc(msg)
        except Exception as exc:  # noqa: BLE001
            if "id" in msg:
                _write_message(
                    stdout,
                    {
                        "jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "error": {
                            "code": -32603,
                            "message": f"Internal error: {exc}",
                        },
                    },
                )
            continue
        if resp is not None:
            _write_message(stdout, resp)
    return 0


if __name__ == "__main__":
    # Quiet reminder on stderr only — never pollute stdout (MCP channel).
    sys.stderr.write(
        f"{SERVER_NAME} MCP {SERVER_VERSION} — proxy to 127.0.0.1:8000\n"
    )
    sys.stderr.flush()
    raise SystemExit(main())
