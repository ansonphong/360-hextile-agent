---
name: hextile
description: "Drive 360 Hextile workflows from an AI coding agent. Use when generating 360° panoramas, listing or saving workflow templates, overriding prompts, dry-run validation, 360-LoRA seeds, polling or listing renders, or reading bundled automation guides."
---

# 360 Hextile — Agent Skill

Drive **360 Hextile** (desktop app) while it is running on this machine. Tools talk to `http://127.0.0.1:8000` through the `hextile` MCP server. **The server owns config authority** — merge, `HextileConfig` validation, and queueing happen in the app, not in this plugin.

Before composing fields or teaching the user, call `get_guide` (`workflow-schema`, `best-practices`, `recipes`, `website-index`). Fetch website URLs from `website-index` when you need product-domain depth.

## Prerequisites

1. **360 Hextile is running** (backend on `127.0.0.1:8000`).
2. **python3 ≥ 3.9** on PATH (MCP proxy).
3. App build with **`POST /api/workflows/run`** (workflow automation P0+).

If a tool returns that the app is not running, tell the user verbatim:

> Launch 360 Hextile, then retry

Do not invent endpoints, do not hang, do not shell-sleep-poll forever without `get_status` / `list_runs`.

## What a Workflow is

A **Workflow is a full `.hextile.json` config document** (pipeline / hextile / input / diffusion / prompt / post / …), stored on shelves:

| origin   | Meaning                          |
|----------|----------------------------------|
| `builtin`| Shipped templates (e.g. `quick-scout`) — **immutable** |
| `user`   | User library                     |
| `project`| Project-scoped (needs an active project) |

There is **no** separate workflow-envelope format — only `.hextile.json`. Prefer shelf `workflow_id` + overrides over inventing a full raw document.

## Tools

| Tool | When to use | Mutates? |
|------|-------------|----------|
| `get_capabilities` | Handshake before a session | no |
| `get_guide` | Schema, practices, recipes, website index | no |
| `list_workflows` | Discover templates | no |
| `get_workflow` | Read one template before override or clone | no |
| `save_workflow` | Persist a **new** id on user/project | **yes** |
| `delete_workflow` | Remove a user/project workflow | **yes** |
| `validate_config` | Dry-run merge+validate (terraform plan) | no |
| `run_workflow` | Queue a render after overrides | **yes** |
| `get_status` | Poll `run_id` progress / output paths | no |
| `get_render_config` | Read producing .hextile.json for a render | no |
| `get_logs` | Fetch failed-run logs | no |
| `list_runs` | Find jobs if you lost `run_id` | no |
| `cancel_run` | Kill a long GPU run | **yes** |
| `list_360_loras` | Discover `path` + `base_model` for seeds | no |
| `generate_seed` | Create equirect seed image via 360-LoRA | **yes** |

### Selection guide

1. **Handshake** → `get_capabilities`.
2. **Learn** → `get_guide` (`best-practices` then `workflow-schema`).
3. **Discover** → `list_workflows` → pick `origin` + `id`.
4. **Inspect** → `get_workflow` if you need defaults before overriding.
5. **Plan** → `validate_config` with the same overrides you intend to run.
6. **Need a source image from a prompt?** → `list_360_loras` then `generate_seed` (two-step below).
7. **Run** → `run_workflow` → keep `run_id` → `get_status` until terminal (`completed` / `failed` / `cancelled` / `crashed`). Use `list_runs` if the id is lost.
8. **Save a variant** → `save_workflow` (`user`/`project`, **new** id). Create-only; 409 means pick another id.
9. **Abort** → `cancel_run`.

## Safety — overrides, never authority

- Send **`workflow_id` + `overrides`** (and optional `output`). Prefer **not** composing a full raw config as authority.
- The app deep-merges and validates. A bad override is a structured **422** — read it, fix the override, retry.
- **List / array merge: REPLACE wholesale.** Overriding `passes[]`, `lora.models[]`, or any array **replaces** the template list; it does **not** append. To “add a LoRA”, include the full desired array (template entries you want to keep + your addition).
- Live runs need a non-empty **`input.path`** after merge (empty path is only OK on dry_run).
- `input.source` is `file` or `render` only.

