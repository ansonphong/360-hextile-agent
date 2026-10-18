---
name: fractal
description: "Edit the mounted 360 Hextile Fractal Workshop, control its timeline, Open or Save scenes, and render or export saved Fractal sequences through the existing MCP tools."
---

# 360 Hextile — Fractal Workshop

Use the running APP's discovered Fractal tools. APP owns the mounted authoring session, saved scenes, GPU jobs and folder publication. The general `360-hextile` skill routes workflow automation; the separate `shader` skill retains human Apply. Fractal edits use their own explicit human grant and never edit scene files privately.

## Fresh context and authority

Call `get_fractal_context` before composing work. Use current registry target IDs, retained orphan IDs, scene revision, owner and draft/source/viewer/renderer generations; never guess a numeric JSON leaf is eligible. Copy its complete `witness` unchanged, including the public header and Project witnesses. A source-free observation is not permission. Missing witness, capability or input-conflict information is unavailable evidence: rediscover or report unsupported, rather than inventing it or assuming input is safe. `capabilities` contains exactly one input-acquisition token, `none` or `host_canvas_v1`, from the admitted ready renderer; it reports capability, never arming or grant. `base` and `effective` are bounded finite target maps; `effective` is only the current acknowledged displayed sample and may be empty. Source-free `suspensions` and `diagnostics` may include advisory saved-binding conflicts as `{target: bindingID, code: binding_conflict|binding_unknown}` and expose no physical-key or event data.

Mounted mutations require MCP enabled, Follow ON and a separate explicit human Fractal grant for `actor=external_mcp` on this owner and generation. The returned `fractal_grant` is discovery only; the private lease stays in APP. Local host approval, ordinary workflow Follow, a source grant or `approved:true` cannot replace it. Do not send lease IDs, actor overrides or Copilot tokens. The server binds the actor, exact request hash, witness and generation; only the matching mounted acknowledgement establishes an effect.

Allocate a caller-known `client_request_id` (1–64 ASCII letters, digits, `_` or `-`) per intent and keep its exact arguments. Submit edits through `apply_fractal_edits` with `expected_draft_generation` equal to the witness. One atomic batch contains 1–32 closed ops; an invalid or stale batch changes nothing. After a refusal, refresh context and explain the actual reason. Use a newly consented intent for changed work, never mutate the body under the same request ID.

## Authoring, clock and camera

Discover closed shapes in `tools/list`. The admitted ops are `set_base`, `set_driver_mode`, `set_expression`, `set_mapping`, `remove_mapping`, `remove_driver`, `upsert_modulator`, `remove_modulator`, the five input declarations below, `clock_action`, `set_timeline`, `set_fps`, `use_current_view_as_start` and `resume_driver`.

Fixed uses the authored base; Modulator and Expression drive an effective sample without overwriting it. Use returned target signatures for mappings and stable alias-to-ID `refs` for expressions (`hx-scalar/1`); do not substitute labels for IDs. Typed compound edits validate together. An incompatible retained driver stays authored and suspended until repaired or explicitly removed. Unsupported camera/presentation targets cannot be assumed to affect panorama output.

`control_fractal_timeline` supports play, pause, rewind, seek with exact `{num:string,den:string}`, and step with `frames=-1|1`. These are transient mounted transport actions. `set_timeline` writes authored start/end/loop; `set_fps` writes reduced rational FPS. Frame n samples exactly `start+n/fps`, with end exclusive. `clock_action` reset/align/rate explicitly reanchors the master clock at the selected sample; changing BPM/speed preserves phase according to APP's clock owner. Sample/effective values and playback do not dirty the authored base or save a scene.

Camera navigation changes the live pose. `use_current_view_as_start` explicitly authors that pose as the camera start; seek/playback alone does not. Human takeover can suspend a driver; `resume_driver` resumes only the named admitted driver under fresh authority.

## Input declarations and retained orphans

The five input ops are `set_mouse_input`, `upsert_input_gate`, `remove_input_gate`, `upsert_input_binding`, `remove_input_binding`. MouseInput is exactly `{schema:1,enabled:boolean,abi:"hx-mouse/1"}`. GateChannel is exactly `{id,alias,label,mode:"gate",unit:"gate",min:0,max:1,default:0}`. GateBinding is exactly `{id,channel_id,source:{kind:"key",code,modifiers},scale:1,activation:"interact"}`. IDs and aliases follow the discovered schema; modifiers are unique and sorted from Alt, Control, Meta, Shift. Bindings must reference an existing gate and pass current host key/navigation conflicts.

Declarations cannot arm Interact, inject physical events or captured input, or acquire a plugin host input lease. Reopening an interactive document remains unarmed and preserves its declarations. Only the human's Interact gesture acquires input. An `input_acquisition:none` consumer can inspect, disable or repair a document but refuses enabled live Click/Envelope sampling as `interactive_input_unsupported`; neutral input is not a substitute.

