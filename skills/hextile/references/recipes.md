# Recipes

## Shader file session — complete portable source set

Invoke the installed `360-hextile` skill through the host's observed picker. Verify actual `tools/list`, APP Follow and whole-source grant, then request `open_shader_workspace(workspace_schema=2, ...)` using the live Shader/surface/Project witness. Require a v2 acknowledgement and native access to the same files; if v2 or `source-modules/1` execution is unavailable, report upgrade/reconnect/unsupported rather than describing this as a working compiled session. Legacy v1 still uses the two acknowledged files and its own digest.

In `workspace/authoring.json`, keep the acknowledged schema-2 metadata and declare `source.glsl` plus every helper logical path in `source_files`; keep actual GLSL only in those `.glsl` files. For example, root `source.glsl` may include `#include "lib/noise.glsl"` at file scope, with `workspace/lib/noise.glsl` declared and authored as a helper. Include paths are relative to the including file, depth-first/include-once in one namespace. A missing target, cycle or bad path needs a real located diagnostic. Keep declared but unused helpers in the full-set digest, bundle and Git worktree. Track Looks, provenance, `workspace/resources/` PNGs and every referenced sequence frame rather than one preview frame.

Read the acknowledged v2 digest framing and compare it to the open receipt before editing. After native edits, hash raw `authoring.json` plus all declared source/Look/provenance members in the prescribed UTF-8 path order without newline or JSON normalization. `register_shader_update` freezes this complete candidate; `get_shader_update(..., include_source_diagnostics=true)` can return bounded logical locations under current grant/witness/candidate. Repair a located helper, recompute the whole digest and register a new canonical change ID. Review the complete candidate and check states with the human; only the human Applies. The agent never uses a Git commit, checkout or `.hexshader` export as a substitute for registration. Human detail: https://360hextile.com/docs/create/shader.

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

## Recipe I — Playable Shaders: fly source

Use the installed `360-hextile` skill's attested two-file procedure. Start from a clean Project Shader, observe acknowledged `available=true` and executable `conventions/1`, then use a fully resolved supported definition in `authoring.json`. A preset ID is not a definition: use shipped resolved bytes already in the document or supplied by the user, or already authorized current Project-derived bytes. Never fetch an arbitrary Project profile or require manual preset Apply to complete agent authoring.

This source matches the shipped fly-fractal example. Keep sidecar `space="direction_360"`, values `{ "fold":1, "hue":0 }`, and sorted `extensions.required_capabilities=["conventions/1"]` alongside the resolved convention. Preserve the acknowledged base, capture defaults, output and unrelated extensions/resources.

```glsl
/*@hextile-shader
{"schema":1,"abi":"hx-shader/1","space":"direction_360","parameters":[{"id":"fold","type":"float","label":"Fold","default":1,"ui":{"min":0.5,"max":1.5}},{"id":"hue","type":"float","label":"Hue shift","default":0,"ui":{"min":0,"max":1}}]}
*/
float sceneDist(vec3 point) {
    vec3 q = mod(point + 2.0, 4.0) - 2.0;
    float scale = 1.0;
    for (int i = 0; i < 4; i++) {
        q = abs(q) - vec3(0.35 * hxParam_fold);
        q = q * 1.6;
        scale = scale * 1.6;
    }
    return (length(q) - 0.6) / scale;
}

vec4 hxMain(in HxFrag hx) {
    // The scene ray comes from the generated camera helpers, not from hx.rayOrigin.
    vec3 ro = hxCameraPosition;
    vec3 rd = hxCameraDirection(hx.direction);
    float t = 0.0;
    float steps = 0.0;
    for (int i = 0; i < 64; i++) {
        float d = sceneDist(ro + rd * t);
        if (d < 0.002 || t > 30.0) { break; }
        t = t + d;
        steps = steps + 1.0;
    }
    float hit = 1.0 - step(30.0, t);
    vec3 tint = 0.5 + 0.5 * cos(6.2831853 * (vec3(0.0, 0.33, 0.67) + hxParam_hue + t * 0.05));
    vec3 color = mix(vec3(0.02, 0.03, 0.06), tint * (1.0 - steps / 64.0), hit);
    return vec4(color, 1.0);
}
```

