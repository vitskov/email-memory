from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


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


def _heading_fragments(markdown_path: Path) -> set[str]:
    fragments: set[str] = set()
    occurrences: dict[str, int] = {}
    for heading in MARKDOWN_HEADING.findall(markdown_path.read_text(encoding="utf-8")):
        plain = re.sub(r"`([^`]*)`", r"\1", heading).lower()
        plain = re.sub(r"[^\w\- ]", "", plain)
        base = re.sub(r"\s+", "-", plain.strip())
        occurrence = occurrences.get(base, 0)
        occurrences[base] = occurrence + 1
        fragments.add(base if occurrence == 0 else f"{base}-{occurrence}")
    return fragments


def _local_fragment_references(markdown_path: Path) -> list[tuple[Path, str]]:
    references: list[tuple[Path, str]] = []
    for raw_target in MARKDOWN_LINK.findall(markdown_path.read_text(encoding="utf-8")):
        target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
        if target.startswith(("http://", "https://", "mailto:")) or "#" not in target:
            continue
        path_text, raw_fragment = target.split("#", 1)
        target_path = (
            (markdown_path.parent / unquote(path_text)).resolve()
            if path_text
            else markdown_path.resolve()
        )
        references.append((target_path, unquote(raw_fragment).lower()))
    return references


def test_local_markdown_links_resolve() -> None:
    markdown_files = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    broken = [
        (markdown.relative_to(ROOT), target.relative_to(ROOT))
        for markdown in markdown_files
        for target in _local_link_targets(markdown)
        if not target.exists()
    ]

    assert broken == []


def test_local_markdown_fragments_resolve() -> None:
    markdown_files = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    broken = [
        (markdown.relative_to(ROOT), target.relative_to(ROOT), fragment)
        for markdown in markdown_files
        for target, fragment in _local_fragment_references(markdown)
        if target.exists() and fragment not in _heading_fragments(target)
    ]

    assert broken == []


def test_readme_routes_each_audience_to_the_detailed_guides() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required_targets = {
        "docs/README.md",
        "docs/INSTALLATION.md",
        "docs/DEPLOYMENT.md",
        "docs/CONFIGURATION.md",
        "docs/USAGE.md",
        "docs/MCP_INTEGRATION.md",
        "docs/ARCHITECTURE_OVERVIEW.md",
        "docs/PRIVACY_RELEASE_CONTROLS.md",
    }

    assert "## Choose your path" in readme
    assert required_targets <= set(MARKDOWN_LINK.findall(readme))


def test_readme_leads_with_the_supported_typical_deployment() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quick_start = readme.split("## Quick start: typical Linux deployment", 1)[1]
    quick_start = quick_start.split("\n## ", 1)[0]

    assert "./scripts/deploy.sh --accelerator auto" in quick_start
    assert 'install -d -m 0700 "$HOME/.local/src"' in quick_start
    assert "./scripts/bootstrap.sh" not in quick_start
    assert "./.venv" not in quick_start


def test_documentation_index_routes_tasks_and_defines_terms() -> None:
    index = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    required_targets = {
        "INSTALLATION.md",
        "DEPLOYMENT.md",
        "CONFIGURATION.md",
        "USAGE.md",
        "MCP_INTEGRATION.md",
        "ARCHITECTURE_OVERVIEW.md",
        "PRIVACY_RELEASE_CONTROLS.md",
    }

    assert "## New User" in index
    assert "## Operator" in index
    assert "## Contributor Or Maintainer" in index
    assert "## Terms" in index
    assert required_targets <= set(MARKDOWN_LINK.findall(index))


def test_detailed_guides_route_back_to_the_documentation_index() -> None:
    guides = (
        "INSTALLATION.md",
        "DEPLOYMENT.md",
        "CONFIGURATION.md",
        "USAGE.md",
        "MCP_INTEGRATION.md",
        "ARCHITECTURE_OVERVIEW.md",
        "PRIVACY_RELEASE_CONTROLS.md",
    )

    for guide in guides:
        content = (ROOT / "docs" / guide).read_text(encoding="utf-8")
        assert "README.md" in MARKDOWN_LINK.findall(content)


