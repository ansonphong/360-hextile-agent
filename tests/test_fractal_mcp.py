"""Canonical Fractal MCP schemas, live routes, and unknown receipts."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from hextile_client import Client, HextileClientError  # noqa: E402
from hextile_mcp import TOOLS, TOOL_NAMES, HextileMcpServer  # noqa: E402

SHA = "sha256:" + "ab" * 32
BASE = "sha256:" + "cd" * 32
SCENE = "scn_aabbccdd"
OTHER = "scn_11223344"
SEQ = "seq_" + "ab" * 16
ATTEMPT = "attempt_" + "ab" * 16
MOD = "mod_" + "ab" * 12
NAMES = (
    "get_fractal_context",
    "apply_fractal_edits",
    "control_fractal_timeline",
    "open_fractal_scene",
    "save_fractal_scene",
    "get_fractal_request",
    "preflight_fractal_sequence",
    "render_fractal_sequence",
    "get_fractal_sequence",
    "control_fractal_sequence",
    "preflight_fractal_sequence_export",
    "export_fractal_sequence",
)


def _schema(name: str) -> dict[str, Any]:
    found = [tool for tool in TOOLS if tool["name"] == name]
    assert found, name
    return found[0]


def _witness() -> dict[str, Any]:
    return {
        "header_witness": {"activationToken": "tok", "sessionId": "ses", "subjectKey": "sub"},
        "project_context_witness": "proj",
        "scene_id": SCENE,
        "scene_revision": SHA,
        "draft_generation": 3,
        "source_generation": 1,
        "viewer_generation": 2,
        "renderer_generation": 4,
        "owner_id": "owner",
    }


def _edit(ops: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "client_request_id": "req_1",
        "witness": _witness(),
        "expected_draft_generation": 3,
        "ops": ops,
    }


def _sequence(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "scene_id": SCENE,
        "scene_revision": SHA,
        "idempotency_key": "job-1",
        "name": "orbit",
        "frame_rate": {"num": 1, "den": 1},
        "start_time": {"num": "0", "den": "1"},
        "end_time": {"num": "2", "den": "1"},
        "frame_count": 2,
        "seed": 1,
        "quality": "explore_smoke",
        "width": 128,
        "height": 64,
        "output": {"projection": "equirectangular", "pan": 0, "tilt": 0, "roll": 0},
        "publication_mode": "deferred",
    }
    body.update(over)
    return body


class _Response:
    def __init__(self, data: dict[str, Any], status: int = 200) -> None:
        self.status = status
        self.data = data

    def read(self) -> bytes:
        return json.dumps(self.data).encode()

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _http_error(code: int, detail: Any) -> urllib.error.HTTPError:
    body = json.dumps({"detail": detail}).encode()
    return urllib.error.HTTPError(
        url="http://127.0.0.1:8000/x", code=code, msg="no", hdrs=None, fp=io.BytesIO(body),
    )


def test_fractal_tools_are_closed_and_classified() -> None:
    assert tuple(name for name in TOOL_NAMES if name in NAMES) == NAMES
    read_only = {
        "get_fractal_context", "get_fractal_request",
        "preflight_fractal_sequence", "get_fractal_sequence", "preflight_fractal_sequence_export",
    }
    for name in NAMES:
        tool = _schema(name)
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"]["readOnlyHint"] is (name in read_only)
        assert tool["annotations"]["destructiveHint"] is False
        assert "arm" not in json.dumps(tool["inputSchema"])
        assert "captured_input" not in json.dumps(tool["inputSchema"])
        assert "lease_id" not in json.dumps(tool["inputSchema"])


def test_remove_driver_schema_rejects_extra_fields_and_keeps_batch_cap() -> None:
    tool = _schema("apply_fractal_edits")
    remove = _edit([{"op": "remove_driver", "target": "fractal.look.ambient"}])
    jsonschema.validate(remove, tool["inputSchema"])
    capped = _edit([{"op": "remove_driver", "target": f"fractal.t{i}"} for i in range(32)])
    jsonschema.validate(capped, tool["inputSchema"])
    for bad in (
        _edit([{"op": "remove_driver", "target": "fractal.look.ambient", "driver": True}]),
        _edit([{"op": "remove_driver"}]),
        _edit([{"op": "remove_driver", "target": "fractal.look.ambient", "arm": True}]),
        _edit([{"op": "remove_driver", "target": f"fractal.t{i}"} for i in range(33)]),
        {**remove, "lease_id": "lease_1"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, tool["inputSchema"])


def test_input_declarations_cannot_arm_or_inject() -> None:
    tool = _schema("apply_fractal_edits")
    mouse = _edit([{"op": "set_mouse_input", "value": {"schema": 1, "enabled": False, "abi": "hx-mouse/1"}}])
    gate = _edit([{"op": "upsert_input_gate", "value": {
        "id": "fire", "alias": "Fire", "label": "Fire", "mode": "gate", "unit": "gate",
        "min": 0, "max": 1, "default": 0,
    }}])
    binding = _edit([{"op": "upsert_input_binding", "value": {
        "id": "fire_key", "channel_id": "fire",
        "source": {"kind": "key", "code": "KeyF", "modifiers": []},
        "scale": 1, "activation": "interact",
    }}])
    for good in (mouse, gate, binding):
        jsonschema.validate(good, tool["inputSchema"])
    for bad in (
        _edit([{"op": "set_mouse_input", "value": {**mouse["ops"][0]["value"], "arm": True}}]),
        _edit([{"op": "set_mouse_input", "value": {**mouse["ops"][0]["value"], "events": []}}]),
        _edit([{"op": "upsert_input_binding", "value": {**binding["ops"][0]["value"], "source": {
            "kind": "button", "button": 0,
        }}}]),
        _edit([{"op": "upsert_input_gate", "value": {**gate["ops"][0]["value"], "lease_id": "lease_1"}}]),
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, tool["inputSchema"])


def test_sequence_schema_advertises_both_projections_and_fingerprint() -> None:
    preflight = _schema("preflight_fractal_sequence")
    export = _schema("export_fractal_sequence")
    control = _schema("control_fractal_sequence")
    jsonschema.validate(_sequence(), preflight["inputSchema"])
    dome = _sequence(width=1024, height=1024, output={
        "projection": "fulldome_angular", "diameter": 1024, "fov_deg": 180,
        "pan": 0, "tilt": 0, "roll": 0,
    })
    jsonschema.validate(dome, preflight["inputSchema"])
    folder = {
        "sequence_id": SEQ, "destination_folder": "/tmp/frames", "format": "PNG",
        "transaction_fingerprint": SHA,
    }
    jsonschema.validate(folder, export["inputSchema"])
    jsonschema.validate({"sequence_id": SEQ, "action": "open_folder", "attempt_id": ATTEMPT, "attempt_revision": 2},
                        control["inputSchema"])
    for bad in (
        _sequence(width=64, height=32),
        _sequence(output={"projection": "fulldome", "diameter": 512, "fov_deg": 180, "pan": 0, "tilt": 0, "roll": 0},
                  width=512, height=512),
        {**folder, "transaction_fingerprint": 4},
        {"sequence_id": SEQ, "action": "retry_export"},
    ):
        schema = export["inputSchema"] if "destination_folder" in bad or bad.get("transaction_fingerprint") == 4 else (
            control["inputSchema"] if bad.get("action") else preflight["inputSchema"])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


def test_remove_driver_posts_exact_live_edit() -> None:
    seen: list[Any] = []

    def opener(req: Any, timeout: Any = None) -> _Response:
        seen.append(req)
        return _Response({"ok": True, "ticket_id": "123e4567-e89b-42d3-a456-426614174000",
                          "status": "pending", "kind": "fractal_edit", "client_request_id": "req_1"})

    args = _edit([{"op": "remove_driver", "target": "fractal.look.ambient"}])
    result = Client(opener=opener).apply_fractal_edits(args)
    assert result["status"] == "pending"
    assert len(seen) == 1
    assert seen[0].get_method() == "POST"
    assert seen[0].full_url.endswith("/api/agent/fractal-actions")
    assert seen[0].get_header("X-hextile-agent") == "mcp"
    assert json.loads(seen[0].data) == {"kind": "fractal_edit", **args}
    client = Client(opener=opener)
    for bad in (
        _edit([{"op": "remove_driver", "target": "fractal.look.ambient", "extra": 1}]),
        _edit([{"op": "remove_driver", "target": f"t{i}"} for i in range(33)]),
    ):
        with pytest.raises(HextileClientError):
            client.apply_fractal_edits(bad)
    assert len(seen) == 1


def test_stale_and_absent_driver_are_forwarded_once() -> None:
    posts: list[Any] = []

    def stale(req: Any, timeout: Any = None) -> _Response:
        posts.append(req)
        raise _http_error(409, "stale_snapshot")

    with pytest.raises(HextileClientError) as exc:
        Client(opener=stale).apply_fractal_edits(_edit([{"op": "remove_driver", "target": "missing.target"}]))
    assert exc.value.status_code == 409
    assert exc.value.receipt["code"] == "stale_snapshot"
    assert len(posts) == 1

    def status(req: Any, timeout: Any = None) -> _Response:
        return _Response({"client_request_id": "req_1", "status": "refused", "kind": "fractal_edit",
                          "reason": "unknown_target"})

    receipt = Client(opener=status).get_fractal_request("req_1")
    assert receipt["reason"] == "unknown_target"
    assert receipt["status"] == "refused"


@pytest.mark.parametrize("reason", ["mouse_required", "input_gate_in_use", "envelope_binding_required"])
def test_dormant_input_reasons_stay_on_the_status_receipt(reason: str) -> None:
    def opener(req: Any, timeout: Any = None) -> _Response:
        assert req.full_url.endswith("/api/agent/fractal-actions/requests/req_1")
        return _Response({"client_request_id": "req_1", "status": "refused", "kind": "fractal_edit", "reason": reason})

    assert Client(opener=opener).get_fractal_request("req_1")["reason"] == reason


def test_input_repair_is_one_atomic_batch() -> None:
    seen: list[Any] = []
    click = MOD
    ops = [
        {"op": "remove_modulator", "id": click},
        {"op": "set_mouse_input", "value": {"schema": 1, "enabled": False, "abi": "hx-mouse/1"}},
        {"op": "remove_input_binding", "id": "fire_key"},
        {"op": "remove_input_gate", "id": "fire"},
    ]

    def opener(req: Any, timeout: Any = None) -> _Response:
        seen.append(req)
        return _Response({"ok": True, "ticket_id": "123e4567-e89b-42d3-a456-426614174000",
                          "status": "pending", "kind": "fractal_edit", "client_request_id": "req_1"})

    jsonschema.validate(_edit(ops), _schema("apply_fractal_edits")["inputSchema"])
    Client(opener=opener).apply_fractal_edits(_edit(ops))
    assert len(seen) == 1
    body = json.loads(seen[0].data)
    assert [op["op"] for op in body["ops"]] == [op["op"] for op in ops]
    assert "arm" not in seen[0].data.decode()
    assert "captured_input" not in seen[0].data.decode()


def test_context_projects_sha_orphans_and_drops_secrets() -> None:
    def opener(req: Any, timeout: Any = None) -> _Response:
        assert req.get_method() == "GET"
        assert "/api/agent/live-context" in req.full_url
        return _Response({"current_surface": {
            "kind": "fractal_workshop",
            "scene_id": SCENE,
            "scene_revision": SHA,
            "draft_generation": 3,
            "source_generation": 1,
            "viewer_generation": 2,
            "renderer_generation": 4,
            "owner_id": "owner",
            "project_context_witness": "proj",
            "registry_targets": ["fractal.camera.yaw"],
            "retained_orphans": [{"target": "fractal.look.ambient", "reason": "retained_driver"}],
            "capabilities": ["animation.sample/1"],
            "suspensions": [{"target": "fractal.camera.yaw", "reason": "human"}],
            "diagnostics": [{"code": "cycle", "target": "fractal.camera.yaw"}],
            "base": {"fractal.camera.yaw": 0.5, "note": "secret"},
            "effective": {"fractal.camera.yaw": 1},
            "header_witness": {"activationToken": "tok", "sessionId": "ses", "subjectKey": "sub"},
            "source": "glsl",
            "path": "/tmp/scene",
            "lease_id": "lease_secret",
        }, "fractal_grant": {"lease_id": "lease_secret", "actor": "external_mcp"}})

    result = Client(opener=opener).get_fractal_context()
    assert result["scene_revision"] == SHA
    assert result["witness"] == _witness()
    assert result["retained_orphans"] == [{"target": "fractal.look.ambient", "reason": "retained_driver"}]
    assert result["capabilities"] == ["animation.sample/1"]
    assert result["base"] == {"fractal.camera.yaw": 0.5}
    blob = json.dumps(result)
    assert "secret" not in blob
    assert "lease_" not in blob
    assert "/tmp" not in blob
    assert "glsl" not in blob


def test_context_keeps_actual_targets_beyond_fixed_channels() -> None:
    # animation_targets_v1.json has these 64 fixed channels before dynamic rows.
    fixed = """
        fractal.look.spread fractal.look.shift fractal.look.ambient fractal.look.fog_dist
        fractal.look.exposure fractal.look.ao fractal.look.shadow_softness fractal.look.shaft_density
        fractal.look.shaft_aniso fractal.look.pal_a.x fractal.look.pal_a.y fractal.look.pal_a.z
        fractal.look.pal_b.x fractal.look.pal_b.y fractal.look.pal_b.z fractal.look.pal_c.x
        fractal.look.pal_c.y fractal.look.pal_c.z fractal.look.pal_d.x fractal.look.pal_d.y
        fractal.look.pal_d.z fractal.look.fog_color.x fractal.look.fog_color.y fractal.look.fog_color.z
        fractal.look.bg_top.x fractal.look.bg_top.y fractal.look.bg_top.z fractal.look.bg_bottom.x
        fractal.look.bg_bottom.y fractal.look.bg_bottom.z fractal.look.color.trap_weight
        fractal.look.color.iter_weight fractal.look.color.radius_weight fractal.look.color.axis_weight
        fractal.look.color.range_lo fractal.look.color.range_hi fractal.look.emission.strength
        fractal.look.emission.center fractal.look.emission.width fractal.look.emission.color.x
        fractal.look.emission.color.y fractal.look.emission.color.z fractal.look.glow.strength
        fractal.look.glow.reach fractal.look.glow.center fractal.look.glow.width
        fractal.look.glow.hue_offset fractal.look.glow.hue_bend fractal.look.glow.white_hot
        fractal.look.glow.color.x fractal.look.glow.color.y fractal.look.glow.color.z
        fractal.look.surface.rim_strength fractal.look.surface.rim_power fractal.look.surface.rim_color.x
        fractal.look.surface.rim_color.y fractal.look.surface.rim_color.z fractal.camera.pos.x
        fractal.camera.pos.y fractal.camera.pos.z fractal.camera.yaw fractal.camera.pitch
        fractal.camera.roll fractal.camera.fov
    """.split()
    dynamic = ["fractal.formula.mandelbulb_power_n.power",
               "fractal.hybrid.0.mandelbulb_power_n.power", "fractal.light.lt_c0ffee000001.az"]
    assert len(fixed) == 64
    targets = fixed + dynamic
    values = {target: float(index) for index, target in enumerate(targets)}

    def opener(req: Any, timeout: Any = None) -> _Response:
        return _Response({"current_surface": {
            "kind": "fractal_workshop", "scene_id": SCENE, "scene_revision": SHA,
            "registry_targets": targets, "base": values, "effective": values,
            "source": "private", "path": "/tmp/scene", "lease_id": "lease_secret",
        }})

    result = Client(opener=opener).get_fractal_context()
    assert result["registry_targets"] == targets
    for target in dynamic:
        assert result["base"][target] == values[target]
        assert result["effective"][target] == values[target]
    assert "private" not in json.dumps(result)
    assert "/tmp/scene" not in json.dumps(result)
    assert "lease_secret" not in json.dumps(result)


def test_integer_revision_is_omitted() -> None:
    def opener(req: Any, timeout: Any = None) -> _Response:
        surface = {
            "kind": "fractal_workshop", "scene_id": SCENE, "scene_revision": 4,
            "draft_generation": 1, "source_generation": 0, "viewer_generation": 0,
            "renderer_generation": 0, "owner_id": "owner", "project_context_witness": "proj",
            "registry_targets": [], "retained_orphans": [],
        }
        return _Response({"current_surface": surface})

    result = Client(opener=opener).get_fractal_context()
    assert "scene_revision" not in result
    assert result["scene_id"] == SCENE


def test_save_keeps_publication_when_ok_is_false_and_does_not_repost() -> None:
    seen: list[Any] = []

    def opener(req: Any, timeout: Any = None) -> _Response:
        seen.append(req)
        return _Response({
            "ok": False, "status": "unknown", "kind": "fractal_save", "client_request_id": "save_1",
            "result": {"scene_id": SCENE, "client_request_id": "save_1", "scene_revision": SHA,
                       "base_scene_revision": BASE},
        })

    args = {"client_request_id": "save_1", "witness": _witness(), "mode": "save",
            "target_scene_id": SCENE, "scene_revision": BASE}
    result = Client(opener=opener).save_fractal_scene(args)
    assert len(seen) == 1
    assert json.loads(seen[0].data)["kind"] == "fractal_save"
    assert json.loads(seen[0].data)["mode"] == "save"
    assert result["ok"] is False
    assert result["result"]["scene_revision"] == SHA
    assert result["result"]["base_scene_revision"] == BASE


def test_lost_save_reconciles_write_status_without_a_second_post() -> None:
    seen: list[Any] = []

    def opener(req: Any, timeout: Any = None) -> _Response:
        seen.append(req)
        if req.get_method() == "POST":
            raise urllib.error.URLError(TimeoutError())
        if req.full_url.endswith("/requests/save_1"):
            raise _http_error(410, "request_expired")
        assert req.full_url.endswith(f"/api/fractal/scenes/{SCENE}/write-status/save_1")
        return _Response({"status": "accepted", "scene_id": SCENE, "revision": SHA})

    args = {"client_request_id": "save_1", "witness": _witness(), "mode": "save_as",
            "target_scene_id": SCENE, "name": "Copy"}
    result = Client(opener=opener).save_fractal_scene(args)
    assert result["result"]["scene_revision"] == SHA
    assert result["status"] == "applied"
    posts = [req for req in seen if req.get_method() == "POST"]
    assert len(posts) == 1
    posted = json.loads(posts[0].data)
    assert posted["mode"] == "save_as"
    assert "scene_revision" not in posted


def test_unknown_write_status_is_not_expiry_or_a_resend() -> None:
    posts = {"n": 0}

    def opener(req: Any, timeout: Any = None) -> Any:
        if req.get_method() == "POST":
            posts["n"] += 1
            raise urllib.error.URLError(TimeoutError())
        if "/write-status/" in req.full_url:
            return _Response({"status": "unknown", "scene_id": SCENE, "revision": None})
        raise _http_error(410, "request_expired")

    with pytest.raises(HextileClientError) as exc:
        Client(opener=opener).save_fractal_scene({
            "client_request_id": "save_1", "witness": _witness(), "mode": "save",
            "target_scene_id": SCENE, "scene_revision": SHA,
        })
    assert posts["n"] == 1
    assert exc.value.kind == "fractal_outcome_unknown"
    assert exc.value.receipt["status"] == "unknown"
    assert exc.value.receipt["client_request_id"] == "save_1"
    assert "expired" not in exc.value.receipt.get("reason", "")


def test_changed_request_id_is_conflict() -> None:
    def opener(req: Any, timeout: Any = None) -> _Response:
        raise _http_error(409, "idempotency_conflict")

    with pytest.raises(HextileClientError) as exc:
        Client(opener=opener).open_fractal_scene({
            "client_request_id": "open_1", "witness": _witness(), "scene_id": OTHER,
        })
    assert exc.value.status_code == 409
    assert exc.value.receipt["code"] == "idempotency_conflict"


def test_request_expiry_is_unknown_not_refusal() -> None:
    def opener(req: Any, timeout: Any = None) -> _Response:
        raise _http_error(410, "request_expired")

    with pytest.raises(HextileClientError) as exc:
        Client(opener=opener).get_fractal_request("req_1")
    assert exc.value.kind == "fractal_outcome_unknown"
    assert exc.value.receipt == {"client_request_id": "req_1", "status": "unknown"}


def test_sequence_preflight_keeps_dimensions_and_refuses_geometry() -> None:
    seen: list[Any] = []

    def opener(req: Any, timeout: Any = None) -> _Response:
        seen.append(req)
        if req.full_url.endswith("/preflight"):
            return _Response({
                "frame_count": 2, "source_width": 256, "source_height": 128,
                "output_width": 128, "output_height": 64, "render_identity": SHA,
            })
        raise AssertionError(req.full_url)

    result = Client(opener=opener).preflight_fractal_sequence(_sequence())
    assert result == {
        "frame_count": 2, "source_width": 256, "source_height": 128,
        "output_width": 128, "output_height": 64, "render_identity": SHA,
    }
    assert seen[0].full_url.endswith("/api/fractal/sequences/preflight")
    client = Client(opener=opener)
    for bad in (_sequence(width=130, height=64), _sequence(width=100, height=50),
                _sequence(width=1000, height=1000, output={
                    "projection": "fulldome", "diameter": 1024, "fov_deg": 180,
                    "pan": 0, "tilt": 0, "roll": 0})):
        with pytest.raises(HextileClientError) as exc:
            client.preflight_fractal_sequence(bad)
        assert exc.value.receipt["code"] == "geometry"
    assert len(seen) == 1


def test_sequence_resource_refusal_and_lost_render_do_not_mint() -> None:
    def refuse(req: Any, timeout: Any = None) -> _Response:
        raise _http_error(422, {"code": "resource_unavailable"})

    with pytest.raises(HextileClientError) as exc:
        Client(opener=refuse).preflight_fractal_sequence(_sequence())
    assert exc.value.receipt["code"] == "resource_unavailable"

    posts = {"n": 0}

    def lost(req: Any, timeout: Any = None) -> _Response:
        posts["n"] += 1
        raise urllib.error.URLError(TimeoutError())

    with pytest.raises(HextileClientError) as lost_exc:
        Client(opener=lost).render_fractal_sequence(_sequence())
    assert posts["n"] == 1
    assert lost_exc.value.kind == "fractal_sequence_outcome_unknown"
    assert lost_exc.value.receipt["status"] == "unknown"
    assert lost_exc.value.receipt["sequence_id"] == "seq_" + __import__("hashlib").sha256(
        f"fractal-sequence/1\0{SCENE}\0job-1".encode()).hexdigest()[:32]


def test_render_acknowledgement_binds_known_identity() -> None:
    body = _sequence()
    known = Client._fractal_sequence_identity(body)
    posts: list[Any] = []

    def success(req: Any, timeout: Any = None) -> _Response:
        if req.get_method() == "POST" and req.full_url.endswith("/api/fractal/sequences"):
            posts.append(req)
        return _Response({**known, "status": "queued", "recipe_hash": SHA})

    assert Client(opener=success).render_fractal_sequence(body) == {
        **known, "status": "queued", "recipe_hash": SHA,
    }
    assert len(posts) == 1

    for foreign in ({"sequence_id": SEQ}, {"request_hash": BASE}):
        posts.clear()

        def mismatch(req: Any, timeout: Any = None) -> _Response:
            if req.get_method() == "POST" and req.full_url.endswith("/api/fractal/sequences"):
                posts.append(req)
            return _Response({**known, **foreign, "status": "queued"})

        with pytest.raises(HextileClientError) as exc:
            Client(opener=mismatch).render_fractal_sequence(body)
        assert exc.value.kind == "fractal_sequence_outcome_unknown"
        assert exc.value.receipt == {"status": "unknown", **known}
        assert len(posts) == 1


def test_known_sequence_404_stays_unknown() -> None:
    def opener(req: Any, timeout: Any = None) -> _Response:
        assert req.full_url.endswith(f"/api/fractal/sequences/{SEQ}")
        raise _http_error(404, {"code": "fractal_sequence_missing"})

    with pytest.raises(HextileClientError) as exc:
        Client(opener=opener).get_fractal_sequence(SEQ)
    assert exc.value.kind == "fractal_sequence_outcome_unknown"
    assert exc.value.receipt["sequence_id"] == SEQ
    assert exc.value.receipt["status"] == "unknown"


def test_export_preflight_requires_fingerprint_and_drops_paths() -> None:
    def opener(req: Any, timeout: Any = None) -> _Response:
        assert req.full_url.endswith(f"/api/fractal/sequences/{SEQ}/export/preflight")
        body = json.loads(req.data)
        assert "transaction_fingerprint" not in body
        return _Response({
            "sequence_id": SEQ, "attempt_id": ATTEMPT, "transaction_fingerprint": SHA,
            "status": "ready", "recipe_hash": BASE, "destination_folder": "/tmp/secret",
        })

    result = Client(opener=opener).preflight_fractal_sequence_export({
        "sequence_id": SEQ, "destination_folder": "/tmp/frames", "format": "PNG",
    })
    assert result["transaction_fingerprint"] == SHA
    assert "destination_folder" not in result
    assert "/tmp" not in json.dumps(result)

    def missing(req: Any, timeout: Any = None) -> _Response:
        return _Response({"sequence_id": SEQ, "status": "ready"})

    with pytest.raises(HextileClientError):
        Client(opener=missing).preflight_fractal_sequence_export({
            "sequence_id": SEQ, "destination_folder": "/tmp/frames", "format": "JPG", "quality_if_jpg": 90,
        })


def test_export_posts_fingerprint_and_control_routes() -> None:
    seen: list[tuple[str, str, Any]] = []

    def opener(req: Any, timeout: Any = None) -> _Response:
        body = json.loads(req.data) if req.data else None
        seen.append((req.get_method(), req.full_url, body))
        if req.full_url.endswith("/export") and not req.full_url.endswith("/export/preflight"):
            return _Response({"sequence_id": SEQ, "attempt_id": ATTEMPT, "transaction_fingerprint": SHA,
                              "status": "queued", "recipe_hash": BASE, "destination_folder": "/tmp/secret"})
        if req.full_url.endswith("/export/open-folder"):
            return _Response({"sequence_id": SEQ, "path": "/tmp/secret"})
        return _Response({"sequence_id": SEQ, "status": "queued",
                          "folder_export": {"attempt_id": ATTEMPT, "status": "queued",
                                            "destination_folder": "/tmp/secret"}})

    client = Client(opener=opener)
    exported = client.export_fractal_sequence({
        "sequence_id": SEQ, "destination_folder": "/tmp/frames", "format": "PNG",
        "transaction_fingerprint": SHA,
    })
    assert exported["transaction_fingerprint"] == SHA
    assert "/tmp" not in json.dumps(exported)
    opened = client.control_fractal_sequence({
        "sequence_id": SEQ, "action": "open_folder", "attempt_id": ATTEMPT, "attempt_revision": 2,
    })
    assert opened["action"] == "open_folder"
    assert "path" not in opened
    client.control_fractal_sequence({"sequence_id": SEQ, "action": "stop"})
    client.control_fractal_sequence({
        "sequence_id": SEQ, "action": "retry_export", "attempt_id": ATTEMPT, "attempt_revision": 2,
    })
    assert [item[1].rsplit("/api/fractal/sequences/", 1)[1] for item in seen] == [
        f"{SEQ}/export", f"{SEQ}/export/open-folder", f"{SEQ}/stop", f"{SEQ}/export/retry",
    ]
    assert seen[2][2] is None
    assert seen[3][2] == {"attempt_id": ATTEMPT, "attempt_revision": 2}


def test_server_dispatches_context_without_a_private_file_read() -> None:
    client = type("C", (), {})()
    client.get_fractal_context = lambda: {"scene_id": SCENE, "scene_revision": SHA}
    assert HextileMcpServer(client=client)._get_fractal_context({})["scene_revision"] == SHA


@pytest.mark.parametrize("ops", [
    [{"op": "upsert_input_gate", "value": {"id": "fire", "alias": "Fire", "label": "Fire", "mode": "gate", "unit": "gate", "min": 0, "max": 1, "default": 0, "arm": True}}],
    [{"op": "upsert_input_binding", "value": {"id": "fire_key", "channel_id": "fire", "source": {"kind": "key", "code": "KeyF", "modifiers": ["Super"]}, "scale": 1, "activation": "interact"}}],
])
def test_dispatch_rejects_nested_extra_fields_before_post(ops: list[Any]) -> None:
    seen = []
    def opener(req: Any, timeout: Any = None) -> _Response:
        seen.append(req)
        return _Response({"enabled": True})
    result = HextileMcpServer(client=Client(opener=opener)).call_tool("apply_fractal_edits", _edit(ops))
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["status_code"] == 422
    assert not any(req.full_url.endswith("/api/agent/fractal-actions") for req in seen)


def test_sequence_detail_keeps_current_attempt_for_controls_without_paths() -> None:
    attempt = {"attempt_id": ATTEMPT, "attempt_revision": 4, "status": "running", "frames_completed": 1,
               "transaction_fingerprint": SHA, "destination_folder": "/tmp/secret"}
    def opener(req: Any, timeout: Any = None) -> _Response:
        return _Response({"sequence_id": SEQ, "status": "completed", "frame_rate": {"num": 30000, "den": 1001},
                          "start_time": {"num": "0", "den": "1"}, "end_time": {"num": "1001", "den": "15000"},
                          "attempt_id": ATTEMPT, "attempt_revision": 4, "folder_export": attempt,
                          "publication_attempts": [attempt], "frames_completed": 2, "frames_failed": 0})
    result = Client(opener=opener).get_fractal_sequence(SEQ)
    assert result["attempt_id"] == ATTEMPT
    assert result["attempt_revision"] == 4
    assert result["folder_export"]["attempt_revision"] == 4
    assert result["publication_attempts"] == [result["folder_export"]]
    assert result["frame_rate"] == {"num": 30000, "den": 1001}
    assert result["start_time"] == {"num": "0", "den": "1"}
    assert "/tmp" not in json.dumps(result)


def test_export_receipt_keeps_actual_producer_attempt_revision() -> None:
    def opener(req: Any, timeout: Any = None) -> _Response:
        return _Response({"sequence_id": SEQ, "attempt_id": ATTEMPT, "attempt_revision": 0,
                          "transaction_fingerprint": SHA, "recipe_hash": BASE, "status": "queued"})
    result = Client(opener=opener).export_fractal_sequence({"sequence_id": SEQ, "destination_folder": "/tmp/frames",
                                                        "format": "PNG", "transaction_fingerprint": SHA})
    assert result["attempt_revision"] == 0


@pytest.mark.parametrize("over", [
    {"frame_rate": {"num": 60, "den": 2}},
    {"frame_rate": {"num": 121, "den": 1}},
    {"start_time": {"num": "0", "den": "2"}},
    {"end_time": {"num": "1", "den": "1"}},
])
def test_exact_sequence_range_refuses_before_http(over: dict[str, Any]) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> _Response:
        raise AssertionError("invalid exact time reached HTTP")
    with pytest.raises(HextileClientError) as exc:
        Client(opener=unexpected).preflight_fractal_sequence(_sequence(**over))
    assert exc.value.status_code == 422