The closed convention has exactly `schema, profile, instructions, channels, bindings, mappings, camera`; schema is 1. Profile has `id, revision, label`; each channel has `id, alias, label, mode, unit, min, max, default`. Bindings have `id, channel_id, source, scale, activation`. The fly camera's exact fields are `kind, move, look, look_gate, boost, reset, speed, sensitivity, invert_y, boost_factor, position, angles, offset_position, offset_angles`; use acknowledged compatible resolved bytes, not a partial camera. Unreal fly (`hextile.unreal-fly/1`) uses RMB look, W/S travel, A/D strafe, Q/E rise/fall and Shift boost. Arrow fly (`hextile.arrow-fly/1`) keeps the same channels but uses Arrows and PageUp/PageDown with `activation="interact"`; GLSL stays unchanged.

Generated signatures: `vec3 hxCameraPosition`, `mat3 hxCameraRotation`, `vec3 hxCameraDirection(vec3 canonicalDirection)`. A fly camera supplies uniforms; `camera:null` supplies `const vec3(0.0)` and `const mat3(1.0)`. The helper returns `normalize(hxCameraRotation * canonicalDirection)`. Wire `ro=hxCameraPosition` and `rd=hxCameraDirection(hx.direction)` into your own ray marcher once. `hx.direction`, `hx.rayOrigin` and `hx.rayDirection` remain canonical; helper declarations alone never move the scene. Camera angles/sensitivity are radians/radians per CSS pixel, +Y is up and +Z forward.

## Recipe J — Playable Shaders: one-prompt Enter BLAST, camera null

“Make it when I press ENTER then we get a blast event” means one complete candidate: parameters are destinations, logical controls are inputs, and an envelope supplies shared shaping. Observe APP `available=true`, executable `conventions/1` AND `modulation/2`, then candidate `execution_ready`; missing declarations on a clean nonconvention base do not mean an unsupported runtime. Use only `source.glsl` plus `authoring.json`, register once and follow the installed skill's bounded Prepare/compile/poll/repair loop. No event callback or new MCP tool is needed.

Write this source (the shipped camera-null example):

```glsl
/*@hextile-shader
{"schema":1,"abi":"hx-shader/1","space":"direction_360","parameters":[{"id":"slice","type":"float","label":"Slice","default":0,"ui":{"min":0,"max":1}},{"id":"glow","type":"float","label":"Glow","default":0,"ui":{"min":0,"max":2}}]}
*/
vec4 hxMain(in HxFrag hx) {
    vec3 d = hx.direction;
    // Slice is driven by the X accumulator; Glow is driven by the BLAST envelope.
    float bands = 0.5 + 0.5 * sin(8.0 * (d.x + 0.5 * d.y + 0.25 * d.z) + 6.2831853 * hxParam_slice);
    // BLAST is read raw here too, so the ring also widens during the blast.
    float ring = exp(-(6.0 - 3.0 * BLAST) * abs(d.y));
    vec3 base = mix(vec3(0.05, 0.08, 0.20), vec3(0.20, 0.50, 0.90), bands);
    vec3 color = base + vec3(1.0, 0.55, 0.2) * hxParam_glow * ring;
    return vec4(color, 1.0);
}
```

In the acknowledged sidecar keep `kind="360hextile.shader-workspace"`, `schema=1` and its immutable `base`. Set `values={"slice":0,"glow":0}`, keep its capture defaults/output, set `space="direction_360"`, and merge the following complete extension members without deleting unrelated extensions. IDs shown are stable sample IDs; preserve existing IDs on revisions and use distinct valid IDs for new rows.

