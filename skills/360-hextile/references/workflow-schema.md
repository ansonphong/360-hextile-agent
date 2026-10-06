# Workflow schema (agent)

A **Workflow** is a full `.hextile.json` document. There is no second format.

Validated by APP `HextileConfig`. The MCP never merges or validates as authority.

## Required / important fields

| Field | Role |
|-------|------|
| `schema_version` | Usually `prompt-data-model-v2` |
| `pipeline` | Model family id, e.g. `sd21`, `sdxl`, `flux_schnell`, `realesrgan` |
| `hextile` | Template + tile sizes (`template` like `Hextile_20_2K`) |
| `input` | `source` is **`file` or `render` only**. `path` must be non-empty for a **live** run |
| `diffusion` | `model`, `seed` (`-1` random), `strength`, `guidance_scale`, `num_inference_steps` |
| `prompt` | At least `prompt.global`. Tile / directional prompts optional |
| `output` | Width/height, `file_format`, optional `path` |
| `passes` | Optional multipass list (max 10). Arrays **replace wholesale** on merge |
| `upscaling` | Optional. `scale_factor` is `2` or `4` |
| `tiles`, `sequence`, `coverage`, `post_processing` | Optional |

Retired render-time `input.source` values (`pattern`, `360_lora`, `solid_color`, `marble`, `blockade`) coerce to `file`. Do not write them.

## Live run vs dry_run

- `validate_config` / `dry_run: true` — empty `input.path` is OK.
- `run_workflow` live — after merge, `input.path` must be a real file (or a render id when `source=render`).

## Persist

`POST /api/workflows/{user|project}` create-only. Builtin is immutable. Existing id → 409.

## Builtin starting points

`quick-scout` (fast 2K / `sd21`), `progressive-build`, `structure-first`, `bloom-from-noise`, `grand-remaster`, `painters-pass`.

## Separate Shader workspace format

`authoring.json` is **not** a `.hextile.json` Workflow. A negotiated `workspace_schema:2` Project Shader stores metadata/config and complete path inventories in `workspace/authoring.json`; root `workspace/source.glsl` and arbitrary declared relative `.glsl` helpers hold the actual code. No GLSL bodies belong in durable JSON. The ordered full source set, including unused helpers, and declared Looks/provenance are raw-byte registered using the v2 digest returned in APP open instructions; resources/originals are verified separately. Do not use the v1 source/authoring pair digest for v2 or serialize transient `source_modules` API text into the portable manifest. `source-modules/1` must be executable on the running APP before promising compilation, independently of format negotiation. See the installed `360-hextile` skill and https://360hextile.com/docs/create/shader.
