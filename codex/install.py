#!/usr/bin/env python3
"""Idempotent Codex twin installer for hextile-agent.

Writes:
  ~/.agents/skills/hextile/SKILL.md
  ~/.agents/skills/hextile/.hextile-agent-marker  (version marker)
  [mcp_servers.hextile] into ~/.codex/config.toml  (stdio only)

Usage:
  python3 codex/install.py              # install / refresh
  python3 codex/install.py --uninstall  # reverse
  python3 codex/install.py --dry-run    # print actions only
  python3 codex/install.py --home DIR   # override HOME (tests)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

PACKAGE_VERSION = "0.2.1"
MIN_CODEX = "0.34.0"
MARKER_NAME = ".hextile-agent-marker"
MCP_SECTION = "mcp_servers.hextile"
BEGIN_MARK = "# >>> hextile-agent begin (do not edit between markers)"
END_MARK = "# <<< hextile-agent end"


def package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def skill_source() -> Path:
    return package_root() / "skills" / "hextile" / "SKILL.md"


def mcp_script() -> Path:
    return package_root() / "mcp" / "hextile_mcp.py"


def strip_frontmatter(text: str) -> str:
    """Strip YAML frontmatter for AGENTS-fragment style copies."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            rest = text[end + 4 :]
            return rest.lstrip("\n")
    return text


def write_agents_fragment() -> Path:
    """Release helper: copy SKILL → codex/AGENTS-fragment.md (frontmatter stripped)."""
    src = skill_source().read_text(encoding="utf-8")
    out = package_root() / "codex" / "AGENTS-fragment.md"
    body = strip_frontmatter(src)
    header = (
        "<!-- Generated from skills/hextile/SKILL.md — edit SKILL.md only, "
        "then re-run: python3 codex/install.py --write-fragment -->\n\n"
    )
    out.write_text(header + body, encoding="utf-8")
    return out


def mcp_block(script: Path) -> str:
    # stdio only (OPEN-1). Absolute path so Codex does not depend on cwd.
    command_s = json.dumps(sys.executable)
    script_s = json.dumps(str(script.resolve()))
    return (
        f"{BEGIN_MARK}\n"
        f"[{MCP_SECTION}]\n"
        f"command = {command_s}\n"
        f"args = [{script_s}]\n"
        f"{END_MARK}\n"
    )


def ensure_skills(home: Path, dry_run: bool) -> Path:
    agents_root = home / ".agents" / "skills" / "hextile"
    dest_skill = agents_root / "SKILL.md"
    marker = agents_root / MARKER_NAME
    src = skill_source()
    if not src.is_file():
        raise SystemExit(f"Missing skill source: {src}")
    if dry_run:
        print(f"[dry-run] would write {dest_skill}")
        print(f"[dry-run] would write {marker}")
        return agents_root
    agents_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_skill)
    src_refs = package_root() / "skills" / "hextile" / "references"
    dest_refs = agents_root / "references"
    if src_refs.is_dir():
        if dest_refs.exists():
            shutil.rmtree(dest_refs)
        shutil.copytree(src_refs, dest_refs)
        print(f"Wrote {dest_refs}")
    marker.write_text(
        f"version={PACKAGE_VERSION}\nsource={package_root()}\n",
        encoding="utf-8",
    )
    print(f"Wrote {dest_skill}")
    print(f"Wrote {marker}")
    return agents_root


def patch_config(config_path: Path, script: Path, dry_run: bool) -> None:
    block = mcp_block(script)
    existing = ""
    if config_path.is_file():
        existing = config_path.read_text(encoding="utf-8")

    # Remove previous marked block or prior [mcp_servers.hextile] section.
    cleaned = _remove_hextile_section(existing)
    cleaned = cleaned.rstrip() + ("\n\n" if cleaned.strip() else "") + block

    if dry_run:
        print(f"[dry-run] would update {config_path}")
        print(block)
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(cleaned, encoding="utf-8")
    print(f"Updated {config_path} ([{MCP_SECTION}] stdio)")


def _remove_hextile_section(text: str) -> str:
    if BEGIN_MARK in text and END_MARK in text:
        pattern = re.compile(
            re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK) + r"\n?",
            re.DOTALL,
        )
        text = pattern.sub("", text)
    # Also strip a bare [mcp_servers.hextile] table if present without markers.
    pattern2 = re.compile(
        r"^[ \t]*\[mcp_servers\.hextile\][ \t]*\n.*?(?=^[ \t]*\[|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    text = pattern2.sub("\n", text)
    return text


def _rmtree(path: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] would remove {path}")
        return
    shutil.rmtree(path)
    print(f"Removed {path}")


def uninstall(home: Path, dry_run: bool) -> None:
    agents_root = home / ".agents" / "skills" / "hextile"
    codex_home = home / ".codex"
    config_path = codex_home / "config.toml"
    if agents_root.is_dir():
        _rmtree(agents_root, dry_run)
    else:
        print(f"No skills dir at {agents_root}")

    # Marker-owned leftover from pre-0.2.1 (~/.codex/skills/hextile). Never
    # delete an unmarked tree or sibling files.
    legacy_dir = codex_home / "skills" / "hextile"
    if legacy_dir.is_dir() and (legacy_dir / MARKER_NAME).is_file():
        _rmtree(legacy_dir, dry_run)
    elif legacy_dir.is_dir():
        print(f"Leaving unmarked {legacy_dir}")

    if config_path.is_file():
        existing = config_path.read_text(encoding="utf-8")
        cleaned = _remove_hextile_section(existing)
        if cleaned != existing:
            if dry_run:
                print(f"[dry-run] would strip [{MCP_SECTION}] from {config_path}")
            else:
                config_path.write_text(cleaned, encoding="utf-8")
                print(f"Stripped [{MCP_SECTION}] from {config_path}")
        else:
            print("No hextile MCP block in config.toml")
    else:
        print(f"No config at {config_path}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Install hextile Codex twin")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Override HOME (tests). Codex dir becomes HOME/.codex",
    )
    p.add_argument(
        "--write-fragment",
        action="store_true",
        help="Only regenerate codex/AGENTS-fragment.md from SKILL.md",
    )
    args = p.parse_args(argv)

    if args.write_fragment:
        out = write_agents_fragment()
        print(f"Wrote {out}")
        return 0

    home = args.home if args.home is not None else Path(os.path.expanduser("~"))

    print(
        f"hextile-agent Codex installer v{PACKAGE_VERSION} "
        f"(requires Codex >= {MIN_CODEX}; stdio MCP only)"
    )

    script = mcp_script()
    if not script.is_file():
        print(f"ERROR: MCP script missing: {script}", file=sys.stderr)
        return 1

    if args.uninstall:
        uninstall(home, args.dry_run)
        return 0

    ensure_skills(home, args.dry_run)
    patch_config(home / ".codex" / "config.toml", script, args.dry_run)
    # Keep fragment in sync on install.
    if not args.dry_run:
        frag = write_agents_fragment()
        print(f"Synced {frag}")
    print("Done. Restart Codex and check /mcp for 'hextile'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
