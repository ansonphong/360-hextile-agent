#!/usr/bin/env python3
"""stdio MCP proxy for 360 Hextile (stdlib only — no pip).

JSON-RPC 2.0 over newline-delimited stdin/stdout.
Talks only to http://127.0.0.1:8000. No app logic, no local merge authority.

Catalog + persist + run + monitor + config + seed + models + guides + live context + live apply + sequences + batch.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
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
SERVER_VERSION = "0.3.0"
ACTIVITY_SCHEMA = "hextile.agent.activity.v1"
CHILD_TOKEN_ENV = "HEXTILE_MCP_CHILD_TOKEN"

# Canonical tool names — drift tests assert SKILL.md ⊆ this list.
TOOL_NAMES = (
    "list_workflows",
    "get_workflow",
    "get_capabilities",
    "describe_image",
    "get_live_context",
    "apply_config_delta",
    "save_workflow",
    "delete_workflow",
    "run_workflow",
    "validate_config",
    "get_status",
    "get_gpu_diagnostics",
    "get_seed_memory_advice",
    "get_render_config",
    "get_logs",
    "list_runs",
    "search_library_prompts",
    "cancel_run",
    "retry_run",
    "generate_seed",
    "get_seed_job",
    "list_seed_history",
    "get_seed_batch",
    "cancel_seed",
    "list_360_loras",
    "list_installed_models",
    "get_model_readiness",
    "get_model_download_queue",
    "install_model",
    "repair_model",
    "cancel_model_download",
    "get_guide",
    "extract_sequence_video",
    "create_sequence",
    "start_sequence",
    "get_sequence",
    "list_sequences",
    "stop_sequence",
    "preflight_batch",
    "get_batch_preflight",
    "start_batch",
    "list_batches",
    "get_batch",
    "get_batch_items",
    "pause_batch",
    "resume_batch",
    "cancel_batch",
    "retry_batch",
    "import_batch_outputs",
    "get_unreal_export_catalog",
    "preflight_unreal_export",
    "export_to_unreal_project",
    "preflight_file_export",
    "export_render_file",
    "get_layer_draft",
    "generate_layer",
    "get_layer_generation",
    "cancel_layer_generation",
    "rematte_layer",
    "land_generated_layer",
    "commit_layer_draft",
    "preflight_spot_clone",
    "clone_spot",
    "get_spot_clone",
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
        "get_gpu_diagnostics",
        "get_seed_memory_advice",
        "get_render_config",
        "get_logs",
        "list_runs",
        "search_library_prompts",
        "list_seed_history",
        "get_seed_job",
        "get_seed_batch",
        "list_360_loras",
        "list_installed_models",
        "get_model_readiness",
        "get_model_download_queue",
        "get_guide",
        "get_sequence",
        "list_sequences",
        "get_batch_preflight",
        "list_batches",
        "get_batch",
        "get_batch_items",
        "get_unreal_export_catalog",
        "preflight_unreal_export",
        "preflight_file_export",
        "get_layer_draft",
        "get_layer_generation",
        "preflight_spot_clone",
        "get_spot_clone",
    }
)
_MUTATING = frozenset(
    {
        "apply_config_delta",
        "save_workflow",
        "delete_workflow",
        "run_workflow",
        "describe_image",
        "generate_seed",
        "cancel_run",
        "retry_run",
        "cancel_seed",
        "install_model",
        "repair_model",
        "cancel_model_download",
        "extract_sequence_video",
        "create_sequence",
        "start_sequence",
        "stop_sequence",
        "preflight_batch",
        "start_batch",
        "pause_batch",
        "resume_batch",
        "cancel_batch",
        "retry_batch",
        "import_batch_outputs",
        "export_to_unreal_project",
        "export_render_file",
        "generate_layer",
        "cancel_layer_generation",
        "rematte_layer",
        "land_generated_layer",
        "commit_layer_draft",
        "clone_spot",
    }
)
_BATCH_TOOLS = frozenset(
    {
        "preflight_batch",
        "get_batch_preflight",
        "start_batch",
        "list_batches",
        "get_batch",
        "get_batch_items",
        "pause_batch",
        "resume_batch",
        "cancel_batch",
        "retry_batch",
        "import_batch_outputs",
    }
)
_DESTRUCTIVE = frozenset(
    {
        "cancel_run",
        "cancel_seed",
        "delete_workflow",
        "stop_sequence",
        "cancel_batch",
        "cancel_model_download",
        "cancel_layer_generation",
    }
)
_CONTROL_TOOLS = frozenset(
    {
        "get_status",
        "get_seed_job",
        "get_logs",
        "get_sequence",
        "get_batch",
        "get_batch_items",
        "cancel_run",
        "cancel_seed",
        "stop_sequence",
        "cancel_batch",
        "cancel_model_download",
        "get_layer_generation",
        "cancel_layer_generation",
    }
)
_LAYER_TOOLS = frozenset({
    "get_layer_draft", "generate_layer", "get_layer_generation", "cancel_layer_generation",
    "rematte_layer", "land_generated_layer", "commit_layer_draft",
})
_SPOT_CLONE_TOOLS = frozenset({"preflight_spot_clone", "clone_spot", "get_spot_clone"})
_PRIVATE_ARGUMENT_KEYS = frozenset(
    {
        "actor",
        "operation_id",
        "hextile_op",
        "token",
        "x-hextile-copilot-token",
        "x-hextile-op",
    }
)
_FILE_EXPORT_OPTIONS = frozenset({
    "layout", "width", "height", "fov", "pan", "tilt", "roll",
    "jpeg_quality", "include_360_metadata",
})
_FILE_EXPORT_REQUIRED = (
    "render_id", "source_node_id", "folder_path", "output_name",
    "projection", "encoding",
)
_WINDOWS_DEVICE_STEM = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(?:\.|$)",
    re.IGNORECASE,
)
PREFLIGHT_POLL_TIMEOUT_S = 60.0
PREFLIGHT_POLL_INTERVAL_S = 2.0
PREFLIGHT_POLL_BACKOFF_CAP_S = 10.0

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
    if name in _DESTRUCTIVE:
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


_FILE_EXPORT_PROPERTIES: dict[str, Any] = {
    "render_id": {"type": "string"},
    "source_node_id": {"type": "string"},
    "folder_path": {"type": "string", "description": "Selected existing local folder"},
    "output_name": {"type": "string", "description": "Windows-safe basename only"},
    "projection": {"type": "string", "enum": [
        "equirectangular", "cubemap_cross", "fulldome", "fulldome_angular",
        "rectilinear", "stereographic",
    ]},
    "encoding": {"type": "string", "enum": ["png", "jpg", "hdr", "exr"]},
    "layout": {"type": "string", "enum": ["cross", "six_faces"]},
    "width": {"type": "integer"},
    "height": {"type": "integer"},
    "fov": {"type": "number"},
    "pan": {"type": "number"},
    "tilt": {"type": "number"},
    "roll": {"type": "number"},
    "jpeg_quality": {"type": "integer", "minimum": 1, "maximum": 100},
    "include_360_metadata": {"type": "boolean"},
}


def _layer_object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


_LAYER_ID = {"type": "string", "pattern": "^[0-9a-f]{12}([0-9a-f]{20})?$"}
_LAYER_JOB_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
_LAYER_TARGET = {"type": "string", "enum": ["saved", "live"]}
_LAYER_ALPHA = _layer_object({"mode": {"type": "string", "enum": ["auto-cutout", "opaque"]}}, ("mode",))
_LAYER_SCENE = _layer_object({
    "enabled": {"type": "boolean"}, "strength": {"type": "number", "minimum": 0.3, "maximum": 1},
    "yaw": {"type": "number", "minimum": -180, "maximum": 180},
    "pitch": {"type": "number", "minimum": -90, "maximum": 90},
    "fov": {"type": "number", "minimum": 20, "maximum": 90},
}, ("enabled",))
_LAYER_PROMPT = _layer_object({
    "global": {"type": "string", "minLength": 1, "maxLength": 2000},
    "negative": {"type": "string", "maxLength": 2000},
}, ("global",))
_LAYER_OUTPUT = _layer_object({
    "width": {"type": "integer", "enum": [1024, 1152, 896]},
    "height": {"type": "integer", "enum": [1024, 1152, 896]},
}, ("width", "height"))
_LAYER_DIFFUSION = _layer_object({
    "model": {"type": "string", "minLength": 1, "maxLength": 256},
    "quantization": {"type": "string", "minLength": 1, "maxLength": 64},
    "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295},
    "scheduler": {"type": "string", "minLength": 1, "maxLength": 64},
    "guidance_scale": {"type": "number", "minimum": 0, "maximum": 100},
    "num_inference_steps": {"type": "integer", "minimum": 1, "maximum": 500},
    "lora": _layer_object({
        "enabled": {"type": "boolean"},
        "models": {"type": "array", "maxItems": 32, "items": _layer_object({
            "model_path": {"type": "string", "minLength": 1, "maxLength": 512},
            "strength": {"type": "number"}, "enabled": {"type": "boolean"},
        }, ("model_path", "strength", "enabled"))},
        "settings": _layer_object({"combine_mode": {"type": "string", "enum": ["additive", "normalized"]}}, ("combine_mode",)),
    }),
}, ("model", "quantization", "scheduler", "guidance_scale", "num_inference_steps"))
_LAYER_GEN_SETTINGS = _layer_object({
    "scene": _LAYER_SCENE, "include_global": {"type": "boolean"},
    "include_style": {"type": "boolean"}, "include_directional": {"type": "boolean"},
    "include_semantic": {"type": "boolean"}, "modularity": {"type": "boolean"},
}, ("scene",))
_LAYER_CONTROLS = _layer_object({
    "pipeline": {"type": "string", "minLength": 1, "maxLength": 64},
    "prompt": _LAYER_PROMPT, "diffusion": _LAYER_DIFFUSION, "output": _LAYER_OUTPUT,
    "alpha": _LAYER_ALPHA, "layer_gen": _LAYER_GEN_SETTINGS,
}, ("pipeline", "prompt", "diffusion", "output", "alpha", "layer_gen"))
_LAYER_EDITS = _layer_object({
    "pipeline": {"type": "string", "minLength": 1, "maxLength": 64},
    "prompt": _LAYER_PROMPT, "diffusion": _LAYER_DIFFUSION,
    "output": _LAYER_OUTPUT, "alpha": _LAYER_ALPHA,
    "layer_gen": _LAYER_GEN_SETTINGS,
    "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295},
})
_LAYER_POSE = _layer_object({
    "yaw": {"type": "number", "minimum": -180, "maximum": 180},
    "pitch": {"type": "number", "minimum": -90, "maximum": 90},
    "roll": {"type": "number", "minimum": -180, "maximum": 180},
    "fov": {"type": "number", "minimum": 5, "maximum": 360},
    "projection": {"type": "string", "enum": ["auto"]},
}, ("yaw", "pitch", "roll", "fov"))
_LAYER_RECOVERY_TARGET = _layer_object({
    "layer_id": {"type": "string", "maxLength": 64},
    "asset": {"type": "string"}, "recipeSource": {"type": "string"},
    "takes_fingerprint": {"type": "string", "maxLength": 4096},
}, ("layer_id", "asset", "recipeSource", "takes_fingerprint"))

_CLONE_NODE_ID = {"type": "string", "pattern": "^[0-9a-f]{12}$"}
_CLONE_PARENT_ID = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"}
_CLONE_FINGERPRINT = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_CLONE_POSE = _layer_object({
    "yaw": {"type": "number", "minimum": -180, "maximum": 180},
    "pitch": {"type": "number", "minimum": -90, "maximum": 90},
    "roll": {"type": "number", "minimum": -180, "maximum": 180},
}, ("yaw", "pitch"))
_CLONE_DESTINATION_POSE = _layer_object({
    **_CLONE_POSE["properties"], "fov": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 180},
}, ("yaw", "pitch", "roll", "fov"))
_CLONE_MASK_COMMON = {
    "blend_clip": {"type": "number", "minimum": 0, "maximum": 1},
    "blend_blur": {"type": "integer", "minimum": 0, "maximum": 128},
}
_CLONE_MASK = {
    "oneOf": [
        _layer_object({
            **_CLONE_MASK_COMMON, "source": {"const": "shape"},
            "shape": {"type": "string", "enum": ["triangle", "square", "pentagon", "hexagon", "kite", "rhombus", "circle"]},
            "sides": {"type": "integer", "minimum": 3, "maximum": 64},
            "radius": {"type": "number", "exclusiveMinimum": 0, "maximum": 1024},
            "roll": {"type": "number", "minimum": -180, "maximum": 180},
            "aspect": {"type": "number", "minimum": 0.1, "maximum": 10},
            "corner_radius": {"type": "number", "minimum": 0, "maximum": 1},
            "inpaint_blur": {"type": "integer", "minimum": 0, "maximum": 255},
        }, ("source",)),
        _layer_object({
            **_CLONE_MASK_COMMON, "source": {"const": "from_template"},
            "template_id": _CLONE_PARENT_ID,
            "face_index": {"type": "integer", "minimum": 0, "maximum": 4095},
        }, ("source", "template_id")),
        _layer_object({
            **_CLONE_MASK_COMMON, "source": {"const": "from_raster"},
            "raster_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}\\.png$"},
            "grow_px": {"type": "integer", "minimum": -64, "maximum": 64},
            "inpaint_blur": {"type": "integer", "minimum": 0, "maximum": 255},
        }, ("source", "raster_id")),
    ],
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
        "describe_image",
        "Describe one exact local image or equirectangular crop using APP's local vision. "
        "Requires approval for local compute/GPU; returns text, never image bytes. "
        "Allow up to 300 seconds. No URL, base64, or current-selection fallback.",
        {
            "source": {
                "oneOf": [
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "render_node"},
                            "render_id": {"type": "string", "minLength": 1},
                            "node_id": {"type": "string", "minLength": 1},
                        },
                        "required": ["kind", "render_id", "node_id"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "local_file"},
                            "path": {"type": "string", "minLength": 1},
                        },
                        "required": ["kind", "path"],
                        "additionalProperties": False,
                    },
                ],
            },
            "crop": {
                "type": "object",
                "properties": {
                    "yaw": {"type": "number", "minimum": -180, "maximum": 180},
                    "pitch": {"type": "number", "minimum": -90, "maximum": 90},
                    "fov": {"type": "number", "minimum": 1, "maximum": 150},
                },
                "required": ["yaw", "pitch", "fov"],
                "additionalProperties": False,
            },
        },
        required=["source"],
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
        "get_gpu_diagnostics",
        "Read the app's current GPU/VRAM status without changing GPU state. "
        "NVML card/process values may be cached; inspect sample time and age. "
        "Device use, process attribution, allocator pools, and model footprints "
        "overlap; unknown values stay unknown. This does not predict whether a "
        "particular workload will fit.",
        {},
    ),
    _tool_def(
        "get_seed_memory_advice",
        "Read separate GPU status and APP memory-pressure advice for one listed "
        "360-LoRA resolution. The GPU sample age applies only to gpu; advice "
        "samples memory independently and neither result guarantees job fit.",
        {
            "lora_path": {"type": "string", "description": "Relative path from list_360_loras"},
            "width": {"type": "integer", "description": "Listed preset width"},
            "height": {"type": "integer", "description": "Listed preset height"},
            "cpu_offload": {"type": "boolean", "description": "Estimate with explicit CPU offload; default false"},
        },
        required=["lora_path", "width", "height"],
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
        "search_library_prompts",
        "Search saved global and tile prompts in render and sequence library rows. "
        "Returns exact IDs, snippets, and a cursor for the next bounded page. Read-only.",
        {
            "query": {"type": "string", "minLength": 2, "maxLength": 256},
            "kinds": {
                "type": "array",
                "items": {"type": "string", "enum": ["render", "sequence"]},
                "minItems": 1,
                "maxItems": 2,
            },
            "lifecycle_status": {
                "type": "string",
                "enum": ["active", "archived", "trashed"],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "cursor": {"type": "string", "maxLength": 1024},
        },
        required=["query"],
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
        "APP returns 400 otherwise. Uses APP tile-reuse policy. Optional "
        "vram_recovery=true may evict idle in-app GPU holders for this exact "
        "render; request and approve that effect explicitly. Omitted/false "
        "is an ordinary retry.",
        {
            "run_id": {"type": "string", "description": "Render id to retry"},
            "vram_recovery": {
                "type": "boolean",
                "description": "Explicitly allow guarded idle-holder eviction for this render only. Default false.",
                "default": False,
            },
        },
        required=["run_id"],
    ),
    _tool_def(
        "generate_seed",
        "Submit one 360-LoRA seed job with a caller-supplied UUID. Returns a "
        "short acknowledgement with job_id; poll get_seed_job for original-index "
        "variation paths, seeds, and terminal errors.",
        {
            "request_id": {
                "type": "string",
                "format": "uuid",
                "description": "Caller-supplied canonical UUID; reuse it with the same body after an uncertain acknowledgement",
            },
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
            "resolution_preset": {
                "type": "string",
                "description": "Exact listed widthxheight preset for this LoRA; APP validates it",
            },
            "cpu_offload": {
                "type": "boolean",
                "description": "Explicit CPU offload for installed SDXL/SD1.5 weights only",
            },
            "allow_low_vram": {
                "type": "boolean",
                "description": "Explicit request-local low-free-memory admission override; never inferred from advice",
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
        required=["request_id", "prompt", "lora_path", "base_model"],
    ),
    _tool_def(
        "get_seed_job",
        "Read one exact 360-LoRA job (GET /api/360-lora/jobs/{job_id}). "
        "Returns status, progress, original-index completed variations with "
        "paths and seeds, variation errors, and typed terminal error.",
        {
            "job_id": {"type": "string", "description": "Job id from generate_seed"},
        },
        required=["job_id"],
    ),
    _tool_def(
        "list_seed_history",
        "List stored 360-LoRA seed batches (GET /api/360-lora/history). "
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
        "Read one stored 360-LoRA seed batch (GET /api/360-lora/history/{batch_id}).",
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
        "Cancel only the named 360-LoRA job (POST /api/360-lora/jobs/{job_id}/cancel). "
        "Use cancel_run for renders.",
        {
            "job_id": {"type": "string", "description": "Exact seed job id to cancel"},
        },
        required=["job_id"],
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
        "get_model_readiness",
        "Read current model fit, availability, and install bundle from the app. "
        "Fit estimates total card capacity, not current free VRAM. Overrides are exploratory only.",
        {"overrides": {"type": "array", "maxItems": 32, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "pipeline_id": {"type": "string", "minLength": 1},
                "model_id": {"type": "string", "minLength": 1},
                "quantization": {"type": "string", "enum": [
                    "fp16", "bf16", "fp8", "nf4", "int8", "none", "bnb_nf4",
                    "bnb_4bit", "bnb_8bit", "gguf_q4", "gguf_q8", "quanto_int8",
                ]},
                "speed_mode": {"type": "string", "enum": ["quality", "turbo", "max_speed"]},
                "text_encoder_precision": {"type": "string", "enum": ["auto", "nf4", "bf16"]},
            },
            "required": ["pipeline_id", "model_id"],
        }}},
    ),
    _tool_def(
        "get_model_download_queue",
        "Read the app's active and queued model downloads, including queue_entry_id for exact cancellation.",
        {},
    ),
    _tool_def(
        "install_model",
        "Queue one registry model, or its current default-catalog selected bundle. "
        "Bundle requires the server-issued selection fingerprint; host approval is required.",
        {
            "pipeline_id": {"type": "string", "minLength": 1},
            "model_id": {"type": "string", "minLength": 1},
            "bundle": {"type": "boolean"},
            "expected_selection_fingerprint": {"type": "string", "minLength": 1},
        },
        required=["pipeline_id", "model_id", "bundle"],
    ),
    _tool_def(
        "repair_model",
        "Ask the app to diagnose and queue repair for one registry model. Host approval is required.",
        {"pipeline_id": {"type": "string", "minLength": 1},
         "model_id": {"type": "string", "minLength": 1}},
        required=["pipeline_id", "model_id"],
    ),
    _tool_def(
        "cancel_model_download",
        "Request cancellation of only the observed queue admission. "
        "A successful request may still be draining; observe the queue. Host approval is required.",
        {"pipeline_id": {"type": "string", "minLength": 1},
         "model_id": {"type": "string", "minLength": 1},
         "expected_queue_entry_id": {"type": "string", "minLength": 1}},
        required=["pipeline_id", "model_id", "expected_queue_entry_id"],
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
    _tool_def(
        "extract_sequence_video",
        "Extract video frames to a folder (POST /api/sequences/extract-video). "
        "Returns folder_path for create_sequence. "
        "Suffixes .mp4 .mpg .mpeg .m4v .mov. Not run_workflow.",
        {
            "video_path": {
                "type": "string",
                "description": "Absolute path to a video file",
            },
        },
        required=["video_path"],
    ),
    _tool_def(
        "create_sequence",
        "Create a seq_* from a folder of stills (POST /api/sequences/create). "
        "config is a full HextileConfig (clone get_workflow upres-still; "
        "never a partial). run_workflow cannot create a seq_*.",
        {
            "folder_path": {
                "type": "string",
                "description": "Absolute folder of stills (or extract_sequence_video folder_path)",
            },
            "config": {
                "type": "object",
                "description": (
                    "Full HextileConfig document (pipeline, hextile.template, "
                    "diffusion.model, input, …). Not a partial."
                ),
            },
            "name": {
                "type": "string",
                "description": "Optional sequence display name",
            },
        },
        required=["folder_path", "config"],
    ),
    _tool_def(
        "start_sequence",
        "Start GPU processing of an existing sequence "
        "(POST /api/sequences/{sequence_id}/start).",
        {
            "sequence_id": {
                "type": "string",
                "description": "Sequence id (seq_*) from create_sequence",
            },
        },
        required=["sequence_id"],
    ),
    _tool_def(
        "get_sequence",
        "Poll sequence status by sequence_id (GET /api/sequences/{id}). "
        "Never pass a seq_* to get_status.",
        {
            "sequence_id": {
                "type": "string",
                "description": "Sequence id (seq_*)",
            },
        },
        required=["sequence_id"],
    ),
    _tool_def(
        "list_sequences",
        "List sequences (monitor without a stored sequence_id). "
        "GET /api/sequences/?lifecycle_status= (default active).",
        {
            "lifecycle_status": {
                "type": "string",
                "description": "active | archived | trashed",
                "default": "active",
            },
        },
    ),
    _tool_def(
        "stop_sequence",
        "Stop a running sequence (POST /api/sequences/{id}/stop). "
        "Status → cancelled. Destructive. Not cancel_run.",
        {
            "sequence_id": {
                "type": "string",
                "description": "Sequence id (seq_*) to stop",
            },
        },
        required=["sequence_id"],
    ),
    _tool_def(
        "preflight_batch",
        "Create a Batch preflight (POST /api/batch-workflows/preflight). "
        "Returns promptly; poll get_batch_preflight for spec_hash and issues. "
        "Does not start GPU work. Paths are APP filesystem paths, not the MCP host.",
        {
            "idempotency_key": {
                "type": "string",
                "description": "Client request UUID (idempotent create)",
            },
            "workflow": {
                "type": "object",
                "additionalProperties": False,
                "description": (
                    "kind=document + document, or kind=catalog + id + origin "
                    "(builtin|user|project)"
                ),
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["document", "catalog"],
                    },
                    "document": {
                        "type": "object",
                        "description": "Full hextile.workflow document",
                    },
                    "id": {"type": "string", "description": "Catalog workflow id"},
                    "origin": {
                        "type": "string",
                        "enum": ["builtin", "user", "project"],
                    },
                },
                "required": ["kind"],
            },
            "input": {
                "type": "object",
                "additionalProperties": False,
                "description": "kind=folder + path, or kind=files + paths[]",
                "properties": {
                    "kind": {"type": "string", "enum": ["folder", "files"]},
                    "path": {"type": "string", "description": "Folder path (kind=folder)"},
                    "recursive": {"type": "boolean", "default": True},
                    "include_hidden": {"type": "boolean", "default": False},
                    "preserve_relative_dirs": {"type": "boolean", "default": True},
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit file list (kind=files)",
                    },
                },
                "required": ["kind"],
            },
            "output": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "directory": {"type": "string"},
                    "format": {"type": "string", "description": "png | jpg | …"},
                    "name_pattern": {
                        "type": "string",
                        "default": "{stem}",
                        "description": "Tokens: {stem} {index} {workflow}",
                    },
                    "collision": {
                        "type": "string",
                        "enum": ["skip", "fail", "version", "overwrite"],
                        "default": "skip",
                    },
                },
                "required": ["directory", "format"],
            },
            "loss_policy": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "allow_alpha_drop": {"type": "boolean", "default": False},
                    "allow_bit_depth_drop": {"type": "boolean", "default": False},
                    "allow_hdr_to_ldr": {"type": "boolean", "default": False},
                    "allow_metadata_drop": {"type": "boolean", "default": False},
                    "allow_color_profile_drop": {"type": "boolean", "default": False},
                },
            },
            "source_verification": {
                "type": "string",
                "enum": ["stat", "sha256"],
                "default": "stat",
            },
            "failure_policy": {
                "type": "string",
                "enum": ["continue", "stop"],
                "default": "continue",
            },
            "library_import": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "enabled": {"type": "boolean", "default": False},
                },
            },
            "supersedes_preflight_id": {
                "type": "string",
                "description": "Replace an existing ready draft",
            },
        },
        required=["idempotency_key", "workflow", "input", "output"],
    ),
    _tool_def(
        "get_batch_preflight",
        "Poll GET /api/batch-workflows/preflights/{id} until scanning ends "
        "(ready|failed|expired). Returns spec_hash, can_start, paged issues. "
        "Read-only. Default page 100, max 500.",
        {
            "preflight_id": {
                "type": "string",
                "description": "Preflight id from preflight_batch",
            },
            "cursor": {"type": "string", "description": "Issue-page cursor"},
            "limit": {
                "type": "integer",
                "description": "Page size (1–500, default 100)",
                "minimum": 1,
                "maximum": 500,
                "default": 100,
            },
        },
        required=["preflight_id"],
    ),
    _tool_def(
        "start_batch",
        "Queue a Batch job from a ready preflight "
        "(POST /api/batch-workflows). Does not rescan. "
        "Requires matching spec_hash. Never apply this recipe to the studio.",
        {
            "preflight_id": {"type": "string"},
            "spec_hash": {
                "type": "string",
                "description": "spec_hash from get_batch_preflight",
            },
            "idempotency_key": {"type": "string"},
        },
        required=["preflight_id", "spec_hash", "idempotency_key"],
    ),
    _tool_def(
        "list_batches",
        "List Batch jobs (GET /api/batch-workflows). Read-only. "
        "Default page 100, max 500. Not list_runs — Batch IDs are not Render IDs.",
        {
            "status": {"type": "string", "description": "Optional job status filter"},
            "cursor": {"type": "string"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 100,
            },
        },
    ),
    _tool_def(
        "get_batch",
        "Read one Batch job (GET /api/batch-workflows/{job_id}). Read-only. "
        "Never pass job_id to get_status.",
        {
            "job_id": {"type": "string", "description": "Opaque Batch job id"},
        },
        required=["job_id"],
    ),
    _tool_def(
        "get_batch_items",
        "Page Batch item attempts (GET /api/batch-workflows/{job_id}/items). "
        "Read-only. Default page 100, max 500.",
        {
            "job_id": {"type": "string"},
            "outcome": {"type": "string", "description": "Optional outcome filter"},
            "cursor": {"type": "string"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 100,
            },
        },
        required=["job_id"],
    ),
    _tool_def(
        "pause_batch",
        "Durably request pause (POST /api/batch-workflows/{job_id}/pause). "
        "Active item finishes and publishes first.",
        {
            "job_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
            "expected_revision": {
                "type": "integer",
                "minimum": 0,
                "description": "Optimistic job revision",
            },
        },
        required=["job_id", "idempotency_key", "expected_revision"],
    ),
    _tool_def(
        "resume_batch",
        "Resume a paused Batch job (POST /api/batch-workflows/{job_id}/resume). "
        "Same frozen recipe. Captured project must be active.",
        {
            "job_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
            "expected_revision": {"type": "integer", "minimum": 0},
        },
        required=["job_id", "idempotency_key", "expected_revision"],
    ),
    _tool_def(
        "cancel_batch",
        "Cancel a Batch job (POST /api/batch-workflows/{job_id}/cancel). "
        "Destructive and terminal. Published outputs stay. Not cancel_run.",
        {
            "job_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
            "expected_revision": {"type": "integer", "minimum": 0},
        },
        required=["job_id", "idempotency_key", "expected_revision"],
    ),
    _tool_def(
        "retry_batch",
        "Retry failed/interrupted items (POST /api/batch-workflows/{job_id}/retry-failed). "
        "Same job and workflow hash. Cancelled jobs cannot retry.",
        {
            "job_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
            "expected_revision": {"type": "integer", "minimum": 0},
        },
        required=["job_id", "idempotency_key", "expected_revision"],
    ),
    _tool_def(
        "import_batch_outputs",
        "Import published Batch outputs into the Library "
        "(POST /api/batch-workflows/{job_id}/import). No GPU. "
        "scope=all_published or item_ids (at most 500), not both.",
        {
            "job_id": {"type": "string"},
            "idempotency_key": {"type": "string"},
            "expected_revision": {"type": "integer", "minimum": 0},
            "scope": {
                "type": "string",
                "enum": ["all_published"],
                "description": "Import all published items (default when item_ids omitted)",
            },
            "item_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 500,
                "description": "Specific published item ids (max 500)",
            },
        },
        required=["job_id", "idempotency_key", "expected_revision"],
    ),
    _tool_def(
        "get_unreal_export_catalog",
        "Read the Unreal export catalog (GET /api/renders/unreal-export/catalog). "
        "Empty catalog is valid. Does not write Content.",
        {},
    ),
    _tool_def(
        "preflight_unreal_export",
        "Advisory Unreal-project preflight. Same body as the Export modal. No writes.",
        {
            "render_id": {"type": "string"},
            "projection": {"type": "string"},
            "encoding": {"type": "string"},
            "layout": {"type": "string"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "pan": {"type": "number"},
            "tilt": {"type": "number"},
            "roll": {"type": "number"},
            "source_node_id": {"type": "string"},
            "hdri": {"type": "object"},
            "unreal_project": {"type": "object"},
        },
        required=["render_id", "unreal_project"],
    ),
    _tool_def(
        "export_to_unreal_project",
        "Export one render into a paired Unreal project via POST /api/renders/{id}/export. "
        "Requires actual Unreal Engine (HDRI) M1 state. Timeout 300s; one attempt. "
        "Does not encode locally or write Content from the plugin.",
        {
            "render_id": {"type": "string"},
            "projection": {"type": "string"},
            "encoding": {"type": "string"},
            "layout": {"type": "string"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "pan": {"type": "number"},
            "tilt": {"type": "number"},
            "roll": {"type": "number"},
            "source_node_id": {"type": "string"},
            "hdri": {"type": "object"},
            "unreal_project": {"type": "object"},
        },
        required=["render_id", "unreal_project"],
    ),
    _tool_def(
        "preflight_file_export",
        "Read-only check of an exact graph-node export into a selected folder. "
        "Returns source_revision, output_fingerprint, exact paths and blockers; no writes.",
        _FILE_EXPORT_PROPERTIES,
        required=list(_FILE_EXPORT_REQUIRED),
    ),
    _tool_def(
        "export_render_file",
        "Create-only ordinary render export. Requires the source_revision and "
        "output_fingerprint from preflight plus fresh user approval. One 300s "
        "attempt; after a connection loss inspect the destination before a new attempt.",
        {
            **_FILE_EXPORT_PROPERTIES,
            "source_revision": {"type": "string"},
            "output_fingerprint": {"type": "string"},
        },
        required=[*_FILE_EXPORT_REQUIRED, "source_revision", "output_fingerprint"],
    ),
    _tool_def(
        "get_layer_draft",
        "Inspect the exact saved render HEAD and v6 Layers draft before any generation or land. "
        "Returns bounded active layer ids, bare source-pair leaves and take fingerprints; no viewer needed.",
        {"render_id": {"type": "string"}, "parent_id": _LAYER_ID},
        required=["render_id", "parent_id"],
    ),
    _tool_def(
        "generate_layer",
        "Start one Generate Layer job. Saved target requires a stable 32-hex request_id and exact "
        "HEAD/draft/revision; generate uses controls, regen/variation use the witnessed source pair. "
        "This does not land or commit. Live target is approved internal Copilot only; external MCP gets 403.",
        {
            "target": _LAYER_TARGET, "render_id": {"type": "string"},
            "request_id": _LAYER_JOB_ID, "expected_head": _LAYER_ID, "parent_id": _LAYER_ID,
            "expected_draft_id": _LAYER_ID, "expected_mutation_rev": {"type": "integer", "minimum": 0},
            "mode": {"type": "string", "enum": ["generate", "regen", "variation"]},
            "controls": _LAYER_CONTROLS,
            "source_layer_id": {"type": "string", "maxLength": 64},
            "source_asset": {"type": "string", "pattern": "^[^/\\\\]+\\.png$"},
            "recipeSource": {"type": "string", "pattern": "^[0-9a-f]{32}\\.hextile\\.json$"},
            "edits": _LAYER_EDITS, "takes_fingerprint": {"type": "string", "maxLength": 4096},
            "scene_source": _layer_object({
                "node_id": _LAYER_ID, "revision": {"type": "string", "maxLength": 128},
            }, ("node_id", "revision")),
        },
        required=["target", "render_id", "expected_head", "parent_id", "mode"],
    ),
    _tool_def(
        "get_layer_generation",
        "Read one exact Generate Layer job by id until terminal. Ready means an artifact exists, not a landed layer.",
        {"render_id": {"type": "string"}, "job_id": _LAYER_JOB_ID},
        required=["render_id", "job_id"],
    ),
    _tool_def(
        "cancel_layer_generation",
        "Request cancellation of one exact Generate Layer job. The acknowledgement is not GPU release; "
        "read get_layer_generation until terminal and released.",
        {"render_id": {"type": "string"}, "job_id": _LAYER_JOB_ID},
        required=["render_id", "job_id"],
    ),
    _tool_def(
        "rematte_layer",
        "Re-matte an exact retained RGB token, or Keep rectangle with alpha.mode=opaque. "
        "One call only: the server mints the new job id, so transport loss is unknown and non-replayable.",
        {"render_id": {"type": "string"}, "job_id": _LAYER_JOB_ID,
         "rgb_draft": {"type": "string", "minLength": 8, "maxLength": 200},
         "alpha": _LAYER_ALPHA, "target": _LAYER_RECOVERY_TARGET},
        required=["render_id", "job_id", "rgb_draft", "alpha"],
    ),
    _tool_def(
        "land_generated_layer",
        "Land one ready job pair into an exact v6 draft; no commit. New insertion needs a stable "
        "client layer_id, regen uses its witnessed source layer id. Saved target requires exact HEAD/draft/revision. "
        "Live target is approved internal Copilot only; external MCP gets 403.",
        {"target": _LAYER_TARGET, "render_id": {"type": "string"},
         "layer_id": {"type": "string", "pattern": "^pl_[A-Za-z0-9_-]{3,}$", "maxLength": 64},
         "job_id": _LAYER_JOB_ID, "expected_head": _LAYER_ID, "draft_id": _LAYER_ID,
         "composition_id": {"type": "string", "pattern": "^comp_[0-9a-f]{12}$"},
         "expected_mutation_rev": {"type": "integer", "minimum": 0},
         "pose": _LAYER_POSE, "source_layer_id": {"type": "string", "maxLength": 64}},
        required=["target", "render_id", "layer_id", "job_id", "expected_head", "draft_id",
                  "composition_id", "expected_mutation_rev", "pose"],
    ),
    _tool_def(
        "commit_layer_draft",
        "Explicitly bake the exact saved v6 draft onto current HEAD as a final op:layers node. "
        "No force or implicit retarget. Live target is approved internal Copilot only; external MCP gets 403.",
        {"target": _LAYER_TARGET, "render_id": {"type": "string"},
         "parent_id": _LAYER_ID, "draft_id": _LAYER_ID,
         "composition_id": {"type": "string", "pattern": "^comp_[0-9a-f]{12}$"},
         "expected_mutation_rev": {"type": "integer", "minimum": 0}},
        required=["target", "render_id", "parent_id", "draft_id", "composition_id", "expected_mutation_rev"],
    ),
    _tool_def(
        "preflight_spot_clone",
        "Read-only Spot Clone readiness for an exact saved render parent. Returns HEAD, "
        "bounded mask selectors and mutable source fingerprints; no viewer needed.",
        {"render_id": {"type": "string"}, "parent_id": _CLONE_PARENT_ID},
        required=["render_id", "parent_id"],
    ),
    {
        "name": "clone_spot",
        "description": "After explicit approval, clone one saved render from a witnessed parent and mask, "
        "or commit only an already prepared live Spot Clone through the private Copilot ticket. "
        "Keep node_id stable; a lost reply requires get_spot_clone, never a new id.",
        "inputSchema": {
            "type": "object",
            "oneOf": [
                _layer_object({
                    "target": {"const": "saved"}, "render_id": {"type": "string"},
                    "node_id": _CLONE_NODE_ID, "expected_head": _CLONE_PARENT_ID,
                    "parent_id": _CLONE_PARENT_ID, "destination_id": _CLONE_PARENT_ID,
                    "source_pose": _CLONE_POSE, "destination_pose": _CLONE_DESTINATION_POSE,
                    "mask": _CLONE_MASK, "expected_mask_fingerprint": _CLONE_FINGERPRINT,
                }, ("target", "render_id", "node_id", "expected_head", "parent_id",
                    "destination_id", "source_pose", "destination_pose", "mask")),
                _layer_object({
                    "target": {"const": "live"}, "render_id": {"type": "string"},
                    "node_id": _CLONE_NODE_ID, "expected_head": _CLONE_PARENT_ID,
                    "prepared_operation_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "expected_mask_fingerprint": _CLONE_FINGERPRINT,
                }, ("target", "render_id", "node_id", "expected_head",
                    "prepared_operation_id", "expected_mask_fingerprint")),
            ],
        },
        "annotations": _annotations("clone_spot"),
    },
    _tool_def(
        "get_spot_clone",
        "Read the stored receipt for one exact Spot Clone node after an unknown reply. "
        "A missing node remains unknown; only POST detects a different recipe under the same id.",
        {"render_id": {"type": "string"}, "node_id": _CLONE_NODE_ID},
        required=["render_id", "node_id"],
    ),
]

assert {t["name"] for t in TOOLS} == set(TOOL_NAMES)
assert _READ_ONLY | _MUTATING == set(TOOL_NAMES)
assert _BATCH_TOOLS <= set(TOOL_NAMES)
assert _DESTRUCTIVE <= _MUTATING


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


def _seed_activity(name: str, args: Mapping[str, Any], payload: Any = None) -> Optional[dict[str, str]]:
    """Keep a seed job's identity separate from Render run.run_id."""
    if name not in ("generate_seed", "get_seed_job", "cancel_seed"):
        return None
    job_id = args.get("request_id" if name == "generate_seed" else "job_id")
    if not isinstance(job_id, str) or not job_id:
        return None
    seed = {"job_id": job_id}
    if isinstance(payload, Mapping) and payload.get("status") is not None:
        seed["status"] = str(payload["status"])
    return seed


