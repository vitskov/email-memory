from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def _local_link_targets(markdown_path: Path) -> list[Path]:
    targets: list[Path] = []
    for raw_target in MARKDOWN_LINK.findall(markdown_path.read_text(encoding="utf-8")):
        target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
        if target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        path_text = unquote(target.split("#", 1)[0])
        if path_text:
            targets.append((markdown_path.parent / path_text).resolve())
    return targets


def test_local_markdown_links_resolve() -> None:
    markdown_files = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    broken = [
        (markdown.relative_to(ROOT), target.relative_to(ROOT))
        for markdown in markdown_files
        for target in _local_link_targets(markdown)
        if not target.exists()
    ]

    assert broken == []


def test_readme_routes_each_audience_to_the_detailed_guides() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required_targets = {
        "docs/INSTALLATION.md",
        "docs/CONFIGURATION.md",
        "docs/MCP_INTEGRATION.md",
        "docs/ARCHITECTURE_OVERVIEW.md",
        "docs/PRIVACY_RELEASE_CONTROLS.md",
    }

    assert "## Choose your path" in readme
    assert required_targets <= set(MARKDOWN_LINK.findall(readme))


def test_readme_keeps_hermes_optional() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Optional Hermes integration", 1)[1].split("\n## ", 1)[0]
    normalized_section = " ".join(section.split())

    assert "optional" in normalized_section.lower()
    assert "not an installation or MCP requirement" in normalized_section
