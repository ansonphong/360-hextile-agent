# Recipes

## A — Prompt-only scout from a builtin

```
get_workflow(origin=builtin, id=quick-scout)
validate_config(workflow_id=quick-scout, origin=builtin, overrides={
  "prompt": {"global": "<user look>"},
  "input": {"path": "<existing equirect file>", "source": "file"}
})
run_workflow(same)
get_status(run_id)
```

`quick-scout` ships with empty `input.path` — a live run needs a file (or a seed from B).

## B — 360-LoRA seed then run

```
list_360_loras          # pick path + base_model (and trigger_word if listed)
generate_seed(request_id=<fresh UUID>, prompt, lora_path, base_model, n=4)
get_seed_job(job_id)    # poll until terminal; retain this exact ID
# choose completed_variations entry with original index=0, if present
run_workflow(workflow_id=quick-scout, overrides={
  "input": {"path": "<variation path>", "source": "file"},
  "prompt": {"global": "<look>"}
})
```

If the submit acknowledgement is uncertain, GET the same UUID. A provisional 404 permits replay of the identical body and UUID, never a new ID. `partial`, `cancelled`, `failed`, and `interrupted` may retain completed entries; report the status and any missing requested index honestly. `cancel_seed(job_id)` affects only that seed job. `list_seed_history` and `get_seed_batch` remain for stored batches.

## C — Save a user variant

```
get_workflow(builtin, quick-scout) → document
# apply overrides locally
validate_config(document=modified)
save_workflow(origin=user, id=my-scout, document=modified)
run_workflow(workflow_id=my-scout, origin=user)
```

If `save_workflow` returns 409, pick a new id or `delete_workflow` first (only if the user asked).

## D — Monitor without run_id

```
list_runs(lifecycle_status=active)
# each row: render_id, status, progress, output_path
get_status(run_id=render_id)
```

Terminal statuses: `completed`, `failed`, `cancelled`, `crashed`. Stop uses `cancelled`, not `stopped`.

## Recipe E — Upres still

```
get_capabilities
validate_config(workflow_id=upres-still, origin=builtin, overrides={
  "input": {"path": "<absolute image path>", "source": "file"},
  "upscaling": {"scale_factor": 2 or 4}
})
run_workflow(same)
get_status(run_id)
```

Builtin default is 4×. Omit `upscaling` unless the user asked for 2×. `cancel_run` before a second heavy GPU job.

## Recipe F — Upres folder or video

`run_workflow` cannot create a `seq_*`. Do not loop `run_workflow` over frames.

Tools: `extract_sequence_video`, `create_sequence`, `start_sequence`, `get_sequence`, `list_sequences`, `stop_sequence`.

```
extract_sequence_video({video_path})   # video only; use returned folder_path
get_workflow(origin=builtin, id=upres-still)
# If pipeline is top-level, that object is the document.
# If nested document.pipeline exists, use that nested object.
# Patch input.mode=sequence, input.path=<folder>, input.source=file,
# optional upscaling.scale_factor 2 or 4.
# Never POST a partial {pipeline, input, upscaling} stub — 422.
create_sequence({folder_path, config: patched document})
start_sequence
get_sequence   # poll while queued or processing
```

`create_sequence.config` is a full `HextileConfig` cloned from `upres-still`. Stop and report `paused` and `incomplete` (`incomplete` is resumable in the app; v1 has no resume MCP tool). Terminal: `completed`, `completed_with_errors`, `failed`, `cancelled`. Cancel with `stop_sequence`. Use `list_sequences` if the id is lost.

## Recipe G — Generate Layer into an exact saved render

No viewer is required. Ask for approval for each mutating call. Find the exact saved render in `list_runs`; its `head` is the parent for `get_layer_draft`. Here `r-123` and the 12-hex HEAD are samples. Use the actual IDs returned by each call.

