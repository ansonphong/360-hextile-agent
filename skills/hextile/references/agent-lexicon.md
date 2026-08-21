# Agent lexicon (K2)

One vocabulary for Desktop Copilot and MCP. Install id: **`hextile-agent@360-hextile`**.
Marketplace name `360-hextile` (frozen). MCP `SERVER_NAME` / skill token `hextile`.
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

- MCP **renders**, monitors, cancels, seeds, saves shelf Workflows.
- Copilot **edits the open file**, navigates, operates armed Layers.
- Neither creates / commits / arms Layers.
- Copilot cannot start a render. Copilot never queues GPU.
- MCP cannot `goto` or operate Layers.
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
  I can't operate Layers. Use Copilot in the studio, or the viewer.

REFUSE_LAYERS_CREATE
  I can't create, commit, or arm Layers.

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
