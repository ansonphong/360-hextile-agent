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
6. Need a seed image? `list_360_loras` → `generate_seed` → set `input.path` + `source=file`.
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

- Cancel with `cancel_run` before starting a second heavy GPU job if one is `running`.
- Do not delete builtin. Do not `delete_workflow` unless the user asked.
- License HTTP 402 → tell the user to activate in Settings.

## Website

Fetch URLs from `get_guide` `website-index` when you need product-domain depth (templates, pipelines, LoRA, prompts). Those pages exist on 360hextile.com today.