```json
{
  "conventions": {
    "schema": 1,
    "profile": {"id":"hextile.example-enter-blast/1","revision":1,"label":"Enter blast example"},
    "instructions": "No camera moves here. Arm Interact, then press Enter: Blast rises in 20 ms and fades over 400 ms, driving Glow from 0 to 2. Each right mouse click adds 0.1 to X, and X drives Slice. If Enter belongs to a built-in shortcut, use Bind key to pick another key.",
    "channels": [
      {"id":"x","alias":"X","label":"X","mode":"accumulator","unit":"unitless","min":0,"max":1,"default":0},
      {"id":"blast_press","alias":"BLAST_PRESS","label":"Blast press","mode":"gate","unit":"unitless","min":0,"max":1,"default":0}
    ],
    "bindings": [
      {"id":"x_right_button","channel_id":"x","source":{"kind":"button","button":2},"scale":0.1,"activation":"interact"},
      {"id":"blast_enter","channel_id":"blast_press","source":{"kind":"key","code":"Enter","modifiers":[]},"scale":1,"activation":"interact"}
    ],
    "mappings": [
      {"id":"map_x_slice","source_id":"x","target":"shader.params.slice","enabled":true,"mode":"range","signature":{"kind":"float","unit":"unitless","min":0,"max":1},"range_min":0,"range_max":1}
    ],
    "camera": null
  },
  "master_clock": {"schema":1,"bpm":120,"speed":1,"anchor":{"time":{"num":"0","den":"1"},"seconds":0,"beats":0}},
  "modulation": {
    "schema": 2,
    "grammar": "hx-math/1",
    "definitions": [
      {"id":"mod_0123456789abcdef01234567","alias":"BLAST","label":"Blast","kind":"envelope","enabled":true,"input_id":"blast_press","mode":"oneshot","settings":{"attack":0.02,"release":0.4,"attack_curve":{"shape":"linear","exponent":1},"release_curve":{"shape":"ease_out","exponent":2},"output_min":0,"output_max":1}}
    ],
    "bindings": [
      {"id":"binding_89abcdef0123456789abcdef","source_id":"mod_0123456789abcdef01234567","target":"shader.params.glow","enabled":true,"mode":"range","signature":{"kind":"float","unit":"unitless","min":0,"max":2},"range_min":0,"range_max":2}
    ]
  },
  "required_capabilities": ["conventions/1", "master-clock/1", "modulation/2"]
}
```

Enter → `blast_press` (default-0 gate) → `BLAST` (`kind="envelope"`, `mode="oneshot"`, UI Burst) → Range 0–2 → `shader.params.glow` → `hxParam_glow` in GLSL. RMB button 2 separately increments `x`/`X` by 0.1 and maps Range 0–1 to Slice. The source also reads raw `BLAST` to widen the ring; generated channel/modulator aliases are `uniform float <alias>` when referenced. Mappings alone cannot create a visible effect unless the shader uses the target parameter.

**Held alternative:** retain IDs/input/mapping and use `mode="held"`, `attack=0.2`, `release=0.6`. Down attacks, a long hold sustains, and last aggregate up releases from the current level. **Burst:** `mode="oneshot"` completes Attack → Release even after key-up or a long hold; fresh presses retrigger from the current level, with no stacked voices. Attack 0 gives an instant Burst; Release 0 returns immediately. OS key repeat creates no fresh press.

Attack/Release are finite seconds `[0,60]`. Curve objects have exactly `shape, exponent`; shapes are `linear`, `ease_in`, `ease_out`, `ease_in_out`. Linear uses exponent 1; nonlinear preset default is 2, adjustable independently per stage within `[1,8]`. The example explicitly chooses 20 ms Linear Attack and 400 ms Ease Out Release with exponent 2. Author all settings; omitted fields are not defaults. Envelopes use monotonic interaction time, independent of scene pause and Master Clock BPM/Speed. Retain the default Master Clock object and declaration required by the modulation document; do not invent a separate clock-bit gate for this recipe.

Aliases match ASCII `[A-Za-z][A-Za-z0-9_]{0,31}`, contain no `__`, are unique across channels/modulators and avoid GLSL/host names and source declarations. Modulator IDs are `mod_` plus 24 lowercase hex digits; binding IDs are `binding_` plus 24 lowercase hex digits. An envelope must reference an existing default-0 gate; orphaned inputs, incompatible targets, missing declarations or malformed settings yield real Prepare diagnostics, not a static pass. Preserve schema 2 on revisions; sidecar schema 1 is a separate contract.

Report an effective Enter shortcut conflict and propose an explicit remap. It is not a compile failure; never press keys, arm Interact, change Hotkeys, set live pose or save Project presets to prove the recipe. Review controls/modulators/parameters/values/code and the exact candidate-bound check states together; human Apply alone saves through APP's checked door. Compilation is not physical input or visual proof. Legacy `propose_shader` and every Copilot Board row must keep convention/envelope bytes and Playable declarations unchanged; only the current attested workspace can first create or revise them. Future admitted INPUT adapters may route MIDI/OSC/gamepad to the same logical gate/evaluator, but those device routes, addresses and live events never belong in these portable files.
