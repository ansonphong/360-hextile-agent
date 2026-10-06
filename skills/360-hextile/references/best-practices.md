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
6. Need a seed image? `list_360_loras` → choose a listed resolution → `get_seed_memory_advice` → approve exact generation options, allocate a UUID, and `generate_seed(request_id=UUID, …)` → poll `get_seed_job(job_id)` until terminal. Pick the requested original `completed_variations[].index`, then set its path as `input.path` with `source=file`. Gaps do not shift indices.
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
- `get_seed_memory_advice(lora_path,width,height,cpu_offload?)` reads GPU status and APP's exact-LoRA/resolution memory advice separately. Its `sampling=separate` means GPU NVML timestamp/age applies only to `gpu`, not to `advice`; their free/total readings may differ. Preserve null/unknown values. APP 404/409/422 errors are refusals, not a partial success. Select a listed `resolution_preset` matching `widthxheight`; `generate_seed` passes it, `cpu_offload`, and `allow_low_vram` only when explicitly supplied. Never infer either override from high/unknown risk; no read reserves capacity or guarantees fit.
- `get_model_readiness` reports installation and selected-bundle fit against total card VRAM, not currently free headroom or a guaranteed render. Optional overrides explore a prospective profile only. Read `get_gpu_diagnostics` for current pressure and `get_model_download_queue` for active/queued admissions. Keep the default catalog's selected primary, complete bundle membership, and stable `selection_fingerprint` together when explaining an install.
- Before `install_model`, `repair_model`, or `cancel_model_download`, disclose exact registry IDs and obtain the external host's existing mutation confirmation. `bundle=true` needs the default-catalog fingerprint and the app rejects changed selection at enqueue; `bundle=false` queues just one identity. Repair targets a registry model and lets the app diagnose files; never supply a path or URL. Cancel needs the observed queued/active `queue_entry_id`, rejects a replaced or settled admission, and may return while the native writer is still cancelling. An `approved` tool argument is not authority. On an uncertain acknowledgement, read the queue before any repeat. Older app responses without fingerprint/token capability do not authorize a guarded action.
- An ordinary `retry_run(run_id)` sends no recovery body. Only after explicit user request and host mutation approval for the exact `run_id` and `vram_recovery=true` should the guarded retry be used; it may evict idle in-app holders for that render. An `approved` tool argument is not approval. Do not propose process killing or unload-all.
- Cancel with `cancel_run` before starting a second heavy GPU job if one is `running`. `stop_sequence` before a second sequence if one is `processing`.
- Keep the exact seed request and UUID through an uncertain acknowledgement. GET that UUID; if 404 is provisional, replay the same request and UUID or report unknown. `cancel_seed(job_id)` targets only that job.
- GPU upres of many frames is a sequence via the `upres` skill, not a loop of `run_workflow` over a frame folder.
- Do not delete builtin. Do not `delete_workflow` unless the user asked.
- License HTTP 402 → tell the user to activate in Settings.

## Website

Fetch URLs from `get_guide` `website-index` when you need product-domain depth (templates, pipelines, LoRA, prompts). Those pages exist on 360hextile.com today.

## Shader authoring — Playable Shaders

The installed **`shader`** skill is the authoritative Shader procedure: discover actual tools and current surface/grants, negotiate `workspace_schema:2` for a complete portable source set or omit it for legacy v1, acknowledge attachment, prove shared-file bytes, edit only declared native members, register their format-specific raw digest and poll `get_shader_update` with handle/change_id and the Project witness returned by open. V2 `authoring.json` is metadata-only; actual root/helpers stay in declared `.glsl` files. Include unused helpers, Looks and provenance in the v2 digest; preserve BOM/CRLF raw bytes, while resources/originals have separate verification. Registration starts APP Prepare and mounted compilation automatically. `include_source_diagnostics:true` retrieves bounded logical file/line diagnostics only under the current source grant/witness/candidate; omission stays source-free. Candidate-bound diagnostics drive at most three repairs; each repair registers the full set with a fresh canonical change ID. Unavailable/unsupported or exhausted checks report real receipts and host re-entry Retry guidance. Human Apply remains separate. A known schema or source fixture does not prove installed executable support or shared-file access.

One Project-root Git repository may track many complete `shaders/<stable-id>/workspace/` trees and `360-project.index.json`. Track authored config, GLSL, Looks, provenance, every referenced PNG/frame, optional original and cover image. The minimal ignore template at https://360hextile.com/static/templates/shaders/gitignore.txt applies only after **all covered Shaders** have complete verified portable copies; mixed legacy Projects need explicit per-asset rules. The attributes example at https://360hextile.com/static/templates/shaders/gitattributes.txt uses `-text` for `workspace/**` and the marker to preserve bytes. Optional LFS needs materialized objects before clone admission. Native Git diff/commit/checkout/push requires the user's instruction; MCP has no Git tool. Clone opens unaccepted drafts, keeps stable IDs and requires Fork on collision. Text Undo, reviewed structural discard, checkpoint Revert (new revision), and Git restore are distinct; Git restoration needs new register/Prepare/review/human Apply. `.hexshader` export reads the accepted snapshot only, including unused modules, Looks, provenance and every referenced ordered frame, never dirty files or Git HEAD; original inclusion remains truthful and rights-gated. See https://360hextile.com/docs/create/shader for the human journey.

