---
name: hextile
description: "Drive 360 Hextile workflows from an AI coding agent. Use when generating 360° panoramas, listing or saving workflow templates, overriding prompts, dry-run validation, 360-LoRA seeds, polling or listing renders, Batch jobs over many local images, or reading bundled automation guides."
---

# 360 Hextile — Agent Skill

Drive **360 Hextile** (desktop app) while it is running on this machine. Tools talk to `http://127.0.0.1:8000` through the `hextile` MCP server. **The server owns config authority** — merge, `HextileConfig` validation, and queueing happen in the app, not in this plugin.

Install id: **`hextile-agent@360-hextile`**. Marketplace `360-hextile`. Skill/MCP token `hextile`. Do not install `hextile@360-hextile`.

See `references/agent-lexicon.md` for locked words, the complementarity card, and `REFUSE_*` strings.

- Copilot never queues GPU (`REFUSE_RENDER_COPILOT`). Press RENDER, or use `run_workflow` here.
- MCP live-apply (`apply_config_delta`) needs follow ON (`REFUSE_LIVE_FOLLOW_OFF`). Gate A stays locked. Follow is not Copilot Auto.
- MCP cannot `goto` or operate Layers (`REFUSE_NAV_MCP`, `REFUSE_LAYERS_MCP`).

Before composing fields or teaching the user, call `get_guide` (`workflow-schema`, `best-practices`, `recipes`, `website-index`). Fetch website URLs from `website-index` when you need product-domain depth.

## Prerequisites

1. **360 Hextile is running** (backend on `127.0.0.1:8000`).
2. **python3 ≥ 3.9** on PATH (MCP proxy). On Windows, Claude `.mcp.json` `command` should be `python` or `py -3`, not `python3`. Codex installer already writes `sys.executable`.
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
| `get_live_context` | Studio snapshot (follow + doc_generation + live export) | no |
| `apply_config_delta` | Live delta on the open file (follow ON). Never queues | **yes** |
| `get_guide` | Schema, practices, recipes, website index | no |
| `list_workflows` | Discover templates | no |
| `get_workflow` | Read one template before override or clone | no |
| `save_workflow` | Persist a **new** id on user/project | **yes** |
| `delete_workflow` | Remove a user/project workflow | **yes** |
| `validate_config` | Dry-run merge+validate (terraform plan) | no |
| `list_installed_models` | Installed weights — dry-run is Pydantic only | no |
| `get_model_readiness` | Read current model fit, missing components, and install bundles | no |
| `get_model_download_queue` | Read active/queued downloads and exact queue-entry tokens | no |
| `install_model` | Queue one registry model or a fingerprint-bound selected bundle | **yes** |
| `repair_model` | Request server-diagnosed repair of one registry model | **yes** |
| `cancel_model_download` | Request stop of one observed queue admission | **yes** |
| `run_workflow` | Queue a render after overrides | **yes** |
| `get_status` | Poll `run_id` progress / output paths | no |
| `get_gpu_diagnostics` | Read app-owned GPU/VRAM snapshot, its sample age, and known/unknown holders | no |
| `get_seed_memory_advice` | Read separate GPU status and selected 360-LoRA resolution pressure advice | no |
| `get_render_config` | Read producing .hextile.json for a render | no |
| `get_logs` | Fetch failed-run logs | no |
| `list_runs` | Find jobs if you lost `run_id` | no |
| `cancel_run` | Kill a long GPU run | **yes** |
| `retry_run` | Retry a crashed/failed run; `vram_recovery=true` explicitly permits guarded idle-holder eviction for that exact render | **yes** |
| `list_360_loras` | Discover `path` + `base_model` for seeds | no |
| `generate_seed` | Submit a 360-LoRA job with a caller-supplied UUID | **yes** |
| `get_seed_job` | Read exact seed job progress, original-index results, and errors | no |
| `list_seed_history` | List stored seed batches | no |
| `get_seed_batch` | Read one stored seed batch | no |
| `cancel_seed` | Cancel the named 360-LoRA job, not a render | **yes** |
| `extract_sequence_video` | Extract video frames to a folder (video sequence door) | **yes** |
| `create_sequence` | Create a `seq_*` from a folder + full config (not `run_workflow`) | **yes** |
| `start_sequence` | Queue GPU for an existing sequence | **yes** |
| `get_sequence` | Poll `sequence_id` progress / output (not `get_status`) | no |
| `list_sequences` | Find sequences if you lost `sequence_id` | no |
| `stop_sequence` | Kill a running sequence | **yes** |
| `preflight_batch` | Strict Batch preflight over a frozen workflow + folder/files | **yes** |
| `get_batch_preflight` | Poll preflight until ready/failed/expired; get spec_hash | no |
| `start_batch` | Queue a Batch job from a ready preflight (not run_workflow) | **yes** |
| `list_batches` | List Batch jobs (not list_runs) | no |
| `get_batch` | Poll one Batch job (never pass job_id to get_status) | no |
| `get_batch_items` | Page Batch item attempts | no |
| `pause_batch` | Pause after the current item publishes | **yes** |
| `resume_batch` | Resume a paused Batch job (same frozen recipe) | **yes** |
| `cancel_batch` | Terminal cancel; keep published files (not cancel_run) | **yes** |
| `retry_batch` | Retry failed/interrupted items on the same job | **yes** |
| `import_batch_outputs` | Import published outputs into the Library (no GPU) | **yes** |
| `get_unreal_export_catalog` | Read the Unreal export catalog (empty is valid) | no |
| `preflight_unreal_export` | Advisory Unreal-project preflight (same body as the Export modal) | no |
| `export_to_unreal_project` | Export HDR (+ optional masters) into a paired Unreal project. Requires APP Unreal-project sink and actual M1 state. 300s, one attempt | **yes** |
| `preflight_file_export` | Check exact source revision and proposed ordinary file destinations before export | no |
| `export_render_file` | Export an ordinary render file after preflight and approval; create-only, 300s, one attempt | **yes** |

