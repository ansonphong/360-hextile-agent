"""get_shader_context and propose_shader are thin proxies, not apply or import."""

from __future__ import annotations

import json
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
    client.propose_shader({
        "kind": "shader_document_candidate",
        "base": {"shader": {"origin": "user", "shaderId": "aurora"}},
    })
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
        include_source=False, working_revision=None,
    )
    identity = server._propose_shader({
        "kind": "shader_document_candidate", "base": {"shader": {"origin": "user", "shaderId": "aurora"}},
    })
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
