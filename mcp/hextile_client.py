"""Thin HTTP client for the local 360 Hextile backend.

Stdlib only (urllib). Default base URL: http://127.0.0.1:8000.
No app logic — pure proxy helpers for the MCP server and Codex install.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_S = 30.0
# Generate endpoints can take longer (GPU).
GENERATE_TIMEOUT_S = 300.0

APP_DOWN_MSG = (
    "360 Hextile isn't running. Launch 360 Hextile, then retry."
)
UPGRADE_MSG = (
    "Upgrade 360 Hextile (needs workflows/run). "
    "This plugin requires a build with POST /api/workflows/run."
)

_ERROR_BODY_SNIPPET = 800


class HextileClientError(RuntimeError):
    """HTTP or transport failure talking to the local backend."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: Optional[str] = None,
        kind: str = "http",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.kind = kind  # app_down | upgrade | http | other


class Client:
    """Sync HTTP wrapper around local APP routes (stdlib urllib)."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        opener: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Injectable for tests (callable urlopen or opener.open).
        self._opener = opener

    # ── low-level ───────────────────────────────────────────────────────

    def request_json(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        params: Optional[Mapping[str, Any]] = None,
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
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
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
                snippet = (exc.read() or b"")[:_ERROR_BODY_SNIPPET].decode(
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

    def get_json(
        self,
        path: str,
        *,
        timeout: Optional[float] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        return self.request_json("GET", path, timeout=timeout, params=params)

    def post_json(
        self,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
        params: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        return self.request_json(
            "POST", path, body, timeout=timeout, params=params
        )

    def delete_json(
        self,
        path: str,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        return self.request_json("DELETE", path, timeout=timeout)

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
        return self.post_json("/api/workflows/run", body)

    def dry_run_workflow(self, **kwargs: Any) -> Any:
        """POST /api/workflows/run with dry_run=true."""
        kwargs["dry_run"] = True
        return self.run_workflow(**kwargs)

    # ── renders ─────────────────────────────────────────────────────────

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

    def retry_run(self, run_id: str) -> Any:
        """POST /api/renders/{run_id}/retry — APP tile-reuse on crashed/failed."""
        rid = urllib.parse.quote(run_id, safe="")
        return self.post_json(f"/api/renders/{rid}/retry")

    def list_runs(self, lifecycle_status: str = "active") -> Any:
        """GET /api/renders/?lifecycle_status= (default active)."""
        status = lifecycle_status or "active"
        return self.get_json(
            "/api/renders/",
            params={"lifecycle_status": status},
        )

    # ── seed ────────────────────────────────────────────────────────────

    def generate_seed(
        self,
        prompt: str,
        *,
        lora_path: str,
        base_model: str,
        n: int = 4,
        **extra: Any,
    ) -> Any:
        """POST /api/360-lora/generate (not /api/lora-360)."""
        body: dict[str, Any] = {
            "prompt": prompt,
            "lora_path": lora_path,
            "base_model": base_model,
            "num_variations": n,
        }
        body.update(extra)
        return self.post_json(
            "/api/360-lora/generate", body, timeout=GENERATE_TIMEOUT_S
        )

    def list_360_loras(self) -> Any:
        """GET /api/360-lora/loras — catalog for generate_seed path + base_model."""
        return self.get_json("/api/360-lora/loras")

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
        return {
            "ok": False,
            "error": str(exc),
            "kind": exc.kind,
            "status_code": exc.status_code,
        }
    return {"ok": False, "error": str(exc), "kind": "other"}
