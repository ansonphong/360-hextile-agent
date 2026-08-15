"""Focused Codex installer discovery — skills land under ~/.agents/skills.

Not a copy test. Drives install.py main() with --home temp and proves:
- SKILL.md + references/ under <home>/.agents/skills/hextile
- MCP table stays in <home>/.codex/config.toml with sys.executable
- fresh home has no <home>/.codex/skills/hextile
- uninstall removes the new root and marker-owned legacy
- unmarked sibling files survive
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INSTALL_SPEC = importlib.util.spec_from_file_location(
    "hextile_codex_install_discovery", ROOT / "codex" / "install.py"
)
assert INSTALL_SPEC is not None and INSTALL_SPEC.loader is not None
INSTALL = importlib.util.module_from_spec(INSTALL_SPEC)
INSTALL_SPEC.loader.exec_module(INSTALL)


def test_install_writes_agents_skills_and_codex_mcp_table(tmp_path: Path) -> None:
    rc = INSTALL.main(["--home", str(tmp_path)])
    assert rc == 0

    skill = tmp_path / ".agents" / "skills" / "hextile" / "SKILL.md"
    refs = tmp_path / ".agents" / "skills" / "hextile" / "references"
    assert skill.is_file()
    assert refs.is_dir()
    assert not (tmp_path / ".codex" / "skills" / "hextile").exists()

    config = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.hextile]" in config
    assert sys.executable in config


def test_uninstall_removes_agents_root_and_marker_legacy_keeps_survivor(
    tmp_path: Path,
) -> None:
    assert INSTALL.main(["--home", str(tmp_path)]) == 0

    survivor = tmp_path / ".codex" / "skills" / "other" / "keep.txt"
    survivor.parent.mkdir(parents=True, exist_ok=True)
    survivor.write_text("stay", encoding="utf-8")

    legacy = tmp_path / ".codex" / "skills" / "hextile"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / INSTALL.MARKER_NAME).write_text("legacy\n", encoding="utf-8")
    (legacy / "SKILL.md").write_text("old", encoding="utf-8")

    assert INSTALL.main(["--uninstall", "--home", str(tmp_path)]) == 0

    assert not (tmp_path / ".agents" / "skills" / "hextile").exists()
    assert not legacy.exists()
    assert survivor.is_file()
    assert survivor.read_text(encoding="utf-8") == "stay"


def test_uninstall_leaves_unmarked_legacy_codex_skills(tmp_path: Path) -> None:
    unmarked = tmp_path / ".codex" / "skills" / "hextile"
    unmarked.mkdir(parents=True, exist_ok=True)
    keep = unmarked / "hand-written.md"
    keep.write_text("mine", encoding="utf-8")

    assert INSTALL.main(["--uninstall", "--home", str(tmp_path)]) == 0
    assert keep.is_file()
    assert keep.read_text(encoding="utf-8") == "mine"