### Selection guide

1. **Handshake** → `get_capabilities` then `get_live_context` before compose.
2. **Learn** → `get_guide` (`best-practices` then `workflow-schema`).
3. **Discover** → `list_workflows` → pick `origin` + `id`.
4. **Inspect** → `get_workflow` if you need defaults before overriding.
5. **Plan** → `validate_config` with the same overrides you intend to run.
6. **Need a source image from a prompt?** → `list_360_loras`, choose a listed preset, then `get_seed_memory_advice` before approved `generate_seed` with a fresh UUID; poll `get_seed_job` (below).
7. **Run** → `run_workflow` → keep `run_id` → `get_status` until terminal (`completed` / `failed` / `cancelled` / `crashed`). Use `list_runs` if the id is lost.
8. **Save a variant** → `save_workflow` (`user`/`project`, **new** id). Create-only; 409 means pick another id.
9. **Abort** → `cancel_run`.
10. **Many local images, one frozen recipe** → `preflight_batch` → `get_batch_preflight` → `start_batch` (not `run_workflow`, not a Sequence). Poll `get_batch`. Abort with `cancel_batch`.

### GPU diagnosis and retry

Call `get_gpu_diagnostics` for the app's selected device and current status. The NVML card/process sample may be cached: read `accounting.nvml_sampled_at_unix_ms` and `nvml_sample_age_ms`; null means no valid NVML sample. Device use, per-process attribution, Torch/CuPy pools, and registered holder footprints overlap, so do not add them together or claim a workload will fit. WDDM can leave this-app attribution null; a holder's `busy_state=unknown` is not idle. Use a workload-specific estimator where available and keep its result advisory.

`retry_run(run_id)` is an ordinary retry. If the user explicitly requests idle-holder recovery for that failed render, explain that `retry_run(run_id, vram_recovery=true)` may evict idle in-app GPU holders for that exact render, then obtain the host's mutation approval for those exact arguments. Tool arguments such as `approved:true` are never approval. Do not offer process killing or unload-all as a diagnosis action.

### Model readiness and queue actions

Read `get_model_readiness` and `get_model_download_queue` first. Disclose the exact `pipeline_id` / `model_id`, selected bundle members, and current queue state before asking the host for its existing mutation confirmation. `get_model_readiness.overrides` is for exploring fit only; refresh the default catalog without overrides before a bundle install. Fit compares estimated workload needs with total card capacity; use `get_gpu_diagnostics` to explain current free pressure separately.

For `install_model(bundle=true)`, use the selected primary's current default-catalog `selection_fingerprint` as `expected_selection_fingerprint`. The app checks it again at admission and rejects drift with 409. `bundle=false` queues only the named registry identity. `repair_model` names one registry identity; the app diagnoses files itself. Do not pass paths, URLs, or an `approved` flag. For `cancel_model_download`, use the `queue_entry_id` from the exact queued/active row as `expected_queue_entry_id`. A successful cancel response requests a stop; keep reading the queue until it settles. If an action acknowledgement is unknown, read the queue before any repeat. If the app lacks the fingerprint or queue-entry token, do not make the guarded action.

## Upres

