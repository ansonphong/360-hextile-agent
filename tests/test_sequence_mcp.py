"""Fake-opener HTTP tests for sequence Client methods and MCP tools/list.

No live APP, no GPU, no network.
"""

from __future__ import annotations

import io
import asyncio
import http.client
import importlib.util
import json
import jsonschema
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = ROOT / "mcp"
sys.path.insert(0, str(MCP_DIR))

from hextile_client import (  # noqa: E402
    LONG_OPERATION_TIMEOUT_S,
    Client,
    HextileClientError,
)
from hextile_mcp import HextileMcpServer  # noqa: E402

SEQUENCE_TOOLS = (
    "extract_sequence_video",
    "create_sequence",
    "start_sequence",
    "get_sequence",
    "list_sequences",
    "stop_sequence",
)

# Full HextileConfig-shaped object (not a pipeline-only stub). Nested keys
# prove create_sequence emits the complete object, not a subset.
FULL_CONFIG: dict[str, Any] = {
    "pipeline": "realesrgan",
    "hextile": {
        "template": "Hextile_20_2K",
        "rendertiles": "all",
        "tile_input_width": 512,
        "tile_output_width": 512,
    },
    "diffusion": {
        "model": "realesrgan-x4plus",
        "seed": -1,
        "strength": 0.45,
        "guidance_scale": 7.5,
        "num_inference_steps": 20,
    },
    "input": {
        "format": "equirectangular",
        "mode": "sequence",
        "source": "file",
        "path": "/frames/stills",
        "width": 2048,
        "height": 1024,
    },
    "upscaling": {
        "model": "realesrgan-x4plus",
        "scale_factor": 4,
        "tile_size": 0,
        "template": "",
    },
    "output": {
        "format": "equirectangular",
        "width": 8192,
        "height": 4096,
        "file_format": "PNG",
    },
    "prompt": {
        "global": "identity-check",
        "negative": "blurry",
        "directional": {"mode": "none", "points": []},
    },
    "post_processing": [],
    "sequence": {"enabled": True},
}

BASE = "http://hextile.test"


class _Resp:
    status = 200

    def __init__(self, payload: Any = None) -> None:
        self._payload = {} if payload is None else payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def getcode(self) -> int:
        return 200

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _recorder(seen: dict[str, Any], payload: Any = None):
    def opener(req, timeout=None):  # noqa: ANN001
        seen["url"] = req.full_url
        seen["data"] = req.data
        seen["timeout"] = timeout
        seen["method"] = req.get_method()
        return _Resp(payload)

    return opener


def _client(seen: dict[str, Any], payload: Any = None) -> Client:
    return Client(base_url=BASE, opener=_recorder(seen, payload))


def _http_error_opener(code: int, msg: str):
    def opener(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=code,
            msg=msg,
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(json.dumps({"detail": msg}).encode()),
        )

    return opener


def test_extract_sequence_video_posts_path_and_generate_timeout() -> None:
    seen: dict[str, Any] = {}
    _client(seen).extract_sequence_video("/clips/scene.mp4")
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/sequences/extract-video"
    assert json.loads(seen["data"]) == {"video_path": "/clips/scene.mp4"}
    assert seen["timeout"] == LONG_OPERATION_TIMEOUT_S
    assert LONG_OPERATION_TIMEOUT_S == 300.0


def test_create_sequence_posts_full_config_lossless() -> None:
    seen: dict[str, Any] = {}
    _client(seen).create_sequence("/frames/stills", FULL_CONFIG, name="upres-folder")
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/sequences/create"
    body = json.loads(seen["data"])
    assert body["folder_path"] == "/frames/stills"
    assert body["name"] == "upres-folder"
    assert body["config"] == FULL_CONFIG
    assert set(body["config"]) == set(FULL_CONFIG)
    assert body["config"]["hextile"]["template"] == "Hextile_20_2K"
    assert body["config"]["diffusion"]["model"] == "realesrgan-x4plus"
    assert body["config"]["input"] == FULL_CONFIG["input"]
    assert body["config"]["upscaling"] == FULL_CONFIG["upscaling"]


