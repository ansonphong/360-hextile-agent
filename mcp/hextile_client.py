"""Thin HTTP client for the local 360 Hextile backend.

Stdlib only (urllib). Default base URL: http://127.0.0.1:8000.
No app logic — pure proxy helpers for the MCP server and Codex install.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_S = 30.0
LONG_OPERATION_TIMEOUT_S = 300.0  # Export, sequence extraction, and local vision

APP_DOWN_MSG = (
    "360 Hextile isn't running. Launch 360 Hextile, then retry."
)
UPGRADE_MSG = (
    "Upgrade 360 Hextile (needs workflows/run). "
    "This plugin requires a build with POST /api/workflows/run."
)
BATCH_UNAVAILABLE_MSG = (
    "Batch workflows are unavailable. Enable Batch in 360 Hextile "
    "(HEXTILE_BATCH_WORKFLOWS_ENABLED), then retry."
)
_BATCH_KNOWN_404 = (
    "unknown_preflight",
    "unknown_job",
    "unknown_workflow",
    "preview_unavailable",
)

_ERROR_BODY_SNIPPET = 800
_REQUEST_HEADERS: ContextVar[Mapping[str, str]] = ContextVar(
    "hextile_request_headers", default={}
)

_WORKSPACE_HANDLE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_WORKSPACE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WORKSPACE_SUFFIX = re.compile(r"[1-9][0-9]{0,15}\Z")
MAX_WORKSPACE_GENERATION = 2**53 - 1


def validate_shader_workspace(operation: str, args: Mapping[str, Any]) -> None:
    """Closed transport admission; APP owns grants, replay and saved-base checks."""
    def refuse(message: str = "Expected closed Shader workspace arguments") -> None:
        raise HextileClientError(message, kind="other", status_code=422)

    def text(value: Any, maximum: int = 256) -> bool:
        return type(value) is str and 1 <= len(value) <= maximum

    def digest(value: Any) -> bool:
        return type(value) is str and _WORKSPACE_DIGEST.fullmatch(value) is not None

    if not isinstance(args, Mapping):
        refuse()
    if operation == "open":
        required = {"shader", "project_context_witness", "expected_working_revision",
                    "expected_document_hash", "expected_source_hash", "studio_surface_id"}
        base = {"expected_working_revision", "expected_document_hash", "expected_source_hash"}
        supplied = base & args.keys()
        if (not required - base <= args.keys()
                or args.keys() - required - {"existing_workspace", "workspace_schema"}
                or ("workspace_schema" in args and (type(args["workspace_schema"]) is not int or args["workspace_schema"] != 2))
                or (supplied and supplied != base)
                or (args.get("workspace_schema") != 2 and supplied != base)):
            refuse()
        shader = args["shader"]
        if (type(shader) is not dict or set(shader) != {"origin", "shaderId"}
                or shader["origin"] != "project" or not text(shader["shaderId"], 128)
                or re.fullmatch(r"[A-Za-z0-9_-]+", shader["shaderId"]) is None
                or not text(args["project_context_witness"]) or not text(args["studio_surface_id"])
                or (supplied and (type(args["expected_working_revision"]) is not int or args["expected_working_revision"] < 1
                                 or not digest(args["expected_document_hash"]) or not digest(args["expected_source_hash"])))):
            refuse()
        if "existing_workspace" in args:
            adopt = args["existing_workspace"]
            if (type(adopt) is not dict or set(adopt) != {"action", "expected_content_digest"}
                    or adopt["action"] != "adopt" or not digest(adopt["expected_content_digest"])):
                refuse()
        return
    required = {"handle", "change_id", "expected_generation", "content_digest"} if operation == "register" else {"handle", "change_id", "project_context_witness"}
    optional = {"include_source_diagnostics"} if operation == "get" else set()
    if (operation not in {"register", "get"} or not required <= args.keys()
            or args.keys() - required - optional
            or ("include_source_diagnostics" in args and type(args["include_source_diagnostics"]) is not bool)):
        refuse()
    handle, change_id = args["handle"], args["change_id"]
    if type(handle) is not str or _WORKSPACE_HANDLE.fullmatch(handle) is None:
        refuse("Workspace handle must be opaque ASCII of at most 64 characters")
    # Bound the string and decimal before integer conversion, including namespace.
    if type(change_id) is not str or len(change_id) > 81 or not change_id.startswith(handle + ":"):
        suffix = ""
    else:
        suffix = change_id[len(handle) + 1:]
    if _WORKSPACE_SUFFIX.fullmatch(suffix) is None or int(suffix) > MAX_WORKSPACE_GENERATION:
        raise HextileClientError("generation_conflict: use <handle>:<positive canonical generation>",
                                 kind="http", status_code=409,
                                 receipt={"code": "generation_conflict"})
    if operation == "register":
        if (type(args["expected_generation"]) is not int
                or not 0 <= args["expected_generation"] < MAX_WORKSPACE_GENERATION
                or not digest(args["content_digest"])):
            refuse()
        # APP compares suffix to expected_generation+1 after checking retained replay,
        # so altered retained inputs keep their authoritative idempotency_conflict.
    elif not text(args["project_context_witness"]):
        refuse()


def _layer_transport_unknown(exc: BaseException) -> bool:
    return isinstance(exc, http.client.HTTPException) or (
        isinstance(exc, HextileClientError) and exc.kind == "app_down"
    )


class HextileClientError(RuntimeError):
    """HTTP or transport failure talking to the local backend."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: Optional[str] = None,
        kind: str = "http",
        receipt: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.kind = kind  # app_down | upgrade | http | other
        self.receipt = dict(receipt) if receipt is not None else None


# Transport shape only. The APP store alone resolves references, checks domains/grants,
# and materializes a canonical candidate; this proxy never generates IDs or replays edits.
_SHADER_EDIT_FIELDS = {
    "add_source": ({"definition"}, {"ref"}), "remove_source": ({"source"}, set()),
    "set_label": ({"source", "label"}, set()), "install_wave": ({"source", "wave"}, set()),
    "set_expression": ({"source", "source_text"}, set()),
    "set_setting": ({"source", "setting", "value"}, set()),
    "set_period_unit": ({"source", "period_unit"}, set()),
    "set_click": ({"source", "button", "mode"}, set()), "rename_alias": ({"source", "alias"}, set()),
    "set_source_enabled": ({"source", "enabled"}, set()), "add_binding": ({"binding"}, {"ref"}),
    "update_binding": ({"binding", "value"}, set()), "remove_binding": ({"binding"}, set()),
    "set_binding_enabled": ({"binding", "enabled"}, set()), "set_mouse_input": ({"value"}, set()),
    "set_master_clock": ({"value"}, set()),
}