def test_usage_guide_separates_installed_and_checkout_commands() -> None:
    usage = (ROOT / "docs" / "USAGE.md").read_text(encoding="utf-8")
    normalized = " ".join(usage.split())

    assert "## Choose The Command Surface" in usage
    assert "current/venv/bin/email-memory-store" in usage
    assert "current/bin/email-memory-store-deploy" in usage
    assert "./.venv/bin/email-memory-store" in usage
    for command in (
        "runtime-doctor",
        "pipeline-status",
        "embed-status",
        "search",
        "ask",
        "browse",
        "cleanup-expired",
    ):
        assert command in usage
    assert (
        "email-memory never controls the hermes gateway lifecycle" in normalized.lower()
    )


def test_readme_distinguishes_standalone_and_deployment_provider_requirements() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Hermes and LLM integration", 1)[1].split("\n## ", 1)[0]
    normalized_section = " ".join(section.split())

    assert "standalone package installation" in normalized_section
    assert "without an LLM" in normalized_section
    assert "requires a configured Hermes executable" in normalized_section
    assert "one selected LLM provider" in normalized_section


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
        "target revision predates add-on/control support",
        "verify that no control job is active",
        "email-memory-store-hermes-addon --disable",
        "Only then check out the old revision",
        "Automatic transaction rollback to a previous add-on-capable release is unaffected",
        "weekly alert day",
        "current -> envs/<release>",
        "email_memory_store.integrations.hermes_fact_store:MemoryStore",
        "`telegram`, `slack`, or `discord`",
        "First index and readiness",
        "`awaiting-index`",
        "schema-version-3",
        '"$email_memory_deploy" nightly',
        "| `2` | `awaiting-index` |",
    }

    assert all(fragment in normalized for fragment in required_fragments)


def test_public_docs_define_hermes_gateway_lifecycle_boundary() -> None:
    guides = [
        ROOT / "docs" / "DEPLOYMENT.md",
        ROOT / "docs" / "INSTALLATION.md",
        ROOT / "docs" / "CONFIGURATION.md",
        ROOT / "docs" / "MCP_INTEGRATION.md",
        ROOT / "docs" / "ARCHITECTURE_OVERVIEW.md",
    ]

    for guide in guides:
        normalized = " ".join(guide.read_text(encoding="utf-8").split()).lower()
        assert "email-memory never controls the hermes gateway lifecycle" in normalized


def test_hermes_telegram_addon_contract_is_documented() -> None:
    integration = (ROOT / "docs" / "MCP_INTEGRATION.md").read_text(encoding="utf-8")
    normalized = " ".join(integration.split())

    required_fragments = {
        "## Hermes Telegram Button Menu",
        "existing generic Hermes installation",
        "built-in `clarify`",
        "numbered native inline buttons one per row",
        "there is no custom horizontal keyboard",
        "`email_memory_store`",
        "`email_memory_store_control`",
        "`system_status`, `job_start`, `job_status`",
        "`maintenance`, `retry_failed_bodies`, or `reconcile`",
        "`untrusted`",
        "Hermes approval gates the write-capable `job_start` tool",
        "`system_status` and `job_status` carry `readOnlyHint=true` and may be approval-exempt",
        "`worker_interrupted`",
        "never replays the operation automatically",
        "not a hard capability sandbox",
        "## Profile scope",
        "current single-profile design",
        "every Hermes-authorized platform session",
        "not only messages in the Email Memory Telegram topic",
        "Retrieval is registered as `full`",
        "send `/reload-mcp`",
        "complete its built-in confirmation prompt",
        "adding `now` does not bypass or complete the confirmation",
        "send `/new`",
        "complete that command's confirmation",
        "injects the topic's automatic skill only during session creation",
        "`menu` restores the main choices",
        "It cannot load the skill into a topic session that predates installation",
        "email-memory-store-hermes-addon",
        '"$email_memory_addon" --disable',
        "leaves the retrieval MCP registration",
        "mandatory before deploying or downgrading to a revision that predates add-on/control support",
        "verify `active_job` is empty",
        "Only then check out and deploy the pre-add-on revision",
        "Automatic transactional rollback to a previous add-on-capable release is unaffected",
    }

    assert all(fragment in normalized for fragment in required_fragments)
    assert "`/reload-mcp now`" not in integration