def test_start_sequence_quotes_id() -> None:
    seen: dict[str, Any] = {}
    sequence_id = "seq_a/b"
    _client(seen).start_sequence(sequence_id)
    quoted = urllib.parse.quote(sequence_id, safe="")
    assert quoted == "seq_a%2Fb"
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/sequences/{quoted}/start"
    assert "/seq_a/b/" not in seen["url"]


def test_stop_sequence_quotes_id() -> None:
    seen: dict[str, Any] = {}
    sequence_id = "seq id"
    _client(seen).stop_sequence(sequence_id)
    quoted = urllib.parse.quote(sequence_id, safe="")
    assert quoted == "seq%20id"
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/sequences/{quoted}/stop"


def test_get_sequence_gets_quoted_id() -> None:
    seen: dict[str, Any] = {}
    sequence_id = "seq_a/b"
    _client(seen).get_sequence(sequence_id)
    quoted = urllib.parse.quote(sequence_id, safe="")
    assert seen["method"] == "GET"
    assert seen["url"] == f"{BASE}/api/sequences/{quoted}"
    assert seen["data"] is None


def test_list_sequences_default_lifecycle_active() -> None:
    seen: dict[str, Any] = {}
    _client(seen).list_sequences()
    assert seen["method"] == "GET"
    assert seen["url"] == f"{BASE}/api/sequences/?lifecycle_status=active"


def test_list_sequences_passes_lifecycle_status() -> None:
    seen: dict[str, Any] = {}
    _client(seen).list_sequences("archived")
    assert seen["method"] == "GET"
    assert seen["url"] == f"{BASE}/api/sequences/?lifecycle_status=archived"


def test_extract_sequence_video_402_is_hextile_client_error() -> None:
    client = Client(
        base_url=BASE,
        opener=_http_error_opener(402, "Payment Required"),
    )
    with pytest.raises(HextileClientError) as ei:
        client.extract_sequence_video("/v.mp4")
    assert ei.value.status_code == 402


def test_create_sequence_400_is_hextile_client_error() -> None:
    client = Client(
        base_url=BASE,
        opener=_http_error_opener(400, "Bad Request"),
    )
    with pytest.raises(HextileClientError) as ei:
        client.create_sequence("/frames/stills", FULL_CONFIG)
    assert ei.value.status_code == 400


