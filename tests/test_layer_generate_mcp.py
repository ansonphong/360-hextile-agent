"""Critical saved Layers MCP wire, privacy, and uncertain-outcome contract."""

from __future__ import annotations

import json
import io
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from hextile_client import Client  # noqa: E402
from hextile_mcp import HextileMcpServer, TOOLS  # noqa: E402

JOB = "1" * 32
HEAD = "a" * 12
DRAFT = "b" * 12
BAG = "comp_" + "c" * 12
POSE = {"yaw": 20, "pitch": -10, "roll": 0, "fov": 40, "projection": "auto"}


class Response:
    status = 200

    def __init__(self, body: dict[str, Any]):
        self.body = body

    def read(self) -> bytes:
        return json.dumps(self.body).encode()

    def getcode(self) -> int:
        return 200

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["content"][0]["text"])


def test_seven_closed_tools_and_policy() -> None:
    names = {"get_layer_draft", "generate_layer", "get_layer_generation",
             "cancel_layer_generation", "rematte_layer", "land_generated_layer", "commit_layer_draft"}
    catalog = {tool["name"]: tool for tool in TOOLS}
    assert names <= catalog.keys()
    for name in names:
        assert catalog[name]["inputSchema"]["additionalProperties"] is False
        assert catalog[name]["annotations"]["readOnlyHint"] is (name in {"get_layer_draft", "get_layer_generation"})
    controls = catalog["generate_layer"]["inputSchema"]["properties"]["controls"]
    assert controls["additionalProperties"] is False
    assert controls["properties"]["diffusion"]["additionalProperties"] is False
    assert catalog["land_generated_layer"]["inputSchema"]["properties"]["pose"]["additionalProperties"] is False
    for name in ("cancel_layer_generation", "rematte_layer"):
        schema = catalog[name]["inputSchema"]
        assert schema["properties"]["execution_target"]["enum"] == ["saved", "live"]
        assert "execution_target" not in schema["required"]
    assert "target" in catalog["rematte_layer"]["inputSchema"]["properties"]


@pytest.mark.parametrize("name", ["generate_layer", "land_generated_layer", "commit_layer_draft"])
def test_live_empty_layers_revision_is_admitted_but_negative_revision_is_not(name: str) -> None:
    tool = next(tool for tool in TOOLS if tool["name"] == name)
    revision = tool["inputSchema"]["properties"]["expected_mutation_rev"]
    assert revision == {"type": "integer", "minimum": 0}
    assert 0 >= revision["minimum"]
    assert -1 < revision["minimum"]