Even a dormant Click definition requires admitted enabled mouse input (`mouse_required`). A dormant Envelope retains its gate reference; removing that gate while any Envelope references it fails `input_gate_in_use`. An enabled Envelope needs a valid binding for its gate (`envelope_binding_required`). Repair the whole proposed document in one batch: remove/repoint dependent definitions and bindings before removing their gate, or remove every Click before disabling mouse. Merely disabling a definition does not erase its reference. Gate and binding validity is checked on the final atomic proposal, with the exact refusal codes above; a rejected proposal leaves everything intact.

For a retained orphan, read fresh context → choose the exact `retained_orphans[].target` → call `apply_fractal_edits` with `{op:"remove_driver",target:"<exact retained ID>"}` under the same explicit grant and witness. The target may be absent from the active registry but must retain a driver. Extra fields, an absent driver, stale authority or more than 32 ops refuse. This removes only that target's expression, bindings and exact suspension, preserving shared sources/gates, other signatures and drivers.

## Open, Save and uncertain outcomes

`open_fractal_scene` opens an exact saved `scn_` ID. `save_fractal_scene(mode="save")` requires the adopted `scene_revision` (`sha256:` plus 64 lowercase hex) and target scene ID. Save As uses `mode="save_as"` and a caller-proposed create-only `scn_[0-9a-f]{8}` ID, without an overwrite revision. Only a definitive collision permits another ID. A revision conflict requires fresh inspection; never force an overwrite.

Open or Save As that adopts B revokes A's lease. Fresh edits on B need new human consent. The first admitted receipt and an exact pending replay remain truthful after Follow OFF or lease loss; they do not grant new delivery or effects. Never interpret an old receipt as permission on B.

For pending work, poll `get_fractal_request` with the original request ID. Exact replay means exactly the same request ID and immutable arguments, not a new ID or a re-composed witness. Prefer status GET after transport loss. A 410 or absent historical receipt is `unknown`, not proof of refusal or cancellation. Save reconciliation also checks `GET /api/fractal/scenes/{target}/write-status/{client_request_id}`; accepted publication proves the saved SHA even if a later mounted acknowledgement reports `ok:false` or `status:unknown`. Keep known scene/base revisions and do not resend that Save. Unknown durable status remains unknown; it never authorizes another mint or write.

## Saved native sequences and folder publication

Use `preflight_fractal_sequence` on an actually saved scene revision, not a transient sample. Submit the same closed deferred body to `render_fractal_sequence`: saved scene/revision, stable `idempotency_key`, name, reduced rational `frame_rate` (1–120 FPS), reduced nonnegative exact start/end (0 ≤ start < end ≤ 86400), matching ceil((end-start)*FPS) frame count (1–10000), seed, quality, dimensions, output and `publication_mode:"deferred"`.

Equirectangular output requires exact 2:1, even height 64–8192 and width 128–16384 (minimum 128×64), with zero pan/tilt/roll. Dome `fulldome` and `fulldome_angular` retain square diameters 1024/2048/4096/8192, FOV 10–360 and bounded frozen pose. Preflight returns all four source/output dimensions and render identity; respect geometry, `resource_unavailable`, saved revision, asset and presentation-target refusals. Preflight does not start a GPU job or publish a folder.

APP derives the known sequence ID as `"seq_" + sha256(UTF8("fractal-sequence/1\0" + scene_id + "\0" + idempotency_key)).hexdigest()[:32]`. Keep this before POST. Lost render replies carry this ID and expected request hash; GET `get_fractal_sequence` and compare `request_hash`. A missing known ID remains unknown. Never change the key or blindly resubmit/mint. Completed means verified native masters, not a chosen folder or a Render per tick.

Native work uses ordinary host approval of the exact MCP output intent. APP's existing GPU admission, operational sequence entitlement, frozen assets and publication owners remain authoritative. A Fractal Follow lease is not needed for saved-native jobs and cannot waive these guards. `control_fractal_sequence` stop/resume targets the exact sequence; read status after any uncertain control reply. Stop is a request until the owner settles.

For a separately approved existing folder, call `preflight_fractal_sequence_export` with the known sequence, destination, PNG or JPG (JPG requires quality 1–100; PNG omits it). Preserve its top-level `transaction_fingerprint`. Submit only that same intent and fingerprint to `export_fractal_sequence`. Poll `get_fractal_sequence` for its exact `attempt_id`, `attempt_revision`, `folder_export` and publication history. After a lost export reply, GET those identities; never blindly POST again. Cancel/retry/open-folder controls require the observed current attempt ID and revision. Opening a folder is an explicit mutating action. Destination paths stay out of returned projections; they are never authority to bypass ordinary output policy.

## Discovery evidence

The installer copies this sibling skill into a temporary home in focused tests. That proves source/temp discovery and does not prove an installed host, its MCP inventory, native GPU output or folder publication. Confirm the actual installed picker and `tools/list` before promising those operations. Do not install, publish or modify caches merely to use this procedure.
