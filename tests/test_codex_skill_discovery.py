"""Focused Codex installer discovery — skills land under ~/.agents/skills.

Not a copy test. Drives install.py main() with --home temp and proves:
- SKILL.md and source references under both hextile and 360-hextile
- SKILL.md under <home>/.agents/skills/hextile-upres
- MCP table stays in <home>/.codex/config.toml with sys.executable
- fresh home has no <home>/.codex/skills/hextile
- uninstall removes the new roots and marker-owned legacy
- unmarked sibling files survive under both ~/.codex/skills and ~/.agents/skills
- install/refresh refuses unowned destinations before changing any sibling
"""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

INSTALL_SPEC = importlib.util.spec_from_file_location(
    "hextile_codex_install_discovery", ROOT / "codex" / "install.py"
)
assert INSTALL_SPEC is not None and INSTALL_SPEC.loader is not None
INSTALL = importlib.util.module_from_spec(INSTALL_SPEC)
INSTALL_SPEC.loader.exec_module(INSTALL)


@pytest.fixture(autouse=True)
def isolated_generated_fragment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Exercise the real generator without rewriting the checked-in fragment."""
    isolated_root = tmp_path / "fragment-package"
    skill = isolated_root / "skills" / "hextile" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "skills" / "hextile" / "SKILL.md", skill)
    (isolated_root / "codex").mkdir()
    actual_generator = INSTALL.write_agents_fragment

    def generate_in_tmp() -> Path:
        with monkeypatch.context() as scoped:
            scoped.setattr(INSTALL, "package_root", lambda: isolated_root)
            return actual_generator()

    monkeypatch.setattr(INSTALL, "write_agents_fragment", generate_in_tmp)
    return isolated_root / "codex" / "AGENTS-fragment.md"


def test_install_writes_agents_skills_and_codex_mcp_table(tmp_path: Path) -> None:
    rc = INSTALL.main(["--home", str(tmp_path)])
    assert rc == 0

    for name in ("hextile", "360-hextile", "hextile-upres"):
        source = ROOT / "skills" / name
        installed = tmp_path / ".agents" / "skills" / name
        assert (installed / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
        assert f"version={INSTALL.PACKAGE_VERSION}\n" in (
            installed / INSTALL.MARKER_NAME
        ).read_text(encoding="utf-8")
        for reference in (source / "references").rglob("*"):
            if reference.is_file():
                assert (installed / reference.relative_to(source)).read_bytes() == reference.read_bytes()
    assert not (tmp_path / ".codex" / "skills" / "hextile").exists()

    specialized = (tmp_path / ".agents" / "skills" / "360-hextile" / "SKILL.md").read_text(encoding="utf-8")
    umbrella = (tmp_path / ".agents" / "skills" / "hextile" / "SKILL.md").read_text(encoding="utf-8")
    assert specialized.startswith("---\nname: 360-hextile\n")
    assert umbrella.startswith("---\nname: hextile\n")
    assert "installed `360-hextile`" in umbrella
    assert "workspace_schema:2" in specialized
    assert "negotiate v2" in umbrella
    for body in (specialized, umbrella):
        assert "source-modules/1" in body
        assert "https://360hextile.com/docs/create/shader" in body
        assert "human" in body.lower() and "Apply" in body
    assert "https://360hextile.com/static/templates/shaders/gitignore.txt" in specialized
    assert "https://360hextile.com/static/templates/shaders/gitattributes.txt" in specialized
    assert "only after **every Shader covered**" in specialized
    assert "Native Git diff/commit/checkout/push runs only on the user's explicit instruction" in specialized
    assert "never exports dirty workspace bytes or Git HEAD" in specialized
    assert "shared-file access" in specialized
    assert "unaccepted v2 clone has no saved revision or `shader.json`" in specialized
    assert "do not call that saved-context read or invent a revision" in specialized
    assert "Repair only acknowledged editable members" in specialized
    assert "including unused helpers in v2" in specialized
    assert "v2 `authoring.json` has no saved-base field" in specialized
    assert "v1 pair or complete v2 declared set" in umbrella

    references = tmp_path / ".agents" / "skills" / "hextile" / "references"
    recipes = (references / "recipes.md").read_text(encoding="utf-8")
    practices = (references / "best-practices.md").read_text(encoding="utf-8")
    assert "unaccepted clone with no saved revision" in recipes
    assert "The JSON block is an extension fragment, not a replacement `authoring.json`" in recipes
    assert "v2's closed metadata and full declared inventories without adding a `base`" in recipes
    assert "v2's complete metadata/inventory" in practices
    assert "workspace v1's sidecar schema 1 and v2's metadata schema 2" in practices

    config = (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.hextile]" in config
    assert sys.executable in config
    assert str(INSTALL.mcp_script().resolve()) in json.loads(
        next(line[7:] for line in config.splitlines() if line.startswith("args = "))
    )

    # Marker-owned refresh restores the package skill, keeping one MCP table.
    shader = tmp_path / ".agents" / "skills" / "360-hextile" / "SKILL.md"
    shader.write_text("stale installed skill", encoding="utf-8")
    assert INSTALL.main(["--home", str(tmp_path)]) == 0
    assert shader.read_bytes() == (ROOT / "skills" / "360-hextile" / "SKILL.md").read_bytes()
    assert (tmp_path / ".codex" / "config.toml").read_text().count("[mcp_servers.hextile]") == 1


def test_uninstall_removes_agents_root_and_marker_legacy_keeps_survivor(
    tmp_path: Path,
) -> None:
    assert INSTALL.main(["--home", str(tmp_path)]) == 0

    survivor = tmp_path / ".codex" / "skills" / "other" / "keep.txt"
    survivor.parent.mkdir(parents=True, exist_ok=True)
    survivor.write_text("stay", encoding="utf-8")

    agents_survivor = tmp_path / ".agents" / "skills" / "other" / "keep.txt"
    agents_survivor.parent.mkdir(parents=True, exist_ok=True)
    agents_survivor.write_text("stay-agents", encoding="utf-8")

    legacy = tmp_path / ".codex" / "skills" / "hextile"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / INSTALL.MARKER_NAME).write_text("legacy\n", encoding="utf-8")
    (legacy / "SKILL.md").write_text("old", encoding="utf-8")

    assert INSTALL.main(["--uninstall", "--home", str(tmp_path)]) == 0

    assert not (tmp_path / ".agents" / "skills" / "hextile").exists()
    assert not (tmp_path / ".agents" / "skills" / "360-hextile").exists()
    assert not (tmp_path / ".agents" / "skills" / "hextile-upres").exists()
    assert not legacy.exists()
    assert survivor.is_file()
    assert survivor.read_text(encoding="utf-8") == "stay"
    assert agents_survivor.is_file()
    assert agents_survivor.read_text(encoding="utf-8") == "stay-agents"
    assert "[mcp_servers.hextile]" not in (tmp_path / ".codex" / "config.toml").read_text()


def test_uninstall_leaves_unmarked_legacy_codex_skills(tmp_path: Path) -> None:
    unmarked = tmp_path / ".codex" / "skills" / "hextile"
    unmarked.mkdir(parents=True, exist_ok=True)
    keep = unmarked / "hand-written.md"
    keep.write_text("mine", encoding="utf-8")

    assert INSTALL.main(["--uninstall", "--home", str(tmp_path)]) == 0
    assert keep.is_file()
    assert keep.read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize("name", ["hextile", "360-hextile", "hextile-upres"])
def test_uninstall_leaves_unmarked_agents_skill(tmp_path: Path, name: str) -> None:
    unmarked = tmp_path / ".agents" / "skills" / name
    unmarked.mkdir(parents=True, exist_ok=True)
    keep = unmarked / "hand-written.md"
    keep.write_text("mine", encoding="utf-8")

    assert INSTALL.main(["--uninstall", "--home", str(tmp_path)]) == 0
    assert keep.is_file()
    assert keep.read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize("name", ["hextile", "360-hextile"])
@pytest.mark.parametrize("refresh", [False, True])
def test_install_refuses_unmarked_skill_without_changing_home(
    tmp_path: Path, name: str, refresh: bool,
) -> None:
    unmarked = tmp_path / ".agents" / "skills" / name
    if refresh:
        assert INSTALL.main(["--home", str(tmp_path)]) == 0
        (unmarked / INSTALL.MARKER_NAME).unlink()
    else:
        unmarked.mkdir(parents=True)
    (unmarked / "SKILL.md").write_text("foreign skill", encoding="utf-8")
    (unmarked / "keep.txt").write_text("foreign content", encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    with pytest.raises(SystemExit, match="Refusing to overwrite unowned skill destination"):
        INSTALL.main(["--home", str(tmp_path)])

    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("name", ["hextile", "360-hextile"])
def test_install_and_uninstall_leave_symlinked_skill(tmp_path: Path, name: str) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / INSTALL.MARKER_NAME).write_text("foreign marker", encoding="utf-8")
    keep = foreign / "SKILL.md"
    keep.write_text("foreign skill", encoding="utf-8")
    dest = tmp_path / ".agents" / "skills" / name
    dest.parent.mkdir(parents=True)
    dest.symlink_to(foreign, target_is_directory=True)

    with pytest.raises(SystemExit, match="Refusing to overwrite unowned skill destination"):
        INSTALL.main(["--home", str(tmp_path)])
    assert INSTALL.main(["--uninstall", "--home", str(tmp_path)]) == 0
    assert dest.is_symlink()
    assert keep.read_text(encoding="utf-8") == "foreign skill"


@pytest.mark.parametrize("name,relative,conflict", [
    ("360-hextile", "SKILL.md", "symlink"),
    ("360-hextile", "SKILL.md", "directory"),
    ("hextile", "references", "file"),
    ("hextile", "references", "symlink"),
    ("hextile", "references/best-practices.md", "directory"),
    ("hextile", "references/best-practices.md", "symlink"),
])
def test_refresh_refuses_copied_node_conflicts_before_any_change(
    tmp_path: Path, name: str, relative: str, conflict: str,
) -> None:
    assert INSTALL.main(["--home", str(tmp_path)]) == 0
    skills = tmp_path / ".agents" / "skills"
    # This sibling would be refreshed first without a complete preflight.
    (skills / "hextile" / "SKILL.md").write_text("stale sibling", encoding="utf-8")
    target = skills / name / relative
    was_directory = target.is_dir()
    if was_directory:
        shutil.rmtree(target)
    else:
        target.unlink()
    foreign = tmp_path / "foreign-target"
    if conflict == "symlink":
        if was_directory:
            foreign.mkdir()
            (foreign / "keep.txt").write_text("foreign content", encoding="utf-8")
        else:
            foreign.write_text("foreign content", encoding="utf-8")
        target.symlink_to(foreign, target_is_directory=was_directory)
    elif conflict == "directory":
        target.mkdir()
    else:
        target.write_text("foreign content", encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    with pytest.raises(SystemExit, match="Refusing conflicting skill destination"):
        INSTALL.main(["--home", str(tmp_path)])

    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    assert target.is_symlink() if conflict == "symlink" else target.exists()


@pytest.mark.parametrize("link", ["root", "marker"])
def test_uninstall_preserves_symlinked_legacy_ownership(tmp_path: Path, link: str) -> None:
    legacy = tmp_path / ".codex" / "skills" / "hextile"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / INSTALL.MARKER_NAME).write_text("foreign marker", encoding="utf-8")
    (foreign / "keep.txt").write_text("foreign content", encoding="utf-8")
    legacy.parent.mkdir(parents=True)
    if link == "root":
        legacy.symlink_to(foreign, target_is_directory=True)
    else:
        legacy.mkdir()
        (legacy / INSTALL.MARKER_NAME).symlink_to(foreign / INSTALL.MARKER_NAME)
        (legacy / "keep.txt").write_text("legacy content", encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    assert INSTALL.main(["--uninstall", "--home", str(tmp_path)]) == 0

    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    assert legacy.exists()


def test_fragment_tracks_legacy_hub_and_source_versions_agree(isolated_generated_fragment: Path) -> None:
    committed = (ROOT / "codex" / "AGENTS-fragment.md").read_text(encoding="utf-8")
    assert committed.split("\n\n", 1)[1] == INSTALL.strip_frontmatter(
        (ROOT / "skills" / "hextile" / "SKILL.md").read_text(encoding="utf-8")
    )
    assert INSTALL.main(["--write-fragment"]) == 0
    assert isolated_generated_fragment.read_text(encoding="utf-8") == committed
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    server = ast.parse((ROOT / "mcp" / "hextile_mcp.py").read_text(encoding="utf-8"))
    server_version = next(
        ast.literal_eval(node.value) for node in server.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SERVER_VERSION" for target in node.targets)
    )
    assert INSTALL.PACKAGE_VERSION == manifest["version"] == server_version == "0.4.0"
    assert f"**v{INSTALL.PACKAGE_VERSION}**" in (ROOT / "README.md").read_text(encoding="utf-8")