def test_mcp_tools_list_includes_sequence_names() -> None:
    server = HextileMcpServer(client=Client(opener=lambda *a, **k: None))
    resp = server.handle_rpc(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert resp is not None
    names = [t["name"] for t in resp["result"]["tools"]]
    for name in SEQUENCE_TOOLS:
        assert name in names


def test_shader_render_export_mcp_tool_chain() -> None:
    server = HextileMcpServer(client=Client(opener=lambda *a, **k: None))
    listed = server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
    assert {"preflight_shader_sequence", "render_shader_sequence", "get_shader_sequence"} <= tools.keys()
    assert {"preflight_shader_sequence_export", "export_shader_sequence", "control_shader_sequence"} <= tools.keys()
    body = {"shader_ref": {"origin": "project", "id": "aurora"},
            "project_context_witness": "project-witness", "working_revision": 2,
            "document_hash": "sha256:" + "a" * 64, "source_hash": "sha256:" + "b" * 64,
            "idempotency_key": "native-001", "name": "Aurora", "frame_rate": {"num": 24, "den": 1},
            "frame_count": 48, "start_time": {"num": "0", "den": "1"},
            "end_time": {"num": "2", "den": "1"}, "width": 2048, "height": 1024,
            "seed": 7, "output": {"projection": "equirectangular", "pan": 0, "tilt": 0, "roll": 0},
            "publication_mode": "deferred"}
    for name in ("preflight_shader_sequence", "render_shader_sequence"):
        schema = tools[name]["inputSchema"]
        jsonschema.validate(body, schema)
        assert schema["additionalProperties"] is False
        assert tools[name]["annotations"]["readOnlyHint"] is (name == "preflight_shader_sequence")
        for bad in ({**body, "folder_export": {"destination_folder": "C:\\private"}},
                    {**body, "publication_mode": "automatic"},
                    {**body, "seed": float("inf")}):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)
    assert tools["get_shader_sequence"]["annotations"]["readOnlyHint"] is True
    assert tools["preflight_shader_sequence_export"]["annotations"]["readOnlyHint"] is True
    assert tools["export_shader_sequence"]["annotations"]["readOnlyHint"] is False
    assert tools["control_shader_sequence"]["annotations"]["readOnlyHint"] is False
    with pytest.raises(HextileClientError):
        Client().preflight_shader_sequence({**body, "frame_count": 49})
    dome = {**body, "width": 2048, "height": 2048,
            "output": {"projection": "fulldome_angular", "diameter": 2048,
                       "fov_deg": 360, "pan": 0, "tilt": 0, "roll": 0}}
    jsonschema.validate(dome, tools["render_shader_sequence"]["inputSchema"])
    for bad in ({**dome, "output": {**dome["output"], "fov_deg": 361}},
                {**body, "start_time": {"num": "1", "den": "2"}}):
        with pytest.raises(HextileClientError):
            Client().render_shader_sequence(bad)
    guide = (ROOT / "skills" / "360-hextile" / "references" / "recipes.md").read_text(encoding="utf-8")
    import re
    example = json.loads(re.search(r"## Saved Shader native sequence.*?```json\n(.*?)\n```", guide, re.S).group(1))
    jsonschema.validate(example, tools["render_shader_sequence"]["inputSchema"])
    sys.path.insert(0, str(ROOT.parent / "360-HEXTILE-APP"))
    from backend.api.routes.shaders_pkg.sequences import SequenceRequest
    assert SequenceRequest.model_validate(example).publication_mode == "deferred"

    seen: list[Any] = []
    sid = "seq_" + "a" * 32
    digest = "sha256:" + "c" * 64
    attempt = "attempt_" + "c" * 32
    folder = {"sequence_id": sid, "destination_folder": "C:\\Chosen\\Frames", "format": "PNG"}
    jsonschema.validate(folder, tools["preflight_shader_sequence_export"]["inputSchema"])
    jsonschema.validate({**folder, "transaction_fingerprint": digest},
                        tools["export_shader_sequence"]["inputSchema"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**folder, "format": "JPG"},
                            tools["preflight_shader_sequence_export"]["inputSchema"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**folder, "quality_if_jpg": 90},
                            tools["preflight_shader_sequence_export"]["inputSchema"])
    for invalid in ({**folder, "format": "JPG"}, {**folder, "quality_if_jpg": 80},
                    {**folder, "format": "GIF"}):
        with pytest.raises(HextileClientError):
            Client().preflight_shader_sequence_export(invalid)
    for action in ("stop", "resume", "cancel_export", "retry_export"):
        args = {"sequence_id": sid, "action": action}
        if action.endswith("export"):
            args["attempt_id"] = attempt
        jsonschema.validate(args, tools["control_shader_sequence"]["inputSchema"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"sequence_id": sid, "action": "retry_export"},
                            tools["control_shader_sequence"]["inputSchema"])
    with pytest.raises(HextileClientError):
        Client().control_shader_sequence({"sequence_id": sid, "action": "retry_export"})
    class Response:
        status = 200
        def __init__(self, payload: Any) -> None:
            self.payload = payload
        def read(self) -> bytes:
            return json.dumps(self.payload).encode()
        def __enter__(self):
            return self
        def __exit__(self, *_exc: object) -> None:
            return None
    def opener(req: Any, timeout=None):
        seen.append(req)
        if req.full_url.endswith("/export/preflight"):
            return Response({"sequence_id": sid, "attempt_id": attempt,
                             "transaction_fingerprint": digest, "status": "ready",
                             "destination_folder": "C:\\private\\frames"})
        if req.full_url.endswith("/preflight"):
            return Response({"sequence_id": sid, "status": "ready", "request_hash": digest,
                             "private_path": "C:\\secret\\frames"})
        if req.full_url.endswith("/export"):
            return Response({"sequence_id": sid, "attempt_id": attempt,
                             "transaction_fingerprint": digest, "status": "queued",
                             "destination_folder": "C:\\private\\frames"})
        if req.full_url.endswith(("/stop", "/resume", "/export/cancel", "/export/retry")):
            return Response({"sequence_id": sid, "status": "queued", "path": "C:\\private",
                             "folder_export": {"attempt_id": attempt, "status": "queued"}})
        if req.get_method() == "POST":
            return Response({"sequence_id": sid, "status": "queued", "request_hash": digest,
                             "recipe_hash": digest})
        return Response({"sequence_id": sid, "status": "completed", "request_hash": digest,
                         "frames_completed": 48, "frame_count": 48,
                         "shader_source": {"source": "secret formula", "path": "C:\\secret"},
                         "folder_export": {"destination_folder": "C:\\secret", "status": "completed",
                                           "frames_completed": 48},
                         "error": "C:\\secret", "publication_attempts": [{"attempt_id": "attempt_" + "b" * 32,
                             "status": "completed", "destination_folder": "C:\\secret"}]})
    transport = Client(base_url=BASE, opener=opener)
    assert transport.preflight_shader_sequence(body) == {"sequence_id": sid, "status": "ready", "request_hash": digest}
    assert transport.render_shader_sequence(body)["request_hash"] == digest
    status_result = transport.get_shader_sequence(sid)
    assert status_result["frames_completed"] == 48
    assert status_result["folder_export"] == {"status": "completed", "frames_completed": 48}
    assert "C:\\secret" not in json.dumps(status_result) and "formula" not in json.dumps(status_result)
    assert [req.get_method() for req in seen] == ["POST", "POST", "GET"]
    assert [req.full_url for req in seen] == [f"{BASE}/api/shaders/sequences/preflight",
                                              f"{BASE}/api/shaders/sequences",
                                              f"{BASE}/api/shaders/sequences/{sid}"]
    assert all(req.get_header("X-hextile-project-witness") == "project-witness" for req in seen[:2])
    assert all(json.loads(req.data) == body for req in seen[:2])
    assert transport.preflight_shader_sequence_export(folder) == {
        "sequence_id": sid, "attempt_id": attempt, "transaction_fingerprint": digest, "status": "ready"}
    assert transport.export_shader_sequence({**folder, "transaction_fingerprint": digest})["status"] == "queued"
    assert transport.control_shader_sequence({"sequence_id": sid, "action": "cancel_export",
                                              "attempt_id": attempt})["attempt_id"] == attempt
    assert [req.full_url.rsplit("/api", 1)[-1] for req in seen[-3:]] == [
        f"/shaders/sequences/{sid}/export/preflight", f"/shaders/sequences/{sid}/export",
        f"/shaders/sequences/{sid}/export/cancel"]
    assert json.loads(seen[-1].data) == {"attempt_id": attempt}
    for action, suffix in (("stop", "/stop"), ("resume", "/resume"),
                           ("retry_export", "/export/retry")):
        arguments = {"sequence_id": sid, "action": action}
        if action == "retry_export":
            arguments["attempt_id"] = attempt
        assert transport.control_shader_sequence(arguments)["action"] == action
        assert seen[-1].full_url.endswith(suffix)
    lost = Client(opener=lambda *_a, **_kw: (_ for _ in ()).throw(urllib.error.URLError(TimeoutError())))
    with pytest.raises(HextileClientError) as uncertain:
        lost.render_shader_sequence(body)
    assert uncertain.value.kind == "shader_sequence_outcome_unknown"
    with pytest.raises(HextileClientError) as uncertain_export:
        lost.export_shader_sequence({**folder, "transaction_fingerprint": digest})
    assert uncertain_export.value.kind == "shader_sequence_outcome_unknown"
    assert "Chosen" not in str(uncertain_export.value)
    for bad in ({"sequence_id": sid, "status": 17},
                {"sequence_id": sid},
                {"sequence_id": "seq_" + "f" * 32, "status": "queued"}):
        malformed = Client(opener=lambda *_a, _bad=bad, **_kw: Response(_bad))
        for action_args in ({"sequence_id": sid, "action": "stop"},
                            {"sequence_id": sid, "action": "retry_export", "attempt_id": attempt}):
            with pytest.raises(HextileClientError) as control_unknown:
                malformed.control_shader_sequence(action_args)
            assert control_unknown.value.kind == "shader_sequence_outcome_unknown"
            assert "GET" in str(control_unknown.value)
    for bad in ({"sequence_id": sid, "attempt_id": attempt,
                 "transaction_fingerprint": digest, "status": 17},
                {"sequence_id": sid, "attempt_id": attempt,
                 "transaction_fingerprint": digest},
                {"sequence_id": "seq_" + "f" * 32, "attempt_id": attempt,
                 "transaction_fingerprint": digest, "status": "queued"}):
        malformed = Client(opener=lambda *_a, _bad=bad, **_kw: Response(_bad))
        with pytest.raises(HextileClientError) as export_unknown:
            malformed.export_shader_sequence({**folder, "transaction_fingerprint": digest})
        assert export_unknown.value.kind == "shader_sequence_outcome_unknown"
        assert "GET" in str(export_unknown.value)
    missing = Client(opener=_http_error_opener(404, "C:\\private\\formula"))
    with pytest.raises(HextileClientError) as unknown:
        missing.get_shader_sequence(sid)
    assert unknown.value.kind == "shader_sequence_outcome_unknown"
    assert "private" not in str(unknown.value)
    def stale_opener(req: Any, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {},
                                     io.BytesIO(b'{"detail":{"code":"stale_document_hash","source":"C:\\\\private"}}'))
    with pytest.raises(HextileClientError) as stale:
        Client(opener=stale_opener).preflight_shader_sequence(body)
    assert stale.value.receipt == {"code": "stale_document_hash"}
    assert "private" not in str(stale.value) and "private" not in json.dumps(stale.value.receipt)
    with pytest.raises(HextileClientError) as refused:
        Client(opener=stale_opener).render_shader_sequence(body)
    assert refused.value.kind == "http" and refused.value.status_code == 422
    assert refused.value.receipt == {"code": "stale_document_hash"}

    # A lost or malformed response must not expose transport text or replay a render.
    private = "C:\\private\\shader-formula"
    for name, args in (("preflight_shader_sequence", body),
                       ("render_shader_sequence", body),
                       ("get_shader_sequence", {"sequence_id": sid})):
        for failure in (http.client.BadStatusLine(private), RuntimeError(private)):
            calls: list[Any] = []
            def broken(req: Any, timeout=None):
                if "/api/shaders/sequences" in req.full_url:
                    calls.append(req)
                raise failure
            result = HextileMcpServer(client=Client(opener=broken)).call_tool(name, args)
            payload = json.loads(result["content"][0]["text"])
            assert result["isError"] is True
            assert "shader-formula" not in json.dumps(result) and "trace" not in payload
            assert len(calls) == 1
            assert payload["kind"] == ("shader_sequence_outcome_unknown" if name == "render_shader_sequence" else "other")

    class Interrupted(Response):
        def read(self) -> bytes:
            raise http.client.IncompleteRead(private.encode())
    for name, args in (("preflight_shader_sequence", body),
                       ("render_shader_sequence", body),
                       ("get_shader_sequence", {"sequence_id": sid})):
        result = HextileMcpServer(client=Client(opener=lambda *_a, **_k: Interrupted({}))).call_tool(name, args)
        payload = json.loads(result["content"][0]["text"])
        assert "shader-formula" not in json.dumps(result) and "trace" not in payload
        assert payload["kind"] == ("shader_sequence_outcome_unknown" if name == "render_shader_sequence" else "other")

    class UnexpectedClient:
        def preflight_shader_sequence(self, _args: Any) -> Any:
            raise RuntimeError(private)
        render_shader_sequence = preflight_shader_sequence
        get_shader_sequence = preflight_shader_sequence
    for name, args in (("preflight_shader_sequence", body),
                       ("render_shader_sequence", body),
                       ("get_shader_sequence", {"sequence_id": sid})):
        result = HextileMcpServer(client=UnexpectedClient()).call_tool(name, args)
        payload = json.loads(result["content"][0]["text"])
        assert "shader-formula" not in json.dumps(result) and "trace" not in payload
        assert payload["kind"] == ("shader_sequence_outcome_unknown" if name == "render_shader_sequence" else "other")

    for status in (500, 503):
        calls: list[Any] = []
        def admitted_then_error(req: Any, timeout=None):
            if "/api/shaders/sequences" in req.full_url:
                calls.append(req)
            if req.get_method() == "POST":
                raise urllib.error.HTTPError(req.full_url, status, private, {}, io.BytesIO(private.encode()))
            return Response({"sequence_id": sid, "status": "queued", "request_hash": digest})
        recovery_server = HextileMcpServer(client=Client(opener=admitted_then_error))
        result = recovery_server.call_tool("render_shader_sequence", body)
        payload = json.loads(result["content"][0]["text"])
        assert result["isError"] is True and payload["kind"] == "shader_sequence_outcome_unknown"
        assert payload["receipt"] == {"status": "unknown"}
        assert "GET" in payload["error"] and "request_hash" in payload["error"]
        assert "shader-formula" not in json.dumps(result) and "trace" not in payload
        assert [req.get_method() for req in calls] == ["POST"]
        recovered = recovery_server.call_tool("get_shader_sequence", {"sequence_id": sid})
        assert json.loads(recovered["content"][0]["text"])["request_hash"] == digest
        assert [req.get_method() for req in calls] == ["POST", "GET"]
        preflight = recovery_server.call_tool("preflight_shader_sequence", body)
        preflight_error = json.loads(preflight["content"][0]["text"])
        assert preflight_error["kind"] == "http" and preflight_error["status_code"] == status

    if importlib.util.find_spec("mcp") is not None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        script = ("import sys; sys.path.insert(0, " + repr(str(MCP_DIR)) + "); "
                  "import hextile_mcp; original=hextile_mcp.HextileMcpServer; "
                  "hextile_mcp.HextileMcpServer=lambda: original(client=FakeClient(), child_token='test'); "
                  "raise SystemExit(hextile_mcp.main())")
        script = ("class FakeClient:\n"
                  "    def preflight_shader_sequence(self, args): return {'sequence_id':'" + sid + "','status':'ready','request_hash':'" + digest + "'}\n"
                  "    def render_shader_sequence(self, args): return {'sequence_id':'" + sid + "','status':'queued','request_hash':'" + digest + "'}\n"
                  "    def get_shader_sequence(self, sid): return {'sequence_id':sid,'status':'completed','frames_completed':48}\n"
                  "    def control_shader_sequence(self, args): return {'sequence_id':args['sequence_id'],'action':args['action'],'status':'queued'}\n"
                  "    def preflight_shader_sequence_export(self, args): return {'sequence_id':args['sequence_id'],'attempt_id':'" + attempt + "','transaction_fingerprint':'" + digest + "','status':'ready'}\n"
                  "    def export_shader_sequence(self, args): return {'sequence_id':args['sequence_id'],'attempt_id':'" + attempt + "','transaction_fingerprint':'" + digest + "','status':'queued'}\n"
                  + script)
        async def exercise_sdk() -> None:
            params = StdioServerParameters(command=sys.executable, args=["-c", script])
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    catalog = await session.list_tools()
                    assert {"preflight_shader_sequence", "render_shader_sequence", "get_shader_sequence",
                            "control_shader_sequence", "preflight_shader_sequence_export",
                            "export_shader_sequence"} <= {tool.name for tool in catalog.tools}
                    for name, args in (("preflight_shader_sequence", body), ("render_shader_sequence", body),
                                       ("get_shader_sequence", {"sequence_id": sid}),
                                       ("control_shader_sequence", {"sequence_id": sid, "action": "stop"}),
                                       ("preflight_shader_sequence_export", folder),
                                       ("export_shader_sequence", {**folder, "transaction_fingerprint": digest})):
                        result = await session.call_tool(name, args)
                        assert result.isError is False
                        assert json.loads(result.content[0].text)["sequence_id"] == sid
        asyncio.run(exercise_sdk())
