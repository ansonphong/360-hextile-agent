# hextile-agent

**v0.2.1**

Claude Code plugin + Codex twin that drive **360 Hextile** over localhost HTTP.

Marketplace host: **`ansonphong/hextile-agent`**

The MCP process is a thin **stdio** JSON-RPC ↔ HTTP proxy (`python3`, stdlib only).  
All merge, validation, and render logic stay in the running app at `http://127.0.0.1:8000`.

## Prerequisites

| Requirement | Notes |
|-------------|--------|
| **360 Hextile running** | Backend on `127.0.0.1:8000` |
| **python3 ≥ 3.9** | On PATH — powers the MCP proxy (zero pip installs) |
| App with `POST /api/workflows/run` | Workflow automation P0+ |
| Claude Code **or** Codex ≥ 0.34.0 | Install path below |

## Install — Claude Code

From a public clone (or local path):

```bash
# When published:
/plugin marketplace add ansonphong/hextile-agent
/plugin install hextile@hextile-agent
```

Local development (this tree):

```bash
# From Claude Code, add this directory as a marketplace root, or symlink/copy
# into your plugin search path. The package root is the plugin root:
#   .claude-plugin/plugin.json
#   .mcp.json          → python3 ${CLAUDE_PLUGIN_ROOT}/mcp/hextile_mcp.py
#   skills/hextile/SKILL.md
```

Confirm tools appear (`list_workflows`, `get_guide`, …). With the app down, tools return a clean error — the MCP process stays up.

## Install — Grok

```bash
grok plugin marketplace add /mnt/d/Projects/360-Hextile/hextile-agent
grok plugin install /mnt/d/Projects/360-Hextile/hextile-agent --trust
```

Enable `hextile` in `~/.grok/config.toml` `[plugins].enabled` (or Space in `/plugins`). Reload plugins (`r`) or start a new session.

When published: `grok plugin marketplace add ansonphong/hextile-agent` then install `hextile`.

## Install — Codex

```bash
git clone https://github.com/ansonphong/hextile-agent.git
cd hextile-agent
python3 codex/install.py
```

This writes:

- `~/.codex/skills/hextile/SKILL.md` + `references/`
- `[mcp_servers.hextile]` **stdio** entry in `~/.codex/config.toml`  
  (`command = "python3"`, `args = ["…/mcp/hextile_mcp.py"]`)

Uninstall:

```bash
python3 codex/install.py --uninstall
```

Restart Codex and run `/mcp` — you should see `hextile`.

Requires **Codex ≥ 0.34.0**. v1 is **stdio only** (no streamable HTTP dual-stack).

## Tools (14)

| Tool | HTTP / source |
|------|----------------|
| `list_workflows` | `GET /api/workflows` |
| `get_workflow` | `GET /api/workflows/{origin}/{id}` |
| `get_capabilities` | `GET /api/workflows/capabilities` |
| `save_workflow` | `POST /api/workflows/{user\|project}` |
| `delete_workflow` | `DELETE /api/workflows/{origin}/{id}` |
| `run_workflow` | `POST /api/workflows/run` |
| `validate_config` | same, `dry_run: true` |
| `get_status` | `GET /api/renders/{id}` |
| `get_render_config` | `GET /api/renders/{id}/config` |
| `list_runs` | `GET /api/renders/` |
| `cancel_run` | `POST /api/renders/{id}/stop` |
| `generate_seed` | `POST /api/360-lora/generate` |
| `list_360_loras` | `GET /api/360-lora/loras` |
| `get_guide` | bundled `skills/hextile/references/*.md` |

Instruction surface: **`skills/hextile/SKILL.md`** (canonical).  
Codex `AGENTS-fragment.md` is a frontmatter-stripped copy — edit SKILL only.

## App not running

Every tool returns a structured error, roughly:

> 360 Hextile isn't running. Launch 360 Hextile, then retry.

The proxy never hangs waiting for the app at startup.

## curl fallback

If python3 / MCP is unavailable, agents can still drive the API — see the curl appendix in `skills/hextile/SKILL.md`.

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
  skills/hextile/SKILL.md
  skills/hextile/references/
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
HOME=/tmp/hextile-agent-codex-test python3 codex/install.py
HOME=/tmp/hextile-agent-codex-test python3 codex/install.py --uninstall
```

## Docs

Bundled agent guides: `get_guide` (`workflow-schema`, `best-practices`, `website-index`, `recipes`).  
`website-index` points at **existing** https://360hextile.com/docs/ pages (templates, pipelines, input, prompts). A dedicated Automation docs section is still 06-docs.

## License

MIT — see package metadata in `.claude-plugin/plugin.json`.
