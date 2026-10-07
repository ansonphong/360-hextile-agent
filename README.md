# 360-hextile-agent

**v0.5.3**

Claude Code plugin + Codex twin that drive **360 Hextile** over localhost HTTP.

Primary marketplace: **`ansonphong/360-hextile-plugins`** — the shared 360 Hextile catalog. Install id **`360-hextile@360-hextile`**. The studio-matte plugin `hextile-pipe` ships from the same catalog.
Standalone marketplace (footnote): **`ansonphong/360-hextile-agent`** — this repo on its own, install id **`360-hextile@hextile-agent`**.

The MCP process is a thin **stdio** JSON-RPC ↔ HTTP proxy (`python3`, stdlib only).  
All merge, validation, and render logic stay in the running app at `http://127.0.0.1:8000`.

## Prerequisites

| Requirement | Notes |
|-------------|--------|
| **360 Hextile running** | Backend on `127.0.0.1:8000` |
| **python3 ≥ 3.9** | On PATH — powers the MCP proxy (zero pip installs). On Windows, Claude `.mcp.json` `command` should be `python` or `py -3`, not `python3`. Optional launcher: `mcp/hextile-mcp.cmd` (picks `python` / `python3` / `py -3`). Committed `.mcp.json` stays `python3`. Codex installer already writes `sys.executable`. |
| App with `POST /api/workflows/run` | Workflow automation P0+ |
| Claude Code **or** Codex ≥ 0.34.0 | Install path below |

## Install — Claude Code

Primary — the 360 Hextile catalog (ships this plugin **and** `hextile-pipe`):

```bash
/plugin marketplace add ansonphong/360-hextile-plugins
/plugin install 360-hextile@360-hextile
```

Standalone — this repo as its own marketplace:

```bash
/plugin marketplace add ansonphong/360-hextile-agent
/plugin install 360-hextile@hextile-agent
```

Local checkout of this tree still works: add the directory as a marketplace root, then `/plugin install 360-hextile@hextile-agent`.

```bash
# package root is the plugin root:
#   .claude-plugin/plugin.json
#   .claude-plugin/marketplace.json
#   .mcp.json          → python3 ${CLAUDE_PLUGIN_ROOT}/mcp/hextile_mcp.py
#   skills/360-hextile/SKILL.md
```

On Windows, change Claude `.mcp.json` `command` to `python` or `py -3` (not `python3`). Do not rewrite the committed POSIX `.mcp.json`. Codex installer already writes `sys.executable`.

Confirm tools appear (`list_workflows`, `get_guide`, …). With the app down, tools return a clean error — the MCP process stays up.

## Install — Grok

Primary — the 360 Hextile catalog:

```bash
grok plugin marketplace add ansonphong/360-hextile-plugins
grok plugin install 360-hextile --trust
```

Standalone — this repo as its own marketplace:

```bash
grok plugin marketplace add ansonphong/360-hextile-agent
grok plugin install 360-hextile --trust
```

Enable `360-hextile` in `~/.grok/config.toml` `[plugins].enabled` (or Space in `/plugins`). Reload plugins (`r`) or start a new session.

A local checkout still works: `grok plugin marketplace add /path/to/hextile-agent` then `grok plugin install 360-hextile --trust`.

## Install — Codex

Primary — use a current Codex CLI with `codex plugin` support:

```bash
codex plugin marketplace add ansonphong/360-hextile-plugins
codex plugin add 360-hextile@360-hextile
```

This installs the bundled skills and `hextile` stdio MCP proxy. Git and Python 3.9 or newer are prerequisites, and `python3` must be available to the agent. Start a new Codex session and check `/mcp` for `hextile`, with 360 Hextile open on the same computer at `127.0.0.1:8000`. No separate clone or Python installer is needed for this route.

Alternative manual setup — clone this repo and run the installer:

```bash
git clone https://github.com/ansonphong/360-hextile-agent.git
cd 360-hextile-agent
python3 codex/install.py
```

This writes:

- `~/.agents/skills/360-hextile/SKILL.md` + `references/`
- `~/.agents/skills/shader/SKILL.md`
- `~/.agents/skills/upres/SKILL.md`
- `[mcp_servers.hextile]` **stdio** entry in `~/.codex/config.toml`  
  (`command` is the absolute `sys.executable`, `args = ["…/mcp/hextile_mcp.py"]`)

Uninstall:

```bash
python3 codex/install.py --uninstall
```

Restart Codex and run `/mcp` — you should see `hextile`.

The manual installer requires **Codex ≥ 0.34.0**. The MCP proxy uses **stdio** (no streamable HTTP dual-stack).

## /360-hextile:upres

