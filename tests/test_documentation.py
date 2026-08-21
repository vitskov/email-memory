from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
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


def test_public_guides_exclude_private_deployment_and_obsolete_runbooks() -> None:
    public_guides = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    )
    obsolete_or_private_runbook_phrases = {
        "## Stable Deployment Environment",
        "runtime-provider package",
        "process ID and restart count",
        "scheduler launchers",
        "normal in-process retry budget",
        "bounded nightly scan",
        "legacy body cursor",
        "runtime_provider",
        "Explicit legacy root",
    }

    assert all(
        phrase not in public_guides for phrase in obsolete_or_private_runbook_phrases
    )


def test_cross_document_guidance_names_sections_that_exist() -> None:
    integration = (ROOT / "docs" / "MCP_INTEGRATION.md").read_text(encoding="utf-8")
    installation = (ROOT / "docs" / "INSTALLATION.md").read_text(encoding="utf-8")

    assert "public-core bootstrap and package upgrade checks" in integration
    assert "## Clone And Deploy" in installation
    assert "## Upgrade" in installation


def test_deployment_guide_documents_public_transaction_contract() -> None:
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    normalized = " ".join(deployment.split())
    required_fragments = {
        "./scripts/deploy.sh --accelerator auto",
        'install -d -m 0700 "$HOME/.local/src"',
        "umask 077",
        "currently Linux-only",
        "canonical passwd home",
        "rejects ambient `HOME`/XDG root changes and a custom deployment root",
        "Python 3.14",
        "schema version 2",
        "credential",
        "real, read-only mail authentication probe",
        "current/.deployment-readiness.json",
        "current/bin/",
        "email-memory-store-deploy doctor",
        "Rollback is automatic within a deployment transaction",
        "There is currently no public manual `rollback` subcommand",
        "weekly alert day",
        "current -> envs/<release>",
        "email_memory_store.integrations.hermes_fact_store:MemoryStore",
        "`telegram`, `slack`, or `discord`",
    }

    assert all(fragment in normalized for fragment in required_fragments)


def test_public_docs_define_hermes_gateway_lifecycle_boundary() -> None:
    guides = [
        ROOT / "docs" / "DEPLOYMENT.md",
        ROOT / "docs" / "INSTALLATION.md",
        ROOT / "docs" / "CONFIGURATION.md",
        ROOT / "docs" / "ARCHITECTURE_OVERVIEW.md",
    ]

    for guide in guides:
        normalized = " ".join(guide.read_text(encoding="utf-8").split()).lower()
        assert "email-memory never controls the hermes gateway lifecycle" in normalized


def test_deployment_guide_commands_expose_help() -> None:
    source_launcher = ROOT / "scripts" / "deploy.sh"
    commands = [
        [str(source_launcher), "--help"],
    ]

    for command in commands:
        subprocess.run(command, check=True, capture_output=True, text=True)


def test_documented_installed_commands_expose_help() -> None:
    bin_dir = Path(sys.executable).parent
    commands = [
        [str(bin_dir / "email-memory-store"), "--help"],
        [str(bin_dir / "email-memory-store"), "setup-private", "--help"],
        [str(bin_dir / "email-memory-store"), "init-db", "--help"],
        [str(bin_dir / "email-memory-store"), "runtime-doctor", "--help"],
        [str(bin_dir / "email-memory-store"), "status", "--help"],
        [str(bin_dir / "email-memory-store"), "search", "--help"],
        [str(bin_dir / "email-memory-store"), "embed-status", "--help"],
        [str(bin_dir / "email-memory-store-mcp"), "--help"],
    ]

    for command in commands:
        subprocess.run(command, check=True, capture_output=True, text=True)