def validate_shader_proposal(body: Mapping[str, Any]) -> None:
    """Reject foreign/XOR/oversized transport bodies before a local HTTP request."""
    def refuse() -> None:
        raise HextileClientError("Expected a closed Shader proposal", kind="other")

    if not isinstance(body, Mapping):
        refuse()
    kind = body.get("kind")
    if kind == "shader_document_candidate":
        alternatives = set(body) & {"complete_candidate", "source_edit", "modulation_edits"}
        if len(alternatives) != 1:
            refuse()
        field = next(iter(alternatives))
        if set(body) != {"kind", "base", "explanation", field} or type(body[field]) is not dict:
            refuse()
        if field == "modulation_edits":
            batch = body[field]
            if (set(batch) != {"schema", "edits"} or type(batch["schema"]) is not int
                    or batch["schema"] != 1 or type(batch["edits"]) is not list or len(batch["edits"]) > 32):
                refuse()
            try:
                size = len(json.dumps(batch, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, UnicodeError, RecursionError):
                refuse()
            if size > 65536:
                refuse()
            for edit in batch["edits"]:
                if type(edit) is not dict or type(edit.get("op")) is not str or edit["op"] not in _SHADER_EDIT_FIELDS:
                    refuse()
                required, optional = _SHADER_EDIT_FIELDS[edit["op"]]
                if not (required | {"op"}) <= edit.keys() or edit.keys() - required - optional - {"op"}:
                    refuse()
    elif kind == "shader_variation_board":
        if set(body) != {"kind", "base", "board_id", "candidates"}:
            refuse()
    else:
        refuse()


class Client:
    """Sync HTTP wrapper around local APP routes (stdlib urllib)."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        opener: Any = None,
        internal_mode: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Injectable for tests (callable urlopen or opener.open).
        self._opener = opener
        self.internal_mode = internal_mode

    @contextmanager
    def request_headers(self, headers: Mapping[str, str]):
        """Bind private headers to one tools/call execution context."""
        token = _REQUEST_HEADERS.set(dict(headers))
        try:
            yield
        finally:
            _REQUEST_HEADERS.reset(token)

    # ── low-level ───────────────────────────────────────────────────────

    def request_json(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        """HTTP JSON request. Raises HextileClientError on failure."""
        url = self._url(path)
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            if qs:
                url = url + ("&" if "?" in url else "?") + qs
        data: Optional[bytes] = None
        req_headers = {"Accept": "application/json", **_REQUEST_HEADERS.get()}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        if headers:
            req_headers.update(dict(headers))
        req = urllib.request.Request(
            url, data=data, headers=req_headers, method=method
        )
        to = self.timeout if timeout is None else timeout
        try:
            if self._opener is not None:
                resp_cm = self._opener(req, timeout=to)
            else:
                resp_cm = urllib.request.urlopen(req, timeout=to)
            with resp_cm as resp:
                raw = resp.read()
                code = getattr(resp, "status", None) or resp.getcode()
                if not raw:
                    return None
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError as exc:
                    snippet = raw[:_ERROR_BODY_SNIPPET].decode("utf-8", "replace")
                    raise HextileClientError(
                        f"Invalid JSON from {path}: {snippet}",
                        status_code=code,
                        body=snippet,
                        kind="other",
                    ) from exc
        except HextileClientError:
            raise
        except urllib.error.HTTPError as exc:
            snippet = ""
            try:
                # Hotkeys has bounded structured conflict details, not a log snippet.
                limit = 64 * 1024 if path == "/api/agent/hotkeys/requests" or path.startswith("/api/agent/shader-workspaces/") else _ERROR_BODY_SNIPPET
                snippet = (exc.read(limit) or b"").decode(
                    "utf-8", "replace"
                )
            except Exception:
                snippet = str(exc.reason or "")
            if exc.code == 404 and "/api/workflows/run" in path:
                raise HextileClientError(
                    UPGRADE_MSG,
                    status_code=404,
                    body=snippet,
                    kind="upgrade",
                ) from exc
            if "/api/batch-workflows" in path:
                blob = snippet.lower()
                if exc.code == 409 and "batch_unavailable" in blob:
                    raise HextileClientError(
                        BATCH_UNAVAILABLE_MSG,
                        status_code=409,
                        body=snippet,
                        kind="capability",
                    ) from exc
                if exc.code == 404 and not any(code in blob for code in _BATCH_KNOWN_404):
                    raise HextileClientError(
                        BATCH_UNAVAILABLE_MSG,
                        status_code=404,
                        body=snippet,
                        kind="capability",
                    ) from exc
            raise HextileClientError(
                f"HTTP {exc.code} {method} {path}: {snippet}",
                status_code=exc.code,
                body=snippet,
                kind="http",
            ) from exc
        except (
            urllib.error.URLError,
            ConnectionError,
            socket.timeout,
            TimeoutError,
            OSError,
        ) as exc:
            # Connection refused / DNS / timeout while app is down.
            raise HextileClientError(
                APP_DOWN_MSG,
                status_code=None,
                body=str(exc),
                kind="app_down",
            ) from exc

    def hotkeys_request(self, op: str, args: Mapping[str, Any]) -> Any:
        """Proxy the frontend-owned profile once; never replay an uncertain write."""
        mutating = op not in {"list", "inspect"}
        unknown_message = "Hotkeys outcome unknown; read the profile before approving another mutation."
        try:
            result = self.request_json(
                "POST", "/api/agent/hotkeys/requests", {"op": op, "args": dict(args)},
                timeout=30.0,
                headers=None if self.internal_mode else {"X-Hextile-Agent": "mcp"},
            )
            if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("data"), dict):
                raise HextileClientError("Invalid Hotkeys acknowledgement", kind="other", status_code=200)
            return result["data"]
        except http.client.HTTPException as exc:
            raise HextileClientError(
                unknown_message if mutating else APP_DOWN_MSG,
                kind="hotkeys_outcome_unknown" if mutating else "app_down",
            ) from exc
        except HextileClientError as exc:
            try:
                detail = json.loads(exc.body or "{}").get("detail", {})
            except (ValueError, AttributeError):
                detail = {}
            if not isinstance(detail, dict):
                detail = {}
            code = detail.get("code")
            malformed_success = exc.kind == "other" and exc.status_code is not None and 200 <= exc.status_code < 300
            if mutating and (code == "outcome_unknown" or exc.kind == "app_down" or malformed_success):
                raise HextileClientError(unknown_message, kind="hotkeys_outcome_unknown", status_code=exc.status_code) from exc
            if isinstance(code, str):
                recovery = {
                    "stale_revision": "Read the profile again before proposing a new edit.",
                    "hotkey_conflict": "Inspect conflicts and resolve each action explicitly.",
                    "hotkeys_app_unavailable": "Open the app and read the profile again.",
                    "unsupported_hotkey_profile": "Only an explicitly approved reset can replace a future profile.",
                }.get(code, "Read or inspect Hotkeys before another edit.")
                raise HextileClientError(
                    f"{code}: {recovery}", status_code=exc.status_code, kind=exc.kind,
                    receipt={key: detail[key] for key in ("code", "conflicts", "request_id") if key in detail},
                ) from exc
            raise

    def get_json(
        self,
        path: str,
        *,
        timeout: Optional[float] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        return self.request_json("GET", path, timeout=timeout, params=params, headers=headers)

    def post_json(
        self,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        return self.request_json(
            "POST", path, body, timeout=timeout, params=params, headers=headers
        )

    def delete_json(
        self,
        path: str,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        return self.request_json("DELETE", path, timeout=timeout)

    def post_activity(self, envelope: Mapping[str, Any]) -> Any:
        """POST /api/agent/events — 1s timeout. Caller swallows errors."""
        return self.request_json(
            "POST", "/api/agent/events", envelope, timeout=1
        )

    # ── workflows ───────────────────────────────────────────────────────

    def list_workflows(self) -> Any:
        """GET /api/workflows."""
        return self.get_json("/api/workflows")

    def get_workflow(self, origin: str, workflow_id: str) -> Any:
        """GET /api/workflows/{origin}/{id}."""
        o = urllib.parse.quote(origin, safe="")
        wid = urllib.parse.quote(workflow_id, safe="")
        return self.get_json(f"/api/workflows/{o}/{wid}")

    def get_capabilities(self) -> Any:
        """GET /api/workflows/capabilities."""
        return self.get_json("/api/workflows/capabilities")

    def describe_image(self, body: Mapping[str, Any]) -> Any:
        """Ask APP to caption one exact local source; never open image bytes here."""
        return self.post_json(
            "/api/prompts/describe-source", body, timeout=LONG_OPERATION_TIMEOUT_S
        )

    def get_unreal_export_catalog(self) -> Any:
        return self.get_json("/api/renders/unreal-export/catalog")

    def preflight_unreal_export(self, render_id: str, body: Mapping[str, Any]) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        return self.post_json(f"/api/renders/{rid}/export/preflight", body)

    def preflight_file_export(self, render_id: str, body: Mapping[str, Any]) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        return self.post_json(f"/api/renders/{rid}/export/file-preflight", body)

    def export_render_file(self, render_id: str, body: Mapping[str, Any]) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        try:
            receipt = self.post_json(
                f"/api/renders/{rid}/export", body, timeout=LONG_OPERATION_TIMEOUT_S
            )
            destination = Path(str(body["destination"]))
            paths = receipt.get("paths") if isinstance(receipt, Mapping) else None
            expected_count = 6 if body.get("layout") == "six_faces" else 1
            valid_paths = (
                isinstance(paths, list)
                and len(paths) == expected_count
                and all(
                    isinstance(path, str)
                    and path
                    and Path(path).is_absolute()
                    and not path.endswith(("/", "\\"))
                    and Path(path).suffix
                    and Path(path).name == Path(path).name.rstrip(" .")
                    and not {".", ".."}.intersection(path.replace("\\", "/").split("/"))
                    for path in paths
                )
                and len({os.path.normcase(os.path.normpath(path)) for path in paths}) == expected_count
            )
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("outcome") != "complete"
                or not valid_paths
                or (expected_count == 1 and paths[0] != str(destination))
                or (
                    expected_count == 6
                    and any(Path(path).parent != destination for path in paths)
                )
            ):
                raise HextileClientError(
                    "Export outcome unknown; inspect the destination before a new attempt.",
                    kind="file_outcome_unknown",
                )
            return receipt
        except http.client.HTTPException as exc:
            raise HextileClientError(
                "Export outcome unknown; inspect the destination before a new attempt.",
                body=str(exc),
                kind="file_outcome_unknown",
            ) from exc
        except HextileClientError as exc:
            malformed_2xx = (
                exc.kind == "other"
                and exc.status_code is not None
                and 200 <= exc.status_code < 300
            )
            if exc.kind == "app_down" or malformed_2xx:
                raise HextileClientError(
                    "Export outcome unknown; inspect the destination before a new attempt.",
                    status_code=exc.status_code,
                    body=None if malformed_2xx else exc.body,
                    kind="file_outcome_unknown",
                ) from exc
            raise

    def export_to_unreal_project(self, render_id: str, body: Mapping[str, Any]) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        payload = dict(body)
        payload.pop("destination", None)
        try:
            return self.post_json(
                f"/api/renders/{rid}/export",
                payload,
                timeout=LONG_OPERATION_TIMEOUT_S,
            )
        except HextileClientError as exc:
            if exc.kind == "app_down":
                raise HextileClientError(
                    "Connection ended; check the folder before retry.",
                    status_code=exc.status_code,
                    body=exc.body,
                    kind="ue_outcome_unknown",
                ) from exc
            raise

    # ── saved Layers generation ─────────────────────────────────────────

    def get_layer_draft(self, render_id: str, parent_id: str) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        return self.get_json(f"/api/renders/{rid}/layers/agent-state", params={"parent_id": parent_id})

    def generate_layer(self, render_id: str, body: Mapping[str, Any]) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        try:
            return self.post_json(f"/api/renders/{rid}/layers/agent-generate", body)
        except (HextileClientError, http.client.HTTPException) as exc:
            if not _layer_transport_unknown(exc):
                raise
            try:
                observed = self.get_layer_generation(render_id, str(body["request_id"]))
                if (isinstance(observed, Mapping) and observed.get("job_id") == body["request_id"]
                        and observed.get("parent_id") == body.get("parent_id")):
                    return {"job_id": observed["job_id"], "draft_id": observed.get("package_id"),
                            "composition_id": observed.get("composition_id"), "head": observed["parent_id"],
                            "state": observed.get("state", "unknown")}
            except (HextileClientError, http.client.HTTPException):
                pass
            raise HextileClientError(
                "Generation submission outcome unknown. Read get_layer_generation with the same request_id; "
                "a missing RAM job after restart is not permission to submit a new id.",
                kind="layer_outcome_unknown",
                receipt={"state": "unknown", "render_id": render_id, "request_id": body["request_id"],
                         "reconcile": "get_layer_generation"},
            ) from exc

    def get_layer_generation(self, render_id: str, job_id: str) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        jid = urllib.parse.quote(job_id, safe="")
        return self.get_json(f"/api/renders/{rid}/layers/generate/{jid}")

    def cancel_layer_generation(self, render_id: str, job_id: str) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        jid = urllib.parse.quote(job_id, safe="")
        return self.post_json(f"/api/renders/{rid}/layers/generate/{jid}/agent-cancel")

    def rematte_layer(self, render_id: str, job_id: str, body: Mapping[str, Any]) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        jid = urllib.parse.quote(job_id, safe="")
        try:
            return self.post_json(f"/api/renders/{rid}/layers/generate/{jid}/agent-rematte", body)
        except (HextileClientError, http.client.HTTPException) as exc:
            if not _layer_transport_unknown(exc):
                raise
            raise HextileClientError(
                "Re-matte outcome unknown and non-replayable: the server chooses its job id. "
                "Inspect local job activity before requesting a newly approved action.",
                kind="layer_outcome_unknown",
                receipt={"state": "unknown", "non_replayable": True, "render_id": render_id,
                         "parent_job_id": job_id},
            ) from exc

    def land_generated_layer(self, render_id: str, body: Mapping[str, Any]) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        try:
            return self.post_json(f"/api/renders/{rid}/layers/agent-land", body)
        except (HextileClientError, http.client.HTTPException) as exc:
            if not _layer_transport_unknown(exc):
                raise
            try:
                job = self.get_layer_generation(render_id, str(body["job_id"]))
                draft = self.get_layer_draft(render_id, str(body["expected_head"]))
                if (isinstance(job, Mapping) and job.get("state") == "ready"
                        and job.get("parent_id") == body["expected_head"]
                        and job.get("package_id") == body["draft_id"]
                        and job.get("composition_id") == body["composition_id"]
                        and isinstance(draft, Mapping) and draft.get("head") == body["expected_head"]
                        and draft.get("draft_id") == body["draft_id"]
                        and draft.get("composition_id") == body["composition_id"]):
                    for layer in draft.get("active_bag_layers", []):
                        if (isinstance(layer, Mapping) and layer.get("layer_id") == body["layer_id"]
                                and layer.get("asset") == job.get("asset")
                                and layer.get("recipeSource") == job.get("recipeSource")):
                            return {"draft_id": body["draft_id"], "composition_id": body["composition_id"],
                                    "mutation_rev": draft.get("mutation_rev"), "layer_id": body["layer_id"],
                                    "already_landed": True}
            except (HextileClientError, http.client.HTTPException):
                pass
            raise HextileClientError(
                "Layer landing outcome unknown. Inspect the exact saved draft and job before any repeat; "
                "never mint a new layer id to retry.",
                kind="layer_outcome_unknown",
                receipt={"state": "unknown", "render_id": render_id, "job_id": body["job_id"],
                         "layer_id": body["layer_id"], "draft_id": body["draft_id"],
                         "reconcile": "get_layer_draft and get_layer_generation"},
            ) from exc

    def commit_layer_draft(self, render_id: str, body: Mapping[str, Any]) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        try:
            return self.post_json(f"/api/renders/{rid}/layers/agent-commit", body, timeout=LONG_OPERATION_TIMEOUT_S)
        except (HextileClientError, http.client.HTTPException) as exc:
            if not _layer_transport_unknown(exc):
                raise
            try:
                rid = urllib.parse.quote(render_id, safe="")
                graph = self.get_json(f"/api/renders/{rid}/graph")
                if isinstance(graph, Mapping) and graph.get("head") == body["draft_id"]:
                    for node in graph.get("nodes", []):
                        if (isinstance(node, Mapping) and node.get("id") == body["draft_id"]
                                and node.get("op") == "layers" and node.get("parent") == body["parent_id"]):
                            return {"node_id": body["draft_id"], "head": body["draft_id"]}
            except (HextileClientError, http.client.HTTPException):
                pass
            raise HextileClientError(
                "Layer commit outcome unknown. Inspect the exact draft id in the render graph "
                "and HEAD before considering a new approval; do not replay blindly.",
                kind="layer_outcome_unknown",
                receipt={"state": "unknown", "render_id": render_id,
                         "draft_id": body["draft_id"], "parent_id": body["parent_id"],
                         "reconcile": "inspect render graph HEAD and draft node"},
            ) from exc

    def creative_live(self, tool_name: str, arguments: Mapping[str, Any]) -> Any:
        headers = _REQUEST_HEADERS.get()
        if not self.internal_mode or not headers.get("X-Hextile-Copilot-Token") or not headers.get("X-Hextile-Op"):
            raise HextileClientError("Live creative actions require an approved internal Copilot turn.",
                                     status_code=403, kind="http")
        try:
            return self.post_json("/api/agent/creative-live", {"tool_name": tool_name, "arguments": dict(arguments)})
        except (HextileClientError, http.client.HTTPException) as exc:
            if tool_name != "rematte_layer" or not _layer_transport_unknown(exc):
                raise
            raise HextileClientError(
                "Re-matte outcome unknown and non-replayable: the server chooses its job id. "
                "Inspect local job activity before requesting a newly approved action.",
                kind="layer_outcome_unknown",
                receipt={"state": "unknown", "non_replayable": True,
                         "render_id": arguments.get("render_id"), "parent_job_id": arguments.get("job_id")},
            ) from exc

    # ── saved Spot Clone ────────────────────────────────────────────────

    def preflight_spot_clone(self, render_id: str, parent_id: str) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        return self.get_json(f"/api/renders/{rid}/spot-clone/agent-preflight",
                             params={"parent_id": parent_id})

    def clone_spot(self, render_id: str, body: Mapping[str, Any]) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        try:
            result = self.post_json(f"/api/renders/{rid}/spot-clone/agent", body,
                                    timeout=LONG_OPERATION_TIMEOUT_S)
            if (not isinstance(result, Mapping) or result.get("node_id") != body["node_id"]
                    or result.get("state") != "complete"):
                raise HextileClientError("Spot Clone acknowledgement was incomplete",
                                         kind="spot_clone_outcome_unknown")
            return result
        except (HextileClientError, http.client.HTTPException) as exc:
            if not (_layer_transport_unknown(exc) or
                    isinstance(exc, HextileClientError) and exc.kind == "spot_clone_outcome_unknown"):
                raise
            raise HextileClientError(
                "Spot Clone outcome unknown. Read get_spot_clone with the same render_id "
                "and node_id; do not retry with a new id.",
                kind="spot_clone_outcome_unknown",
                receipt={"state": "unknown", "render_id": render_id,
                         "node_id": body["node_id"], "reconcile": "get_spot_clone"},
            ) from exc

    def get_spot_clone(self, render_id: str, node_id: str) -> Any:
        rid = urllib.parse.quote(render_id, safe="")
        nid = urllib.parse.quote(node_id, safe="")
        return self.get_json(f"/api/renders/{rid}/spot-clone/agent/{nid}")

    def apply_config_delta(
        self,
        *,
        config_partial: Mapping[str, Any],
        doc_generation: str,
        explanation: str = "",
    ) -> Any:
        """POST /api/agent/apply-live. No local merge. Never coerce {}."""
        if not isinstance(config_partial, Mapping):
            raise HextileClientError(
                "config_partial must be a JSON object",
                status_code=None,
                kind="other",
            )
        return self.post_json(
            "/api/agent/apply-live",
            {
                "config_partial": dict(config_partial),
                "doc_generation": str(doc_generation),
                "explanation": explanation,
            },
            headers={"X-Hextile-Agent": "mcp"},
        )

    def get_shader_context(
        self,
        *,
        origin: str,
        shader_id: str,
        project_context_witness: str,
        include_source: bool = False,
        include_formula: bool = False,
        working_revision: Optional[int] = None,
    ) -> Any:
        """GET /api/agent/shader-context. Metadata unless include_source is true."""
        if (origin not in ("builtin", "user", "project") or not shader_id or not project_context_witness
                or type(include_source) is not bool or type(include_formula) is not bool
                or (working_revision is not None and (type(working_revision) is not int or working_revision < 1))):
            raise HextileClientError(
                "origin, shader_id, and project_context_witness are required",
                status_code=None,
                kind="other",
            )
        params: dict[str, Any] = {
            "origin": origin,
            "shader_id": shader_id,
            "include_source": "true" if include_source else "false",
            "include_formula": "true" if include_formula else "false",
        }
        if working_revision is not None:
            params["working_revision"] = int(working_revision)
        return self.get_json(
            "/api/agent/shader-context",
            params=params,
            headers={
                "X-Hextile-Agent": "mcp",
                "X-Hextile-Project-Witness": project_context_witness,
            },
        )

    def propose_shader(self, body: Mapping[str, Any]) -> Any:
        """POST /api/agent/shader-proposals. Does not prepare, consume, import, or delete."""
        validate_shader_proposal(body)
        return self.post_json(
            "/api/agent/shader-proposals",
            dict(body),
            headers={"X-Hextile-Agent": "mcp"},
        )

    def _shader_workspace_request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None,
                                  *, params: Optional[Mapping[str, Any]] = None) -> Any:
        """Proxy once. An uncertain reply never becomes a receipt or automatic retry."""
        try:
            return self.request_json(method, path, body, params=params,
                                     timeout=15.0 if path.endswith("/open") else None,
                                     headers={"X-Hextile-Agent": "mcp"})
        except http.client.HTTPException as exc:
            raise HextileClientError("Shader workspace outcome unknown; rediscover current authority before retrying.",
                                     kind="workspace_outcome_unknown") from exc
        except HextileClientError as exc:
            if exc.kind == "other" and exc.status_code is not None and 200 <= exc.status_code < 300:
                raise HextileClientError("Shader workspace acknowledgement unknown; rediscover current authority before retrying.",
                                         kind="workspace_outcome_unknown", status_code=exc.status_code) from exc
            try:
                detail = json.loads(exc.body or "{}").get("detail")
            except (ValueError, AttributeError):
                detail = None
            if isinstance(detail, dict):
                exc.receipt = detail
            if exc.status_code == 404 and detail == "Not Found":
                raise HextileClientError("Upgrade 360 Hextile to a version with Shader workspaces, then retry.",
                                         kind="upgrade", status_code=404, body=exc.body) from exc
            raise

    def open_shader_workspace(self, body: Mapping[str, Any]) -> Any:
        validate_shader_workspace("open", body)
        result = self._shader_workspace_request("POST", "/api/agent/shader-workspaces/open", dict(body))
        if (not isinstance(result, dict) or type(result.get("handle")) is not str
                or _WORKSPACE_HANDLE.fullmatch(result["handle"]) is None
                or type(result.get("generation")) is not int or not 0 <= result["generation"] <= MAX_WORKSPACE_GENERATION
                or type(result.get("content_digest")) is not str or _WORKSPACE_DIGEST.fullmatch(result["content_digest"]) is None
                or result.get("shader") != body["shader"]
                or result.get("project_context_witness") != body["project_context_witness"]
                or result.get("studio_surface_id") != body["studio_surface_id"]
                or result.get("attachment_state", "attached") != "attached"
                or (body.get("workspace_schema") != 2 and (
                    type(result.get("files")) is not dict or set(result["files"]) != {"source_path", "authoring_path"}
                    or any(type(value) is not str or not value for value in result["files"].values())))):
            raise HextileClientError("Shader workspace acknowledgement unknown; rediscover or make one identical open retry. No paths were released.",
                                     kind="workspace_outcome_unknown")
        if body.get("workspace_schema") == 2 and (
                result.get("workspace_schema") != 2 or type(result.get("files")) is not list
                or not result["files"] or len(result["files"]) > 32
                or any(type(row) is not dict or set(row) != {"path", "native_path"}
                       or any(type(value) is not str or not value for value in row.values()) for row in result["files"])
                or any(type(result.get(key)) is not str or not result[key] for key in ("workspace_root", "authoring_path"))):
            raise HextileClientError("Upgrade 360 Hextile and reconnect for workspace_schema 2; no workspace paths were released.",
                                     kind="upgrade")
        return result

    def register_shader_update(self, args: Mapping[str, Any]) -> Any:
        validate_shader_workspace("register", args)
        handle = urllib.parse.quote(args["handle"], safe="")
        body = {key: args[key] for key in ("change_id", "expected_generation", "content_digest")}
        result = self._shader_workspace_request("POST", f"/api/agent/shader-workspaces/{handle}/updates", body)
        if (not isinstance(result, dict) or result.get("handle") != args["handle"]
                or result.get("change_id") != args["change_id"]
                or result.get("content_digest") != args["content_digest"]
                or type(result.get("generation")) is not int
                or not 0 <= result["generation"] <= MAX_WORKSPACE_GENERATION
                or type(result.get("candidate_id")) is not str or not result["candidate_id"]):
            raise HextileClientError(
                "Shader workspace registration acknowledgement unknown; read the current receipt before retrying.",
                kind="workspace_outcome_unknown")
        return result

    def get_shader_update(self, args: Mapping[str, Any]) -> Any:
        validate_shader_workspace("get", args)
        handle = urllib.parse.quote(args["handle"], safe="")
        change_id = urllib.parse.quote(args["change_id"], safe="")
        params = {"project_context_witness": args["project_context_witness"]}
        if args.get("include_source_diagnostics") is True:
            params["include_source_diagnostics"] = "true"
        result = self._shader_workspace_request("GET", f"/api/agent/shader-workspaces/{handle}/updates/{change_id}", params=params)
        if args.get("include_source_diagnostics") is True and (
                not isinstance(result, dict) or type(result.get("source_diagnostics")) is not list):
            raise HextileClientError("Upgrade 360 Hextile and reconnect for granted source diagnostics; this APP did not acknowledge support.",
                                     kind="upgrade")
        return result

    def get_live_context(
        self,
        *,
        include_nav: bool = False,
        include_active_tool: bool = False,
    ) -> Any:
        """GET /api/agent/live-context. No local merge."""
        return self.get_json(
            "/api/agent/live-context",
            params={
                "include_nav": 1 if include_nav else 0,
                "include_active_tool": 1 if include_active_tool else 0,
            },
        )

    def save_workflow(
        self,
        origin: str,
        workflow_id: str,
        document: Mapping[str, Any],
    ) -> Any:
        """POST /api/workflows/{origin} — create-only on user|project."""
        origin_s = str(origin or "")
        if origin_s == "builtin":
            raise HextileClientError(
                "Built-in workflows are immutable. Save to origin=user or origin=project.",
                status_code=403,
                kind="http",
            )
        if origin_s not in ("user", "project"):
            raise HextileClientError(
                "origin must be 'user' or 'project'",
                status_code=None,
                kind="other",
            )
        if not workflow_id:
            raise HextileClientError(
                "id is required", status_code=None, kind="other"
            )
        if not isinstance(document, Mapping):
            raise HextileClientError(
                "document must be a JSON object", status_code=None, kind="other"
            )
        o = urllib.parse.quote(origin_s, safe="")
        return self.post_json(
            f"/api/workflows/{o}",
            {"id": str(workflow_id), "document": dict(document)},
        )

    def delete_workflow(self, origin: str, workflow_id: str) -> Any:
        """DELETE /api/workflows/{origin}/{id} — user|project only."""
        origin_s = str(origin or "")
        if origin_s == "builtin":
            raise HextileClientError(
                "Built-in workflows are immutable. Delete only origin=user or origin=project.",
                status_code=403,
                kind="http",
            )
        if origin_s not in ("user", "project"):
            raise HextileClientError(
                "origin must be 'user' or 'project'",
                status_code=None,
                kind="other",
            )
        if not workflow_id:
            raise HextileClientError(
                "id is required", status_code=None, kind="other"
            )
        o = urllib.parse.quote(origin_s, safe="")
        wid = urllib.parse.quote(str(workflow_id), safe="")
        return self.delete_json(f"/api/workflows/{o}/{wid}")

    def run_workflow(
        self,
        *,
        workflow_id: Optional[str] = None,
        origin: str = "builtin",
        document: Optional[dict[str, Any]] = None,
        overrides: Optional[dict[str, Any]] = None,
        output: Optional[dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> Any:
        """POST /api/workflows/run (merge + validate [+ queue])."""
        body: dict[str, Any] = {"origin": origin, "dry_run": dry_run}
        if workflow_id is not None:
            body["workflow_id"] = workflow_id
        if document is not None:
            body["document"] = document
        if overrides is not None:
            body["overrides"] = overrides
        if output is not None:
            body["output"] = output
        return self.post_json(
            "/api/workflows/run",
            body,
            headers=None if self.internal_mode else {"X-Hextile-Agent": "mcp"},
        )

    def dry_run_workflow(self, **kwargs: Any) -> Any:
        """POST /api/workflows/run with dry_run=true."""
        kwargs["dry_run"] = True
        return self.run_workflow(**kwargs)

    # ── renders ─────────────────────────────────────────────────────────

    def get_gpu_diagnostics(self) -> Any:
        """GET /api/processors/vram-status; the app owns GPU measurements."""
        return self.get_json("/api/processors/vram-status")

    def get_status(self, run_id: str) -> Any:
        """GET /api/renders/{run_id}."""
        rid = urllib.parse.quote(run_id, safe="")
        return self.get_json(f"/api/renders/{rid}")

    def get_render_config(self, render_id: str) -> Any:
        """GET /api/renders/{render_id}/config — parsed canonical JSON."""
        rid = urllib.parse.quote(render_id, safe="")
        return self.get_json(f"/api/renders/{rid}/config")

    def get_logs(self, run_id: str) -> Any:
        """GET /api/renders/{run_id}/logs."""
        rid = urllib.parse.quote(run_id, safe="")
        return self.get_json(f"/api/renders/{rid}/logs")

    def cancel_run(self, run_id: str) -> Any:
        """POST /api/renders/{run_id}/stop → cancelled."""
        rid = urllib.parse.quote(run_id, safe="")
        return self.post_json(f"/api/renders/{rid}/stop")

    def retry_run(self, run_id: str, *, vram_recovery: bool = False) -> Any:
        """POST retry; request render-scoped idle-holder recovery only when true."""
        rid = urllib.parse.quote(run_id, safe="")
        return self.post_json(
            f"/api/renders/{rid}/retry",
            {"vram_recovery": True} if vram_recovery else None,
        )

    def list_runs(self, lifecycle_status: str = "active") -> Any:
        """GET /api/renders/?lifecycle_status= (default active)."""
        status = lifecycle_status or "active"
        return self.get_json(
            "/api/renders/",
            params={"lifecycle_status": status},
        )

    def search_library_prompts(self, body: Mapping[str, Any]) -> Any:
        """POST a bounded render/sequence prompt search page."""
        return self.post_json("/api/renders/library-prompts/search", body)

    # ── sequences ───────────────────────────────────────────────────────

    def extract_sequence_video(self, video_path: str) -> Any:
        """POST /api/sequences/extract-video {video_path}."""
        return self.post_json(
            "/api/sequences/extract-video",
            {"video_path": video_path},
            timeout=LONG_OPERATION_TIMEOUT_S,
        )

    def create_sequence(
        self,
        folder_path: str,
        config: Mapping[str, Any],
        name: Optional[str] = None,
    ) -> Any:
        """POST /api/sequences/create — folder_path + full config. No local merge."""
        if not isinstance(config, Mapping):
            raise HextileClientError(
                "config must be a JSON object",
                status_code=None,
                kind="other",
            )
        body: dict[str, Any] = {
            "folder_path": folder_path,
            "config": dict(config),
        }
        if name is not None:
            body["name"] = name
        return self.post_json("/api/sequences/create", body)

    def start_sequence(self, sequence_id: str) -> Any:
        """POST /api/sequences/{sequence_id}/start."""
        sid = urllib.parse.quote(sequence_id, safe="")
        return self.post_json(f"/api/sequences/{sid}/start")

    def get_sequence(self, sequence_id: str) -> Any:
        """GET /api/sequences/{sequence_id}."""
        sid = urllib.parse.quote(sequence_id, safe="")
        return self.get_json(f"/api/sequences/{sid}")

    def list_sequences(self, lifecycle_status: str = "active") -> Any:
        """GET /api/sequences/?lifecycle_status= (default active)."""
        status = lifecycle_status or "active"
        return self.get_json(
            "/api/sequences/",
            params={"lifecycle_status": status},
        )

    def stop_sequence(self, sequence_id: str) -> Any:
        """POST /api/sequences/{sequence_id}/stop."""
        sid = urllib.parse.quote(sequence_id, safe="")
        return self.post_json(f"/api/sequences/{sid}/stop")

    # ── seed ────────────────────────────────────────────────────────────

    def get_seed_memory_advice(
        self, lora_path: str, width: int, height: int, *, cpu_offload: bool = False
    ) -> Any:
        """Read two independent APP samples; neither reserves VRAM."""
        gpu = self.get_gpu_diagnostics()
        advice = self.get_json(
            "/api/360-lora/memory-advice",
            params={
                "lora_path": lora_path,
                "width": width,
                "height": height,
                "cpu_offload": cpu_offload,
            },
        )
        return {"sampling": "separate", "gpu": gpu, "advice": advice}

    def generate_seed(
        self,
        prompt: str,
        *,
        request_id: str,
        lora_path: str,
        base_model: str,
        n: int = 4,
        **extra: Any,
    ) -> Any:
        """Submit one idempotent 360-LoRA job; return its short acknowledgement."""
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError("noncanonical UUID")
        except (TypeError, ValueError, AttributeError) as exc:
            raise HextileClientError(
                "request_id must be a canonical UUID", kind="other"
            ) from exc
        body: dict[str, Any] = {
            "request_id": request_id,
            "prompt": prompt,
            "lora_path": lora_path,
            "base_model": base_model,
            "num_variations": n,
        }
        body.update(extra)
        try:
            return self.post_json("/api/360-lora/jobs", body)
        except HextileClientError as exc:
            if exc.kind != "app_down":
                raise
            raise HextileClientError(
                f"Seed acknowledgement unknown for job_id {request_id}. "
                "Call get_seed_job with this ID; replay only the same request body and ID if needed.",
                status_code=exc.status_code,
                body=exc.body,
                kind=exc.kind,
            ) from exc

    def get_seed_job(self, job_id: str) -> Any:
        """GET exact job status, progress, original-index results, and errors."""
        jid = urllib.parse.quote(job_id, safe="")
        return self.get_json(f"/api/360-lora/jobs/{jid}")

    def list_seed_history(
        self,
        offset: int = 0,
        limit: int = 50,
        status: Optional[str] = None,
    ) -> Any:
        """GET /api/360-lora/history — always send offset+limit for {batches, total}."""
        params: dict[str, Any] = {"offset": int(offset), "limit": int(limit)}
        if status:
            params["status"] = status
        return self.get_json("/api/360-lora/history", params=params)

    def get_seed_batch(self, batch_id: str) -> Any:
        """GET /api/360-lora/history/{batch_id}."""
        bid = urllib.parse.quote(batch_id, safe="")
        return self.get_json(f"/api/360-lora/history/{bid}")

    def cancel_seed(self, job_id: str) -> Any:
        """POST job-scoped cancel; never cancel a different seed job."""
        jid = urllib.parse.quote(job_id, safe="")
        return self.post_json(f"/api/360-lora/jobs/{jid}/cancel")

    def list_360_loras(self) -> Any:
        """GET /api/360-lora/loras — catalog for generate_seed path + base_model."""
        return self.get_json("/api/360-lora/loras")

    # ── models ──────────────────────────────────────────────────────────

    def list_installed_models(self, pipeline_id: Optional[str] = None) -> Any:
        """GET /api/models/{pipeline_id}?installed_only=true, or catalog/status."""
        pid = str(pipeline_id).strip() if pipeline_id else ""
        if pid:
            quoted = urllib.parse.quote(pid, safe="")
            return self.get_json(
                f"/api/models/{quoted}",
                params={"installed_only": "true"},
            )
        status = self.get_json("/api/models/catalog/status")
        note = "Pass pipeline_id to list installed weights for that pipeline."
        if isinstance(status, dict):
            out = dict(status)
            out["note"] = note
            return out
        return {"catalog_status": status, "note": note}

    def get_model_readiness(self, overrides: list[Mapping[str, Any]]) -> Any:
        return self.post_json("/api/models/selection-catalog", {"overrides": overrides})

    def get_model_download_queue(self) -> Any:
        return self.get_json("/api/models/queue")

    def _model_mutation(self, path: str, body: Optional[Mapping[str, Any]] = None,
                        *, params: Optional[Mapping[str, Any]] = None,
                        bundle: bool = False) -> Any:
        try:
            result = self.post_json(path, body, params=params)
            if (not isinstance(result, dict)
                    or type(result.get("success")) is not bool
                    or not isinstance(result.get("message"), str)
                    or (bundle and (not isinstance(result.get("model_dispositions"), dict)
                                    or not isinstance(result.get("failed_model_ids"), list)))):
                raise HextileClientError(
                    "Model queue action outcome unknown; read the download queue before any repeat.",
                    kind="model_outcome_unknown",
                )
            return result
        except http.client.HTTPException as exc:
            raise HextileClientError(
                "Model queue action outcome unknown; read the download queue before any repeat.",
                body=str(exc), kind="model_outcome_unknown",
            ) from exc
        except HextileClientError as exc:
            if exc.kind == "app_down" or (
                exc.kind == "other" and exc.status_code is not None
                and 200 <= exc.status_code < 300
            ):
                raise HextileClientError(
                    "Model queue action outcome unknown; read the download queue before any repeat.",
                    body=exc.body, kind="model_outcome_unknown",
                ) from exc
            raise

    def install_model_single(self, pipeline_id: str, model_id: str) -> Any:
        return self._model_mutation("/api/models/queue/add", {
            "pipeline_id": pipeline_id, "model_id": model_id,
        })

    def install_model_bundle(self, pipeline_id: str, model_id: str,
                             expected_selection_fingerprint: str) -> Any:
        pid = urllib.parse.quote(pipeline_id, safe="")
        return self._model_mutation(
            f"/api/models/queue/pipeline/{pid}",
            params={"selected_model_id": model_id,
                    "expected_selection_fingerprint": expected_selection_fingerprint},
            bundle=True,
        )

    def repair_model(self, pipeline_id: str, model_id: str) -> Any:
        pid = urllib.parse.quote(pipeline_id, safe="")
        mid = urllib.parse.quote(model_id, safe="")
        return self._model_mutation(f"/api/models/{pid}/{mid}/repair")

    def cancel_model_download(self, pipeline_id: str, model_id: str,
                              expected_queue_entry_id: str) -> Any:
        pid = urllib.parse.quote(pipeline_id, safe="")
        mid = urllib.parse.quote(model_id, safe="")
        return self._model_mutation(
            f"/api/models/{pid}/{mid}/cancel",
            params={"expected_queue_entry_id": expected_queue_entry_id},
        )

    # ── batch workflows (thin HTTP; no Batch UUID in Render activity) ──

    def preflight_batch(self, body: Mapping[str, Any]) -> Any:
        """POST /api/batch-workflows/preflight — returns promptly (scan is async)."""
        if not isinstance(body, Mapping):
            raise HextileClientError(
                "preflight body must be a JSON object",
                status_code=None,
                kind="other",
            )
        return self.post_json(
            "/api/batch-workflows/preflight",
            dict(body),
            headers={"X-Hextile-Agent": "mcp"},
        )

    def get_batch_preflight(
        self,
        preflight_id: str,
        *,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """GET /api/batch-workflows/preflights/{id}?cursor=&limit=."""
        if not preflight_id:
            raise HextileClientError(
                "preflight_id is required", status_code=None, kind="other"
            )
        pid = urllib.parse.quote(str(preflight_id), safe="")
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = int(limit)
        return self.get_json(
            f"/api/batch-workflows/preflights/{pid}",
            params=params or None,
        )

    def start_batch(
        self,
        *,
        preflight_id: str,
        spec_hash: str,
        idempotency_key: str,
    ) -> Any:
        """POST /api/batch-workflows {preflight_id, spec_hash, idempotency_key}."""
        if not preflight_id or not spec_hash or not idempotency_key:
            raise HextileClientError(
                "preflight_id, spec_hash, and idempotency_key are required",
                status_code=None,
                kind="other",
            )
        return self.post_json(
            "/api/batch-workflows",
            {
                "preflight_id": str(preflight_id),
                "spec_hash": str(spec_hash),
                "idempotency_key": str(idempotency_key),
            },
            headers={"X-Hextile-Agent": "mcp"},
        )

    def list_batches(
        self,
        *,
        status: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """GET /api/batch-workflows?status=&cursor=&limit=."""
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = int(limit)
        return self.get_json("/api/batch-workflows", params=params or None)

    def get_batch(self, job_id: str) -> Any:
        """GET /api/batch-workflows/{job_id}."""
        if not job_id:
            raise HextileClientError(
                "job_id is required", status_code=None, kind="other"
            )
        jid = urllib.parse.quote(str(job_id), safe="")
        return self.get_json(f"/api/batch-workflows/{jid}")

    def get_batch_items(
        self,
        job_id: str,
        *,
        outcome: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        """GET /api/batch-workflows/{job_id}/items?outcome=&cursor=&limit=."""
        if not job_id:
            raise HextileClientError(
                "job_id is required", status_code=None, kind="other"
            )
        jid = urllib.parse.quote(str(job_id), safe="")
        params: dict[str, Any] = {}
        if outcome:
            params["outcome"] = outcome
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = int(limit)
        return self.get_json(
            f"/api/batch-workflows/{jid}/items",
            params=params or None,
        )

    def pause_batch(
        self, job_id: str, *, idempotency_key: str, expected_revision: int
    ) -> Any:
        """POST /api/batch-workflows/{job_id}/pause."""
        return self._batch_control(job_id, "pause", idempotency_key, expected_revision)

    def resume_batch(
        self, job_id: str, *, idempotency_key: str, expected_revision: int
    ) -> Any:
        """POST /api/batch-workflows/{job_id}/resume."""
        return self._batch_control(job_id, "resume", idempotency_key, expected_revision)

    def cancel_batch(
        self, job_id: str, *, idempotency_key: str, expected_revision: int
    ) -> Any:
        """POST /api/batch-workflows/{job_id}/cancel."""
        return self._batch_control(job_id, "cancel", idempotency_key, expected_revision)

    def retry_batch(
        self, job_id: str, *, idempotency_key: str, expected_revision: int
    ) -> Any:
        """POST /api/batch-workflows/{job_id}/retry-failed."""
        return self._batch_control(
            job_id, "retry-failed", idempotency_key, expected_revision
        )

    def import_batch_outputs(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        expected_revision: int,
        scope: Optional[str] = None,
        item_ids: Optional[list[str]] = None,
    ) -> Any:
        """POST /api/batch-workflows/{job_id}/import — published outputs only."""
        if not job_id:
            raise HextileClientError(
                "job_id is required", status_code=None, kind="other"
            )
        if not idempotency_key:
            raise HextileClientError(
                "idempotency_key is required", status_code=None, kind="other"
            )
        body: dict[str, Any] = {
            "idempotency_key": str(idempotency_key),
            "expected_revision": int(expected_revision),
        }
        if item_ids is not None and scope is not None:
            raise HextileClientError(
                "import accepts either scope or item_ids",
                status_code=None,
                kind="other",
            )
        if item_ids is not None:
            if len(item_ids) > 500:
                raise HextileClientError(
                    "at most 500 item ids", status_code=None, kind="other"
                )
            body["item_ids"] = [str(i) for i in item_ids]
        else:
            body["scope"] = str(scope or "all_published")
        jid = urllib.parse.quote(str(job_id), safe="")
        return self.post_json(
            f"/api/batch-workflows/{jid}/import",
            body,
            headers={"X-Hextile-Agent": "mcp"},
        )

    def _batch_control(
        self,
        job_id: str,
        action: str,
        idempotency_key: str,
        expected_revision: int,
    ) -> Any:
        if not job_id:
            raise HextileClientError(
                "job_id is required", status_code=None, kind="other"
            )
        if not idempotency_key:
            raise HextileClientError(
                "idempotency_key is required", status_code=None, kind="other"
            )
        jid = urllib.parse.quote(str(job_id), safe="")
        return self.post_json(
            f"/api/batch-workflows/{jid}/{action}",
            {
                "idempotency_key": str(idempotency_key),
                "expected_revision": int(expected_revision),
            },
            headers={"X-Hextile-Agent": "mcp"},
        )

    # ── handshake (OPEN-3) ──────────────────────────────────────────────

    def probe(self) -> dict[str, Any]:
        """Reachability + workflows/run presence.

        Order:
          1. TCP/HTTP connect — refuse → app_down
          2. POST /api/workflows/run dry_run minimal body
             - 404 → upgrade
             - 422 → ok (handler present)
             - 2xx → ok
        """
        try:
            self.post_json(
                "/api/workflows/run",
                {
                    "dry_run": True,
                    # Intentionally incomplete so a live handler returns 422.
                    "workflow_id": None,
                    "document": None,
                },
            )
            return {"ok": True, "message": "360 Hextile reachable"}
        except HextileClientError as exc:
            if exc.kind == "app_down":
                return {"ok": False, "kind": "app_down", "message": str(exc)}
            if exc.kind == "upgrade" or exc.status_code == 404:
                return {"ok": False, "kind": "upgrade", "message": UPGRADE_MSG}
            if exc.status_code == 422:
                return {
                    "ok": True,
                    "message": "360 Hextile reachable (workflows/run present)",
                }
            # Other HTTP (402 license, 500, …) still means the app is up.
            return {
                "ok": True,
                "message": f"360 Hextile reachable (HTTP {exc.status_code})",
                "detail": str(exc),
            }

    # ── internals ───────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path


def error_payload(exc: BaseException) -> dict[str, Any]:
    """Stable tool-result shaped error for MCP content."""
    if isinstance(exc, HextileClientError):
        payload: dict[str, Any] = {
            "ok": False,
            "error": str(exc),
            "kind": exc.kind,
            "status_code": exc.status_code,
        }
        if exc.kind == "capability":
            payload["code"] = "batch_unavailable"
        if exc.receipt is not None:
            payload["receipt"] = exc.receipt
        return payload
    return {"ok": False, "error": str(exc), "kind": "other"}