def _layer_receipt(name: str, data: Any) -> dict[str, Any]:
    """Project APP's private generation payload into bounded MCP facts."""
    if not isinstance(data, Mapping):
        raise HextileClientError("Invalid Layers response from APP", kind="other")
    if name == "get_layer_draft":
        result = {key: data[key] for key in (
            "head", "parent_id", "draft_id", "composition_id", "mutation_rev", "studio_owned"
        ) if key in data}
        result["active_bag_layers"] = [
            {key: layer[key] for key in ("layer_id", "asset", "recipeSource", "takes_fingerprint") if key in layer}
            for layer in data.get("active_bag_layers", []) if isinstance(layer, Mapping)
        ]
        return result
    if name in {"get_layer_generation", "cancel_layer_generation", "rematte_layer"}:
        result = {key: data[key] for key in (
            "job_id", "mode", "state", "render_id", "parent_id", "package_id", "composition_id",
            "width", "height", "seed", "warnings", "released",
        ) if key in data}
        alpha = data.get("alpha")
        if isinstance(alpha, Mapping):
            result["alpha"] = {key: alpha[key] for key in ("mode", "method", "model_id", "revision") if key in alpha}
        draft = data.get("rgb_draft")
        if isinstance(draft, Mapping):
            result["rgb_draft"] = {key: draft[key] for key in ("token", "expires_at") if key in draft}
        error = data.get("error")
        if isinstance(error, Mapping) and "code" in error:
            result["error"] = {"code": error["code"]}
        return result
    keys = {
        "generate_layer": ("job_id", "draft_id", "composition_id", "head", "state"),
        "land_generated_layer": ("draft_id", "composition_id", "mutation_rev", "layer_id", "already_landed"),
        "commit_layer_draft": ("node_id", "head", "width", "height"),
    }[name]
    return {key: data[key] for key in keys if key in data}