def test_saved_lifecycle_uses_named_routes_and_strips_private_payload() -> None:
    seen: list[tuple[str, str, Any]] = []

    def opener(req: Any, timeout: Any = None) -> Response:
        body = json.loads(req.data) if req.data else None
        seen.append((req.get_method(), req.full_url, body))
        path = req.full_url
        if path.endswith("/agent-state?parent_id=" + HEAD):
            return Response({"head": HEAD, "parent_id": HEAD, "draft_id": DRAFT,
                             "composition_id": BAG, "mutation_rev": 2, "studio_owned": False,
                             "active_bag_layers": [], "secret_path": "C:/private"})
        if path.endswith("/agent-generate"):
            return Response({"job_id": JOB, "draft_id": DRAFT, "composition_id": BAG,
                             "head": HEAD, "state": "admitted", "recipe": {"secret": "x"}})
        if path.endswith("/generate/" + JOB):
            return Response({"job_id": JOB, "state": "ready", "width": 1024, "height": 1024,
                             "rgb_draft": {"token": "rgb-token", "path": "C:/private"},
                             "recipe": {"secret": "x"}, "asset": "some.png"})
        if path.endswith("/agent-land"):
            return Response({"draft_id": DRAFT, "composition_id": BAG, "mutation_rev": 2,
                             "layer_id": "pl_enamel1", "already_landed": False,
                             "asset": "private.png", "recipeSource": "private.hextile.json"})
        if path.endswith("/layers/agent-commit"):
            return Response({"node_id": DRAFT, "head": DRAFT, "path": "C:/private", "width": 1024, "height": 1024})
        return Response({})  # activity event

    server = HextileMcpServer(client=Client(opener=opener))
    assert payload(server.call_tool("get_layer_draft", {"render_id": "r1", "parent_id": HEAD}))["studio_owned"] is False
    started = payload(server.call_tool("generate_layer", {"target": "saved", "render_id": "r1",
        "request_id": JOB, "expected_head": HEAD, "parent_id": HEAD, "mode": "generate", "controls": {"pipeline": "sdxl"}}))
    assert started == {"job_id": JOB, "draft_id": DRAFT, "composition_id": BAG, "head": HEAD, "state": "admitted"}
    status = payload(server.call_tool("get_layer_generation", {"render_id": "r1", "job_id": JOB}))
    assert status["rgb_draft"] == {"token": "rgb-token"} and "recipe" not in status and "asset" not in status
    landed = payload(server.call_tool("land_generated_layer", {"target": "saved", "render_id": "r1",
        "layer_id": "pl_enamel1", "job_id": JOB, "expected_head": HEAD, "draft_id": DRAFT,
        "composition_id": BAG, "expected_mutation_rev": 1, "pose": POSE}))
    assert landed["mutation_rev"] == 2 and "asset" not in landed
    committed = payload(server.call_tool("commit_layer_draft", {"target": "saved", "render_id": "r1",
        "parent_id": HEAD, "draft_id": DRAFT, "composition_id": BAG, "expected_mutation_rev": 2}))
    assert committed == {"node_id": DRAFT, "head": DRAFT, "width": 1024, "height": 1024}
    routes = [(method, url.split("/api/")[-1]) for method, url, _ in seen if "/api/renders/" in url]
    assert routes == [("GET", f"renders/r1/layers/agent-state?parent_id={HEAD}"),
                      ("POST", "renders/r1/layers/agent-generate"),
                      ("GET", f"renders/r1/layers/generate/{JOB}"),
                      ("POST", "renders/r1/layers/agent-land"),
                      ("GET", f"renders/r1/layers/agent-state?parent_id={HEAD}"),
                      ("POST", "renders/r1/layers/agent-commit")]
    start_body = next(body for _method, url, body in seen if url.endswith("/agent-generate"))
    assert start_body["request_id"] == JOB and "target" not in start_body


def test_external_live_refused_without_http_and_private_live_wire() -> None:
    calls: list[Any] = []

    def opener(req: Any, timeout: Any = None) -> Response:
        calls.append(req)
        return Response({"ticket_id": "private-ticket"})

    args = {"target": "live", "render_id": "r1", "request_id": JOB,
            "expected_head": HEAD, "parent_id": HEAD, "mode": "generate"}
    external = HextileMcpServer(client=Client(opener=opener))
    assert external.call_tool("generate_layer", args)["isError"] is True
    assert payload(external.call_tool("generate_layer", args))["status_code"] == 403
    assert not [req for req in calls if req.full_url.endswith("/creative-live")]

    internal = HextileMcpServer(client=Client(opener=opener, internal_mode=True), child_token="child-secret")
    assert internal.call_tool("generate_layer", args)["isError"] is True  # no approved operation id
    result = internal.call_tool("generate_layer", args, operation_id="approved-op")
    assert result["isError"] is False
    request = calls[-1]
    assert request.full_url.endswith("/api/agent/creative-live")
    assert json.loads(request.data) == {"tool_name": "generate_layer", "arguments": args}
    assert request.get_header("X-hextile-copilot-token") == "child-secret"
    assert request.get_header("X-hextile-op") == "approved-op"