GPU Real-ESRGAN upres through the running 360 Hextile app. Skill name is **`/360-hextile:upres`** (not bare `/upres`; the description also triggers on `/upres`). Not `hextile-pipe`. Do not invent `upscale_image`.

| Input | Door |
|-------|------|
| One still | `run_workflow` + builtin `upres-still` (`pipeline: realesrgan`) |
| Folder of stills, or video | sequence tools (`extract_sequence_video` if video, then `create_sequence` / `start_sequence` / `get_sequence`) |

`run_workflow` cannot create a `seq_*`. Folder and video must use the sequence tools.

### Hosts

Install id stays **`360-hextile@360-hextile`**. Hosts do **not** auto-update.

| Host | How `/360-hextile:upres` lands |
|------|---------------------------|
| **Claude Code** | Plugin skills auto-discover from `plugin.json` `"skills": "./skills/"` after `/plugin install 360-hextile@360-hextile` (or marketplace update + `/plugin update 360-hextile`). |
| **Grok** | Catalog SHA pin in `ansonphong/360-hextile-plugins`. After that pin is on GitHub: `grok plugin marketplace update` then `grok plugin update 360-hextile`. |
| **Codex** | `python3 codex/install.py` writes `~/.agents/skills/upres/SKILL.md`. |

## Selected tools

| Tool | HTTP / source |
|------|----------------|
| `list_workflows` | `GET /api/workflows` |
| `get_workflow` | `GET /api/workflows/{origin}/{id}` |
| `get_capabilities` | `GET /api/workflows/capabilities` |
| `describe_image` | `POST /api/prompts/describe-source` (local vision, 300s) |
| `get_live_context` | `GET /api/agent/live-context` |
| `apply_config_delta` | `POST /api/agent/apply-live` |
| `save_workflow` | `POST /api/workflows/{user\|project}` |
| `delete_workflow` | `DELETE /api/workflows/{origin}/{id}` |
| `run_workflow` | `POST /api/workflows/run` |
| `validate_config` | same, `dry_run: true` |
| `get_status` | `GET /api/renders/{id}` |
| `get_render_config` | `GET /api/renders/{id}/config` |
| `get_logs` | `GET /api/renders/{id}/logs` |
| `list_runs` | `GET /api/renders/` |
| `cancel_run` | `POST /api/renders/{id}/stop` |
| `retry_run` | `POST /api/renders/{id}/retry` |
| `generate_seed` | `POST /api/360-lora/generate` |
| `list_seed_history` | `GET /api/360-lora/history` |
| `get_seed_batch` | `GET /api/360-lora/history/{batch_id}` |
| `cancel_seed` | `POST /api/360-lora/cancel` |
| `list_360_loras` | `GET /api/360-lora/loras` |
| `list_installed_models` | `GET /api/models/{pipeline_id}?installed_only=true` |
| `get_guide` | bundled `skills/360-hextile/references/*.md` |
| `extract_sequence_video` | `POST /api/sequences/extract-video` |
| `create_sequence` | `POST /api/sequences/create` |
| `start_sequence` | `POST /api/sequences/{id}/start` |
| `get_sequence` | `GET /api/sequences/{id}` |
| `list_sequences` | `GET /api/sequences/` |
| `stop_sequence` | `POST /api/sequences/{id}/stop` |

Instruction surface: **`skills/360-hextile/SKILL.md`** (general automation hub), **`skills/shader/SKILL.md`** (authoritative Shader file procedure) and **`skills/upres/SKILL.md`**.
Codex `AGENTS-fragment.md` is a frontmatter-stripped copy of the hub — edit SKILL only.

### Shader authoring in your coding host

The `shader` skill edits the open Project Shader through the acknowledged `source.glsl` / `authoring.json` pair, registers a raw-byte framed digest and polls real APP Prepare/compiler receipts, with at most three repairs. Workshop Apply stays human. Installed MCP connectivity and native access to the same physical Project/worktree files are both required; a remote connector alone is insufficient. APP grants control disclosure/ingestion/Apply, not the host's filesystem permissions. Reasoning belongs to your host account/provider and may incur charges or quota limits.

Claude prefixes plugin skills with the plugin name: `/360-hextile:360-hextile` (hub), `/360-hextile:shader`, `/360-hextile:upres`. Codex uses separately installed sibling skill directories (`360-hextile`, `shader`, `upres`): confirm all three in the installed picker. Check actual Codex/Grok invocation syntax on the installed client; source layout is not installed-host proof.

Open a clean Project Shader in Workshop, enable MCP Follow and whole-asset source_read/propose grants, then invoke the installed skill with your requested change. If open acknowledgement is unknown, preserve files and reconnect; never edit presumed paths. Timeout or exhausted repairs includes the last exact receipt and instructions to re-enter this same host skill. APP Retry rechecks a candidate; it cannot wake an idle coding host. Playable and input-adapter guidance is conditional on observed executable support, not merely document declarations. Neither compiler pass nor a preview certifies physical input behavior or saving.