def _layer_error_payload(exc: HextileClientError) -> dict[str, Any]:
    if exc.kind == "layer_outcome_unknown":
        return error_payload(exc)
    code = None
    if exc.body:
        try:
            detail = json.loads(exc.body).get("detail")
            if isinstance(detail, Mapping) and isinstance(detail.get("code"), str):
                code = detail["code"]
        except (ValueError, AttributeError):
            pass
    return {"ok": False, "error": "Layer operation refused or unavailable",
            "kind": exc.kind, "status_code": exc.status_code,
            "code": code or ("app_down" if exc.kind == "app_down" else "layer_refused")}


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
        child_token: Optional[str] = None,
    ) -> None:
        self.child_token = child_token if child_token is not None else os.environ.get(CHILD_TOKEN_ENV)
        self.internal_mode = bool(self.child_token)
        self.client = client or Client(internal_mode=self.internal_mode)
        self.notify = notify
        self.session_id: Optional[str] = None
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "list_workflows": self._list_workflows,
            "get_workflow": self._get_workflow,
            "get_capabilities": self._get_capabilities,
            "describe_image": self._describe_image,
            "get_live_context": self._get_live_context,
            "apply_config_delta": self._apply_config_delta,
            "save_workflow": self._save_workflow,
            "delete_workflow": self._delete_workflow,
            "run_workflow": self._run_workflow,
            "validate_config": self._validate_config,
            "get_status": self._get_status,
            "get_gpu_diagnostics": self._get_gpu_diagnostics,
            "get_seed_memory_advice": self._get_seed_memory_advice,
            "get_render_config": self._get_render_config,
            "get_logs": self._get_logs,
            "list_runs": self._list_runs,
            "search_library_prompts": self._search_library_prompts,
            "cancel_run": self._cancel_run,
            "retry_run": self._retry_run,
            "generate_seed": self._generate_seed,
            "get_seed_job": self._get_seed_job,
            "list_seed_history": self._list_seed_history,
            "get_seed_batch": self._get_seed_batch,
            "cancel_seed": self._cancel_seed,
            "list_360_loras": self._list_360_loras,
            "list_installed_models": self._list_installed_models,
            "get_model_readiness": self._get_model_readiness,
            "get_model_download_queue": self._get_model_download_queue,
            "install_model": self._install_model,
            "repair_model": self._repair_model,
            "cancel_model_download": self._cancel_model_download,
            "get_guide": self._get_guide,
            "extract_sequence_video": self._extract_sequence_video,
            "create_sequence": self._create_sequence,
            "start_sequence": self._start_sequence,
            "get_sequence": self._get_sequence,
            "list_sequences": self._list_sequences,
            "stop_sequence": self._stop_sequence,
            "preflight_batch": self._preflight_batch,
            "get_batch_preflight": self._get_batch_preflight,
            "start_batch": self._start_batch,
            "list_batches": self._list_batches,
            "get_batch": self._get_batch,
            "get_batch_items": self._get_batch_items,
            "pause_batch": self._pause_batch,
            "resume_batch": self._resume_batch,
            "cancel_batch": self._cancel_batch,
            "retry_batch": self._retry_batch,
            "import_batch_outputs": self._import_batch_outputs,
            "get_unreal_export_catalog": self._get_unreal_export_catalog,
            "preflight_unreal_export": self._preflight_unreal_export,
            "export_to_unreal_project": self._export_to_unreal_project,
            "preflight_file_export": self._preflight_file_export,
            "export_render_file": self._export_render_file,
            "get_layer_draft": self._get_layer_draft,
            "generate_layer": self._generate_layer,
            "get_layer_generation": self._get_layer_generation,
            "cancel_layer_generation": self._cancel_layer_generation,
            "rematte_layer": self._rematte_layer,
            "land_generated_layer": self._land_generated_layer,
            "commit_layer_draft": self._commit_layer_draft,
            "preflight_spot_clone": self._preflight_spot_clone,
            "clone_spot": self._clone_spot,
            "get_spot_clone": self._get_spot_clone,
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
            operation_id = meta.get("hextile_op")
            if not isinstance(operation_id, str) or not operation_id:
                operation_id = None
            try:
                result = self.call_tool(
                    name,
                    arguments,
                    progress_token=progress_token,
                    operation_id=operation_id,
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
        operation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        call_id = str(uuid.uuid4())
        args = dict(arguments) if isinstance(arguments, dict) else {}
        for key in tuple(args):
            if key.lower() in _PRIVATE_ARGUMENT_KEYS:
                args.pop(key, None)
        overrides_keys = _overrides_keys(name, args)
        self._emit_activity(
            tool=name, call_id=call_id, phase="started",
            seed=_seed_activity(name, args),
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
            private_headers: dict[str, str] = {}
            if self.internal_mode and operation_id:
                private_headers = {
                    "X-Hextile-Copilot-Token": str(self.child_token),
                    "X-Hextile-Op": operation_id,
                }
            bind_headers = getattr(self.client, "request_headers", None)
            header_context = bind_headers(private_headers) if bind_headers else nullcontext()
            with header_context:
                data = handler(args)
            if name in {"install_model", "repair_model", "cancel_model_download"} and isinstance(data, dict) and data.get("success") is False:
                self._emit_activity(
                    tool=name, call_id=call_id, phase="failed",
                    error={"kind": "model_rejected", "status_code": None,
                           "message": str(data.get("message", "Model action rejected"))},
                    seed=_seed_activity(name, args), overrides_keys=overrides_keys,
                )
                return _err_result(data)
            phase = (
                "cancelled"
                if name in (
                    "cancel_run",
                    "stop_sequence",
                    "cancel_batch",
                )
                else "succeeded"
            )
            # Batch and seed IDs must never enter run.run_id (Render consumers).
            run = None if name in _BATCH_TOOLS or name in ("generate_seed", "get_seed_job", "cancel_seed") or name in _LAYER_TOOLS or name in _SPOT_CLONE_TOOLS else _run_from_payload(data)
            self._emit_activity(
                tool=name,
                call_id=call_id,
                phase=phase,
                run=run,
                seed=_seed_activity(name, args, data),
                overrides_keys=overrides_keys,
            )
            return _ok_result(data)
        except HextileClientError as exc:
            safe_error = _layer_error_payload(exc) if name in _LAYER_TOOLS else error_payload(exc)
            self._emit_activity(
                tool=name,
                call_id=call_id,
                phase="failed",
                error={
                    "kind": exc.kind,
                    "status_code": exc.status_code,
                    "message": safe_error["error"] if name in _LAYER_TOOLS else str(exc),
                },
                seed=_seed_activity(name, args),
                overrides_keys=overrides_keys,
            )
            return _err_result(safe_error)
        except Exception as exc:  # noqa: BLE001
            if name in _LAYER_TOOLS:
                self._emit_activity(
                    tool=name, call_id=call_id, phase="failed",
                    error={"kind": "other", "status_code": None, "message": "Layer operation failed"},
                    overrides_keys=overrides_keys,
                )
                return _err_result({"ok": False, "error": "Layer operation failed",
                                    "kind": "other", "code": "internal_error"})
            self._emit_activity(
                tool=name,
                call_id=call_id,
                phase="failed",
                error={
                    "kind": "other",
                    "status_code": None,
                    "message": str(exc),
                },
                seed=_seed_activity(name, args),
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
        seed: Optional[Mapping[str, str]] = None,
        error: Optional[Mapping[str, Any]] = None,
        overrides_keys: Optional[list[str]] = None,
    ) -> None:
        if self.internal_mode:
            return
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
        if seed:
            envelope["seed"] = dict(seed)
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

    def _describe_image(self, args: dict[str, Any]) -> Any:
        if set(args) - {"source", "crop"}:
            raise HextileClientError("Unsupported describe_image argument", kind="other")
        source = args.get("source")
        if not isinstance(source, dict):
            raise HextileClientError("source is required", kind="other")
        kind = source.get("kind")
        if kind == "render_node":
            keys = {"kind", "render_id", "node_id"}
            if set(source) != keys or any(
                not isinstance(source[key], str) or not source[key] for key in ("render_id", "node_id")
            ):
                raise HextileClientError("Exact render_id and node_id are required", kind="other")
        elif kind == "local_file":
            if (set(source) != {"kind", "path"} or not isinstance(source.get("path"), str)
                    or not Path(source["path"]).is_absolute()):
                raise HextileClientError("An absolute local_file path is required", kind="other")
        else:
            raise HextileClientError("Unsupported description source", kind="other")

        body: dict[str, Any] = {"source": dict(source)}
        if "crop" in args:
            crop = args["crop"]
            bounds = {"yaw": (-180, 180), "pitch": (-90, 90), "fov": (1, 150)}
            if (not isinstance(crop, dict) or set(crop) != set(bounds) or any(
                isinstance(crop[key], bool) or not isinstance(crop[key], (int, float))
                or not math.isfinite(crop[key]) or not lo <= crop[key] <= hi
                for key, (lo, hi) in bounds.items()
            )):
                raise HextileClientError("Invalid description crop", kind="other")
            body["crop"] = dict(crop)

        result = self.client.describe_image(body)
        mode = "crop" if "crop" in body else "photo"
        if (not isinstance(result, dict) or result.get("mode") != mode
                or not isinstance(result.get("prompt"), str) or not result["prompt"].strip()
                or not isinstance(result.get("image_signature"), str)):
            raise HextileClientError("Invalid description response from APP", kind="other")
        # Construct the public result from known text fields; never relay raster extras.
        return {
            "prompt": result["prompt"], "source": body["source"], "mode": mode,
            "crop": body.get("crop"), "image_signature": result["image_signature"],
        }

    def _get_unreal_export_catalog(self, _args: dict[str, Any]) -> Any:
        return self.client.get_unreal_export_catalog()

    def _unreal_export_body(self, args: dict[str, Any]) -> dict[str, Any]:
        body = dict(args)
        body.pop("render_id", None)
        body.pop("destination", None)
        return body

    def _preflight_unreal_export(self, args: dict[str, Any]) -> Any:
        render_id = str(args.get("render_id") or "")
        return self.client.preflight_unreal_export(render_id, self._unreal_export_body(args))

    def _export_to_unreal_project(self, args: dict[str, Any]) -> Any:
        render_id = str(args.get("render_id") or "")
        return self.client.export_to_unreal_project(render_id, self._unreal_export_body(args))

    def _file_export_body(self, args: dict[str, Any], *, export: bool) -> dict[str, Any]:
        allowed = set(_FILE_EXPORT_REQUIRED) | _FILE_EXPORT_OPTIONS
        if export:
            allowed.update(("source_revision", "output_fingerprint"))
        unexpected = set(args) - allowed
        if unexpected:
            raise HextileClientError(
                f"Unsupported file export argument: {sorted(unexpected)[0]}", kind="other"
            )
        for key in _FILE_EXPORT_REQUIRED + (("source_revision", "output_fingerprint") if export else ()):
            if not isinstance(args.get(key), str) or not args[key]:
                raise HextileClientError(f"{key} is required", kind="other")
        leaf = args["output_name"]
        if (leaf in (".", "..") or leaf.endswith((".", " "))
                or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in leaf)
                or _WINDOWS_DEVICE_STEM.match(leaf)):
            raise HextileClientError("unsafe output_name", kind="other")
        folder = Path(args["folder_path"])
        if not folder.is_absolute() or not folder.is_dir():
            raise HextileClientError("folder_path must be an existing absolute folder", kind="other")
        parent = folder.resolve(strict=True)
        destination = parent / leaf
        if destination.resolve(strict=False).parent != parent:
            raise HextileClientError("output_name escapes selected folder", kind="other")
        body = {
            "source_node_id": args["source_node_id"],
            "destination": str(destination),
            "projection": args["projection"],
            "encoding": args["encoding"],
            **{key: args[key] for key in _FILE_EXPORT_OPTIONS if key in args},
        }
        if export:
            body.update({
                "create_only": True,
                "expected_source_revision": args["source_revision"],
                "expected_output_fingerprint": args["output_fingerprint"],
            })
        return body

    def _preflight_file_export(self, args: dict[str, Any]) -> Any:
        return self.client.preflight_file_export(
            args["render_id"], self._file_export_body(args, export=False)
        )

    def _export_render_file(self, args: dict[str, Any]) -> Any:
        return self.client.export_render_file(
            args["render_id"], self._file_export_body(args, export=True)
        )

    def _layer_saved_or_live(self, name: str, args: dict[str, Any], keys: frozenset[str]) -> Any:
        target = args.get("target")
        if target not in {"saved", "live"}:
            raise HextileClientError("target must be saved or live", kind="other")
        unexpected = set(args) - keys - {"target", "render_id"}
        if unexpected:
            raise HextileClientError(f"Unsupported {name} argument: {sorted(unexpected)[0]}", kind="other")
        if target == "live":
            if not self.internal_mode:
                raise HextileClientError("Live Layers target requires an approved internal Copilot turn.",
                                         status_code=403, kind="http")
            return self.client.creative_live(name, args)
        body = {key: args[key] for key in keys if key in args}
        render_id = str(args["render_id"])
        if name == "generate_layer":
            if not isinstance(body.get("request_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", body["request_id"]):
                raise HextileClientError("saved Generate Layer needs a stable 32-hex request_id", kind="other")
            return _layer_receipt(name, self.client.generate_layer(render_id, body))
        if name == "land_generated_layer":
            return _layer_receipt(name, self.client.land_generated_layer(render_id, body))
        state = self.client.get_layer_draft(render_id, str(body["parent_id"]))
        if (not isinstance(state, Mapping) or state.get("studio_owned")
                or state.get("head") != body["parent_id"]
                or state.get("draft_id") != body["draft_id"]
                or state.get("composition_id") != body["composition_id"]
                or state.get("mutation_rev") != body["expected_mutation_rev"]):
            raise HextileClientError("Saved Layers commit witness changed or belongs to the open studio.",
                                     status_code=409, kind="http")
        return _layer_receipt(name, self.client.commit_layer_draft(render_id, body))

    def _get_layer_draft(self, args: dict[str, Any]) -> Any:
        return _layer_receipt("get_layer_draft", self.client.get_layer_draft(
            str(args["render_id"]), str(args["parent_id"])
        ))

    def _generate_layer(self, args: dict[str, Any]) -> Any:
        return self._layer_saved_or_live("generate_layer", args, frozenset({
            "request_id", "expected_head", "parent_id", "expected_draft_id", "expected_mutation_rev",
            "mode", "controls", "source_layer_id", "source_asset", "recipeSource", "edits",
            "takes_fingerprint", "scene_source",
        }))

    def _get_layer_generation(self, args: dict[str, Any]) -> Any:
        return _layer_receipt("get_layer_generation", self.client.get_layer_generation(
            str(args["render_id"]), str(args["job_id"])
        ))

    def _saved_layer_job(self, args: dict[str, Any]) -> None:
        job = self.client.get_layer_generation(str(args["render_id"]), str(args["job_id"]))
        if not isinstance(job, Mapping):
            raise HextileClientError("Layer job is unavailable", status_code=409, kind="http")
        state = self.client.get_layer_draft(str(args["render_id"]), str(job.get("parent_id") or ""))
        if (not isinstance(state, Mapping) or state.get("studio_owned")
                or state.get("draft_id") != job.get("package_id")
                or state.get("composition_id") != job.get("composition_id")):
            raise HextileClientError("Layer job is not owned by the exact saved draft.",
                                     status_code=409, kind="http")

    def _cancel_layer_generation(self, args: dict[str, Any]) -> Any:
        self._saved_layer_job(args)
        return _layer_receipt("cancel_layer_generation", self.client.cancel_layer_generation(
            str(args["render_id"]), str(args["job_id"])
        ))

    def _rematte_layer(self, args: dict[str, Any]) -> Any:
        self._saved_layer_job(args)
        body = {"version": 1, "rgb_draft": args["rgb_draft"], "alpha": args["alpha"]}
        if "target" in args:
            body["target"] = args["target"]
        return _layer_receipt("rematte_layer", self.client.rematte_layer(
            str(args["render_id"]), str(args["job_id"]), body
        ))

    def _land_generated_layer(self, args: dict[str, Any]) -> Any:
        return self._layer_saved_or_live("land_generated_layer", args, frozenset({
            "layer_id", "job_id", "expected_head", "draft_id", "composition_id",
            "expected_mutation_rev", "pose", "source_layer_id",
        }))

    def _commit_layer_draft(self, args: dict[str, Any]) -> Any:
        return self._layer_saved_or_live("commit_layer_draft", args, frozenset({
            "parent_id", "draft_id", "composition_id", "expected_mutation_rev",
        }))

    def _preflight_spot_clone(self, args: dict[str, Any]) -> Any:
        if set(args) != {"render_id", "parent_id"}:
            raise HextileClientError("Spot Clone preflight needs only render_id and parent_id", kind="other")
        return self.client.preflight_spot_clone(args["render_id"], args["parent_id"])

    def _get_spot_clone(self, args: dict[str, Any]) -> Any:
        if set(args) != {"render_id", "node_id"} or not re.fullmatch(r"[0-9a-f]{12}", str(args.get("node_id", ""))):
            raise HextileClientError("Spot Clone receipt needs exact render_id and 12-hex node_id", kind="other")
        return self.client.get_spot_clone(args["render_id"], args["node_id"])

    def _clone_spot(self, args: dict[str, Any]) -> Any:
        target = args.get("target")
        if target == "live" and not self.internal_mode:
            raise HextileClientError("Live Spot Clone requires an approved internal Copilot turn.",
                                     status_code=403, kind="http")
        if target not in {"saved", "live"}:
            raise HextileClientError("Spot Clone target must be saved or live", kind="other")
        common = {"target", "render_id", "node_id", "expected_head"}
        if target == "live":
            required = common | {"prepared_operation_id", "expected_mask_fingerprint"}
            if set(args) != required:
                raise HextileClientError("Live Spot Clone takes only its prepared operation witness", kind="other")
            if not re.fullmatch(r"[0-9a-f]{12}", str(args["node_id"])):
                raise HextileClientError("Spot Clone needs a stable 12-hex node_id", kind="other")
            try:
                ticket = self.client.creative_live("clone_spot", args)
                if not isinstance(ticket, Mapping) or ticket.get("status") != "pending":
                    raise HextileClientError("Live Spot Clone acknowledgement was incomplete",
                                             kind="spot_clone_outcome_unknown")
                return ticket
            except (HextileClientError, http.client.HTTPException) as exc:
                if not (isinstance(exc, http.client.HTTPException) or
                        isinstance(exc, HextileClientError) and exc.kind in {
                            "app_down", "spot_clone_outcome_unknown",
                        }):
                    raise
                raise HextileClientError(
                    "Live Spot Clone outcome unknown. Read get_spot_clone by the same render_id "
                    "and node_id; do not start a new operation.",
                    kind="spot_clone_outcome_unknown",
                    receipt={"state": "unknown", "render_id": args["render_id"],
                             "node_id": args["node_id"], "reconcile": "get_spot_clone"},
                ) from exc

        required = common | {"parent_id", "destination_id", "source_pose", "destination_pose", "mask"}
        allowed = required | {"expected_mask_fingerprint"}
        if not required <= set(args) or set(args) - allowed:
            raise HextileClientError("Saved Spot Clone needs only the exact witnessed recipe", kind="other")
        if (not re.fullmatch(r"[0-9a-f]{12}", str(args["node_id"]))
                or args["expected_head"] != args["parent_id"]):
            raise HextileClientError("Saved Spot Clone needs a stable node_id and matching parent/HEAD", kind="other")
        mask = args["mask"]
        if not isinstance(mask, dict):
            raise HextileClientError("Spot Clone mask must be a closed selector", kind="other")
        source = mask.get("source")
        mask_keys = {
            "shape": {"source", "shape", "sides", "radius", "roll", "aspect", "corner_radius",
                      "inpaint_blur", "blend_clip", "blend_blur"},
            "from_template": {"source", "template_id", "face_index", "blend_clip", "blend_blur"},
            "from_raster": {"source", "raster_id", "grow_px", "inpaint_blur", "blend_clip", "blend_blur"},
        }
        if source not in mask_keys or set(mask) - mask_keys[source]:
            raise HextileClientError("Spot Clone mask must be a closed shape/template/raster selector", kind="other")
        if source == "shape":
            if "expected_mask_fingerprint" in args:
                raise HextileClientError("Shape masks do not take a source fingerprint", kind="other")
        elif (not isinstance(args.get("expected_mask_fingerprint"), str)
              or not re.fullmatch(r"[0-9a-f]{64}", args["expected_mask_fingerprint"])):
            raise HextileClientError("Template/raster masks need the exact preflight fingerprint", kind="other")
        for pose_name, pose_keys in (("source_pose", {"yaw", "pitch", "roll"}),
                                     ("destination_pose", {"yaw", "pitch", "roll", "fov"})):
            pose = args[pose_name]
            if not isinstance(pose, dict) or set(pose) - pose_keys:
                raise HextileClientError(f"{pose_name} must be a closed pose", kind="other")
        body = {key: args[key] for key in allowed - {"target", "render_id"} if key in args}
        return self.client.clone_spot(args["render_id"], body)

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
        for key in ("id", "name", "configFileName", "currentRenderId"):
            if key in partial:
                raise HextileClientError(
                    "I can't change the document name, file name, or current render id.",
                    status_code=422,
                    kind="http",
                )
        if "pipeline" in partial:
            raise HextileClientError(
                "I can't change the pipeline on the open file. "
                "Use run_workflow if you need a different pipeline on a render.",
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

    def _get_gpu_diagnostics(self, args: dict[str, Any]) -> Any:
        return self.client.get_gpu_diagnostics()

    def _get_seed_memory_advice(self, args: dict[str, Any]) -> Any:
        if not all(key in args for key in ("lora_path", "width", "height")):
            raise HextileClientError("lora_path, width, and height are required", kind="other")
        return self.client.get_seed_memory_advice(
            str(args["lora_path"]),
            int(args["width"]),
            int(args["height"]),
            cpu_offload=args.get("cpu_offload") is True,
        )

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

    def _search_library_prompts(self, args: dict[str, Any]) -> Any:
        return self.client.search_library_prompts(args)

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
        return self.client.retry_run(str(run_id), vram_recovery=args.get("vram_recovery") is True)

    def _generate_seed(self, args: dict[str, Any]) -> Any:
        request_id = args.get("request_id")
        prompt = args.get("prompt")
        lora_path = args.get("lora_path")
        base_model = args.get("base_model")
        if not request_id or not prompt or not lora_path or not base_model:
            raise HextileClientError(
                "request_id, prompt, lora_path, and base_model are required",
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
            "resolution_preset",
            "cpu_offload",
            "allow_low_vram",
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
            request_id=str(request_id),
            lora_path=str(lora_path),
            base_model=str(base_model),
            n=n,
            **extra,
        )

    def _get_seed_job(self, args: dict[str, Any]) -> Any:
        job_id = args.get("job_id")
        if not job_id:
            raise HextileClientError(
                "job_id is required", status_code=None, kind="other"
            )
        return self.client.get_seed_job(str(job_id))

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

    def _cancel_seed(self, args: dict[str, Any]) -> Any:
        job_id = args.get("job_id")
        if not job_id:
            raise HextileClientError(
                "job_id is required", status_code=None, kind="other"
            )
        return self.client.cancel_seed(str(job_id))

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

    def _model_args(self, args: dict[str, Any], allowed: set[str]) -> tuple[str, str]:
        unexpected = set(args) - allowed
        if unexpected:
            raise HextileClientError(f"Unsupported model argument: {sorted(unexpected)[0]}", kind="other")
        for key in ("pipeline_id", "model_id"):
            if not isinstance(args.get(key), str) or not args[key].strip():
                raise HextileClientError(f"{key} is required", kind="other")
        return args["pipeline_id"], args["model_id"]

    def _get_model_readiness(self, args: dict[str, Any]) -> Any:
        if set(args) - {"overrides"}:
            raise HextileClientError("Unsupported readiness argument", kind="other")
        overrides = args.get("overrides", [])
        if not isinstance(overrides, list) or len(overrides) > 32:
            raise HextileClientError("overrides must contain at most 32 entries", kind="other")
        allowed = {"pipeline_id", "model_id", "quantization", "speed_mode", "text_encoder_precision"}
        for item in overrides:
            if not isinstance(item, dict) or set(item) - allowed:
                raise HextileClientError("Unsupported model override", kind="other")
            self._model_args(item, allowed)
        return self.client.get_model_readiness(overrides)

    def _get_model_download_queue(self, args: dict[str, Any]) -> Any:
        if args:
            raise HextileClientError("Download queue takes no arguments", kind="other")
        return self.client.get_model_download_queue()

    def _install_model(self, args: dict[str, Any]) -> Any:
        pipeline_id, model_id = self._model_args(
            args, {"pipeline_id", "model_id", "bundle", "expected_selection_fingerprint"}
        )
        if type(args.get("bundle")) is not bool:
            raise HextileClientError("bundle must be a boolean", kind="other")
        fingerprint = args.get("expected_selection_fingerprint")
        if args["bundle"]:
            if not isinstance(fingerprint, str) or not fingerprint.strip():
                raise HextileClientError("Bundle install requires expected_selection_fingerprint", kind="other")
            catalog = self.client.get_model_readiness([])
            rows = catalog.get("models") if isinstance(catalog, dict) else None
            selected = next((row for row in rows if isinstance(row, dict)
                             and row.get("pipeline_id") == pipeline_id
                             and row.get("id") == model_id), None) if isinstance(rows, list) else None
            if not selected or not selected.get("selection_fingerprint"):
                raise HextileClientError("Default catalog selection fingerprint unavailable; refresh readiness", kind="capability")
            if selected["selection_fingerprint"] != fingerprint:
                raise HextileClientError("Selected model bundle changed; refresh readiness", status_code=409, kind="http")
            return self.client.install_model_bundle(pipeline_id, model_id, fingerprint)
        if fingerprint is not None:
            raise HextileClientError("Single-model install does not accept a bundle fingerprint", kind="other")
        return self.client.install_model_single(pipeline_id, model_id)

    def _repair_model(self, args: dict[str, Any]) -> Any:
        pipeline_id, model_id = self._model_args(args, {"pipeline_id", "model_id"})
        return self.client.repair_model(pipeline_id, model_id)

    def _cancel_model_download(self, args: dict[str, Any]) -> Any:
        pipeline_id, model_id = self._model_args(
            args, {"pipeline_id", "model_id", "expected_queue_entry_id"}
        )
        entry_id = args.get("expected_queue_entry_id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise HextileClientError("expected_queue_entry_id is required", kind="other")
        queue = self.client.get_model_download_queue()
        rows = (queue.get("queued", []) + queue.get("downloads", [])) if isinstance(queue, dict) else []
        current = next((row for row in rows if isinstance(row, dict)
                        and row.get("pipeline_id") == pipeline_id
                        and row.get("model_id") == model_id), None)
        if not current or not current.get("queue_entry_id"):
            raise HextileClientError("Observed queue entry unavailable; refresh queue", kind="capability")
        if current["queue_entry_id"] != entry_id:
            raise HextileClientError("Download queue entry changed or settled", status_code=409, kind="http")
        return self.client.cancel_model_download(pipeline_id, model_id, entry_id)

    def _get_guide(self, args: dict[str, Any]) -> Any:
        return load_guide(str(args.get("name") or "index"))

    def _extract_sequence_video(self, args: dict[str, Any]) -> Any:
        video_path = args.get("video_path")
        if not video_path:
            raise HextileClientError(
                "video_path is required", status_code=None, kind="other"
            )
        return self.client.extract_sequence_video(str(video_path))

    def _create_sequence(self, args: dict[str, Any]) -> Any:
        folder_path = args.get("folder_path")
        config = args.get("config")
        if not folder_path:
            raise HextileClientError(
                "folder_path is required", status_code=None, kind="other"
            )
        if not isinstance(config, dict):
            raise HextileClientError(
                "config is required and must be a JSON object",
                status_code=None,
                kind="other",
            )
        return self.client.create_sequence(
            str(folder_path),
            config,
            name=args.get("name"),
        )

    def _start_sequence(self, args: dict[str, Any]) -> Any:
        sequence_id = args.get("sequence_id")
        if not sequence_id:
            raise HextileClientError(
                "sequence_id is required", status_code=None, kind="other"
            )
        return self.client.start_sequence(str(sequence_id))

    def _get_sequence(self, args: dict[str, Any]) -> Any:
        sequence_id = args.get("sequence_id")
        if not sequence_id:
            raise HextileClientError(
                "sequence_id is required", status_code=None, kind="other"
            )
        return self.client.get_sequence(str(sequence_id))

    def _list_sequences(self, args: dict[str, Any]) -> Any:
        return self.client.list_sequences(
            str(args.get("lifecycle_status") or "active")
        )

    def _stop_sequence(self, args: dict[str, Any]) -> Any:
        sequence_id = args.get("sequence_id")
        if not sequence_id:
            raise HextileClientError(
                "sequence_id is required", status_code=None, kind="other"
            )
        return self.client.stop_sequence(str(sequence_id))

    def _preflight_batch(self, args: dict[str, Any]) -> Any:
        for key in ("idempotency_key", "workflow", "input", "output"):
            if key not in args:
                raise HextileClientError(
                    f"{key} is required", status_code=None, kind="other"
                )
        body = {
            "idempotency_key": str(args["idempotency_key"]),
            "workflow": args["workflow"],
            "input": args["input"],
            "output": args["output"],
        }
        for key in (
            "loss_policy",
            "source_verification",
            "failure_policy",
            "library_import",
            "supersedes_preflight_id",
        ):
            if key in args:
                body[key] = args[key]
        return self.client.preflight_batch(body)

    def _get_batch_preflight(self, args: dict[str, Any]) -> Any:
        preflight_id = args.get("preflight_id")
        if not preflight_id:
            raise HextileClientError(
                "preflight_id is required", status_code=None, kind="other"
            )
        cursor = args.get("cursor")
        limit = args.get("limit")
        deadline = time.monotonic() + PREFLIGHT_POLL_TIMEOUT_S
        delay = PREFLIGHT_POLL_INTERVAL_S
        while True:
            data = self.client.get_batch_preflight(
                str(preflight_id),
                cursor=str(cursor) if cursor else None,
                limit=int(limit) if limit is not None else None,
            )
            status = data.get("status") if isinstance(data, Mapping) else None
            if status != "scanning" or time.monotonic() >= deadline:
                return data
            time.sleep(delay)
            delay = min(delay * 2, PREFLIGHT_POLL_BACKOFF_CAP_S)

    def _start_batch(self, args: dict[str, Any]) -> Any:
        return self.client.start_batch(
            preflight_id=str(args.get("preflight_id") or ""),
            spec_hash=str(args.get("spec_hash") or ""),
            idempotency_key=str(args.get("idempotency_key") or ""),
        )

    def _list_batches(self, args: dict[str, Any]) -> Any:
        limit = args.get("limit")
        return self.client.list_batches(
            status=str(args["status"]) if args.get("status") else None,
            cursor=str(args["cursor"]) if args.get("cursor") else None,
            limit=int(limit) if limit is not None else None,
        )

    def _get_batch(self, args: dict[str, Any]) -> Any:
        job_id = args.get("job_id")
        if not job_id:
            raise HextileClientError(
                "job_id is required", status_code=None, kind="other"
            )
        return self.client.get_batch(str(job_id))

    def _get_batch_items(self, args: dict[str, Any]) -> Any:
        job_id = args.get("job_id")
        if not job_id:
            raise HextileClientError(
                "job_id is required", status_code=None, kind="other"
            )
        limit = args.get("limit")
        return self.client.get_batch_items(
            str(job_id),
            outcome=str(args["outcome"]) if args.get("outcome") else None,
            cursor=str(args["cursor"]) if args.get("cursor") else None,
            limit=int(limit) if limit is not None else None,
        )

    def _pause_batch(self, args: dict[str, Any]) -> Any:
        return self._batch_control(self.client.pause_batch, args)

    def _resume_batch(self, args: dict[str, Any]) -> Any:
        return self._batch_control(self.client.resume_batch, args)

    def _cancel_batch(self, args: dict[str, Any]) -> Any:
        return self._batch_control(self.client.cancel_batch, args)

    def _retry_batch(self, args: dict[str, Any]) -> Any:
        return self._batch_control(self.client.retry_batch, args)

    def _batch_control(self, method: Callable[..., Any], args: dict[str, Any]) -> Any:
        job_id = args.get("job_id")
        key = args.get("idempotency_key")
        if not job_id:
            raise HextileClientError(
                "job_id is required", status_code=None, kind="other"
            )
        if not key:
            raise HextileClientError(
                "idempotency_key is required", status_code=None, kind="other"
            )
        if "expected_revision" not in args:
            raise HextileClientError(
                "expected_revision is required", status_code=None, kind="other"
            )
        return method(
            str(job_id),
            idempotency_key=str(key),
            expected_revision=int(args["expected_revision"]),
        )

    def _import_batch_outputs(self, args: dict[str, Any]) -> Any:
        job_id = args.get("job_id")
        key = args.get("idempotency_key")
        if not job_id:
            raise HextileClientError(
                "job_id is required", status_code=None, kind="other"
            )
        if not key:
            raise HextileClientError(
                "idempotency_key is required", status_code=None, kind="other"
            )
        if "expected_revision" not in args:
            raise HextileClientError(
                "expected_revision is required", status_code=None, kind="other"
            )
        return self.client.import_batch_outputs(
            str(job_id),
            idempotency_key=str(key),
            expected_revision=int(args["expected_revision"]),
            scope=str(args["scope"]) if args.get("scope") else None,
            item_ids=list(args["item_ids"]) if args.get("item_ids") is not None else None,
        )

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
    control_executor = ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="hextile-mcp-control"
    )
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
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        lane = control_executor if params.get("name") in _CONTROL_TOOLS else executor
        fut = lane.submit(run_rpc, msg)
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
        control_executor.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    # Quiet reminder on stderr only — never pollute stdout (MCP channel).
    sys.stderr.write(
        f"{SERVER_NAME} MCP {SERVER_VERSION} — proxy to 127.0.0.1:8000\n"
    )
    sys.stderr.flush()
    raise SystemExit(main())