@pytest.mark.parametrize("name,args", [
    ("cancel_layer_generation", {"render_id": "r1", "job_id": JOB, "execution_target": "live"}),
    ("rematte_layer", {"render_id": "r1", "job_id": JOB, "execution_target": "live",
                       "rgb_draft": "rgb-token", "alpha": {"mode": "opaque"},
                       "target": {"layer_id": "pl_enamel1", "asset": "take.png",
                                  "recipeSource": "take.hextile.json", "takes_fingerprint": "fp"}}),
])
def test_live_job_control_is_private_and_preserves_recovery_target(name: str, args: dict[str, Any]) -> None:
    calls: list[Any] = []

    def opener(req: Any, timeout: Any = None) -> Response:
        calls.append(req)
        return Response({"job_id": JOB, "state": "cancel_requested"})

    external = HextileMcpServer(client=Client(opener=opener))
    refused = external.call_tool(name, args)
    assert refused["isError"] is True and payload(refused)["status_code"] == 403
    assert not [req for req in calls if req.full_url.endswith("/creative-live") or "/api/renders/" in req.full_url]

    internal = HextileMcpServer(client=Client(opener=opener, internal_mode=True), child_token="child-secret")
    assert internal.call_tool(name, args)["isError"] is True  # No approved operation id.
    assert not [req for req in calls if req.full_url.endswith("/creative-live") or "/api/renders/" in req.full_url]
    result = internal.call_tool(name, args, operation_id="approved-op")
    assert result["isError"] is False
    effects = [req for req in calls if req.full_url.endswith("/creative-live")]
    assert len(effects) == 1
    request = effects[0]
    assert request.full_url.endswith("/api/agent/creative-live")
    assert json.loads(request.data) == {"tool_name": name, "arguments": args}
    assert request.get_header("X-hextile-copilot-token") == "child-secret"
    assert request.get_header("X-hextile-op") == "approved-op"


def test_uncertain_rematte_is_one_call_and_non_replayable() -> None:
    attempts: list[str] = []

    def unavailable(req: Any, timeout: Any = None) -> Response:
        attempts.append(req.full_url)
        if req.full_url.endswith("/generate/" + JOB):
            return Response({"job_id": JOB, "parent_id": HEAD, "package_id": DRAFT,
                             "composition_id": BAG, "state": "failed"})
        if req.full_url.endswith("/agent-state?parent_id=" + HEAD):
            return Response({"head": HEAD, "draft_id": DRAFT, "composition_id": BAG,
                             "studio_owned": False})
        raise urllib.error.URLError("connection dropped after submit")

    server = HextileMcpServer(client=Client(opener=unavailable, internal_mode=True), child_token="test")
    result = server.call_tool("rematte_layer", {"render_id": "r1", "job_id": JOB,
                                                "rgb_draft": "rgb-token", "alpha": {"mode": "opaque"}})
    assert result["isError"] is True
    error = payload(result)
    assert error["kind"] == "layer_outcome_unknown"
    assert error["receipt"] == {"state": "unknown", "non_replayable": True,
                                "render_id": "r1", "parent_job_id": JOB}
    assert len([url for url in attempts if url.endswith("/agent-rematte")]) == 1


def test_uncertain_live_rematte_is_one_call_and_non_replayable() -> None:
    attempts: list[str] = []

    def unavailable(req: Any, timeout: Any = None) -> Response:
        attempts.append(req.full_url)
        raise urllib.error.URLError("connection dropped after submit")

    server = HextileMcpServer(client=Client(opener=unavailable, internal_mode=True), child_token="test")
    result = server.call_tool("rematte_layer", {"execution_target": "live", "render_id": "r1",
                                                "job_id": JOB, "rgb_draft": "rgb-token",
                                                "alpha": {"mode": "opaque"}}, operation_id="approved-op")
    assert result["isError"] is True
    error = payload(result)
    assert error["kind"] == "layer_outcome_unknown"
    assert error["receipt"] == {"state": "unknown", "non_replayable": True,
                                "render_id": "r1", "parent_job_id": JOB}
    assert len(attempts) == 1 and attempts[0].endswith("/api/agent/creative-live")


@pytest.mark.parametrize("name,arguments,stable_key", [
    ("generate_layer", {"target": "saved", "render_id": "r1", "request_id": JOB,
                        "expected_head": HEAD, "parent_id": HEAD, "mode": "generate"}, "request_id"),
    ("land_generated_layer", {"target": "saved", "render_id": "r1", "layer_id": "pl_enamel1",
                              "job_id": JOB, "expected_head": HEAD, "draft_id": DRAFT,
                              "composition_id": BAG, "expected_mutation_rev": 1, "pose": POSE}, "layer_id"),
    ("commit_layer_draft", {"target": "saved", "render_id": "r1", "parent_id": HEAD,
                            "draft_id": DRAFT, "composition_id": BAG, "expected_mutation_rev": 2}, "draft_id"),
])
def test_uncertain_saved_writes_keep_stable_reconciliation_id(name: str, arguments: dict[str, Any], stable_key: str) -> None:
    def unavailable(req: Any, timeout: Any = None) -> Response:
        if req.full_url.endswith("/agent-state?parent_id=" + HEAD):
            return Response({"head": HEAD, "draft_id": DRAFT, "composition_id": BAG,
                             "mutation_rev": 2, "studio_owned": False})
        raise urllib.error.URLError("connection dropped after submit")

    server = HextileMcpServer(client=Client(opener=unavailable, internal_mode=True), child_token="test")
    result = server.call_tool(name, arguments)
    assert result["isError"] is True
    error = payload(result)
    assert error["kind"] == "layer_outcome_unknown"
    assert error["receipt"]["state"] == "unknown"
    assert error["receipt"][stable_key] == arguments[stable_key]