def test_hermes_routing_ids_have_a_separate_private_attachment() -> None:
    configuration = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    private_environment = configuration.split("## Private References", 1)[1].split(
        "## Hermes Add-On Routing", 1
    )[0]
    addon = " ".join(
        configuration.split("## Hermes Add-On Routing", 1)[1]
        .split("\n## ", 1)[0]
        .split()
    )

    assert "`hermes-addon.json`" in addon
    assert "positive ASCII integer strings" in addon
    assert "owner-only (`0600`)" in addon
    assert "never stores or requests the Telegram bot token" in addon
    assert "owner-only Hermes configuration" in addon
    assert "standard input, never child-process arguments" in addon
    assert "does not log or print them" in addon
    assert '"telegram_menu"' not in private_environment
    assert "deliberately has no Telegram menu field" in private_environment


def test_front_readme_discloses_single_profile_addon_reach() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())

    for fragment in (
        "originate only from the separate owner-only `hermes-addon.json`",
        "owner-only Hermes configuration",
        "child-process arguments or logs",
        "current single-profile design",
        "every Hermes-authorized platform session in that profile",
        "control registration is `untrusted`",
        "approval remains an independent guard for write-capable `job_start`",
        "`system_status` and `job_status` are annotated read-only and may be approval-exempt",
        "Telegram topic does not narrow tool reach",
    ):
        assert fragment in normalized


def test_addon_architecture_separates_retrieval_control_and_topic_routing() -> None:
    architecture = (ROOT / "docs" / "ARCHITECTURE_OVERVIEW.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(architecture.split())

    for fragment in (
        "## Hermes Telegram Add-On Boundary",
        "retrieval MCP",
        "control MCP",
        "`search` and `ask`",
        "`system_status`, `job_start`, and `job_status`",
        "`awaiting-index`",
        "not a capability sandbox",
        "owner-only `hermes-addon.json`",
    ):
        assert fragment in normalized


def test_addon_docs_define_conditional_transactions_and_name_ownership() -> None:
    integration = " ".join(
        (ROOT / "docs" / "MCP_INTEGRATION.md").read_text(encoding="utf-8").split()
    )
    deployment = " ".join(
        (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8").split()
    )
    configuration = " ".join(
        (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8").split()
    )
    architecture = " ".join(
        (ROOT / "docs" / "ARCHITECTURE_OVERVIEW.md").read_text(encoding="utf-8").split()
    )
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())

    for fragment in (
        "serializes Email Memory add-on install and disable operations with each other",
        "ordinary `hermes config` commands and other Hermes configuration writers do not acquire it",
        "Do not run any other Hermes configuration mutation",
        "compare-and-swap guard",
        "conditional rollback",
        "control jobs remain disabled",
        "later unrelated edit is preserved rather than overwritten",
        "not an absolute rollback guarantee",
        "existing `email-memory` skill",
        "existing `email_memory_store_control` registration",
        "package-owned core retrieval registration may be hardened",
        "an unrelated registration is never overwritten",
    ):
        assert fragment in integration

    for content in (deployment, configuration, architecture, readme):
        assert "do not run" in content.lower()
        assert "Hermes configuration" in content
        assert "control jobs" in content

    for content in (deployment, configuration, architecture):
        assert "`email-memory`" in content
        assert "`email_memory_store`" in content
        assert "`email_memory_store_control`" in content
        assert "package-owned core retrieval registration may be hardened" in content


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
        [str(bin_dir / "email-memory-store-hermes-addon"), "--help"],
    ]

    for command in commands:
        subprocess.run(command, check=True, capture_output=True, text=True)