```text
list_runs                         # choose exact render_id and its head
get_layer_draft(render_id="r-123", parent_id="aaaaaaaaaaaa")
# Require head == parent_id, studio_owned == false. If a draft exists, carry its
# exact draft_id and mutation_rev; otherwise omit both for pointer-first creation.
generate_layer(target="saved", render_id="r-123",
  request_id="11111111111111111111111111111111",
  expected_head="aaaaaaaaaaaa", parent_id="aaaaaaaaaaaa", mode="generate",
  controls={"pipeline":"sdxl","prompt":{"global":"a red enamel pin","negative":""},
    "diffusion":{"model":"sdxl-base","quantization":"none","seed":1,
      "scheduler":"euler","guidance_scale":7.5,"num_inference_steps":20},
    "output":{"width":1024,"height":1024},"alpha":{"mode":"opaque"},
    "layer_gen":{"scene":{"enabled":false}}})
get_layer_generation(render_id="r-123", job_id="11111111111111111111111111111111")
# Poll until ready. Keep the returned draft_id and composition_id.
land_generated_layer(target="saved", render_id="r-123", layer_id="pl_enamel1",
  job_id="11111111111111111111111111111111", expected_head="aaaaaaaaaaaa",
  draft_id=<returned draft_id>, composition_id=<returned composition_id>,
  expected_mutation_rev=1,
  pose={"yaw":20,"pitch":-10,"roll":0,"fov":40,"projection":"auto"})
commit_layer_draft(target="saved", render_id="r-123", parent_id="aaaaaaaaaaaa",
  draft_id=<same draft_id>, composition_id=<same composition_id>,
  expected_mutation_rev=<land receipt mutation_rev>)
```

`ready` does not insert a layer. Land does not commit. To stop the exact job, call `cancel_layer_generation(render_id="r-123", job_id=<job id>)` and poll until released; omitted `execution_target` means saved. `rematte_layer` also defaults to saved, and its separate `target` identifies the witnessed recovery take. If any acknowledgement is lost, reconcile the exact request/job/layer/draft IDs with `get_layer_generation` and `get_layer_draft`; inspect graph HEAD for an uncertain commit. Never send a fresh request or layer ID as an automatic retry. A missing RAM job after restart remains unknown. A rematte reply loss is non-replayable because APP chooses its new job ID; inspect local job activity before asking for another approved action. Max 64 layers per draft and eight takes per Re-gen layer. External `target=live` or `execution_target=live` is forbidden; an approved internal Copilot child may control only its matching armed live job.

## Recipe H — Spot Clone on an exact saved render

No viewer is required. Choose a final parent and explicit source/destination poses; ask approval for the clone write. The IDs below are examples, not defaults.

```text
preflight_spot_clone(render_id="r-123", parent_id="aaaaaaaaaaaa")
# Require head == parent_id and parent_final. Choose a listed template/raster
# fingerprint, or use a deterministic shape with no fingerprint.
clone_spot(target="saved", render_id="r-123", node_id="bbbbbbbbbbbb",
  expected_head="aaaaaaaaaaaa", parent_id="aaaaaaaaaaaa",
  destination_id="spot-copy-1",
  source_pose={"yaw":10,"pitch":0},
  destination_pose={"yaw":30,"pitch":10,"roll":0,"fov":45},
  mask={"source":"shape","shape":"hexagon"})
get_spot_clone(render_id="r-123", node_id="bbbbbbbbbbbb")
```

For `from_template`, use the listed `template_id` and `face_index`; for `from_raster`, use a listed render-local `raster_id`. Both require `expected_mask_fingerprint` equal to that preflight entry's fingerprint. A changed mask or HEAD refuses the write. On a lost POST reply, read the exact node receipt and recipe digest; 404 remains unknown, not permission for a new id. Stop cannot undo a graph node that already committed. External MCP cannot use `target=live`; Copilot may commit only an already prepared, matching Spot Clone operation after its own approval.