@pytest.mark.parametrize("name,arguments,read_suffix,read_body,expected", [
    ("generate_layer", {"target": "saved", "render_id": "r1", "request_id": JOB,
                        "expected_head": HEAD, "parent_id": HEAD, "mode": "generate"},
     "/generate/" + JOB,
     {"job_id": JOB, "parent_id": HEAD, "package_id": DRAFT, "composition_id": BAG, "state": "admitted"},
     {"job_id": JOB, "draft_id": DRAFT, "composition_id": BAG, "head": HEAD, "state": "admitted"}),
    ("commit_layer_draft", {"target": "saved", "render_id": "r1", "parent_id": HEAD,
                            "draft_id": DRAFT, "composition_id": BAG, "expected_mutation_rev": 2},
     "/graph", {"head": DRAFT, "nodes": [{"id": DRAFT, "op": "layers", "parent": HEAD}]},
     {"node_id": DRAFT, "head": DRAFT}),
])
def test_lost_reply_exact_read_can_confirm_without_replay(
    name: str, arguments: dict[str, Any], read_suffix: str, read_body: dict[str, Any], expected: dict[str, Any]
) -> None:
    calls: list[str] = []

    def opener(req: Any, timeout: Any = None) -> Response:
        calls.append(req.full_url)
        if req.full_url.endswith("/agent-state?parent_id=" + HEAD):
            return Response({"head": HEAD, "draft_id": DRAFT, "composition_id": BAG,
                             "mutation_rev": 2, "studio_owned": False})
        if req.get_method() == "POST" and "/api/renders/" in req.full_url:
            raise urllib.error.URLError("reply lost")
        if req.full_url.endswith(read_suffix):
            return Response(read_body)
        return Response({})

    server = HextileMcpServer(client=Client(opener=opener, internal_mode=True), child_token="test")
    result = server.call_tool(name, arguments)
    assert result["isError"] is False
    assert payload(result) == expected
    assert len([url for url in calls if url.endswith("/agent-generate") or url.endswith("/layers/agent-commit")]) == 1


def test_lost_land_reply_checks_ready_pair_and_same_draft() -> None:
    calls: list[str] = []

    def opener(req: Any, timeout: Any = None) -> Response:
        calls.append(req.full_url)
        if req.full_url.endswith("/agent-land"):
            raise urllib.error.URLError("reply lost")
        if req.full_url.endswith("/generate/" + JOB):
            return Response({"job_id": JOB, "state": "ready", "parent_id": HEAD,
                             "package_id": DRAFT, "composition_id": BAG,
                             "asset": "ready.png", "recipeSource": "ready.hextile.json"})
        if req.full_url.endswith("/agent-state?parent_id=" + HEAD):
            return Response({"head": HEAD, "draft_id": DRAFT, "composition_id": BAG,
                             "mutation_rev": 2, "active_bag_layers": [{"layer_id": "pl_enamel1",
                             "asset": "ready.png", "recipeSource": "ready.hextile.json"}]})
        return Response({})

    server = HextileMcpServer(client=Client(opener=opener, internal_mode=True), child_token="test")
    result = server.call_tool("land_generated_layer", {"target": "saved", "render_id": "r1",
        "layer_id": "pl_enamel1", "job_id": JOB, "expected_head": HEAD, "draft_id": DRAFT,
        "composition_id": BAG, "expected_mutation_rev": 1, "pose": POSE})
    assert result["isError"] is False
    assert payload(result)["already_landed"] is True
    assert len([url for url in calls if url.endswith("/agent-land")]) == 1