### Ordinary render file export

Call `preflight_file_export` with an exact `render_id`, `source_node_id`, selected existing `folder_path`, Windows-safe basename `output_name`, `projection`, and `encoding`. It performs no writes and returns the source revision, exact output paths, blockers, and output fingerprint. Review those paths and obtain approval for the exact options before calling `export_render_file` with the same arguments plus the returned `source_revision` and `output_fingerprint`.

`export_render_file` calls the app's encoder once with `create_only: true`; an existing destination returns a collision instead of being replaced. If the source or derived output names changed, preflight again and obtain new approval. A timeout or lost connection leaves the outcome unknown: inspect the destination before considering a new attempt. These tools accept neither HDRI nor Unreal-project export arguments and never encode files in the plugin.

### Describe an exact local image

`describe_image` accepts `source: {"kind":"render_node","render_id":"...","node_id":"..."}` or `source: {"kind":"local_file","path":"/absolute/image.png"}`. Optionally add `crop: {"yaw":15,"pitch":-10,"fov":70}` for a 2:1 equirectangular panorama. The app resolves the exact source and runs local vision; the MCP proxy never opens the image. Approve the local compute/GPU call before invoking it. The result is text (`prompt`, source identity, mode, crop and image signature), never raster data. URLs, base64 and implicit current-selection sources are not accepted.

### Device-local Hotkeys

`list_hotkeys` reads the current profile, revision, effective bindings and availability;
`inspect_hotkey` explains one action and optional candidate bindings, conflicts and overlaps.
The app must be open; MCP Follow and a render document are not required.
Bindings use `meta_key` for the Meta modifier and `slash` for the slash key.

Start `list_hotkeys` with `{}`. Each page contains at most 20 actions plus `total`
and `next_cursor`; pass that cursor unchanged on the next call until it is null.
Pages share one profile revision. If continuation returns `stale_revision`,
discard the partial listing and restart without a cursor.

Read before every edit and approve the exact action, bindings and returned `revision`.
`set_hotkey_bindings` replaces the complete alias list (`[]` unbinds),
`remove_hotkey_binding` removes one assigned chord, `restore_hotkey` restores one
customized row, and `reset_hotkeys` clears all overrides including inert unknown IDs.
Reset leaves the separate single-key switch unchanged. Conflicts never silently
reassign another action. A stale revision requires a fresh read and new approval.
Only an explicit reset can replace a future-format profile.

If a write returns `hotkeys_outcome_unknown`, its effect is uncertain. Never replay
it automatically: re-read the profile, inspect the actual bindings, and obtain a
new approval only if another mutation is still needed.

## App not running

Every tool returns a structured error, roughly:

> 360 Hextile isn't running. Launch 360 Hextile, then retry.

The proxy never hangs waiting for the app at startup.

## curl fallback

If python3 / MCP is unavailable, agents can still drive the API — see the curl appendix in `skills/360-hextile/SKILL.md`.

```bash
curl -s http://127.0.0.1:8000/api/workflows
curl -s -X POST http://127.0.0.1:8000/api/workflows/run \
  -H 'Content-Type: application/json' \
  -d '{"workflow_id":"quick-scout","origin":"builtin","dry_run":true}'
```

## Layout

```
hextile-agent/
  .claude-plugin/plugin.json
  .claude-plugin/marketplace.json
  .grok-plugin/marketplace.json
  .mcp.json
  skills/360-hextile/SKILL.md
  skills/shader/SKILL.md
  skills/360-hextile/references/
  skills/upres/SKILL.md
  mcp/hextile_mcp.py
  mcp/hextile_client.py
  codex/install.py
  codex/AGENTS-fragment.md
  tests/test_tool_list_drift.py
  README.md
```

## Develop / test

```bash
python3 -m py_compile mcp/hextile_client.py mcp/hextile_mcp.py codex/install.py
python3 -m pytest tests/test_tool_list_drift.py -q

# Codex installer smoke (temp HOME)
python3 -m pytest tests/test_codex_skill_discovery.py -q
python3 codex/install.py --home /tmp/hextile-agent-codex-test
python3 codex/install.py --uninstall --home /tmp/hextile-agent-codex-test
```

## Docs

Bundled agent guides: `get_guide` (`workflow-schema`, `best-practices`, `website-index`, `recipes`).  
`website-index` points at **existing** https://360hextile.com/docs/ pages (templates, pipelines, input, prompts). A dedicated Automation docs section is still 06-docs.

## License

MIT — see package metadata in `.claude-plugin/plugin.json`.
