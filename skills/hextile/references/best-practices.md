# Workflow automation — best practices

## Authority

Send **`workflow_id` + `overrides`**, or a full `document` you cloned from `get_workflow`. Never invent a raw config as the source of truth. The app deep-merges and runs `HextileConfig`. A 422 is a fixable override, not a crash.

## Merge

**Arrays replace wholesale.** To add a LoRA or a pass, send the **full** desired array (keep template entries you still want).

## Build loop

1. `get_capabilities` — confirm `run` + `dry_run`.
2. `get_guide` `workflow-schema` if you are composing fields.
3. `list_workflows` → pick `origin` + `id` (start from a builtin).
4. `get_workflow` → clone mentally; do not rewrite the whole file unless asked.
5. `validate_config` with the same overrides you will run.
6. Need a seed image? `list_360_loras` → allocate a UUID → `generate_seed(request_id=UUID, …)` → poll `get_seed_job(job_id)` until terminal. Pick the requested original `completed_variations[].index`, then set its path as `input.path` with `source=file`. Gaps do not shift indices.
7. `run_workflow` → keep `run_id`.
8. `get_status` until `completed` / `failed` / `cancelled` / `crashed`. Or `list_runs` if you lost the id.
9. Optional: `save_workflow` to `user`/`project` with a **new** id.

## Persist vs ephemeral

- Ephemeral: `run_workflow(document=…)` or template + overrides. Nothing written to the catalog.
- Persist: `save_workflow` then later `run_workflow(workflow_id=…)`. Create-only.

## Prompt and strength

- Change look first via `prompt.global` (and directional tiles if the template uses them).
- `diffusion.strength` is how hard the model overwrites the input. Scout low; remaster higher.
- Keep `hextile.template` unless the user asks for a resolution/tile change.

## Safety

- `get_gpu_diagnostics` reads `/api/processors/vram-status` through the app; the MCP process performs no GPU probe or cleanup. Read `device_index`, `accounting.nvml_sampled_at_unix_ms`, and `nvml_sample_age_ms` before describing a card snapshot. A cached sample retains its original acquisition time. Null sample fields mean NVML could not supply a valid sample.
- Card used/free, compute-PID attribution, Torch/CuPy allocator pools, and registered model footprints are overlapping facts, not additive buckets. WDDM may leave `this_app_bytes` and `unaccounted_bytes` null; compute process enumeration does not cover every graphics process. `busy_state=unknown` is not evidence that a holder can be evicted. Workload-specific pressure advice is only an estimate; runtime admission decides whether a job can run.
- `get_model_readiness` reports installation and selected-bundle fit against total card VRAM, not currently free headroom or a guaranteed render. Optional overrides explore a prospective profile only. Read `get_gpu_diagnostics` for current pressure and `get_model_download_queue` for active/queued admissions. Keep the default catalog's selected primary, complete bundle membership, and stable `selection_fingerprint` together when explaining an install.
- Before `install_model`, `repair_model`, or `cancel_model_download`, disclose exact registry IDs and obtain the external host's existing mutation confirmation. `bundle=true` needs the default-catalog fingerprint and the app rejects changed selection at enqueue; `bundle=false` queues just one identity. Repair targets a registry model and lets the app diagnose files; never supply a path or URL. Cancel needs the observed queued/active `queue_entry_id`, rejects a replaced or settled admission, and may return while the native writer is still cancelling. An `approved` tool argument is not authority. On an uncertain acknowledgement, read the queue before any repeat. Older app responses without fingerprint/token capability do not authorize a guarded action.
- An ordinary `retry_run(run_id)` sends no recovery body. Only after explicit user request and host mutation approval for the exact `run_id` and `vram_recovery=true` should the guarded retry be used; it may evict idle in-app holders for that render. An `approved` tool argument is not approval. Do not propose process killing or unload-all.
- Cancel with `cancel_run` before starting a second heavy GPU job if one is `running`. `stop_sequence` before a second sequence if one is `processing`.
- Keep the exact seed request and UUID through an uncertain acknowledgement. GET that UUID; if 404 is provisional, replay the same request and UUID or report unknown. `cancel_seed(job_id)` targets only that job.
- GPU upres of many frames is a sequence via `/hextile-upres`, not a loop of `run_workflow` over a frame folder.
- Do not delete builtin. Do not `delete_workflow` unless the user asked.
- License HTTP 402 → tell the user to activate in Settings.

## Website

Fetch URLs from `get_guide` `website-index` when you need product-domain depth (templates, pipelines, LoRA, prompts). Those pages exist on 360hextile.com today.