@pytest.mark.parametrize("name,arguments", [
    ("cancel_layer_generation", {"render_id": "r1", "job_id": JOB}),
    ("rematte_layer", {"render_id": "r1", "job_id": JOB,
                       "rgb_draft": "rgb-token", "alpha": {"mode": "opaque"}}),
    ("commit_layer_draft", {"target": "saved", "render_id": "r1", "parent_id": HEAD,
                            "draft_id": DRAFT, "composition_id": BAG, "expected_mutation_rev": 2}),
])
def test_studio_owned_saved_job_refuses_before_mutating_post(name: str, arguments: dict[str, Any]) -> None:
    calls: list[tuple[str, str]] = []

    def opener(req: Any, timeout: Any = None) -> Response:
        calls.append((req.get_method(), req.full_url))
        if req.full_url.endswith("/generate/" + JOB):
            return Response({"parent_id": HEAD, "package_id": DRAFT, "composition_id": BAG})
        if req.full_url.endswith("/agent-state?parent_id=" + HEAD):
            return Response({"head": HEAD, "draft_id": DRAFT, "composition_id": BAG,
                             "mutation_rev": 2, "studio_owned": True})
        return Response({})

    server = HextileMcpServer(client=Client(opener=opener, internal_mode=True), child_token="test")
    result = server.call_tool(name, arguments)
    assert result["isError"] is True
    assert payload(result)["status_code"] == 409
    assert not [url for method, url in calls if method == "POST" and "/api/renders/" in url]


@pytest.mark.parametrize("name,args,route", [
    ("cancel_layer_generation", {"render_id": "r1", "job_id": JOB}, "agent-cancel"),
    ("cancel_layer_generation", {"render_id": "r1", "job_id": JOB, "execution_target": "saved"}, "agent-cancel"),
    ("rematte_layer", {"render_id": "r1", "job_id": JOB,
                       "rgb_draft": "rgb-token", "alpha": {"mode": "opaque"}}, "agent-rematte"),
    ("rematte_layer", {"render_id": "r1", "job_id": JOB, "execution_target": "saved",
                       "rgb_draft": "rgb-token", "alpha": {"mode": "opaque"},
                       "target": {"layer_id": "pl_enamel1", "asset": "take.png",
                                  "recipeSource": "take.hextile.json", "takes_fingerprint": "fp"}}, "agent-rematte"),
])
def test_saved_job_controls_use_agent_only_routes(name: str, args: dict[str, Any], route: str) -> None:
    posts: list[tuple[str, Any]] = []

    def opener(req: Any, timeout: Any = None) -> Response:
        if req.full_url.endswith("/generate/" + JOB):
            return Response({"job_id": JOB, "parent_id": HEAD, "package_id": DRAFT,
                             "composition_id": BAG, "state": "admitted"})
        if req.full_url.endswith("/agent-state?parent_id=" + HEAD):
            return Response({"draft_id": DRAFT, "composition_id": BAG, "studio_owned": False})
        if req.get_method() == "POST" and "/api/renders/" in req.full_url:
            posts.append((req.full_url, json.loads(req.data) if req.data else None))
            return Response({"job_id": JOB, "state": "cancelled", "released": True})
        return Response({})

    server = HextileMcpServer(client=Client(opener=opener, internal_mode=True), child_token="test")
    result = server.call_tool(name, args)
    assert result["isError"] is False
    assert len(posts) == 1 and posts[0][0].endswith("/" + route)
    if name == "rematte_layer":
        expected = {"version": 1, "rgb_draft": "rgb-token", "alpha": {"mode": "opaque"}}
        if "target" in args:
            expected["target"] = args["target"]
        assert posts[0][1] == expected


def test_layer_http_error_keeps_code_but_omits_backend_path() -> None:
    def rejected(req: Any, timeout: Any = None) -> Response:
        raise urllib.error.HTTPError(req.full_url, 409, "conflict", None,
            io.BytesIO(b'{"detail":{"code":"stale_draft","detail":"C:/private/recipe.json"}}'))

    server = HextileMcpServer(client=Client(opener=rejected, internal_mode=True), child_token="test")
    result = server.call_tool("get_layer_draft", {"render_id": "r1", "parent_id": HEAD})
    assert result["isError"] is True
    assert payload(result)["code"] == "stale_draft"
    assert "C:/private" not in result["content"][0]["text"]
