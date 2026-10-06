# Agent lexicon (K2)

One vocabulary for Desktop Copilot and MCP. Install id: **`360-hextile@360-hextile`**.
Marketplace name `360-hextile` (frozen). MCP `SERVER_NAME` `hextile`; hub skill `360-hextile`.
Do not teach `hextile@360-hextile`.

## Locked words

| Say | Never |
|---|---|
| Workflow | “config” as the top user word for a full recipe |
| Preset | calling a full recipe a preset (Preset = reusable *fragment*) |
| Run | using “undo” to mean cancel-GPU on MCP |
| override / merge | a second patch language |
| pole pinch | pole smear / stretch |
| Reframe | Aim & Export |
| MCP follow | Copilot Auto |
| your AI coding agent (Claude Code + Codex + Grok) | “the Claude Code plugin” alone |

## Complementarity card

- MCP **renders**, monitors, cancels, seeds, saves shelf Workflows, and can Generate Layer into an exact saved render.
- Copilot **edits the open file**, navigates, operates armed Layers.
- Neither silently arms Layers or commits a generated layer; Generate, land, and commit are separate approved actions.
- Copilot cannot start a Render run. Approved live Generate Layer may start one Layers GPU job.
- MCP cannot `goto` or operate the open Layers canvas; saved Generate Layer is a narrow exception.
- Live apply from MCP requires follow ON (`hextile_mcp_follow`).
- Gate A (`hextile_copilot_gate_a_passed`) stays locked. Follow is not Auto.

## Shared refuse strings (exact)

```
REFUSE_RENDER_COPILOT
  I can't start a render. Press RENDER, or run it from your AI coding agent (MCP).

REFUSE_LIVE_FOLLOW_OFF
  Studio isn't following. Turn on MCP follow to apply this to the open file, or use run_workflow to queue a render.

REFUSE_NAV_MCP
  I can't open screens. Use Copilot or click the viewer.

REFUSE_LAYERS_MCP
  I can't operate the open Layers canvas. Use Copilot in the studio or the viewer; for an exact saved render, use the Generate Layer tools.

REFUSE_LAYERS_CREATE
  I can't arm Layers or make arbitrary Layers edits. Generate, land, and commit need their explicit saved-render or approved live actions.

REFUSE_STALE_GENERATION
  The open file changed. Call get_live_context again and retry.

REFUSE_IDENTITY
  I can't change the document name, file name, or current render id.
```

## Merge twins (no fourth)

1. `backend.utils.deep_merge` via `POST /api/workflows/run` — run merger.
2. FE `deepMerge` in `copilotApply.ts` — live-doc merger.
3. `render_config._deep_merge` — tile/pass twin, **not** the run merger.

`CopilotService._deep_merge` wraps (1). `overrides` ≡ `config_partial`.

## Live kernel (v1)

- One writer: `copilotApply.applyConfigDelta`.
- One Render-run queue door: `POST /api/workflows/run` (human RENDER or MCP `run_workflow`). Copilot has no Render-run path; approved Generate Layer uses the separate existing Layers job owner.
- FE RAM studio slot (preview-slot). `doc_generation` is the FNV-1a hex **string** of the identity-stripped live export.
- Apply-live body `{config_partial, doc_generation, explanation}`. Ticket; FE merge-then-apply.
- Empty or expired slot → 403 `studio_not_present`. Slot TTL 30s. FE heartbeat 10s. `pagehide` DELETE. Ticket TTL same clock. Follow OFF or empty republish drops leftover tickets. `startFollow` consumes leftover tickets before poll; busy follow does not consume.
- Install id `360-hextile@360-hextile`. Do not rewrite committed `.mcp.json`.