## Seed → run (two-step)

`InputSource` is `file` | `render` only. Generative producers are **not** render-time sources.

1. `list_360_loras` → pick `path` and `base_model` (`sdxl` | `sd15` | `flux_schnell`).
2. `generate_seed(prompt, lora_path, base_model, n?)` → `variations` (absolute paths) + `batch_id`.
3. Pick one path (default index 0 unless the user chooses).
4. `run_workflow` with overrides:

```json
{
  "input": {
    "path": "/absolute/path/from/variations[i]",
    "source": "file"
  }
}
```

Never write retired source types (`pattern`, `360_lora`, …) into render-time `input.source`.

`generate_seed` hits **`POST /api/360-lora/generate`** (not `/api/lora-360`).

## App-down / upgrade recovery

| Symptom | Meaning | What to tell the user |
|---------|---------|------------------------|
| Error contains “isn't running” / “Launch 360 Hextile” | Backend not up | Launch 360 Hextile, then retry |
| “Upgrade 360 Hextile (needs workflows/run)” | App too old | Update the app to a build with workflow run |
| HTTP 422 with validation detail | Bad overrides / missing input | Fix overrides; re-validate |
| HTTP 402 | License gate | Activate license in Settings |
| HTTP 409 on save | Id already exists | New id, or delete only if the user asked |

The MCP process **stays up** when the app is down. Retry tools after launch — do not restart the agent session unless the user asks.

## Suggested happy path

```
get_capabilities
→ get_guide(name="best-practices")
→ list_workflows
→ get_workflow(origin="builtin", id="quick-scout")
→ validate_config(workflow_id="quick-scout", overrides={...})
→ run_workflow(workflow_id="quick-scout", overrides={...})
→ get_status(run_id=...)  # poll until terminal
```

Prompt-only world (when a 360-LoRA is available):

```
list_360_loras → generate_seed → pick variations[0]
→ run_workflow(..., overrides={ input: { path, source: "file" }, prompt: {...} })
→ get_status
```

## curl fallback (no MCP)

If the MCP server cannot start (no python3), agents may use HTTP directly:

```bash
# Catalog
curl -s http://127.0.0.1:8000/api/workflows
curl -s http://127.0.0.1:8000/api/workflows/capabilities

# One template
curl -s http://127.0.0.1:8000/api/workflows/builtin/quick-scout

# Validate (dry_run)
curl -s -X POST http://127.0.0.1:8000/api/workflows/run \
  -H 'Content-Type: application/json' \
  -d '{"workflow_id":"quick-scout","origin":"builtin","dry_run":true,"overrides":{}}'

# Run
curl -s -X POST http://127.0.0.1:8000/api/workflows/run \
  -H 'Content-Type: application/json' \
  -d '{"workflow_id":"quick-scout","origin":"builtin","dry_run":false,"overrides":{"input":{"path":"/abs/seed.png","source":"file"}}}'

# Status / list / cancel
curl -s http://127.0.0.1:8000/api/renders/<run_id>
curl -s 'http://127.0.0.1:8000/api/renders/?lifecycle_status=active'
curl -s -X POST http://127.0.0.1:8000/api/renders/<run_id>/stop

# Persist (user shelf, create-only)
curl -s -X POST http://127.0.0.1:8000/api/workflows/user \
  -H 'Content-Type: application/json' \
  -d '{"id":"my-scout","document":{}}'

# Seed (360-LoRA)
curl -s http://127.0.0.1:8000/api/360-lora/loras
curl -s -X POST http://127.0.0.1:8000/api/360-lora/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"...","lora_path":"...","base_model":"sdxl","num_variations":4}'
```

## Not in this plugin

`start_wizard`, `compile_config`, `describe_image`, `upscale_image`, `list_worlds`, `list_presets`, batch/fan-out, run-ledger resume, in-place workflow UPDATE, Pattern generate. Do not invent those tools.

## Min app version

Requires 360 Hextile with **workflow automation P0** (`POST /api/workflows/run` + dry_run). Plugin package version: see `.claude-plugin/plugin.json`.
