"""Thin HTTP client for the local 360 Hextile backend.

Stdlib only (urllib). Default base URL: http://127.0.0.1:8000.
No app logic — pure proxy helpers for the MCP server and Codex install.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import socket
import uuid
from fractions import Fraction
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


def validate_shader_doctor(operation: str, args: Mapping[str, Any]) -> None:
    """Validate the exact request identity before contacting the mounted Studio."""
    required = ({"request_id", "origin", "shader_id", "project_context_witness", "studio_surface_id"}
                if operation == "request" else {"request_id", "project_context_witness"})
    if not isinstance(args, Mapping) or set(args) != required:
        raise HextileClientError("Expected closed Shader Doctor arguments", kind="other", status_code=422)
    request_id = args["request_id"]
    try:
        valid_id = type(request_id) is str and str(uuid.UUID(request_id)) == request_id
    except (ValueError, AttributeError):
        valid_id = False
    if not valid_id or any(type(args[key]) is not str or not 1 <= len(args[key]) <= 256
                           for key in required - {"request_id"}):
        raise HextileClientError("Invalid Shader Doctor request identity", kind="other", status_code=422)
    if operation == "request" and (args["origin"] not in {"builtin", "user", "project"}
                                   or len(args["shader_id"]) > 128):
        raise HextileClientError("Invalid Shader Doctor shader identity", kind="other", status_code=422)


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
                or shader["origin"] not in ("project", "user") or not text(shader["shaderId"], 128)
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


_SHADER_SEQUENCE_REQUIRED = {
    "shader_ref", "project_context_witness", "working_revision", "document_hash",
    "source_hash", "idempotency_key", "name", "frame_rate", "frame_count",
    "start_time", "end_time", "width", "height", "seed", "output", "publication_mode",
}
_SHADER_SEQUENCE_OPTIONAL = {"resource_manifest_hash", "selected_resource_frames"}


def validate_shader_sequence_request(body: Mapping[str, Any]) -> None:
    """Closed transport guard; APP remains authoritative for saved identity and resources."""
    def refuse() -> None:
        raise HextileClientError("Expected a closed saved-native Shader sequence request", kind="other", status_code=422)

    def integer(value: Any, lo: int, hi: int) -> bool:
        return type(value) is int and lo <= value <= hi

    def text(value: Any, maximum: int) -> bool:
        return type(value) is str and 1 <= len(value) <= maximum

    def digest(value: Any) -> bool:
        return type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None

    def rational(value: Any) -> Fraction:
        if (type(value) is not dict or set(value) != {"num", "den"}
                or any(type(value[key]) is not str or re.fullmatch(
                    r"(0|[1-9][0-9]{0,31})" if key == "num" else r"[1-9][0-9]{0,31}",
                    value[key]) is None for key in ("num", "den"))):
            refuse()
        result = Fraction(int(value["num"]), int(value["den"]))
        if result.denominator != int(value["den"]):
            refuse()
        return result

    if (not isinstance(body, Mapping) or not _SHADER_SEQUENCE_REQUIRED <= set(body)
            or set(body) - _SHADER_SEQUENCE_REQUIRED - _SHADER_SEQUENCE_OPTIONAL
            or body.get("publication_mode") != "deferred"):
        refuse()
    ref = body["shader_ref"]
    if (type(ref) is not dict or set(ref) != {"origin", "id"}
            or ref["origin"] not in ("builtin", "user", "project")
            or type(ref["id"]) is not str or re.fullmatch(r"[a-zA-Z0-9_-]+", ref["id"]) is None):
        refuse()
    if (not text(body["project_context_witness"], 256) or not text(body["idempotency_key"], 128)
            or not text(body["name"], 128) or not integer(body["working_revision"], 1, 2**63 - 1)
            or not digest(body["document_hash"]) or not digest(body["source_hash"])):
        refuse()
    if "resource_manifest_hash" in body and body["resource_manifest_hash"] is not None and not digest(body["resource_manifest_hash"]):
        refuse()
    frames = body.get("selected_resource_frames")
    if frames is not None:
        if type(frames) is not list:
            refuse()
        for frame in frames:
            if (type(frame) is not dict or set(frame) != {"slot_id", "frame_hash", "index", "width", "height"}
                    or not text(frame["slot_id"], 256) or not digest(frame["frame_hash"])
                    or not integer(frame["index"], 0, 2**31 - 1)
                    or not integer(frame["width"], 1, 2**31 - 1)
                    or not integer(frame["height"], 1, 2**31 - 1)):
                refuse()
    rate = body["frame_rate"]
    if (type(rate) is not dict or set(rate) != {"num", "den"}
            or not integer(rate["num"], 1, 1_000_000) or not integer(rate["den"], 1, 1_000_000)):
        refuse()
    fps = Fraction(rate["num"], rate["den"])
    if fps.denominator != rate["den"] or not 1 <= fps <= 120:
        refuse()
    start, end = rational(body["start_time"]), rational(body["end_time"])
    count = (end - start) * fps
    expected_count = -(-count.numerator // count.denominator)
    if (not 0 <= start < end <= 86400 or not integer(body["frame_count"], 1, 10000)
            or body["frame_count"] != expected_count or not integer(body["seed"], 0, 2**32 - 1)
            or not integer(body["width"], 4, 16384) or not integer(body["height"], 2, 8192)):
        refuse()
    output = body["output"]
    if type(output) is not dict or "projection" not in output:
        refuse()
    projection = output["projection"]
    if projection == "equirectangular":
        if (set(output) != {"projection", "pan", "tilt", "roll"}
                or any(type(output[key]) not in (int, float) or output[key] != 0 for key in ("pan", "tilt", "roll"))
                or body["width"] != 2 * body["height"] or body["height"] % 2):
            refuse()
    elif projection in ("fulldome", "fulldome_angular"):
        if (set(output) != {"projection", "diameter", "fov_deg", "pan", "tilt", "roll"}
                or type(output["diameter"]) is not int or output["diameter"] not in (1024, 2048, 4096, 8192)
                or body["width"] != output["diameter"] or body["height"] != output["diameter"]):
            refuse()
        for key, lo, hi in (("fov_deg", 10, 360), ("pan", -180, 180), ("tilt", -90, 90), ("roll", -180, 180)):
            value = output[key]
            if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
                refuse()
    else:
        refuse()


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
_SHADER_EDIT_FIELDS_V2 = {
    **_SHADER_EDIT_FIELDS,
    "set_parameter_expression": ({"target", "source_text", "refs"}, set()),
    "remove_parameter_expression": ({"target"}, set()),
    "set_parameter_mode": ({"target", "mode"}, {"binding"}),
    "set_timeline": ({"start", "end", "loop"}, set()),
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
                    or batch["schema"] not in (1, 2) or type(batch["edits"]) is not list or len(batch["edits"]) > 32):
                refuse()
            try:
                size = len(json.dumps(batch, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, UnicodeError, RecursionError):
                refuse()
            if size > 65536:
                refuse()
            for edit in batch["edits"]:
                fields = _SHADER_EDIT_FIELDS if batch["schema"] == 1 else _SHADER_EDIT_FIELDS_V2
                if type(edit) is not dict or type(edit.get("op")) is not str or edit["op"] not in fields:
                    refuse()
                required, optional = fields[edit["op"]]
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
        # The APP gate must see every proxy request, including private Copilot calls.
        for key in tuple(req_headers):
            if key.lower() == "x-hextile-agent":
                del req_headers[key]
        req_headers["X-Hextile-Agent"] = "mcp"
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

    def get_shader_parameters(self) -> Any:
        """Read only the mounted Shader's source-free exposed controls."""
        return self.get_json("/api/agent/shader-parameters", headers={"X-Hextile-Agent": "mcp"})

    def set_shader_parameter(self, args: Mapping[str, Any]) -> Any:
        """Submit one witnessed value edit or reconcile its original request ID."""
        request_id = args.get("client_request_id") if isinstance(args, Mapping) else None
        if type(request_id) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id) is None:
            raise HextileClientError("Invalid Shader parameter client_request_id", kind="other", status_code=422)
        operation = args.get("operation")
        if operation == "status":
            if set(args) != {"operation", "client_request_id"}:
                raise HextileClientError("Expected exact Shader parameter status arguments", kind="other", status_code=422)
            try:
                data = self.get_json(f"/api/agent/shader-parameter/requests/{request_id}",
                                     headers={"X-Hextile-Agent": "mcp"})
            except HextileClientError as exc:
                if exc.status_code == 404:
                    raise HextileClientError(
                        f"Shader parameter outcome unknown for client_request_id={request_id}; absence is not refusal.",
                        kind="shader_parameter_outcome_unknown", status_code=404,
                        receipt={"client_request_id": request_id, "status": "unknown"}) from exc
                raise
            return self._shader_parameter_receipt(data, request_id, submit=False)
        if operation != "submit" or set(args) != {"operation", "client_request_id", "witness", "parameter_id", "value"}:
            raise HextileClientError("Expected exact Shader parameter submit arguments", kind="other", status_code=422)
        witness = args["witness"]
        if (type(witness) is not dict or set(witness) != {"studio_surface_id", "project_context_witness", "shader", "base", "edit_sequence"}
                or type(witness["studio_surface_id"]) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", witness["studio_surface_id"])
                or type(witness["project_context_witness"]) is not str or not 1 <= len(witness["project_context_witness"]) <= 256
                or type(witness["edit_sequence"]) is not int or not 0 <= witness["edit_sequence"] <= 2**53 - 1
                or type(witness["shader"]) is not dict or set(witness["shader"]) != {"origin", "shaderId"}
                or witness["shader"]["origin"] not in {"builtin", "user", "project"}
                or type(witness["shader"]["shaderId"]) is not str
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", witness["shader"]["shaderId"])):
            raise HextileClientError("Invalid Shader parameter witness", kind="other", status_code=422)
        base = witness["base"]
        digest = lambda value: type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
        if type(base) is not dict:
            raise HextileClientError("Invalid Shader parameter base", kind="other", status_code=422)
        if base.get("kind") == "unaccepted":
            valid_base = set(base) == {"kind", "authoring_digest"} and digest(base["authoring_digest"])
        else:
            tagged = base.get("kind") == "saved"
            required = {"working_revision", "document_hash", "source_hash"} | ({"kind"} if tagged else set())
            valid_base = (required <= set(base) and set(base) <= (required | (set() if tagged else {"manifest_hash"}))
                          and type(base["working_revision"]) is int and base["working_revision"] >= 1
                          and digest(base["document_hash"]) and digest(base["source_hash"]))
            if valid_base and "manifest_hash" in base:
                valid_base = base["manifest_hash"] is None or digest(base["manifest_hash"])
        if not valid_base:
            raise HextileClientError("Invalid Shader parameter base", kind="other", status_code=422)
        if type(args["parameter_id"]) is not str or not 1 <= len(args["parameter_id"]) <= 128:
            raise HextileClientError("Invalid Shader parameter ID", kind="other", status_code=422)
        def finite_json(value: Any, depth: int = 0) -> bool:
            if depth > 8:
                return False
            if value is None or type(value) in (str, bool, int):
                return True
            if type(value) is float:
                return math.isfinite(value)
            if type(value) is list:
                return len(value) <= 64 and all(finite_json(item, depth + 1) for item in value)
            if type(value) is dict:
                return len(value) <= 64 and all(type(key) is str and finite_json(item, depth + 1)
                                                for key, item in value.items())
            return False
        value = args["value"]
        if not finite_json(value) or len(json.dumps(value, allow_nan=False).encode("utf-8")) > 65536:
            raise HextileClientError("Shader parameter value must be bounded finite JSON", kind="other", status_code=422)
        body = {key: args[key] for key in ("client_request_id", "witness", "parameter_id", "value")}
        try:
            data = self.post_json("/api/agent/shader-parameter", body, headers={"X-Hextile-Agent": "mcp"})
            return self._shader_parameter_receipt(data, request_id, submit=True)
        except HextileClientError as exc:
            if (exc.kind == "app_down" or exc.status_code is None or exc.status_code >= 500
                    or exc.kind == "other" and exc.status_code is not None and 200 <= exc.status_code < 300):
                raise HextileClientError(
                    f"Shader parameter outcome unknown for client_request_id={request_id}; use status with this ID.",
                    kind="shader_parameter_outcome_unknown", receipt={"client_request_id": request_id}) from exc
            raise
        except Exception as exc:
            raise HextileClientError(
                f"Shader parameter outcome unknown for client_request_id={request_id}; use status with this ID.",
                kind="shader_parameter_outcome_unknown", receipt={"client_request_id": request_id}) from exc

    @staticmethod
    def _shader_parameter_receipt(data: Any, request_id: str, *, submit: bool) -> dict[str, Any]:
        if (not isinstance(data, Mapping) or (submit and data.get("ok") is not True)
                or "client_request_id" in data and data["client_request_id"] != request_id
                or type(data.get("ticket_id")) is not str
                or data.get("status") not in {"pending", "applied", "refused", "unknown"}):
            raise HextileClientError("Invalid Shader parameter receipt", kind="other", status_code=200)
        try:
            if str(uuid.UUID(data["ticket_id"])) != data["ticket_id"]:
                raise ValueError("noncanonical")
        except (ValueError, AttributeError):
            raise HextileClientError("Invalid Shader parameter receipt", kind="other", status_code=200) from None
        result = {"client_request_id": request_id, "ticket_id": data["ticket_id"], "status": data["status"]}
        for key in ("parameter_id", "reason"):
            if key in data:
                if type(data[key]) is not str or len(data[key]) > 128:
                    raise HextileClientError("Invalid Shader parameter receipt", kind="other", status_code=200)
                result[key] = data[key]
        return result

    def propose_shader(self, body: Mapping[str, Any]) -> Any:
        """POST /api/agent/shader-proposals. Does not prepare, consume, import, or delete."""
        validate_shader_proposal(body)
        return self.post_json(
            "/api/agent/shader-proposals",
            dict(body),
            headers={"X-Hextile-Agent": "mcp"},
        )

    @staticmethod
    def _shader_sequence_error(exc: HextileClientError, *, uncertain: bool = False) -> HextileClientError:
        code = None
        try:
            detail = json.loads(exc.body or "").get("detail")
            if isinstance(detail, Mapping) and type(detail.get("code")) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", detail["code"]):
                code = detail["code"]
        except (ValueError, AttributeError, TypeError):
            pass
        if uncertain and (exc.kind == "app_down" or exc.status_code is None
                          or exc.status_code >= 500
                          or exc.kind == "other" and exc.status_code is not None and 200 <= exc.status_code < 300):
            return HextileClientError(
                "Shader sequence POST outcome unknown. GET the preflight-known sequence_id and compare request_hash; never mint a new ID or blindly repost.",
                status_code=exc.status_code, kind="shader_sequence_outcome_unknown",
                receipt={"status": "unknown"})
        return HextileClientError("Shader sequence refused or unavailable", status_code=exc.status_code,
                                  kind=exc.kind, receipt={"code": code or "shader_sequence_refused"})

    @staticmethod
    def _shader_sequence_receipt(data: Any, *, preflight: bool) -> dict[str, Any]:
        if not isinstance(data, Mapping):
            raise HextileClientError("Invalid Shader sequence receipt", kind="other")
        allowed = ("sequence_id", "status", "request_hash", "recipe_hash")
        result = {key: data[key] for key in allowed if key in data and type(data[key]) is str}
        if (re.fullmatch(r"seq_[0-9a-f]{32}", result.get("sequence_id", "")) is None
                or re.fullmatch(r"sha256:[0-9a-f]{64}", result.get("request_hash", "")) is None
                or result.get("status") not in (("ready", "existing") if preflight else
                                                ("queued", "processing", "completed", "completed_with_errors",
                                                 "failed", "cancelled", "incomplete", "paused", "stopped"))):
            raise HextileClientError("Invalid Shader sequence receipt", kind="other")
        if "recipe_hash" in result and re.fullmatch(r"sha256:[0-9a-f]{64}", result["recipe_hash"]) is None:
            raise HextileClientError("Invalid Shader sequence receipt", kind="other")
        return result

    def _shader_sequence_post(self, body: Mapping[str, Any], *, preflight: bool) -> dict[str, Any]:
        validate_shader_sequence_request(body)
        path = "/api/shaders/sequences/preflight" if preflight else "/api/shaders/sequences"
        try:
            data = self.post_json(path, dict(body),
                                  headers={"X-Hextile-Project-Witness": body["project_context_witness"]})
            return self._shader_sequence_receipt(data, preflight=preflight)
        except HextileClientError as exc:
            raise self._shader_sequence_error(exc, uncertain=not preflight) from exc
        except Exception as exc:  # Transport/read failures can include private exception text.
            safe = HextileClientError("Shader sequence transport failed", kind="other")
            raise self._shader_sequence_error(safe, uncertain=not preflight) from exc

    def preflight_shader_sequence(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._shader_sequence_post(body, preflight=True)

    def render_shader_sequence(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._shader_sequence_post(body, preflight=False)

    def get_shader_sequence(self, sequence_id: str) -> dict[str, Any]:
        if type(sequence_id) is not str or re.fullmatch(r"seq_[0-9a-f]{32}", sequence_id) is None:
            raise HextileClientError("Expected a known Shader sequence ID", kind="other", status_code=422)
        try:
            data = self.get_json(f"/api/shaders/sequences/{sequence_id}")
        except HextileClientError as exc:
            if exc.status_code == 404:
                raise HextileClientError(
                    "Known Shader sequence ID is absent; an uncertain render POST remains outcome unknown. Do not blindly repost.",
                    status_code=404, kind="shader_sequence_outcome_unknown",
                    receipt={"sequence_id": sequence_id, "status": "unknown"}) from exc
            raise self._shader_sequence_error(exc) from exc
        except Exception as exc:  # Keep unexpected transport details out of MCP results.
            raise HextileClientError("Shader sequence status unavailable", kind="other") from exc
        if not isinstance(data, Mapping) or data.get("sequence_id") != sequence_id:
            raise HextileClientError("Invalid Shader sequence status", kind="other")
        result: dict[str, Any] = {"sequence_id": sequence_id}
        statuses = {"queued", "processing", "completed", "completed_with_errors", "failed",
                    "cancelled", "paused", "incomplete", "stopped"}
        if data.get("status") in statuses:
            result["status"] = data["status"]
        if data.get("publication_mode") == "deferred":
            result["publication_mode"] = "deferred"
        for key in ("request_hash", "recipe_hash", "render_identity"):
            if type(data.get(key)) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", data[key]):
                result[key] = data[key]
        if type(data.get("attempt_id")) is str and re.fullmatch(r"attempt_[0-9a-f]{32}", data["attempt_id"]):
            result["attempt_id"] = data["attempt_id"]
        elif data.get("attempt_id") is None and "attempt_id" in data:
            result["attempt_id"] = None
        for key in ("frame_count", "width", "height", "published_cursor", "frames_pending",
                    "frames_queued", "frames_processing", "frames_completed", "frames_failed", "attempt_revision"):
            if type(data.get(key)) is int and data[key] >= 0:
                result[key] = data[key]
        if data.get("current_frame_index") is None and "current_frame_index" in data:
            result["current_frame_index"] = None
        elif type(data.get("current_frame_index")) is int and data["current_frame_index"] >= 0:
            result["current_frame_index"] = data["current_frame_index"]
        rate = data.get("frame_rate")
        if isinstance(rate, Mapping) and all(type(rate.get(key)) is int and rate[key] > 0 for key in ("num", "den")):
            result["frame_rate"] = {key: rate[key] for key in ("num", "den")}
        for key in ("start_time", "end_time"):
            value = data.get(key)
            if isinstance(value, Mapping) and all(type(value.get(part)) is str and re.fullmatch(r"[0-9]{1,32}", value[part]) for part in ("num", "den")):
                result[key] = {part: value[part] for part in ("num", "den")}
        output = data.get("output")
        if isinstance(output, Mapping) and output.get("projection") in ("equirectangular", "fulldome", "fulldome_angular"):
            projected = {"projection": output["projection"]}
            for key in ("diameter", "fov_deg", "pan", "tilt", "roll"):
                value = output.get(key)
                if type(value) in (int, float) and math.isfinite(value):
                    projected[key] = value
            result["output"] = projected
        def attempt(source: Any) -> dict[str, Any] | None:
            if not isinstance(source, Mapping):
                return None
            projected: dict[str, Any] = {}
            for key in ("attempt_id", "transaction_fingerprint"):
                pattern = r"attempt_[0-9a-f]{32}" if key == "attempt_id" else r"sha256:[0-9a-f]{64}"
                if type(source.get(key)) is str and re.fullmatch(pattern, source[key]):
                    projected[key] = source[key]
            if source.get("status") in {"queued", "running", "interrupted", "completed", "failed", "cancelled"}:
                projected["status"] = source["status"]
            for key in ("attempt_revision", "frames_completed", "frames_failed", "quality_if_jpg"):
                if type(source.get(key)) is int and source[key] >= 0:
                    projected[key] = source[key]
            if source.get("format") in ("PNG", "JPG"):
                projected["format"] = source["format"]
            return projected
        if "folder_export" in data:
            result["folder_export"] = attempt(data["folder_export"])
        if isinstance(data.get("publication_attempts"), list):
            result["publication_attempts"] = [attempt(item) for item in data["publication_attempts"][:8]
                                              if isinstance(item, Mapping)]
        return result

    @staticmethod
    def _shader_export_args(args: Mapping[str, Any], *, submit: bool) -> tuple[str, dict[str, Any]]:
        required = {"sequence_id", "destination_folder", "format"}
        optional = {"quality_if_jpg"}
        if submit:
            required.add("transaction_fingerprint")
        if (not isinstance(args, Mapping) or not required <= args.keys()
                or args.keys() - required - optional):
            raise HextileClientError("Expected closed Shader folder export intent", kind="other", status_code=422)
        sid = args["sequence_id"]
        folder = args["destination_folder"]
        encoding = args["format"]
        quality = args.get("quality_if_jpg")
        if (type(sid) is not str or re.fullmatch(r"seq_[0-9a-f]{32}", sid) is None
                or type(folder) is not str or not folder or "\x00" in folder
                or type(encoding) is not str or encoding not in ("PNG", "JPG")
                or (encoding == "JPG") != (type(quality) is int and 1 <= quality <= 100)
                or (encoding == "PNG" and "quality_if_jpg" in args)):
            raise HextileClientError("Invalid Shader folder export intent", kind="other", status_code=422)
        body = {"destination_folder": folder, "format": encoding,
                "quality_if_jpg": quality}
        if submit:
            fingerprint = args["transaction_fingerprint"]
            if type(fingerprint) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
                raise HextileClientError("Invalid Shader export fingerprint", kind="other", status_code=422)
            body["transaction_fingerprint"] = fingerprint
        return sid, body

    @staticmethod
    def _shader_export_receipt(data: Any, sequence_id: str, *, preflight: bool) -> dict[str, Any]:
        if not isinstance(data, Mapping) or data.get("sequence_id") != sequence_id:
            raise HextileClientError("Invalid Shader export receipt", kind="other")
        result = {"sequence_id": sequence_id}
        patterns = {"attempt_id": r"attempt_[0-9a-f]{32}",
                    "transaction_fingerprint": r"sha256:[0-9a-f]{64}",
                    "recipe_hash": r"sha256:[0-9a-f]{64}"}
        for key, pattern in patterns.items():
            if type(data.get(key)) is str and re.fullmatch(pattern, data[key]):
                result[key] = data[key]
        if ("attempt_id" not in result or "transaction_fingerprint" not in result
                or result["attempt_id"] != "attempt_" + result["transaction_fingerprint"][7:39]):
            raise HextileClientError("Invalid Shader export receipt", kind="other")
        allowed_status = {"ready", "existing"} if preflight else {"queued", "existing"}
        if type(data.get("status")) is not str or data["status"] not in allowed_status:
            raise HextileClientError("Invalid Shader export receipt", kind="other", status_code=202)
        result["status"] = data["status"]
        if type(data.get("attempt_revision")) is int and data["attempt_revision"] >= 0:
            result["attempt_revision"] = data["attempt_revision"]
        return result

    def preflight_shader_sequence_export(self, args: Mapping[str, Any]) -> dict[str, Any]:
        sid, body = self._shader_export_args(args, submit=False)
        try:
            data = self.post_json(f"/api/shaders/sequences/{sid}/export/preflight", body)
            return self._shader_export_receipt(data, sid, preflight=True)
        except HextileClientError as exc:
            raise self._shader_sequence_error(exc) from exc
        except Exception as exc:
            raise HextileClientError("Shader export preflight unavailable", kind="other") from exc

    def export_shader_sequence(self, args: Mapping[str, Any]) -> dict[str, Any]:
        sid, body = self._shader_export_args(args, submit=True)
        try:
            data = self.post_json(f"/api/shaders/sequences/{sid}/export", body)
            receipt = self._shader_export_receipt(data, sid, preflight=False)
            if receipt["transaction_fingerprint"] != body["transaction_fingerprint"]:
                raise HextileClientError("Invalid Shader export acknowledgement", kind="other", status_code=202)
            return receipt
        except HextileClientError as exc:
            if (exc.kind == "app_down" or exc.status_code is None or exc.status_code >= 500
                    or exc.kind == "other" and exc.status_code is not None and 200 <= exc.status_code < 300):
                raise HextileClientError(
                    "Shader export POST outcome unknown. GET the preflight-known sequence_id and compare "
                    "attempt_id and transaction_fingerprint; never blindly repost.",
                    status_code=exc.status_code, kind="shader_sequence_outcome_unknown",
                    receipt={"status": "unknown"}) from exc
            raise self._shader_sequence_error(exc) from exc
        except Exception as exc:
            raise HextileClientError(
                "Shader export POST outcome unknown. GET the preflight-known sequence_id and compare "
                "attempt_id and transaction_fingerprint; never blindly repost.",
                kind="shader_sequence_outcome_unknown", receipt={"status": "unknown"}) from exc

    def control_shader_sequence(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if (not isinstance(args, Mapping) or not {"sequence_id", "action"} <= args.keys()
                or args.keys() - {"sequence_id", "action", "attempt_id"}):
            raise HextileClientError("Expected closed Shader sequence control", kind="other", status_code=422)
        sid, action = args["sequence_id"], args["action"]
        attempt = args.get("attempt_id")
        if (type(sid) is not str or re.fullmatch(r"seq_[0-9a-f]{32}", sid) is None
                or type(action) is not str or action not in {"stop", "resume", "cancel_export", "retry_export"}
                or (action in {"cancel_export", "retry_export"}) != (type(attempt) is str and re.fullmatch(r"attempt_[0-9a-f]{32}", attempt) is not None)
                or (action in {"stop", "resume"} and "attempt_id" in args)):
            raise HextileClientError("Invalid Shader sequence control target", kind="other", status_code=422)
        suffix = {"stop": "stop", "resume": "resume", "cancel_export": "export/cancel",
                  "retry_export": "export/retry"}[action]
        try:
            data = self.post_json(f"/api/shaders/sequences/{sid}/{suffix}",
                                  {"attempt_id": attempt} if attempt is not None else None)
            if not isinstance(data, Mapping) or data.get("sequence_id") != sid:
                raise HextileClientError("Invalid Shader sequence control acknowledgement", kind="other", status_code=202)
            statuses = {"queued", "processing", "completed", "completed_with_errors", "failed",
                        "cancelled", "paused", "incomplete", "stopped"} if attempt is None else {
                        "queued", "running", "completed", "failed", "cancelled", "interrupted"}
            if type(data.get("status")) is not str or data["status"] not in statuses:
                raise HextileClientError("Invalid Shader sequence control acknowledgement", kind="other", status_code=202)
            if attempt is not None:
                folder = data.get("folder_export")
                if (not isinstance(folder, Mapping) or folder.get("attempt_id") != attempt
                        or folder.get("status") != data["status"]):
                    raise HextileClientError("Invalid Shader export control acknowledgement", kind="other", status_code=202)
            result = {"sequence_id": sid, "action": action}
            result["status"] = data["status"]
            if attempt is not None:
                result["attempt_id"] = attempt
            return result
        except HextileClientError as exc:
            if (exc.kind == "app_down" or exc.status_code is None or exc.status_code >= 500
                    or exc.kind == "other" and 200 <= exc.status_code < 300):
                raise HextileClientError(
                    "Shader control POST outcome unknown. GET the known sequence_id and exact attempt_id before another action.",
                    status_code=exc.status_code, kind="shader_sequence_outcome_unknown",
                    receipt={"status": "unknown"}) from exc
            raise self._shader_sequence_error(exc) from exc
        except Exception as exc:
            raise HextileClientError(
                "Shader control POST outcome unknown. GET the known sequence_id and exact attempt_id before another action.",
                kind="shader_sequence_outcome_unknown", receipt={"status": "unknown"}) from exc

    @staticmethod
    def _shader_timeline_receipt(data: Any, request_id: str, *, submit: bool) -> dict[str, Any]:
        reasons = {"stale_snapshot", "wrong_owner", "follow_off", "expired", "studio_closed",
                   "cancelled", "preview_unavailable", "outcome_unconfirmed"}
        if (not isinstance(data, Mapping) or (submit and data.get("ok") is not True)
                or "client_request_id" in data and data["client_request_id"] != request_id
                or type(data.get("ticket_id")) is not str):
            raise HextileClientError("Invalid Shader timeline receipt", kind="other", status_code=200)
        try:
            if str(uuid.UUID(data["ticket_id"])) != data["ticket_id"]:
                raise ValueError("noncanonical")
        except (ValueError, AttributeError):
            raise HextileClientError("Invalid Shader timeline receipt", kind="other", status_code=200) from None
        status = data.get("status")
        if type(status) is not str or status not in {"pending", "applied", "refused", "unknown"}:
            raise HextileClientError("Invalid Shader timeline receipt", kind="other", status_code=200)
        result = {"client_request_id": request_id, "ticket_id": data["ticket_id"], "status": status}
        if "action" in data:
            if type(data["action"]) is not str or data["action"] not in {"play", "pause", "seek", "reset"}:
                raise HextileClientError("Invalid Shader timeline receipt", kind="other", status_code=200)
            result["action"] = data["action"]
        if "reason" in data:
            if type(data["reason"]) is not str or data["reason"] not in reasons:
                raise HextileClientError("Invalid Shader timeline receipt", kind="other", status_code=200)
            result["reason"] = data["reason"]
        return result

    @staticmethod
    def _shader_timeline_error(exc: HextileClientError) -> HextileClientError:
        code = None
        try:
            detail = json.loads(exc.body or "").get("detail")
            if type(detail) is str and detail in {"invalid_external_authority", "request_conflict",
                                                      "follow_off", "stale_snapshot", "apply_busy",
                                                      "invalid_client_request_id"}:
                code = detail
        except (ValueError, AttributeError, TypeError):
            pass
        return HextileClientError("Shader timeline refused or unavailable", kind=exc.kind,
                                  status_code=exc.status_code, receipt={"code": code} if code else None)

    def control_shader_timeline(self, args: Mapping[str, Any]) -> Any:
        """Submit once or recover the caller's known request ID; never poll the UI delivery door."""
        request_id = args.get("client_request_id") if isinstance(args, Mapping) else None
        if type(request_id) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id) is None:
            raise HextileClientError("Invalid Shader timeline client_request_id", kind="other", status_code=422)
        operation = args.get("operation")
        if operation == "status":
            if set(args) != {"operation", "client_request_id"}:
                raise HextileClientError("Expected exact timeline status arguments", kind="other", status_code=422)
            try:
                data = self.get_json(f"/api/agent/shader-timeline-control/requests/{request_id}",
                                     headers={"X-Hextile-Agent": "mcp"})
                return self._shader_timeline_receipt(data, request_id, submit=False)
            except HextileClientError as exc:
                if exc.status_code == 404:
                    raise HextileClientError(
                        f"Shader timeline outcome unknown for client_request_id={request_id}; expiry or absence is not refusal.",
                        kind="shader_timeline_outcome_unknown", status_code=404,
                        receipt={"client_request_id": request_id, "status": "unknown"}) from exc
                raise self._shader_timeline_error(exc) from exc
            except Exception as exc:
                raise HextileClientError("Shader timeline status unavailable", kind="other") from exc
        if operation != "submit" or not {"operation", "client_request_id", "action", "witness"} <= set(args):
            raise HextileClientError("Expected exact timeline submit arguments", kind="other", status_code=422)
        action = args["action"]
        expected = {"operation", "client_request_id", "action", "witness"} | ({"time_seconds"} if action == "seek" else set())
        if set(args) != expected or action not in {"play", "pause", "seek", "reset"}:
            raise HextileClientError("Invalid Shader timeline action", kind="other", status_code=422)
        if action == "seek" and (type(args["time_seconds"]) not in (int, float)
                                 or not math.isfinite(args["time_seconds"])
                                 or not 0 <= args["time_seconds"] <= 86400):
            raise HextileClientError("Invalid Shader timeline seek time", kind="other", status_code=422)
        witness = args["witness"]
        witness_keys = {"studio_surface_id", "project_context_witness", "shader", "base",
                        "owner_id", "owner_generation", "viewer_generation", "source_epoch"}
        if (type(witness) is not dict or set(witness) != witness_keys
                or any(type(witness[key]) is not str or not witness[key]
                       for key in ("studio_surface_id", "project_context_witness", "owner_id"))
                or any(type(witness[key]) is not int or witness[key] < 0
                       for key in ("owner_generation", "viewer_generation", "source_epoch"))
                or type(witness["shader"]) is not dict or set(witness["shader"]) != {"origin", "shaderId"}
                or witness["shader"]["origin"] not in {"builtin", "user", "project"}
                or type(witness["shader"]["shaderId"]) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", witness["shader"]["shaderId"]) is None
                or type(witness["base"]) is not dict):
            raise HextileClientError("Invalid Shader timeline witness", kind="other", status_code=422)
        body = {key: value for key, value in args.items() if key != "operation"}
        try:
            receipt = self.post_json("/api/agent/shader-timeline-control", body,
                                     headers={"X-Hextile-Agent": "mcp"})
        except http.client.HTTPException as exc:
            raise HextileClientError(
                f"Shader timeline outcome unknown for client_request_id={request_id}; use status with this ID.",
                kind="shader_timeline_outcome_unknown", receipt={"client_request_id": request_id}) from exc
        except HextileClientError as exc:
            if (exc.kind == "app_down" or exc.status_code is None or exc.status_code >= 500
                    or exc.kind == "other" and exc.status_code is not None
                    and 200 <= exc.status_code < 300):
                raise HextileClientError(
                    f"Shader timeline outcome unknown for client_request_id={request_id}; use status with this ID.",
                    kind="shader_timeline_outcome_unknown", receipt={"client_request_id": request_id}) from exc
            raise self._shader_timeline_error(exc) from exc
        except Exception as exc:
            raise HextileClientError(
                f"Shader timeline outcome unknown for client_request_id={request_id}; use status with this ID.",
                kind="shader_timeline_outcome_unknown", receipt={"client_request_id": request_id}) from exc
        try:
            return self._shader_timeline_receipt(receipt, request_id, submit=True)
        except HextileClientError as exc:
            raise HextileClientError(
                f"Shader timeline acknowledgement unknown for client_request_id={request_id}; use status with this ID.",
                kind="shader_timeline_outcome_unknown", receipt={"client_request_id": request_id}) from exc

    def request_shader_doctor(self, args: Mapping[str, Any]) -> Any:
        """Submit once; an uncertain outcome is reconciled only by exact-ID GET."""
        validate_shader_doctor("request", args)
        request_id = args["request_id"]
        try:
            receipt = self.request_json(
                "POST", "/api/agent/shader-doctor/requests", dict(args),
                headers={"X-Hextile-Agent": "mcp"},
            )
        except http.client.HTTPException as exc:
            raise HextileClientError(
                f"Shader Doctor outcome unknown for request_id={request_id}; poll get_shader_doctor with this ID. Do not resubmit automatically.",
                kind="shader_doctor_outcome_unknown", receipt={"request_id": request_id},
            ) from exc
        except HextileClientError as exc:
            if exc.kind == "app_down" or (exc.kind == "other" and exc.status_code is not None
                                          and 200 <= exc.status_code < 300):
                raise HextileClientError(
                    f"Shader Doctor outcome unknown for request_id={request_id}; poll get_shader_doctor with this ID. Do not resubmit automatically.",
                    kind="shader_doctor_outcome_unknown", receipt={"request_id": request_id},
                ) from exc
            raise
        if not isinstance(receipt, dict) or receipt.get("request_id") != request_id:
            raise HextileClientError(
                f"Shader Doctor acknowledgement unknown for request_id={request_id}; poll get_shader_doctor with this ID.",
                kind="shader_doctor_outcome_unknown", receipt={"request_id": request_id},
            )
        return receipt

    def get_shader_doctor(self, args: Mapping[str, Any]) -> Any:
        """Read the exact request receipt; this never starts another GPU pass."""
        validate_shader_doctor("get", args)
        return self.get_json(
            f"/api/agent/shader-doctor/requests/{args['request_id']}",
            params={"project_context_witness": args["project_context_witness"]},
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

    # ── Fractal workshop and native sequences ──────────────────────────

    _FRACTAL_OP_FIELDS = {
        "set_base": ("target", "value"),
        "set_driver_mode": ("target", "mode"),
        "set_expression": ("target", "source", "refs"),
        "set_mapping": ("value",),
        "remove_mapping": ("id",),
        "remove_driver": ("target",),
        "upsert_modulator": ("value",),
        "remove_modulator": ("id",),
        "set_mouse_input": ("value",),
        "upsert_input_gate": ("value",),
        "remove_input_gate": ("id",),
        "upsert_input_binding": ("value",),
        "remove_input_binding": ("id",),
        "clock_action": ("value",),
        "set_timeline": ("value",),
        "set_fps": ("value",),
        "use_current_view_as_start": (),
        "resume_driver": ("target",),
    }
    _FRACTAL_KINDS = {"fractal_edit", "fractal_transport", "fractal_open", "fractal_save"}
    _FRACTAL_AGENT = {"X-Hextile-Agent": "mcp"}

    @staticmethod
    def _fractal_code(exc: HextileClientError) -> Optional[str]:
        try:
            payload = json.loads(exc.body or "")
        except (ValueError, TypeError):
            return None
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", detail):
            return detail
        if isinstance(detail, dict) and isinstance(detail.get("code"), str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", detail["code"]):
            return detail["code"]
        return None

    @staticmethod
    def _fractal_uncertain(exc: HextileClientError) -> bool:
        return exc.kind == "app_down" or exc.status_code is None or exc.status_code >= 500 or exc.status_code == 410

    def _fractal_forward(self, exc: HextileClientError) -> HextileClientError:
        code = self._fractal_code(exc)
        return HextileClientError(
            "Fractal action refused", status_code=exc.status_code, kind=exc.kind,
            receipt={"code": code} if code else None,
        )

    @staticmethod
    def _fractal_sha(value: Any) -> Optional[str]:
        if type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            return value
        return None

    @classmethod
    def _fractal_witness(cls, value: Any) -> dict[str, Any]:
        required = {
            "header_witness", "project_context_witness", "draft_generation",
            "source_generation", "viewer_generation", "renderer_generation", "owner_id",
        }
        if type(value) is not dict or not required <= set(value) <= required | {"scene_id", "scene_revision"}:
            raise HextileClientError("Invalid Fractal witness", kind="other", status_code=422)
        header = value["header_witness"]
        header_required = {"activationToken", "sessionId", "subjectKey"}
        if type(header) is not dict or not header_required <= set(header) <= header_required | {"projectId"}:
            raise HextileClientError("Invalid Fractal witness", kind="other", status_code=422)
        if any(type(header[key]) is not str or not header[key] for key in header_required):
            raise HextileClientError("Invalid Fractal witness", kind="other", status_code=422)
        if type(value["project_context_witness"]) is not str or not 1 <= len(value["project_context_witness"]) <= 256:
            raise HextileClientError("Invalid Fractal witness", kind="other", status_code=422)
        if type(value["owner_id"]) is not str or not 1 <= len(value["owner_id"]) <= 128:
            raise HextileClientError("Invalid Fractal witness", kind="other", status_code=422)
        for key in ("draft_generation", "source_generation", "viewer_generation", "renderer_generation"):
            if type(value[key]) is not int or value[key] < 0:
                raise HextileClientError("Invalid Fractal witness", kind="other", status_code=422)
        if "scene_id" in value and re.fullmatch(r"scn_[0-9a-f]{8}", value["scene_id"]) is None:
            raise HextileClientError("Invalid Fractal scene id", kind="other", status_code=422)
        if "scene_revision" in value and cls._fractal_sha(value["scene_revision"]) is None:
            raise HextileClientError("Invalid Fractal scene revision", kind="other", status_code=422)
        return value

    @classmethod
    def _fractal_ops(cls, ops: Any) -> list[dict[str, Any]]:
        if type(ops) is not list or not 1 <= len(ops) <= 32:
            raise HextileClientError("Fractal edit batch must contain 1 to 32 ops", kind="other", status_code=422)
        for op in ops:
            fields = cls._FRACTAL_OP_FIELDS.get(op.get("op")) if type(op) is dict else None
            if fields is None or set(op) != {"op", *fields}:
                raise HextileClientError(
                    "Fractal edit op is not closed", kind="other", status_code=422,
                    receipt={"code": "invalid_animation"},
                )
            if "target" in op and (type(op["target"]) is not str or not op["target"]):
                raise HextileClientError("Invalid Fractal target", kind="other", status_code=422)
            if op["op"] == "set_mouse_input" and (
                type(op["value"]) is not dict or set(op["value"]) != {"schema", "enabled", "abi"}
                or op["value"].get("schema") != 1 or op["value"].get("abi") != "hx-mouse/1"
                or type(op["value"].get("enabled")) is not bool
            ):
                raise HextileClientError("Invalid Fractal mouse declaration", kind="other", status_code=422)
        return ops

    def _fractal_request_id(self, value: Any) -> str:
        if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None:
            raise HextileClientError("Invalid Fractal client_request_id", kind="other", status_code=422)
        return value

    def _post_fractal_action(self, kind: str, args: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._fractal_request_id(args.get("client_request_id") if isinstance(args, Mapping) else None)
        witness = self._fractal_witness(args.get("witness") if isinstance(args, Mapping) else None)
        body: dict[str, Any] = {"kind": kind, "client_request_id": request_id, "witness": witness}
        if kind == "fractal_edit":
            if set(args) != {"client_request_id", "witness", "expected_draft_generation", "ops"}:
                raise HextileClientError("Fractal edit arguments are not closed", kind="other", status_code=422)
            if args["expected_draft_generation"] != witness["draft_generation"] or type(args["expected_draft_generation"]) is not int:
                raise HextileClientError("Fractal edit generation does not match the witness", kind="other", status_code=422)
            body["expected_draft_generation"] = args["expected_draft_generation"]
            body["ops"] = self._fractal_ops(args["ops"])
        elif kind == "fractal_transport":
            action = args.get("action")
            allowed = {"client_request_id", "witness", "action"}
            if action == "seek":
                allowed.add("time")
            elif action == "step":
                allowed.add("frames")
            elif action not in {"play", "pause", "rewind"}:
                raise HextileClientError("Invalid Fractal transport action", kind="other", status_code=422)
            if set(args) != allowed:
                raise HextileClientError("Fractal transport arguments are not closed", kind="other", status_code=422)
            body["action"] = action
            if action == "seek":
                body["time"] = args["time"]
            if action == "step":
                body["frames"] = args["frames"]
        elif kind == "fractal_open":
            if set(args) != {"client_request_id", "witness", "scene_id"} or re.fullmatch(r"scn_[0-9a-f]{8}", str(args.get("scene_id"))) is None:
                raise HextileClientError("Fractal open arguments are not closed", kind="other", status_code=422)
            body["scene_id"] = args["scene_id"]
        else:
            allowed = {"client_request_id", "witness", "mode", "target_scene_id"}
            if not isinstance(args, Mapping) or not allowed <= set(args) <= allowed | {"name", "scene_revision"}:
                raise HextileClientError("Fractal save arguments are not closed", kind="other", status_code=422)
            if args["mode"] not in {"save", "save_as"} or re.fullmatch(r"scn_[0-9a-f]{8}", str(args.get("target_scene_id"))) is None:
                raise HextileClientError("Invalid Fractal save target", kind="other", status_code=422)
            if args["mode"] == "save" and self._fractal_sha(args.get("scene_revision")) is None:
                raise HextileClientError("Save requires the adopted scene revision", kind="other", status_code=422)
            if args["mode"] == "save_as" and "scene_revision" in args:
                raise HextileClientError("Save As is create-only and takes no scene revision", kind="other", status_code=422)
            body["mode"] = args["mode"]
            body["target_scene_id"] = args["target_scene_id"]
            if "name" in args:
                body["name"] = args["name"]
            if "scene_revision" in args:
                body["scene_revision"] = args["scene_revision"]
        try:
            data = self.post_json("/api/agent/fractal-actions", body, headers=self._FRACTAL_AGENT)
        except HextileClientError as exc:
            if kind == "fractal_save" and self._fractal_uncertain(exc):
                return self._reconcile_fractal_save(args)
            if self._fractal_uncertain(exc):
                raise HextileClientError(
                    "Fractal action outcome unknown. Read get_fractal_request for this client_request_id. Unknown is not expiry or refusal.",
                    status_code=exc.status_code, kind="fractal_outcome_unknown",
                    receipt={"client_request_id": request_id, "status": "unknown"},
                ) from exc
            raise self._fractal_forward(exc) from exc
        except Exception as exc:
            if kind == "fractal_save":
                return self._reconcile_fractal_save(args)
            raise HextileClientError(
                "Fractal action outcome unknown. Read get_fractal_request for this client_request_id.",
                kind="fractal_outcome_unknown", receipt={"client_request_id": request_id, "status": "unknown"},
            ) from exc
        return self._fractal_action_receipt(data, request_id)

    def _reconcile_fractal_save(self, args: Mapping[str, Any]) -> dict[str, Any]:
        request_id = str(args["client_request_id"])
        target = str(args["target_scene_id"])
        try:
            data = self.get_json(
                f"/api/agent/fractal-actions/requests/{urllib.parse.quote(request_id, safe='')}",
                headers=self._FRACTAL_AGENT,
            )
            receipt = self._fractal_action_receipt(data, request_id)
            if receipt.get("status") != "unknown" or receipt.get("result"):
                return receipt
        except HextileClientError:
            pass
        try:
            written = self.get_json(
                f"/api/fractal/scenes/{urllib.parse.quote(target, safe='')}/write-status/{urllib.parse.quote(request_id, safe='')}"
            )
        except HextileClientError as exc:
            raise HextileClientError(
                "Fractal Save outcome unknown. Unknown is not expiry and is not a reason to post again.",
                status_code=exc.status_code, kind="fractal_outcome_unknown",
                receipt={"client_request_id": request_id, "status": "unknown", "scene_id": target},
            ) from exc
        revision = self._fractal_sha(written.get("revision") if isinstance(written, dict) else None)
        if isinstance(written, dict) and written.get("status") == "accepted" and revision:
            return {
                "client_request_id": request_id, "status": "applied", "kind": "fractal_save",
                "result": {"scene_id": target, "client_request_id": request_id, "scene_revision": revision},
            }
        raise HextileClientError(
            "Fractal Save outcome unknown. Unknown is not expiry and is not a reason to post again.",
            kind="fractal_outcome_unknown",
            receipt={"client_request_id": request_id, "status": "unknown", "scene_id": target},
        )

    def _fractal_action_receipt(self, data: Any, request_id: str) -> dict[str, Any]:
        if not isinstance(data, dict) or data.get("status") not in {"pending", "applied", "refused", "unknown"}:
            raise HextileClientError("Invalid Fractal action receipt", kind="other", status_code=200)
        receipt: dict[str, Any] = {"client_request_id": request_id, "status": data["status"]}
        if type(data.get("ok")) is bool:
            receipt["ok"] = data["ok"]
        if type(data.get("ticket_id")) is str:
            receipt["ticket_id"] = data["ticket_id"]
        if data.get("kind") in self._FRACTAL_KINDS:
            receipt["kind"] = data["kind"]
        if type(data.get("reason")) is str:
            receipt["reason"] = data["reason"]
        body = data.get("result")
        if isinstance(body, dict):
            projected: dict[str, Any] = {}
            if type(body.get("scene_id")) is str:
                projected["scene_id"] = body["scene_id"]
            if type(body.get("client_request_id")) is str:
                projected["client_request_id"] = body["client_request_id"]
            for key in ("scene_revision", "base_scene_revision"):
                revision = self._fractal_sha(body.get(key))
                if revision:
                    projected[key] = revision
            if projected:
                receipt["result"] = projected
        return receipt

    def get_fractal_context(self) -> dict[str, Any]:
        data = self.get_json("/api/agent/live-context", params={"include_nav": 0, "include_active_tool": 0})
        surface = data.get("current_surface") if isinstance(data, dict) else None
        if not isinstance(surface, dict) or surface.get("kind") != "fractal_workshop":
            raise HextileClientError(
                "Fractal workshop is not mounted.", status_code=503, kind="http",
                receipt={"code": "fractal_unavailable"},
            )
        return self._fractal_inspect(surface)

    @classmethod
    def _fractal_inspect(cls, surface: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if type(surface.get("scene_id")) is str and re.fullmatch(r"scn_[0-9a-f]{8}", surface["scene_id"]):
            out["scene_id"] = surface["scene_id"]
        revision = cls._fractal_sha(surface.get("scene_revision"))
        if revision:
            out["scene_revision"] = revision
        for key in ("draft_generation", "source_generation", "viewer_generation", "renderer_generation"):
            if type(surface.get(key)) is int and surface[key] >= 0:
                out[key] = surface[key]
        for key in ("owner_id", "project_context_witness"):
            if type(surface.get(key)) is str and surface[key] and not cls._fractal_secret(surface[key]):
                out[key] = surface[key]
        targets = [item for item in surface.get("registry_targets", []) if type(item) is str and item][:64] if isinstance(surface.get("registry_targets"), list) else []
        if targets:
            out["registry_targets"] = targets
        orphans = cls._fractal_pairs(surface.get("retained_orphans"))
        if orphans:
            out["retained_orphans"] = orphans
        if isinstance(surface.get("capabilities"), list):
            tokens = [item for item in surface["capabilities"] if type(item) is str and re.fullmatch(r"[A-Za-z0-9_./+-]{1,64}", item)][:32]
            if tokens:
                out["capabilities"] = tokens
        suspensions = cls._fractal_pairs(surface.get("suspensions"))
        if suspensions:
            out["suspensions"] = suspensions
        if isinstance(surface.get("diagnostics"), list):
            rows = []
            for item in surface["diagnostics"][:32]:
                if not isinstance(item, dict):
                    continue
                row = {}
                if type(item.get("code")) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item["code"]):
                    row["code"] = item["code"]
                if type(item.get("target")) is str and item["target"]:
                    row["target"] = item["target"]
                if row:
                    rows.append(row)
            if rows:
                out["diagnostics"] = rows
        for key in ("base", "effective"):
            numbers = surface.get(key)
            if isinstance(numbers, dict):
                projected = {
                    name: value for name, value in numbers.items()
                    if type(name) is str and type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value)
                }
                if projected:
                    out[key] = projected
        return out

    @staticmethod
    def _fractal_secret(value: str) -> bool:
        return value.startswith("/") or "\\" in value or (len(value) >= 3 and value[1] == ":")

    @classmethod
    def _fractal_pairs(cls, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        rows = []
        for item in value[:32]:
            if not isinstance(item, dict):
                continue
            target, reason = item.get("target"), item.get("reason")
            if type(target) is str and target and type(reason) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
                rows.append({"target": target, "reason": reason})
        return rows

    def apply_fractal_edits(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self._post_fractal_action("fractal_edit", args)

    def control_fractal_timeline(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self._post_fractal_action("fractal_transport", args)

    def open_fractal_scene(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self._post_fractal_action("fractal_open", args)

    def save_fractal_scene(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self._post_fractal_action("fractal_save", args)

    def get_fractal_request(self, client_request_id: str) -> dict[str, Any]:
        request_id = self._fractal_request_id(client_request_id)
        try:
            data = self.get_json(
                f"/api/agent/fractal-actions/requests/{urllib.parse.quote(request_id, safe='')}",
                headers=self._FRACTAL_AGENT,
            )
        except HextileClientError as exc:
            if exc.status_code == 410:
                raise HextileClientError(
                    "Fractal request outcome unknown. Expiry is not refusal and is not proof a Save was not published.",
                    status_code=410, kind="fractal_outcome_unknown",
                    receipt={"client_request_id": request_id, "status": "unknown"},
                ) from exc
            raise
        return self._fractal_action_receipt(data, request_id)

    def _fractal_sequence_body(self, body: Mapping[str, Any]) -> dict[str, Any]:
        required = {
            "scene_id", "scene_revision", "idempotency_key", "name", "frame_rate",
            "start_time", "end_time", "frame_count", "seed", "quality", "width", "height",
            "output", "publication_mode",
        }
        if not isinstance(body, Mapping) or set(body) != required or body.get("publication_mode") != "deferred":
            raise HextileClientError("Expected a closed deferred Fractal sequence request", kind="other", status_code=422)
        if self._fractal_sha(body.get("scene_revision")) is None or re.fullmatch(r"scn_[0-9a-f]{8}", str(body.get("scene_id"))) is None:
            raise HextileClientError("Invalid saved Fractal scene identity", kind="other", status_code=422)
        output = body["output"]
        width, height = body["width"], body["height"]
        if type(width) is not int or type(height) is not int or not isinstance(output, dict):
            raise HextileClientError("Invalid Fractal sequence geometry", kind="other", status_code=422, receipt={"code": "geometry"})
        projection = output.get("projection")
        if projection == "equirectangular":
            if not (height >= 64 and width >= 128 and height % 2 == 0 and width == 2 * height):
                raise HextileClientError(
                    "Fractal equirectangular output requires exact 2:1 and at least 128x64",
                    kind="other", status_code=422, receipt={"code": "geometry"},
                )
        elif projection in {"fulldome", "fulldome_angular"}:
            if output.get("diameter") not in {1024, 2048, 4096, 8192} or width != height or width != output["diameter"]:
                raise HextileClientError(
                    "Fractal Dome output must be square at diameter 1024, 2048, 4096, or 8192",
                    kind="other", status_code=422, receipt={"code": "geometry"},
                )
        else:
            raise HextileClientError("Invalid Fractal sequence projection", kind="other", status_code=422, receipt={"code": "geometry"})
        return dict(body)

    def _fractal_sequence_error(self, exc: HextileClientError, *, uncertain: bool) -> HextileClientError:
        code = self._fractal_code(exc)
        if uncertain and self._fractal_uncertain(exc):
            return HextileClientError(
                "Fractal sequence POST outcome unknown. GET the acknowledged sequence id and compare request_hash. Do not mint another id.",
                status_code=exc.status_code, kind="fractal_sequence_outcome_unknown",
                receipt={"status": "unknown"},
            )
        return HextileClientError(
            "Fractal sequence refused", status_code=exc.status_code, kind=exc.kind,
            receipt={"code": code} if code else None,
        )

    def _fractal_sequence_post(self, body: Mapping[str, Any], *, preflight: bool) -> dict[str, Any]:
        checked = self._fractal_sequence_body(body)
        path = "/api/fractal/sequences/preflight" if preflight else "/api/fractal/sequences"
        try:
            data = self.post_json(path, checked)
        except HextileClientError as exc:
            raise self._fractal_sequence_error(exc, uncertain=not preflight) from exc
        except Exception as exc:
            safe = HextileClientError("Fractal sequence transport failed", kind="app_down")
            raise self._fractal_sequence_error(safe, uncertain=not preflight) from exc
        if preflight:
            keys = ("frame_count", "source_width", "source_height", "output_width", "output_height")
            if not isinstance(data, dict) or any(type(data.get(key)) is not int or data[key] < 0 for key in keys) or self._fractal_sha(data.get("render_identity")) is None:
                raise HextileClientError("Invalid Fractal sequence preflight receipt", kind="other")
            return {key: data[key] for key in (*keys, "render_identity")}
        if not isinstance(data, dict) or re.fullmatch(r"seq_[0-9a-f]{32}", str(data.get("sequence_id"))) is None:
            raise HextileClientError("Invalid Fractal sequence receipt", kind="other")
        receipt = {"sequence_id": data["sequence_id"]}
        if type(data.get("status")) is str:
            receipt["status"] = data["status"]
        for key in ("request_hash", "recipe_hash"):
            if self._fractal_sha(data.get(key)):
                receipt[key] = data[key]
        return receipt

    def preflight_fractal_sequence(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._fractal_sequence_post(body, preflight=True)

    def render_fractal_sequence(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._fractal_sequence_post(body, preflight=False)

    def get_fractal_sequence(self, sequence_id: str) -> dict[str, Any]:
        if type(sequence_id) is not str or re.fullmatch(r"seq_[0-9a-f]{32}", sequence_id) is None:
            raise HextileClientError("Expected a known Fractal sequence id", kind="other", status_code=422)
        try:
            data = self.get_json(f"/api/fractal/sequences/{sequence_id}")
        except HextileClientError as exc:
            if exc.status_code == 404:
                raise HextileClientError(
                    "Known Fractal sequence id is absent. An uncertain render POST remains unknown. Do not mint another id.",
                    status_code=404, kind="fractal_sequence_outcome_unknown",
                    receipt={"sequence_id": sequence_id, "status": "unknown"},
                ) from exc
            raise self._fractal_sequence_error(exc, uncertain=False) from exc
        if not isinstance(data, dict) or data.get("sequence_id") != sequence_id:
            raise HextileClientError("Invalid Fractal sequence status", kind="other")
        receipt: dict[str, Any] = {"sequence_id": sequence_id}
        if type(data.get("status")) is str:
            receipt["status"] = data["status"]
        for key in ("request_hash", "recipe_hash", "render_identity"):
            if self._fractal_sha(data.get(key)):
                receipt[key] = data[key]
        for key in ("source_width", "source_height", "output_width", "output_height", "frame_count", "width", "height"):
            if type(data.get(key)) is int and data[key] >= 0:
                receipt[key] = data[key]
        output = data.get("output")
        if isinstance(output, dict) and output.get("projection") in {"equirectangular", "fulldome", "fulldome_angular"}:
            projected = {"projection": output["projection"]}
            for key in ("diameter", "fov_deg", "pan", "tilt", "roll"):
                value = output.get(key)
                if type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value):
                    projected[key] = value
            receipt["output"] = projected
        return receipt

    def _fractal_export_args(self, args: Mapping[str, Any], *, submit: bool) -> tuple[str, dict[str, Any]]:
        required = {"sequence_id", "destination_folder", "format"}
        if submit:
            required.add("transaction_fingerprint")
        if not isinstance(args, Mapping) or not required <= set(args) <= required | {"quality_if_jpg"}:
            raise HextileClientError("Expected a closed Fractal folder export", kind="other", status_code=422)
        sequence_id = args["sequence_id"]
        if type(sequence_id) is not str or re.fullmatch(r"seq_[0-9a-f]{32}", sequence_id) is None:
            raise HextileClientError("Expected a known Fractal sequence id", kind="other", status_code=422)
        encoding = args["format"]
        quality = args.get("quality_if_jpg")
        if encoding not in {"PNG", "JPG"} or (encoding == "JPG") != (type(quality) is int and 1 <= quality <= 100):
            raise HextileClientError("Invalid Fractal folder encoding", kind="other", status_code=422)
        if encoding == "PNG" and "quality_if_jpg" in args:
            raise HextileClientError("PNG export omits quality_if_jpg", kind="other", status_code=422)
        body = {"destination_folder": args["destination_folder"], "format": encoding}
        if quality is not None:
            body["quality_if_jpg"] = quality
        if submit:
            if self._fractal_sha(args.get("transaction_fingerprint")) is None:
                raise HextileClientError("Export requires the preflight transaction_fingerprint", kind="other", status_code=422)
            body["transaction_fingerprint"] = args["transaction_fingerprint"]
        return sequence_id, body

    def _fractal_export_receipt(self, data: Any, sequence_id: str) -> dict[str, Any]:
        if not isinstance(data, dict) or data.get("sequence_id") != sequence_id or self._fractal_sha(data.get("transaction_fingerprint")) is None:
            raise HextileClientError("Invalid Fractal export receipt", kind="other")
        receipt = {
            "sequence_id": sequence_id,
            "transaction_fingerprint": data["transaction_fingerprint"],
        }
        if type(data.get("attempt_id")) is str and re.fullmatch(r"attempt_[0-9a-f]{32}", data["attempt_id"]):
            receipt["attempt_id"] = data["attempt_id"]
        if type(data.get("status")) is str:
            receipt["status"] = data["status"]
        if self._fractal_sha(data.get("recipe_hash")):
            receipt["recipe_hash"] = data["recipe_hash"]
        return receipt

    def preflight_fractal_sequence_export(self, args: Mapping[str, Any]) -> dict[str, Any]:
        sequence_id, body = self._fractal_export_args(args, submit=False)
        try:
            data = self.post_json(f"/api/fractal/sequences/{sequence_id}/export/preflight", body)
        except HextileClientError as exc:
            raise self._fractal_sequence_error(exc, uncertain=False) from exc
        return self._fractal_export_receipt(data, sequence_id)

    def export_fractal_sequence(self, args: Mapping[str, Any]) -> dict[str, Any]:
        sequence_id, body = self._fractal_export_args(args, submit=True)
        try:
            data = self.post_json(f"/api/fractal/sequences/{sequence_id}/export", body)
        except HextileClientError as exc:
            if self._fractal_uncertain(exc):
                raise HextileClientError(
                    "Fractal export POST outcome unknown. GET the known sequence and attempt. Do not post again.",
                    status_code=exc.status_code, kind="fractal_sequence_outcome_unknown",
                    receipt={"status": "unknown", "sequence_id": sequence_id},
                ) from exc
            raise self._fractal_sequence_error(exc, uncertain=False) from exc
        receipt = self._fractal_export_receipt(data, sequence_id)
        if receipt["transaction_fingerprint"] != body["transaction_fingerprint"]:
            raise HextileClientError("Invalid Fractal export acknowledgement", kind="other")
        return receipt

    def control_fractal_sequence(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(args, Mapping) or "sequence_id" not in args or "action" not in args:
            raise HextileClientError("Expected closed Fractal sequence control", kind="other", status_code=422)
        sequence_id, action = args["sequence_id"], args["action"]
        if type(sequence_id) is not str or re.fullmatch(r"seq_[0-9a-f]{32}", sequence_id) is None:
            raise HextileClientError("Expected a known Fractal sequence id", kind="other", status_code=422)
        publication = action in {"cancel_export", "retry_export", "open_folder"}
        if action in {"stop", "resume"}:
            if set(args) != {"sequence_id", "action"}:
                raise HextileClientError("Native Fractal control takes no attempt", kind="other", status_code=422)
            suffix, body = action, None
        elif publication:
            if set(args) != {"sequence_id", "action", "attempt_id", "attempt_revision"}:
                raise HextileClientError("Publication control requires the observed attempt id and revision", kind="other", status_code=422)
            if type(args["attempt_id"]) is not str or re.fullmatch(r"attempt_[0-9a-f]{32}", args["attempt_id"]) is None or type(args["attempt_revision"]) is not int:
                raise HextileClientError("Invalid Fractal export attempt", kind="other", status_code=422)
            suffix = {"cancel_export": "export/cancel", "retry_export": "export/retry", "open_folder": "export/open-folder"}[action]
            body = {"attempt_id": args["attempt_id"], "attempt_revision": args["attempt_revision"]}
        else:
            raise HextileClientError("Invalid Fractal sequence control", kind="other", status_code=422)
        try:
            data = self.post_json(f"/api/fractal/sequences/{sequence_id}/{suffix}", body)
        except HextileClientError as exc:
            if self._fractal_uncertain(exc):
                raise HextileClientError(
                    "Fractal control POST outcome unknown. GET the known sequence before another action.",
                    status_code=exc.status_code, kind="fractal_sequence_outcome_unknown",
                    receipt={"status": "unknown", "sequence_id": sequence_id},
                ) from exc
            raise self._fractal_sequence_error(exc, uncertain=False) from exc
        receipt = {"sequence_id": sequence_id, "action": action}
        if isinstance(data, dict) and type(data.get("status")) is str:
            receipt["status"] = data["status"]
        if publication and type(args.get("attempt_id")) is str:
            receipt["attempt_id"] = args["attempt_id"]
        return receipt

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