GPU upres of a still, a folder, or a video → skill `/hextile-upres`. Do not invent `upscale_image`. Do not send GPU upres through `hextile-pipe`.

Stills use `run_workflow` with builtin `upres-still`. Folders and video use that skill's sequence door (`run_workflow` cannot create a `seq_*`). Recipes E (still) and F (folder/video) live in `get_guide` `recipes`.

## Safety — overrides, never authority

- Send **`workflow_id` + `overrides`** (and optional `output`). Prefer **not** composing a full raw config as authority.
- The app deep-merges and validates. A bad override is a structured **422** — read it, fix the override, retry.
- **List / array merge: REPLACE wholesale.** Overriding `passes[]`, `lora.models[]`, or any array **replaces** the template list; it does **not** append. To “add a LoRA”, include the full desired array (template entries you want to keep + your addition).
- Live runs need a non-empty **`input.path`** after merge (empty path is only OK on dry_run).
- `input.source` is `file` or `render` only.

## Seed → run (two-step)

`InputSource` is `file` | `render` only. Generative producers are **not** render-time sources.

1. `list_360_loras` → pick `path`, `base_model`, and an exact listed `resolution_presets` width/height (`sdxl` | `sd15` | `flux_schnell` | `qwen_image`).
2. `get_seed_memory_advice(lora_path, width, height, cpu_offload?)` → read its separate GPU snapshot and APP risk estimate. High/unknown risk never auto-selects CPU offload or low-VRAM override; the APP revalidates at admission.
3. Obtain host mutation approval for the exact options; allocate a UUID, then `generate_seed(request_id, prompt, lora_path, base_model, n?, width?, height?, resolution_preset?, cpu_offload?, allow_low_vram?)` → short acknowledgement with `job_id`. A supplied preset is the listed `widthxheight` string matching the supplied dimensions; omit options to retain APP defaults.
4. Poll `get_seed_job(job_id)` until terminal. Read `completed_variations` by each entry's original `index` (which may have gaps); choose the requested index and its absolute path. A missing index is unavailable, even if another variation succeeded.
5. `run_workflow` with overrides:

```json
{
  "input": {
    "path": "/absolute/path/from/completed_variations[index=i]",
    "source": "file"
  }
}
```

Never write retired source types (`pattern`, `360_lora`, …) into render-time `input.source`.

`generate_seed` submits to **`POST /api/360-lora/jobs`**. Keep the UUID and exact request body until the job is resolved. If its acknowledgement is uncertain, call `get_seed_job` with that UUID. A 404 while submission is outstanding is provisional: replay the same UUID and body or report unknown; never mint another ID automatically. `get_seed_job` exposes progress, original-index paths and seeds, and typed terminal errors. `completed`, `partial`, `cancelled`, `failed`, and `interrupted` are terminal; only `completed` means every requested variation succeeded. Historical `list_seed_history` / `get_seed_batch` read stored batches.

`cancel_seed(job_id)` targets only that exact seed job. Undo of a live apply is studio `loadConfig`, not this tool. Follow GET-apply of a finished **run** may still paint sliders (accepted leftover).

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
→ get_live_context
→ get_guide(name="best-practices")
→ list_workflows
→ get_workflow(origin="builtin", id="quick-scout")
→ validate_config(workflow_id="quick-scout", overrides={...})
→ run_workflow(workflow_id="quick-scout", overrides={...})
→ get_status(run_id=...)  # poll until terminal
```

Prompt-only world (when a 360-LoRA is available):

```
list_360_loras → generate_seed(request_id=<UUID>, ...) → get_seed_job(job_id)
→ pick completed_variations entry with index=0 when available
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
curl -s -X POST http://127.0.0.1:8000/api/360-lora/jobs \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"00000000-0000-4000-8000-000000000001","prompt":"...","lora_path":"...","base_model":"sdxl","num_variations":4}'
curl -s http://127.0.0.1:8000/api/360-lora/jobs/00000000-0000-4000-8000-000000000001
```

## Not in this plugin

`start_wizard`, `compile_config`, `describe_image`, `upscale_image`, `list_worlds`, `list_presets`, run-ledger resume, in-place workflow UPDATE, Pattern generate. Do not invent those tools. Batch is the eleven `*_batch*` tools above — not `run_workflow` per file, not a Sequence, not `/api/batch-convert`. Never put a Batch job id into `get_status` / `cancel_run`.

## Min app version

Requires 360 Hextile with **workflow automation P0** (`POST /api/workflows/run` + dry_run). Plugin package version: see `.claude-plugin/plugin.json`.
