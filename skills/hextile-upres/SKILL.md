---
name: hextile-upres
description: "GPU upres a still, a folder of stills, or a video through 360 Hextile Real-ESRGAN. Use when the user says /hextile-upres, /upres, upres this image, upscale this folder, upres this video, or Real-ESRGAN sequence. Not hextile-pipe. Do not invent upscale_image."
user-invocable: true
---

# /hextile-upres

GPU Real-ESRGAN upres through the running **360 Hextile** app (`hextile` MCP). Do **not** invent `upscale_image`. Do **not** send GPU upres through `hextile-pipe`.

If a tool says the app is not running, tell the user verbatim:

> Launch 360 Hextile, then retry

Default scale **4**. Model **`realesrgan-x4plus`**. Use scale **2** only when the user asks (`upscaling.scale_factor` is `2` or `4`). Optional `upscaling.template` for 360 tiled DirectProcessor; empty = full-image.

## Classify

| Input | Door | Tools |
|-------|------|-------|
| One image file (`.png` `.jpg` `.jpeg` `.webp` `.tiff` `.tif` `.exr`) | stills | `validate_config` → `run_workflow` → `get_status` |
| Directory of images | sequence | `create_sequence` → `start_sequence` → `get_sequence` |
| Video (`.mp4` `.mpg` `.mpeg` `.m4v` `.mov`) | sequence | `extract_sequence_video` first, then the sequence tools |
| Anything else | refuse | Name the three accepted shapes |

`run_workflow` cannot create a `seq_*`. Folder and video **must** use sequence tools. Never loop `run_workflow` over frames.

## GPU safety

1. `get_capabilities` at session start.
2. `list_runs` and/or `list_sequences` before queueing. If a run is `running` or a sequence is `processing`, call `cancel_run` or `stop_sequence` **only if the user asked to replace that job**. Otherwise refuse the second GPU job.
3. Never `get_status(seq_*)`. Never `get_sequence(render_id)`. A frame `render_id` is an ordinary render.
4. Never poll forever.
5. HTTP **402**: Pro / license. Pass APP detail. Folder/video is Pro. A single still may run on Standard.

## Stills door

```
validate_config(
  workflow_id="upres-still",
  origin="builtin",
  overrides={
    "input": { "path": "<absolute image path>", "source": "file" },
    "upscaling": { "scale_factor": 2 or 4 }
  }
)
run_workflow(same)
get_status(run_id) until completed / failed / cancelled / crashed
```

Omit the `upscaling` override unless the user asked for 2× (builtin default is 4). Keep `run_id`. Use `list_runs` if lost. Abort with `cancel_run`.

## Sequence door

Tools: `extract_sequence_video`, `create_sequence`, `start_sequence`, `get_sequence`, `list_sequences`, `stop_sequence`.

Video: `extract_sequence_video({video_path})` first. Use the returned `folder_path`.

`create_sequence.config` **must** be a full `HextileConfig`. Never POST a partial `{pipeline, input, upscaling}` stub — `HextileConfig` requires `pipeline`, `hextile.template`, and `diffusion.model` and will **422**.

Clone, patch, POST:

1. `get_workflow(origin=builtin, id=upres-still)`.
2. That call returns the raw document (`pipeline` at top level — Recipe C: get_workflow → document; `WorkflowFreeformResponse` is extra=allow). Re-anchor on the live payload (if `upres-still` is missing, shape-check builtin `quick-scout`): if `pipeline` is top-level, that object is the document; if nested `document.pipeline` exists, use that nested object.
3. Patch `input.mode=sequence`, `input.path=<folder_path>`, `input.source=file`, and optional `upscaling.scale_factor`.
4. POST the patched object as `create_sequence.config` with `folder_path` (optional `name`).
5. `start_sequence` then `get_sequence`.

Poll `get_sequence` while status is `queued` or `processing`. Stop and report on `paused` and `incomplete` (`incomplete` is resumable in the app; v1 has no resume MCP tool). Terminal: `completed`, `completed_with_errors`, `failed`, `cancelled`. Abort with `stop_sequence`.

## Refuse

- Inventing `upscale_image`.
- GPU upres via `hextile-pipe`.
- A `{pipeline, input, upscaling}` stub as sequence config (422).
- A second heavy GPU job unless the user asked to replace the first.
- Inputs that are not one image file, a folder, or a video with the suffixes above.
