"""get_shader_context and propose_shader are thin proxies, not apply or import."""

from __future__ import annotations

import json
import ast
import http.client
import io
import re
import urllib.error
from copy import deepcopy

import jsonschema
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))

from hextile_client import Client, HextileClientError  # noqa: E402
from hextile_mcp import TOOLS, HextileMcpServer  # noqa: E402


def _schema(name: str) -> dict[str, Any]:
    return next(tool for tool in TOOLS if tool["name"] == name)



def _proposal() -> dict[str, Any]:
    return {
        "kind": "shader_document_candidate", "explanation": "Enable mouse input",
        "base": {"shader": {"origin": "user", "shaderId": "aurora"},
                 "project_context_witness": "wit", "working_revision": 1,
                 "document_hash": "sha256:" + "ab" * 32,
                 "source_hash": "sha256:" + "cd" * 32, "manifest_hash": None},
        "modulation_edits": {"schema": 1, "edits": [
            {"op": "set_mouse_input", "value": {"schema": 1, "enabled": True, "abi": "hx-mouse/1"}}]},
    }


def _operations() -> list[dict[str, Any]]:
    source = {"id": "mod_" + "a" * 24}
    binding = {"id": "binding_" + "b" * 24}
    definition = {"label": "Pulse", "alias": "MOD1", "kind": "time", "enabled": True,
                  "source": "sin(2*pi*cycle)", "period_unit": "beats",
                  "settings": {"period": 4, "speed": 1, "phase": 0,
                               "output_min": -1, "output_max": 1, "duty": .5}}
    added_binding = {"source_id": {"ref": "pulse"}, "target": "gain", "enabled": True,
                     "signature": {"kind": "float", "unit": "scalar", "min": 0, "max": 1},
                     "mode": "range", "range_min": 0, "range_max": 1}
    return [
        {"op": "add_source", "definition": definition, "ref": "pulse"},
        {"op": "remove_source", "source": source},
        {"op": "set_label", "source": source, "label": "Wave"},
        {"op": "install_wave", "source": source, "wave": "Square (50%)"},
        {"op": "set_expression", "source": source, "source_text": "cycle"},
        {"op": "set_setting", "source": source, "setting": "period", "value": 2},
        {"op": "set_period_unit", "source": source, "period_unit": "seconds"},
        {"op": "set_click", "source": source, "button": "right", "mode": "toggle"},
        {"op": "rename_alias", "source": source, "alias": "customAlias"},
        {"op": "set_source_enabled", "source": source, "enabled": False},
        {"op": "add_binding", "binding": added_binding, "ref": "gain"},
        {"op": "update_binding", "binding": binding,
         "value": {**added_binding, "id": binding["id"], "source_id": source["id"]}},
        {"op": "remove_binding", "binding": binding},
        {"op": "set_binding_enabled", "binding": binding, "enabled": False},
        _proposal()["modulation_edits"]["edits"][0],
        {"op": "set_master_clock", "value": {"schema": 1, "bpm": 120, "speed": 1,
         "anchor": {"time": {"num": "0", "den": "1"}, "seconds": 0, "beats": 0}}},
    ]


def test_closed_modulation_catalog_and_existing_alternatives() -> None:
    schema = _schema("propose_shader")["inputSchema"]
    for operation in _operations():
        proposal = _proposal()
        proposal["modulation_edits"]["edits"] = [operation]
        jsonschema.validate(proposal, schema)
    for field, value in (("source_edit", {"from_utf16": 0, "to_utf16": 0,
                                         "expected_text": "", "replacement": "x"}),
                         ("complete_candidate", {"source": "x", "values": {}, "capture_defaults": {},
                          "output": {}, "space": "direction_360", "extensions": {}})):
        proposal = _proposal()
        del proposal["modulation_edits"]
        proposal[field] = value
        jsonschema.validate(proposal, schema)
    variants = schema["properties"]["modulation_edits"]["properties"]["edits"]["items"]["oneOf"]
    assert {variant["properties"]["op"]["const"] for variant in variants} == {op["op"] for op in _operations()}


