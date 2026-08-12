<!-- Generated from skills/hextile/SKILL.md — edit SKILL.md only, then re-run: python3 codex/install.py --write-fragment -->

# 360 Hextile — Agent Skill

Drive **360 Hextile** (desktop app) while it is running on this machine. Tools talk to `http://127.0.0.1:8000` through the `hextile` MCP server. **The server owns config authority** — merge, `HextileConfig` validation, and queueing happen in the app, not in this plugin.

## Prerequisites

1. **360 Hextile is running** (backend on `127.0.0.1:8000`).
2. **python3 ≥ 3.9** on PATH (MCP proxy).
3. App build with **`POST /api/workflows/run`** (workflow automation P0+).

If a tool returns that the app is not running, tell the user verbatim:

> Launch 360 Hextile, then retry

Do not invent endpoints, do not hang, do not shell-sleep-poll forever without `get_status`.

## What a Workflow is

A **Workflow is a full `.hextile.json` config document** (pipeline / hextile / input / diffusion / prompt / post / …), stored on shelves:

| origin   | Meaning                          |
|----------|----------------------------------|
| `builtin`| Shipped templates (e.g. `quick-scout`) |
| `user`   | User library                     |
| `project`| Project-scoped                   |

There is **no** separate workflow-envelope format — only `.hextile.json`. Prefer shelf `workflow_id` + overrides over inventing a full raw document.

## Tools (exactly 7)

| Tool | When to use | Mutates? |
|------|-------------|----------|
| `list_workflows` | Discover templates | no |
| `get_workflow` | Read one template before override | no |
| `validate_config` | Dry-run merge+validate (terraform plan) | no |
| `run_workflow` | Queue a render after overrides | **yes** |
| `get_status` | Poll `run_id` progress / output paths | no |
| `cancel_run` | Kill a long GPU run | **yes** |
| `generate_seed` | Create equirect seed image via 360-LoRA | **yes** |

### Selection guide

1. **Discover** → `list_workflows` → pick `origin` + `id`.
2. **Inspect** → `get_workflow` if you need defaults before overriding.
3. **Plan** → `validate_config` with the same overrides you intend to run.
4. **Need a source image from a prompt?** → `generate_seed` first (two-step below).
5. **Run** → `run_workflow` → keep `run_id` → `get_status` until terminal (`completed` / `failed` / `cancelled` / `crashed`).
6. **Abort** → `cancel_run`.

## Safety — overrides, never authority

- Send **`workflow_id` + `overrides`** (and optional `output`). Prefer **not** composing a full raw config as authority.
- The app deep-merges and validates. A bad override is a structured **422** — read it, fix the override, retry.
- **List / array merge: REPLACE wholesale.** Overriding `passes[]`, `lora.models[]`, or any array **replaces** the template list; it does **not** append. To “add a LoRA”, include the full desired array (template entries you want to keep + your addition).
- Live runs need a non-empty **`input.path`** after merge (empty path is only OK on dry_run).

## Seed → run (two-step)

`InputSource` is `file` | `render` only. Generative producers are **not** render-time sources.

1. `generate_seed(prompt, lora_path, base_model, n?)` → response has `variations` (absolute paths) + `batch_id`.
2. Pick one path (default index 0 unless the user chooses).
3. `run_workflow` with overrides:

```json
{
  "input": {
    "path": "/absolute/path/from/variations[i]",
    "source": "file"
  }
}
```

Never write retired source types (`pattern`, `360_lora`, …) into render-time `input.source`.

`generate_seed` hits **`POST /api/360-lora/generate`** (not `/api/lora-360`). It needs a real `lora_path` and `base_model` (`sdxl` | `sd15` | `flux_schnell`) known to the running app.

## App-down / upgrade recovery

| Symptom | Meaning | What to tell the user |
|---------|---------|------------------------|
| Error contains “isn't running” / “Launch 360 Hextile” | Backend not up | Launch 360 Hextile, then retry |
| “Upgrade 360 Hextile (needs workflows/run)” | App too old | Update the app to a build with workflow run |
| HTTP 422 with validation detail | Bad overrides / missing input | Fix overrides; re-validate |
| HTTP 402 | License gate | Activate license in Settings |

The MCP process **stays up** when the app is down. Retry tools after launch — do not restart the agent session unless the user asks.

## Suggested happy path

```
list_workflows
→ get_workflow(origin="builtin", id="quick-scout")   # optional
→ validate_config(workflow_id="quick-scout", overrides={...})
→ run_workflow(workflow_id="quick-scout", overrides={...})
→ get_status(run_id=...)  # poll until terminal
```

Prompt-only world (when a 360-LoRA is available):

```
generate_seed → pick variations[0]
→ run_workflow(..., overrides={ input: { path, source: "file" }, prompt: {...} })
→ get_status
```

## curl fallback (no MCP)

If the MCP server cannot start (no python3), agents may use HTTP directly:

```bash
# Catalog
curl -s http://127.0.0.1:8000/api/workflows

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

# Status / cancel
curl -s http://127.0.0.1:8000/api/renders/<run_id>
curl -s -X POST http://127.0.0.1:8000/api/renders/<run_id>/stop

# Seed (360-LoRA)
curl -s -X POST http://127.0.0.1:8000/api/360-lora/generate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"...","lora_path":"...","base_model":"sdxl","num_variations":4}'
```

## Not in v1

`start_wizard`, `compile_config`, `describe_image`, `upscale_image`, `list_worlds`, `list_presets`, batch/fan-out, run-ledger resume. Do not invent those tools.

## Min app version

Requires 360 Hextile with **workflow automation P0** (`POST /api/workflows/run` + dry_run). Plugin package version: see `.claude-plugin/plugin.json`.