Teach the keyboard recipe first **only with observed APP available/execution_ready and executable `conventions/1` plus `modulation/2`**: resolved Enter binding → stable default-0 logical gate → schema-2 oneshot BLAST (20 ms Linear Attack, 400 ms Ease Out Release, exponent 2) → existing Range mapping to Glow → declared parameter/default/bounds and GLSL glow term. Keep sorted `conventions/1`, `master-clock/1`, `modulation/2` declarations in the acknowledged authoring metadata and default Master Clock explicit. Use current acknowledged closed schema/guide fields and aliases; no invented ADSR/event fields or header capability declarations. A base without declarations may opt in only with executable support. Preserve unknown extensions/resources, v1's immutable sidecar base or v2's complete metadata/inventory, and modulation schema 2 on later edits.

Focused keyboard, a future MIDI note, OSC `/blast` with sender event identity or an observed gamepad button may target that same typed input through INPUT session routes **only if the adapters are installed and admitted**. Portable conventions bindings stay keyboard/mouse; no MIDI/OSC/gamepad kinds, session routes or live events belong in Shader files. Active host source epoch/sequence ordering and deduplication OR held contributors: first press emits down, another fresh press trigger, intermediate release no edge, final release up; pulses do not hold. Held follows aggregate transitions; Burst retriggers from its current level without stacked voices or two guaranteed visible pulses. Source loss removes only that source's holds. Known future adapters are not installed capability evidence.

Input-schema/orphaned-input/default-1 gate/mapping/compiler diagnostics use the same bounded register/poll repair loop; unsupported runtime or unavailable compiler ends truthfully. Report shortcut conflicts with an explicit remap choice without pressing keys or changing Hotkeys. Control authoring never arms Interact, sets live pose, writes a Project preset registry or self Applies. Read current `recipes` and acknowledged workspace instructions for exact supported definitions; compiler success does not prove physical Enter behavior.

Read Recipes I and J for the fly-fractal and camera-null Enter BLAST sources and exact resolved extension shape. Generated helpers are `vec3 hxCameraPosition`, `mat3 hxCameraRotation`, `vec3 hxCameraDirection(vec3 canonicalDirection)`; a null camera supplies zero position and identity rotation. A fly shader must wire its own ray origin/direction through these helpers once. Channel/modulator aliases are generated `uniform float <alias>` when referenced, with ASCII `[A-Za-z][A-Za-z0-9_]{0,31}`, no `__`, unique across both namespaces and no GLSL/host/source collisions. Always use the generated parameter `hxParam_<id>` in the effect; bindings do not rewrite GLSL.

For one-prompt creation, use an attachable nonconvention base (saved or unaccepted v2), and edit the parameter manifest/GLSL plus values/space/extensions in the **acknowledged** authoring format together. The closed resolved convention schema is 1 and modulation with envelopes is schema 2; these are separate from workspace v1's sidecar schema 1 and v2's metadata schema 2. Preserve v2's closed keys and complete source/Look/provenance/original inventories, without adding a v1 `base`. Use only resolved built-in bytes already present or supplied, or already authorized current Project-derived bytes; no profile-fetch tool or manual preset Apply is part of the agent flow. Preserve sorted capability declarations and the required default Master Clock object; envelopes advance on monotonic interaction time, independently of BPM/Speed and scene pause, without another clock-bit gate.

Attack/Release settings are finite seconds `[0,60]`, independently curved with `linear` (exponent 1), `ease_in`, `ease_out` or `ease_in_out` (preset exponent 2, allowed `[1,8]`). Explicit example settings are 20 ms/400 ms; Held alternative is 200 ms/600 ms. Held sustains until aggregate release; Burst (`oneshot`) completes even after key-up/long hold, retriggers from its current level without stacked voices, and permits Attack 0. Keep existing envelope IDs and schema 2 on revisions. Report unsupported `modulation/2` or malformed/orphaned-input diagnostics through the bounded loop; do not replace unknown extensions or pretend compilation passed.

Legacy proposals and every Board row preserve convention bytes, envelope rows/input refs/settings/envelope-sourced mappings and Playable capability declarations; existing schema-1 typed time/click/clock edits retain their original scope. First creation/revision needs the exact current server-attested workspace, not a model flag. Human review must expose controls, modulators, parameters/values and source together beside exact Prepare/compile/preview/save states. Source/check grants never grant physical input or self Apply.