@pytest.mark.parametrize("change", ["xor", "none", "unknown", "partial", "foreign", "null",
                                     "count", "reference", "definition_id", "boolean"])
def test_modulation_schema_refuses_malformed_batches(change: str) -> None:
    proposal = _proposal()
    edit = proposal["modulation_edits"]["edits"][0]
    if change == "xor": proposal["source_edit"] = {}
    elif change == "none": del proposal["modulation_edits"]
    elif change == "unknown": edit["op"] = "arm"
    elif change == "partial": del edit["value"]["abi"]
    elif change == "foreign": edit["value"]["armed"] = True
    elif change == "null": proposal["modulation_edits"] = None
    elif change == "count": proposal["modulation_edits"]["edits"] *= 33
    elif change == "reference": proposal["modulation_edits"]["edits"] = [{"op": "remove_source", "source": "MOD1"}]
    elif change == "definition_id":
        proposal["modulation_edits"]["edits"] = [_operations()[0]]
        proposal["modulation_edits"]["edits"][0]["definition"]["id"] = "mod_" + "a" * 24
    elif change == "boolean": edit["value"]["enabled"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(proposal, _schema("propose_shader")["inputSchema"])


def test_client_transports_exact_typed_batch_and_explicit_formula_consent() -> None:
    client = Client()
    metadata = {"modulation": {"definitions": [{"id": "mod_" + "a" * 24, "alias": "MOD1"}],
                               "bindings": [], "mouse_input": {"schema": 1, "enabled": True, "abi": "hx-mouse/1"}}}
    with mock.patch.object(client, "get_json", return_value=metadata) as get:
        assert client.get_shader_context(origin="user", shader_id="aurora", project_context_witness="wit") == metadata
        assert get.call_args.kwargs["params"]["include_source"] == "false"
        assert get.call_args.kwargs["params"]["include_formula"] == "false"
        assert "source" not in metadata
        client.get_shader_context(origin="user", shader_id="aurora", project_context_witness="wit",
                                  include_source=True, include_formula=True)
        assert get.call_args.kwargs["params"]["include_source"] == "true"
        assert get.call_args.kwargs["params"]["include_formula"] == "true"
    proposal = _proposal()
    original = deepcopy(proposal)
    with mock.patch.object(client, "post_json", return_value={"ticket_id": "spt_abc"}) as post:
        client.propose_shader(proposal)
        assert post.call_args.args == ("/api/agent/shader-proposals", original)
        assert proposal == original
        for malformed in ({**proposal, "source_edit": {}}, {**proposal, "foreign": True},
                          {**proposal, "modulation_edits": {"schema": 1, "edits": [proposal["modulation_edits"]["edits"][0]] * 33}},
                          {**proposal, "modulation_edits": {"schema": 1, "edits": [{"op": "arm"}]}},
                          {**proposal, "modulation_edits": {"schema": 1, "edits": [{"op": "set_label", "source": {"id": "mod_" + "a" * 24}, "label": "x" * 65537}]}}):
            with pytest.raises(HextileClientError): client.propose_shader(malformed)
        assert post.call_count == 1

def test_shader_tool_schemas_cannot_import_delete_or_consume() -> None:
    for name in ("get_shader_context", "propose_shader"):
        schema = _schema(name)["inputSchema"]
        assert schema["additionalProperties"] is False
        assert not {"import", "delete", "accept", "consume", "config_partial", "path"} & set(schema["properties"])
    assert _schema("get_shader_context")["annotations"]["readOnlyHint"] is True
    assert _schema("propose_shader")["annotations"]["readOnlyHint"] is False
    assert _schema("propose_shader")["annotations"]["destructiveHint"] is False
    assert _schema("get_shader_context")["inputSchema"]["properties"]["include_source"]["default"] is False


def test_shader_client_calls_context_and_proposal_only() -> None:
    seen: list[Any] = []

    class Response:
        status = 200

        def read(self) -> bytes:
            return b'{"ticket_id":"spt_abc","kind":"shader_document_candidate","ready":true,"source":"secret"}'

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    def opener(req: Any, timeout: Any = None) -> Response:
        del timeout
        seen.append(req)
        return Response()

    client = Client(opener=opener)
    client.get_shader_context(
        origin="user", shader_id="aurora", project_context_witness="wit", include_source=False,
    )
    client.propose_shader(_proposal())
    assert [req.get_method() for req in seen] == ["GET", "POST"]
    assert seen[0].full_url.startswith("http://127.0.0.1:8000/api/agent/shader-context?")
    assert "include_source=false" in seen[0].full_url
    assert seen[0].get_header("X-hextile-agent") == "mcp"
    assert seen[0].get_header("X-hextile-project-witness") == "wit"
    assert seen[1].full_url == "http://127.0.0.1:8000/api/agent/shader-proposals"
    assert seen[1].get_header("X-hextile-agent") == "mcp"
    body = json.loads(seen[1].data)
    assert body["kind"] == "shader_document_candidate"
    assert all("imports" not in req.full_url and "consume" not in req.full_url for req in seen)
    with pytest.raises(HextileClientError):
        client.propose_shader({"kind": "config_partial", "config_partial": {"gain": 1}})
    with pytest.raises(HextileClientError):
        client.propose_shader({"kind": "shader_document_candidate", "base": {}, "consume": True})


def test_shader_handlers_return_context_and_ticket_identity() -> None:
    client = mock.Mock()
    client.get_shader_context.return_value = {"source_hash": "sha256:" + "ab" * 32, "manifest": {}}
    client.propose_shader.return_value = {
        "ticket_id": "spt_abc", "kind": "shader_document_candidate", "ready": True,
        "shader": {"origin": "user", "shaderId": "aurora"},
        "complete_candidate": {"source": "secret"},
    }
    server = HextileMcpServer(client=client)
    context = server._get_shader_context({
        "origin": "user", "shader_id": "aurora", "project_context_witness": "wit",
        "include_source": False,
    })
    assert context["manifest"] == {}
    client.get_shader_context.assert_called_once_with(
        origin="user", shader_id="aurora", project_context_witness="wit",
        include_source=False, include_formula=False, working_revision=None,
    )
    identity = server._propose_shader(_proposal())
    assert identity == {
        "ticket_id": "spt_abc", "kind": "shader_document_candidate",
        "shader": {"origin": "user", "shaderId": "aurora"}, "ready": False,
    }
    assert "complete_candidate" not in identity
    with pytest.raises(HextileClientError):
        server._propose_shader({"kind": "shader_document_candidate", "base": {}, "accept": True})
    with pytest.raises(HextileClientError):
        server._get_shader_context({
            "origin": "user", "shader_id": "aurora", "project_context_witness": "wit", "import": True,
        })


def _workspace_open() -> dict[str, Any]:
    return {"shader": {"origin": "project", "shaderId": "aurora"},
            "project_context_witness": "wit", "expected_working_revision": 1,
            "expected_document_hash": "sha256:" + "ab" * 32,
            "expected_source_hash": "sha256:" + "cd" * 32, "studio_surface_id": "surface"}


def _workspace_attachment() -> dict[str, Any]:
    body = _workspace_open()
    return {"handle": "sw_test", "generation": 0, "shader": body["shader"],
            "base": {"working_revision": 1, "document_hash": body["expected_document_hash"],
                     "source_hash": body["expected_source_hash"], "manifest_hash": None},
            "project_context_witness": "wit", "studio_surface_id": "surface",
            "files": {"source_path": "D:/project/workspace/source.glsl", "authoring_path": "D:/project/workspace/authoring.json"},
            "content_digest": "sha256:" + "ef" * 32, "schema": 1, "abi": "hx-shader/1",
            "available": True, "executable_capabilities": ["conventions/1"], "instructions": "observed APP instructions"}


def _workspace_register() -> dict[str, Any]:
    return {"handle": "sw_test", "change_id": "sw_test:1", "expected_generation": 0,
            "content_digest": "sha256:" + "ef" * 32}


def _workspace_receipt() -> dict[str, Any]:
    attachment = _workspace_attachment()
    return {key: value for key, value in {
        **attachment, **_workspace_register(), "generation": 1, "candidate_id": "candidate",
        "check_attempt": 0, "attachment_state": "attached", "prepare": "passed", "compile": "failed",
        "preview": "failed", "presentation": "pending", "save": "not_saved",
        "diagnostics": [{"phase": "compile", "severity": "error", "code": "compile",
                         "message": "x" * 2048, "file": "source.glsl", "line": 42, "column": 3}],
        "raw_log": "x" * 8192, "reason": {"code": "compile_failed", "message": "Repair native source"},
    }.items() if key not in {"files", "instructions", "expected_generation"}}


class _WorkspaceResponse:
    status = 200

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def read(self) -> bytes:
        return json.dumps(self.body).encode()

    def __enter__(self) -> "_WorkspaceResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_workspace_closed_schemas_and_transport_admission() -> None:
    cases = [("open_shader_workspace", _workspace_open()),
             ("register_shader_update", _workspace_register()),
             ("get_shader_update", {"handle": "sw_test", "change_id": "sw_test:1", "project_context_witness": "wit"})]
    client = Client(opener=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no HTTP")))
    for name, args in cases:
        schema = _schema(name)["inputSchema"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(args)
        jsonschema.validate(args, schema)
        malformed = [{**args, "path": "D:/other"}, {key: value for key, value in args.items() if key != schema["required"][0]}]
        if name == "open_shader_workspace":
            adopted = {**args, "existing_workspace": {"action": "adopt", "expected_content_digest": "sha256:" + "ef" * 32}}
            jsonschema.validate(adopted, schema)
            malformed += [{**args, "shader": {**args["shader"], "path": "D:/other"}},
                          {**adopted, "existing_workspace": {**adopted["existing_workspace"], "force": True}},
                          {**args, "shader": {"origin": "user", "shaderId": "aurora"}},
                          {**args, "existing_workspace": None}, {**args, "expected_working_revision": True}]
        for body in malformed:
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(body, schema)
            with pytest.raises(HextileClientError):
                getattr(client, name)(body)


def test_workspace_requests_validate_with_live_app_closed_models() -> None:
    # Load the real route models without importing unrelated desktop/GPU owners.
    # This cross-service boundary catches a closed field or type drifting in APP.
    route = ROOT.parent / "360-HEXTILE-APP/backend/api/routes/agent_shader_workspaces.py"
    tree = ast.parse(route.read_text(encoding="utf-8"))
    imports = [node for node in tree.body if isinstance(node, ast.ImportFrom) and node.module in {"typing", "pydantic"}]
    names = {"ClosedModel", "ShaderRef", "ExistingWorkspace", "OpenIn", "RegisterIn"}
    assignments = {"Digest", "Name", "SurfaceId"}
    definitions = [node for node in tree.body if (isinstance(node, ast.ClassDef) and node.name in names)
                   or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in assignments for target in node.targets))]
    namespace: dict[str, Any] = {"MAX_SAFE_GENERATION": 2**53 - 1}
    exec(compile(ast.Module(body=imports + definitions, type_ignores=[]), str(route), "exec"), namespace)
    for name in names:
        namespace[name].model_rebuild(_types_namespace=namespace)
    for model, body in ((namespace["OpenIn"], _workspace_open()),
                        (namespace["RegisterIn"], {key: value for key, value in _workspace_register().items() if key != "handle"})):
        validated = model.model_validate(body).model_dump(exclude_unset=True)
        assert validated == body
        with pytest.raises(ValueError):
            model.model_validate({**body, "source": "forbidden GLSL JSON"})
    schema = namespace["OpenIn"].model_json_schema()
    public = _schema("open_shader_workspace")["inputSchema"]
    assert set(public["properties"]) == set(schema["properties"])
    assert set(public["required"]) == set(schema["required"])
    schema = namespace["RegisterIn"].model_json_schema()
    public = _schema("register_shader_update")["inputSchema"]
    assert set(public["properties"]) - {"handle"} == set(schema["properties"])


def test_workspace_http_identity_receipt_and_open_replay() -> None:
    seen: list[Any] = []
    attachment, receipt = _workspace_attachment(), _workspace_receipt()

    def opener(req: Any, timeout: Any = None) -> _WorkspaceResponse:
        seen.append((req, timeout))
        return _WorkspaceResponse(attachment if req.full_url.endswith("/open") else receipt)

    server = HextileMcpServer(client=Client(opener=opener, timeout=23))
    # Call handlers to isolate workspace transport from unrelated activity requests.
    assert server._open_shader_workspace(_workspace_open()) == attachment
    assert server._open_shader_workspace(_workspace_open()) == attachment
    assert server._register_shader_update(_workspace_register()) == receipt
    assert server._get_shader_update({"handle": "sw_test", "change_id": "sw_test:1", "project_context_witness": "wit"}) == receipt
    assert [timeout for _, timeout in seen] == [15.0, 15.0, 23, 23]
    assert [req.get_method() for req, _ in seen] == ["POST", "POST", "POST", "GET"]
    assert json.loads(seen[0][0].data) == json.loads(seen[1][0].data) == _workspace_open()
    assert json.loads(seen[2][0].data) == {key: value for key, value in _workspace_register().items() if key != "handle"}
    assert seen[2][0].full_url.endswith("/api/agent/shader-workspaces/sw_test/updates")
    assert seen[3][0].full_url.endswith("/sw_test/updates/sw_test%3A1?project_context_witness=wit")
    assert all(req.get_header("X-hextile-agent") == "mcp" for req, _ in seen)
    assert "files" not in receipt and "source" not in receipt
    assert receipt["diagnostics"][0]["line"] == 42 and len(receipt["raw_log"]) == 8192


@pytest.mark.parametrize("change_id", ["other:1", "sw_test:01", "sw_test:0", "sw_test:+1", "sw_test:1.0",
                                     "sw_test:١", "sw_test:9007199254740992", "sw_test:" + "9" * 10000])
def test_workspace_canonical_change_id_refuses_before_http(change_id: str) -> None:
    client = Client(opener=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no HTTP")))
    with pytest.raises(HextileClientError) as caught:
        client.register_shader_update({**_workspace_register(), "change_id": change_id})
    assert caught.value.status_code == 409
    with pytest.raises(HextileClientError):
        client.get_shader_update({"handle": "sw_test", "change_id": change_id, "project_context_witness": "wit"})


@pytest.mark.parametrize("generation", [True, -1, 1.0, 2**53 - 1])
def test_workspace_generation_is_bounded_safe_integer(generation: Any) -> None:
    with pytest.raises(HextileClientError):
        Client().register_shader_update({**_workspace_register(), "expected_generation": generation})


@pytest.mark.parametrize("status,code", [(409, "idempotency_conflict"), (409, "generation_conflict"),
    (409, "attachment_conflict"), (409, "attachment_not_ready"), (503, "attachment_unavailable"),
    (410, "receipt_expired"), (410, "workspace_expired"), (403, "authority"), (503, "capacity")])
def test_workspace_backend_refusals_preserve_bounded_details(status: int, code: str) -> None:
    detail = {"code": code, "message": "x" * 2048, "recovery": "Rediscover current authority"}

    def opener(req: Any, timeout: Any = None) -> Any:
        raise urllib.error.HTTPError(req.full_url, status, "refused", None,
                                     io.BytesIO(json.dumps({"detail": detail}).encode()))

    server = HextileMcpServer(client=Client(opener=opener))
    result = server.call_tool("register_shader_update", _workspace_register())
    assert result["isError"] is True
    error = json.loads(result["content"][0]["text"])
    assert error["status_code"] == status and error["receipt"] == detail
    assert "files" not in error and "source_path" not in result["content"][0]["text"]


@pytest.mark.parametrize("operation", ["open_shader_workspace", "register_shader_update"])
def test_workspace_lost_open_never_retries_or_claims_attachment(operation: str) -> None:
    for failure in (urllib.error.URLError(TimeoutError("timed out")), http.client.RemoteDisconnected("lost")):
        calls: list[Any] = []

        def opener(req: Any, timeout: Any = None) -> Any:
            calls.append(req)
            raise failure

        with pytest.raises(HextileClientError):
            getattr(Client(opener=opener), operation)(_workspace_open() if operation == "open_shader_workspace" else _workspace_register())
        assert len(calls) == 1
    for reply in ({"attachment_state": "attaching", "files": _workspace_attachment()["files"]},
                  {**_workspace_attachment(), "attachment_state": "stale"}):
        with pytest.raises(HextileClientError) as caught:
            Client(opener=lambda *_a, **_k: _WorkspaceResponse(reply)).open_shader_workspace(_workspace_open())
        assert caught.value.kind == "workspace_outcome_unknown"
        assert caught.value.receipt is None and caught.value.body is None

    class IncompleteResponse(_WorkspaceResponse):
        def read(self) -> bytes:
            return b'{"files":{"source_path":"D:/private/source.glsl"'

    with pytest.raises(HextileClientError) as caught:
        Client(opener=lambda *_a, **_k: IncompleteResponse({})).open_shader_workspace(_workspace_open())
    assert "D:/private" not in str(caught.value)
    assert caught.value.body is None and caught.value.receipt is None


def test_workspace_empty_registration_is_unknown() -> None:
    class EmptyBody:
        status = 200

        def read(self) -> bytes:
            return b"null"

        def __enter__(self) -> "EmptyBody":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    server = HextileMcpServer(client=Client(opener=lambda *_a, **_k: EmptyBody()))
    result = server.call_tool("register_shader_update", _workspace_register())
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["kind"] == "workspace_outcome_unknown"


def test_workspace_replay_and_history_are_app_owned() -> None:
    receipt = _workspace_receipt()
    observed: list[Any] = []

    def opener(req: Any, timeout: Any = None) -> _WorkspaceResponse:
        if req.full_url.endswith("/open"):
            return _WorkspaceResponse({**_workspace_attachment(), "generation": 9})
        body = json.loads(req.data)
        observed.append(body)
        # Retained original ID can replay after current generation advances.
        return _WorkspaceResponse(receipt)

    client = Client(opener=opener)
    assert client.open_shader_workspace(_workspace_open())["generation"] == 9
    assert client.register_shader_update(_workspace_register()) == receipt
    assert client.register_shader_update(_workspace_register()) == receipt
    assert observed[0] == observed[1]
    # Altered retained generation must reach APP's replay classifier, not be
    # relabelled by a proxy without the authoritative receipt history.
    client.register_shader_update({**_workspace_register(), "expected_generation": 1})
    assert observed[-1]["change_id"] == "sw_test:1"


def test_workspace_missing_route_is_actionable_without_rewriting_old_peer_error() -> None:
    def opener(req: Any, timeout: Any = None) -> Any:
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, io.BytesIO(b'{"detail":"Not Found"}'))

    with pytest.raises(HextileClientError) as caught:
        Client(opener=opener).open_shader_workspace(_workspace_open())
    assert caught.value.kind == "upgrade" and "Upgrade 360 Hextile" in str(caught.value)
    assert caught.value.body == '{"detail":"Not Found"}'


def _playable_recipe() -> tuple[list[str], dict[str, Any]]:
    guide = (ROOT / "skills/hextile/references/recipes.md").read_text(encoding="utf-8")
    shader_section = guide.split("## Recipe I — Playable Shaders:", 1)[1]
    sources = re.findall(r"```glsl\n(.*?)```", shader_section, re.S)
    extensions = json.loads(re.search(r"```json\n(.*?)```", shader_section, re.S).group(1))
    return sources, extensions


def test_playable_recipes_match_shipped_sources_and_bound_contracts() -> None:
    # These are the two accepted producer examples, not schema stubs. Read-only
    # admission checks keep a copied guide from teaching fields APP rejects.
    app = ROOT.parent / "360-HEXTILE-APP"
    sys.path.insert(0, str(app))
    from backend.modulation.conventions import validate_conventions
    from backend.modulation.contracts import validate_modulation

    examples = json.loads((app / "frontend/src/lib/shader/convention-examples.json").read_text(encoding="utf-8"))
    sources, extensions = _playable_recipe()
    assert sources == [row["candidate"]["source"] for row in examples]
    assert extensions == examples[1]["candidate"]["extensions"]
    assert extensions["required_capabilities"] == ["conventions/1", "master-clock/1", "modulation/2"]
    manifest = json.loads(sources[1].split("/*@hextile-shader\n", 1)[1].split("\n*/", 1)[0])
    descriptors = [{"id": "shader.params." + p["id"], "kind": p["type"], "unit": "unitless",
                    "min": p["ui"]["min"], "max": p["ui"]["max"], "base": p["default"], "live": True} for p in manifest["parameters"]]
    validate_conventions(extensions["conventions"], descriptors)
    validate_modulation(extensions["modulation"], descriptors, extensions["master_clock"],
                        require_aliases=True, logical_inputs=extensions["conventions"])
    assert "vec3 ro = hxCameraPosition;" in sources[0]
    assert "vec3 rd = hxCameraDirection(hx.direction);" in sources[0]
    assert "hxParam_glow" in sources[1] and "BLAST" in sources[1]
    assert extensions["conventions"]["camera"] is None


@pytest.mark.parametrize("variant", ["held", "instant", "orphan", "default_one", "schema_one"])
def test_playable_recipe_variants_use_real_intrinsic_admission(variant: str) -> None:
    sys.path.insert(0, str(ROOT.parent / "360-HEXTILE-APP"))
    from backend.modulation.contracts import ContractError, validate_modulation_intrinsic

    _, extensions = _playable_recipe()
    definition = extensions["modulation"]["definitions"][0]
    if variant == "held":
        definition["mode"] = "held"
        definition["settings"].update(attack=0.2, release=0.6)
    elif variant == "instant":
        definition["settings"]["attack"] = 0
    elif variant == "orphan":
        definition["input_id"] = "missing_gate"
    elif variant == "default_one":
        extensions["conventions"]["channels"][1]["default"] = 1
    else:
        extensions["modulation"]["schema"] = 1

    def admit() -> None:
        validate_modulation_intrinsic(extensions["modulation"], extensions["master_clock"],
                                      require_aliases=True, logical_inputs=extensions["conventions"])

    if variant in {"held", "instant"}:
        admit()
    else:
        with pytest.raises(ContractError):
            admit()


def test_playable_tool_guidance_preserves_legacy_authority_and_no_new_controls() -> None:
    context = _schema("get_shader_context")["description"]
    proposal = _schema("propose_shader")["description"]
    workspace = _schema("open_shader_workspace")["description"]
    assert "source-free" in context and "include_formula only" in context
    assert "every Board row" in proposal and "preserve" in proposal
    assert "attested" in proposal and "envelope" in proposal
    assert "conventions/1" in workspace and "modulation/2" in workspace
    assert "hxCameraDirection(vec3 canonicalDirection)" in workspace
    assert "never arms Interact" in workspace
    operations = _schema("propose_shader")["inputSchema"]["properties"]["modulation_edits"]
    assert operations["properties"]["schema"] == {"const": 1}
    assert not {"press_key", "set_pose", "arm_interact", "save_project_convention"} & {tool["name"] for tool in TOOLS}
    source = (ROOT / "skills/hextile/SKILL.md").read_text(encoding="utf-8")
    fragment = (ROOT / "codex/AGENTS-fragment.md").read_text(encoding="utf-8")
    assert fragment.split("-->\n\n", 1)[1] == source.split("\n---\n", 1)[1].lstrip("\n")
