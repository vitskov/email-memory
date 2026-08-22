from __future__ import annotations

import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from email_memory_store.hermes_addon import installer
from email_memory_store.hermes_addon.installer import (
    CONTROL_SERVER,
    HermesAddonError,
    disable_hermes_addon,
    install_hermes_addon,
)
from email_memory_store.hermes_addon.skill import SKILL_CONTENT
from email_memory_store.tui.private_setup import PrivateSetupValues, write_private_setup


def _executable(path: Path) -> Path:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _setup(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    hermes = _executable(bin_dir / "hermes")
    _executable(bin_dir / "python")
    launcher = _executable(bin_dir / "launcher")
    config_home = tmp_path / "private-config"
    write_private_setup(
        PrivateSetupValues(
            runtime_root=str(tmp_path / "runtime"),
            hermes_executable=str(hermes),
            account_label="primary",
            account_email="person@example.test",
            telegram_chat_id="123456",
            telegram_thread_id="789",
        ),
        config_home=config_home,
    )
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir(mode=0o700)
    (hermes_home / "skills").mkdir(mode=0o700)
    config = hermes_home / "config.yaml"
    config.write_bytes(b"original: true\n")
    config.chmod(0o600)
    env_path = hermes_home / ".env"
    env_path.write_text("TELEGRAM_ALLOWED_USERS=123456\n", encoding="utf-8")
    env_path.chmod(0o600)
    return config_home, hermes_home, launcher, env_path


def _fake_hermes(
    monkeypatch,
    config: Path,
    env_path: Path,
    *,
    fail_writer: bool = False,
    lose_writer_response: bool = False,
    fail_check_after_writer: bool = False,
    fail_activation_after_writer: bool = False,
    mutate_before_writer: bool = False,
    concurrent_bytes: bytes = b"concurrent config\n",
    initially_enabled: bool = True,
):
    settings: dict[str, object] = {
        "platforms.telegram.extra.dm_topics": [
            {
                "chat_id": 999,
                "topics": [{"name": "Other", "thread_id": 111}],
            }
        ]
    }
    calls: list[tuple[str, ...]] = []
    events: list[str] = []
    enabled = initially_enabled

    def run(executable, args, *, command_env, allow_missing=False):
        del executable, command_env
        call = tuple(args)
        calls.append(call)
        if call == ("config", "env-path"):
            return str(env_path)
        if call[:2] == ("config", "check"):
            if fail_check_after_writer and "write" in events:
                config.write_bytes(concurrent_bytes)
                config.chmod(0o600)
                raise HermesAddonError("Hermes configuration command failed")
            events.append("check")
            return ""
        if call[:2] == ("config", "get"):
            key = call[-1]
            if (
                mutate_before_writer
                and key == "platforms.telegram.extra.dm_topics"
                and "write" not in events
            ):
                config.write_bytes(concurrent_bytes)
                config.chmod(0o600)
            if key not in settings:
                if allow_missing:
                    return ""
                raise HermesAddonError("not configured")
            return json.dumps(settings[key])
        raise AssertionError(call)

    def write(python_executable, *, updates, deletes, expected_digest, command_env):
        del python_executable, command_env
        events.append("write")
        if installer._content_digest(config.read_bytes()) != expected_digest:
            raise HermesAddonError("Hermes structured configuration write failed")
        if fail_writer:
            raise HermesAddonError("Hermes structured configuration write failed")
        for path, value in updates:
            settings[".".join(path)] = value
        for path in deletes:
            settings.pop(".".join(path), None)
        config.write_text(json.dumps(settings, sort_keys=True), encoding="utf-8")
        config.chmod(0o600)
        if lose_writer_response:
            raise HermesAddonError("Hermes structured configuration write failed")
        return installer._content_digest(config.read_bytes())

    def is_enabled(*, environ):
        del environ
        return enabled

    def set_enabled(value, *, environ):
        nonlocal enabled
        del environ
        if value and fail_activation_after_writer and "write" in events:
            raise HermesAddonError("activation failed")
        enabled = value
        events.append(f"enabled:{str(value).lower()}")

    monkeypatch.setattr(installer, "_run_hermes", run)
    monkeypatch.setattr(installer, "_run_structured_writer", write)
    monkeypatch.setattr(installer.control_jobs, "is_enabled", is_enabled)
    monkeypatch.setattr(installer.control_jobs, "set_enabled", set_enabled)
    return settings, calls, events, lambda: enabled


def test_install_adds_topic_skill_and_two_fixed_mcp_modes_transactionally(
    tmp_path, monkeypatch
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    settings, calls, events, enabled = _fake_hermes(
        monkeypatch, hermes_home / "config.yaml", env_path
    )

    result = install_hermes_addon(
        config_home=config_home,
        hermes_home=hermes_home,
        launcher=launcher,
        environ={"HOME": str(tmp_path)},
    )

    assert settings["platforms.telegram.extra.dm_topics"][1] == {
        "chat_id": 123456,
        "topics": [{"name": "Email Memory", "thread_id": 789, "skill": "email-memory"}],
    }
    assert settings["mcp_servers.email_memory_store"]["command"] == str(launcher)
    assert settings["mcp_servers.email_memory_store"]["tools"]["resources"] is False
    assert settings["mcp_servers.email_memory_store"]["tools"]["prompts"] is False
    assert settings[f"mcp_servers.{CONTROL_SERVER}"]["trust"] == "untrusted"
    assert settings[f"mcp_servers.{CONTROL_SERVER}"]["tools"]["resources"] is False
    assert settings[f"mcp_servers.{CONTROL_SERVER}"]["tools"]["prompts"] is False
    assert result.skill_path.read_text(encoding="utf-8") == SKILL_CONTENT
    assert result.skill_path.stat().st_mode & 0o077 == 0
    assert events.index("enabled:false") < events.index("write")
    assert events[-1] == "enabled:true"
    assert enabled() is True
    assert all(
        "123456" not in part and "789" not in part for call in calls for part in call
    )
    assert all(
        call[:2] in {("config", "check"), ("config", "get"), ("config", "env-path")}
        for call in calls
    )


def test_install_restores_exact_files_and_activation_on_writer_failure(
    tmp_path, monkeypatch
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    config = hermes_home / "config.yaml"
    original_config = config.read_bytes()
    skill_dir = hermes_home / "skills" / "email-memory"
    skill_dir.mkdir(mode=0o700)
    skill = skill_dir / "SKILL.md"
    skill.write_text(SKILL_CONTENT, encoding="utf-8")
    skill.chmod(0o600)
    _, _, events, enabled = _fake_hermes(
        monkeypatch, config, env_path, fail_writer=True, initially_enabled=True
    )

    with pytest.raises(HermesAddonError, match="structured configuration"):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )

    assert config.read_bytes() == original_config
    assert skill.read_text(encoding="utf-8") == SKILL_CONTENT
    assert events[-1] == "enabled:true"
    assert enabled() is True


def test_install_recovers_when_writer_commits_but_response_is_lost(
    tmp_path, monkeypatch
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    settings, _, events, enabled = _fake_hermes(
        monkeypatch,
        hermes_home / "config.yaml",
        env_path,
        lose_writer_response=True,
    )

    result = install_hermes_addon(
        config_home=config_home,
        hermes_home=hermes_home,
        launcher=launcher,
        environ={"HOME": str(tmp_path)},
    )

    assert result.skill_path.read_text(encoding="utf-8") == SKILL_CONTENT
    assert settings[f"mcp_servers.{CONTROL_SERVER}"]["trust"] == "untrusted"
    assert events.count("write") == 1
    assert enabled() is True


def test_install_rejects_existing_unowned_skill_before_mutation(tmp_path, monkeypatch):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    skill_dir = hermes_home / "skills" / "email-memory"
    skill_dir.mkdir(mode=0o700)
    skill = skill_dir / "SKILL.md"
    skill.write_text("unrelated skill\n", encoding="utf-8")
    skill.chmod(0o600)
    _, _, events, enabled = _fake_hermes(
        monkeypatch, hermes_home / "config.yaml", env_path
    )

    with pytest.raises(HermesAddonError, match="skill ownership conflicts"):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )

    assert skill.read_text(encoding="utf-8") == "unrelated skill\n"
    assert "write" not in events
    assert "enabled:false" not in events
    assert enabled() is True


@pytest.mark.parametrize(
    ("server", "existing", "message"),
    [
        (
            "mcp_servers.email_memory_store",
            {"command": "/unrelated/retrieval", "args": []},
            "retrieval ownership conflicts",
        ),
        (
            f"mcp_servers.{CONTROL_SERVER}",
            {"command": "/unrelated/control", "args": ["--mode", "control"]},
            "control ownership conflicts",
        ),
    ],
)
def test_install_rejects_existing_unowned_mcp_registration_before_mutation(
    tmp_path, monkeypatch, server, existing, message
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    config = hermes_home / "config.yaml"
    original = config.read_bytes()
    settings, _, events, enabled = _fake_hermes(monkeypatch, config, env_path)
    settings[server] = existing

    with pytest.raises(HermesAddonError, match=message):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )

    assert config.read_bytes() == original
    assert "write" not in events
    assert "enabled:false" not in events
    assert enabled() is True


def test_install_accepts_and_hardens_package_owned_core_retrieval_shape(
    tmp_path, monkeypatch
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    settings, _, _, _ = _fake_hermes(monkeypatch, hermes_home / "config.yaml", env_path)
    settings["mcp_servers.email_memory_store"] = {
        "command": str(launcher),
        "args": [],
        "enabled": True,
    }

    install_hermes_addon(
        config_home=config_home,
        hermes_home=hermes_home,
        launcher=launcher,
        environ={"HOME": str(tmp_path)},
    )

    assert settings["mcp_servers.email_memory_store"] == installer._retrieval_config(
        launcher
    )


def test_install_prewrite_concurrent_config_change_aborts_without_clobber(
    tmp_path, monkeypatch
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    config = hermes_home / "config.yaml"
    concurrent = b"concurrent owner edit\n"
    _, _, _, enabled = _fake_hermes(
        monkeypatch,
        config,
        env_path,
        mutate_before_writer=True,
        concurrent_bytes=concurrent,
    )

    with pytest.raises(HermesAddonError, match="transaction conflicted"):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )

    assert config.read_bytes() == concurrent
    assert enabled() is False


def test_install_postwrite_concurrent_config_change_is_not_rolled_back(
    tmp_path, monkeypatch
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    config = hermes_home / "config.yaml"
    concurrent = b"later concurrent owner edit\n"
    _, _, _, enabled = _fake_hermes(
        monkeypatch,
        config,
        env_path,
        fail_check_after_writer=True,
        concurrent_bytes=concurrent,
    )

    with pytest.raises(HermesAddonError, match="transaction conflicted"):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )

    assert config.read_bytes() == concurrent
    assert enabled() is False


@pytest.mark.parametrize(
    "allowed_users",
    [
        "*",
        "123456,999",
        "@owner",
        "0123456",
        "999",
        "123456\nTELEGRAM_ALLOWED_USERS=123456",
    ],
)
def test_install_fails_closed_on_non_exact_owner_dm_authorization(
    tmp_path, monkeypatch, allowed_users
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    env_path.write_text(f"TELEGRAM_ALLOWED_USERS={allowed_users}\n", encoding="utf-8")
    _fake_hermes(monkeypatch, hermes_home / "config.yaml", env_path)

    with pytest.raises(HermesAddonError, match="owner authorization") as captured:
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )
    assert "123456" not in str(captured.value)
    assert "789" not in str(captured.value)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GATEWAY_ALLOWED_USERS", "999"),
        ("GATEWAY_ALLOW_ALL_USERS", "true"),
        ("TELEGRAM_ALLOW_ALL_USERS", "1"),
        ("TELEGRAM_GROUP_ALLOWED_USERS", "999"),
        ("TELEGRAM_GROUP_ALLOWED_CHATS", "999"),
        ("TELEGRAM_ALLOW_BOTS", "mentions"),
        ("TELEGRAM_GUEST_MODE", "yes"),
        ("GATEWAY_MULTIPLEX_PROFILES", "on"),
    ],
)
def test_install_rejects_alternate_environment_authorization_surfaces(
    tmp_path, monkeypatch, variable, value
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    env_path.write_text(
        f"TELEGRAM_ALLOWED_USERS=123456\n{variable}={value}\n",
        encoding="utf-8",
    )
    _fake_hermes(monkeypatch, hermes_home / "config.yaml", env_path)

    with pytest.raises(HermesAddonError, match="owner authorization") as captured:
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )
    assert "123456" not in str(captured.value)
    assert "789" not in str(captured.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("platforms.telegram.group_allow_from", ["999"]),
        ("platforms.telegram.extra.group_allowed_chats", ["999"]),
        ("platforms.telegram.allow_admin_from", ["999"]),
        ("platforms.telegram.allow_bots", "mentions"),
        ("platforms.telegram.guest_mode", True),
        ("platforms.telegram.dm_policy", "pairing"),
        ("platforms.telegram.group_policy", "open"),
        ("platforms.telegram.extra.unauthorized_dm_behavior", "pair"),
        ("multiplex_profiles", True),
    ],
)
def test_install_rejects_alternate_config_authorization_surfaces(
    tmp_path, monkeypatch, key, value
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    settings, _, events, _ = _fake_hermes(
        monkeypatch, hermes_home / "config.yaml", env_path
    )
    settings[key] = value

    with pytest.raises(HermesAddonError, match="owner authorization") as captured:
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )
    assert "write" not in events
    assert "123456" not in str(captured.value)
    assert "789" not in str(captured.value)


def test_install_rejects_non_owner_pairing_approval(tmp_path, monkeypatch):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    pairing = hermes_home / "platforms" / "pairing"
    (hermes_home / "platforms").mkdir(mode=0o700)
    pairing.mkdir(mode=0o700)
    approvals = pairing / "telegram-approved.json"
    approvals.write_text(json.dumps({"999": {"approved_at": 1}}), encoding="utf-8")
    approvals.chmod(0o600)
    _fake_hermes(monkeypatch, hermes_home / "config.yaml", env_path)

    with pytest.raises(HermesAddonError, match="owner authorization") as captured:
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )
    assert "123456" not in str(captured.value)
    assert "789" not in str(captured.value)


def test_install_rejects_duplicate_topic_binding_without_mutation(
    tmp_path, monkeypatch
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    config = hermes_home / "config.yaml"
    settings, _, events, _ = _fake_hermes(monkeypatch, config, env_path)
    settings["platforms.telegram.extra.dm_topics"] = [
        {"chat_id": 123456, "topics": [{"name": "Email Memory", "thread_id": 789}]},
        {"chat_id": 123456, "topics": [{"name": "Email Memory", "thread_id": 789}]},
    ]

    with pytest.raises(HermesAddonError, match="ambiguous"):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )
    assert "write" not in events


def test_install_rejects_topic_owned_by_unrelated_skill_before_mutation(
    tmp_path, monkeypatch
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    settings, _, events, enabled = _fake_hermes(
        monkeypatch, hermes_home / "config.yaml", env_path
    )
    settings["platforms.telegram.extra.dm_topics"] = [
        {
            "chat_id": 123456,
            "topics": [
                {"name": "Email Memory", "thread_id": 789, "skill": "unrelated"}
            ],
        }
    ]

    with pytest.raises(HermesAddonError, match="topic skill ownership conflicts"):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )

    assert "write" not in events
    assert "enabled:false" not in events
    assert enabled() is True


@pytest.mark.parametrize("existing_skill", [None, "email-memory"])
def test_install_accepts_topic_with_absent_or_owned_skill(
    tmp_path, monkeypatch, existing_skill
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    settings, _, _, _ = _fake_hermes(monkeypatch, hermes_home / "config.yaml", env_path)
    topic = {"name": "Email Memory", "thread_id": 789}
    if existing_skill is not None:
        topic["skill"] = existing_skill
    settings["platforms.telegram.extra.dm_topics"] = [
        {"chat_id": 123456, "topics": [topic]}
    ]

    install_hermes_addon(
        config_home=config_home,
        hermes_home=hermes_home,
        launcher=launcher,
        environ={"HOME": str(tmp_path)},
    )

    assert (
        settings["platforms.telegram.extra.dm_topics"][0]["topics"][0]["skill"]
        == "email-memory"
    )


def test_ambiguous_writer_recovery_never_claims_concurrent_config_for_rollback(
    tmp_path, monkeypatch
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    config = hermes_home / "config.yaml"
    concurrent = b"unrelated concurrent owner edit\n"
    skill_dir = hermes_home / "skills" / "email-memory"
    skill_dir.mkdir(mode=0o700)
    skill = skill_dir / "SKILL.md"
    skill.write_text(SKILL_CONTENT, encoding="utf-8")
    skill.chmod(0o600)
    settings, _, _, enabled = _fake_hermes(
        monkeypatch,
        config,
        env_path,
        mutate_before_writer=True,
        fail_activation_after_writer=True,
        concurrent_bytes=concurrent,
    )
    settings["platforms.telegram.extra.dm_topics"] = [
        {
            "chat_id": 123456,
            "topics": [
                {
                    "name": "Email Memory",
                    "thread_id": 789,
                    "skill": "email-memory",
                }
            ],
        }
    ]
    settings["mcp_servers.email_memory_store"] = installer._retrieval_config(launcher)
    settings[f"mcp_servers.{CONTROL_SERVER}"] = installer._control_config(launcher)

    with pytest.raises(HermesAddonError, match="transaction conflicted"):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )

    assert config.read_bytes() == concurrent
    assert enabled() is False


def test_install_rejects_hardlinked_config_without_mutation(tmp_path, monkeypatch):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    config = hermes_home / "config.yaml"
    extra_link = tmp_path / "config-link"
    extra_link.hardlink_to(config)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        installer, "_run_hermes", lambda *args, **kwargs: calls.append(args[1])
    )

    with pytest.raises(HermesAddonError, match="unsafe"):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )
    assert calls == []


def test_install_rejects_symlinked_config_without_mutation(tmp_path, monkeypatch):
    config_home, hermes_home, launcher, _ = _setup(tmp_path)
    config = hermes_home / "config.yaml"
    target = tmp_path / "actual-config"
    target.write_bytes(config.read_bytes())
    target.chmod(0o600)
    config.unlink()
    config.symlink_to(target)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        installer, "_run_hermes", lambda *args, **kwargs: calls.append(args[1])
    )

    with pytest.raises(HermesAddonError, match="symbolic link"):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )
    assert calls == []
    assert target.read_bytes() == b"original: true\n"


def test_install_rejects_executable_in_writable_ancestry(tmp_path):
    config_home, hermes_home, launcher, _ = _setup(tmp_path)
    launcher.parent.chmod(0o770)

    with pytest.raises(HermesAddonError, match="ancestry is unsafe"):
        install_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            launcher=launcher,
            environ={"HOME": str(tmp_path)},
        )


def test_hermes_python_accepts_uv_symlink_behind_owner_only_ancestor(tmp_path):
    sealed = tmp_path / "sealed"
    sealed.mkdir(mode=0o700)
    release_bin = sealed / "hermes-agent" / "release" / "venv" / "bin"
    release_bin.mkdir(parents=True, mode=0o700)
    hermes = _executable(release_bin / "hermes")
    uv_bin = sealed / "uv" / "python" / "cpython" / "bin"
    uv_bin.mkdir(parents=True, mode=0o775)
    for parent in (
        sealed / "uv",
        sealed / "uv" / "python",
        sealed / "uv" / "python" / "cpython",
    ):
        parent.chmod(0o775)
    python_target = _executable(uv_bin / "python3.14")
    python_target.chmod(0o775)
    (release_bin / "python").symlink_to(python_target)

    assert installer._validate_hermes_python(hermes) == release_bin / "python"


def test_command_environment_rejects_relative_xdg_paths(tmp_path):
    with pytest.raises(HermesAddonError, match="environment is invalid"):
        installer._command_environment(
            {"HOME": str(tmp_path), "XDG_STATE_HOME": "relative-state"},
            hermes_home=None,
        )


def test_structured_writer_keeps_private_ids_out_of_child_argv(monkeypatch, tmp_path):
    observed: list[tuple[list[str], str | None]] = []

    def run(argv, **kwargs):
        observed.append((argv, kwargs.get("input")))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"digest": "a" * 64}),
            stderr="",
        )

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer._run_structured_writer(
        tmp_path / "python",
        updates=((("private", "binding"), {"chat_id": 123456, "thread_id": 789}),),
        deletes=(),
        expected_digest="b" * 64,
        command_env={},
    )

    assert len(observed) == 1
    argv, stdin = observed[0]
    assert argv[1:3] == ["-I", "-c"]
    assert all("123456" not in part and "789" not in part for part in argv)
    assert stdin is not None and "123456" in stdin and "789" in stdin


def test_disable_removes_only_owned_addon_settings_and_leaves_retrieval(
    tmp_path, monkeypatch
):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    config = hermes_home / "config.yaml"
    settings, _, events, enabled = _fake_hermes(monkeypatch, config, env_path)
    install_hermes_addon(
        config_home=config_home,
        hermes_home=hermes_home,
        launcher=launcher,
        environ={"HOME": str(tmp_path)},
    )
    retrieval_before = settings["mcp_servers.email_memory_store"]
    events.clear()

    result = disable_hermes_addon(
        config_home=config_home,
        hermes_home=hermes_home,
        environ={"HOME": str(tmp_path)},
    )

    assert events.index("enabled:false") < events.index("write")
    assert enabled() is False
    assert settings["mcp_servers.email_memory_store"] == retrieval_before
    assert f"mcp_servers.{CONTROL_SERVER}" not in settings
    assert all(
        entry.get("chat_id") != 123456
        for entry in settings["platforms.telegram.extra.dm_topics"]
    )
    assert not result.skill_path.exists()


def test_install_and_disable_are_serialized_and_finish_disabled(tmp_path, monkeypatch):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    _, _, _, enabled = _fake_hermes(monkeypatch, hermes_home / "config.yaml", env_path)
    underlying_writer = installer._run_structured_writer
    install_write_started = threading.Event()
    release_install = threading.Event()
    disable_done = threading.Event()
    errors: list[BaseException] = []
    write_count = 0

    def blocking_writer(*args, **kwargs):
        nonlocal write_count
        write_count += 1
        if write_count == 1:
            install_write_started.set()
            assert release_install.wait(5)
        return underlying_writer(*args, **kwargs)

    monkeypatch.setattr(installer, "_run_structured_writer", blocking_writer)

    def run_install():
        try:
            install_hermes_addon(
                config_home=config_home,
                hermes_home=hermes_home,
                launcher=launcher,
                environ={"HOME": str(tmp_path)},
            )
        except BaseException as error:
            errors.append(error)

    def run_disable():
        try:
            disable_hermes_addon(
                config_home=config_home,
                hermes_home=hermes_home,
                environ={"HOME": str(tmp_path)},
            )
        except BaseException as error:
            errors.append(error)
        finally:
            disable_done.set()

    install_thread = threading.Thread(target=run_install)
    disable_thread = threading.Thread(target=run_disable)
    install_thread.start()
    assert install_write_started.wait(5)
    disable_thread.start()
    assert not disable_done.wait(0.2)
    release_install.set()
    install_thread.join(5)
    disable_thread.join(5)

    assert not install_thread.is_alive()
    assert not disable_thread.is_alive()
    assert errors == []
    assert enabled() is False


def test_disable_restores_files_and_prior_activation_on_failure(tmp_path, monkeypatch):
    config_home, hermes_home, launcher, env_path = _setup(tmp_path)
    config = hermes_home / "config.yaml"
    settings, _, _, _ = _fake_hermes(monkeypatch, config, env_path)
    install_hermes_addon(
        config_home=config_home,
        hermes_home=hermes_home,
        launcher=launcher,
        environ={"HOME": str(tmp_path)},
    )
    original_config = config.read_bytes()
    skill = hermes_home / "skills" / "email-memory" / "SKILL.md"
    failing_settings, _, events, enabled = _fake_hermes(
        monkeypatch, config, env_path, fail_writer=True, initially_enabled=True
    )
    failing_settings["platforms.telegram.extra.dm_topics"] = json.loads(
        original_config
    )["platforms.telegram.extra.dm_topics"]
    failing_settings["mcp_servers.email_memory_store"] = json.loads(original_config)[
        "mcp_servers.email_memory_store"
    ]
    failing_settings[f"mcp_servers.{CONTROL_SERVER}"] = json.loads(original_config)[
        f"mcp_servers.{CONTROL_SERVER}"
    ]

    with pytest.raises(HermesAddonError, match="structured configuration"):
        disable_hermes_addon(
            config_home=config_home,
            hermes_home=hermes_home,
            environ={"HOME": str(tmp_path)},
        )
    assert config.read_bytes() == original_config
    assert skill.read_text(encoding="utf-8") == SKILL_CONTENT
    assert events[-1] == "enabled:true"
    assert enabled() is True


def test_public_skill_encodes_button_confirmation_and_recovery_contracts():
    normalized = " ".join(SKILL_CONTENT.split())
    assert "Search\n2. Ask\n3. Status\n4. Operations" in SKILL_CONTENT
    assert "Update\n2. Retry failures\n3. Reconcile\n4. Main menu" in SKILL_CONTENT
    assert "first and recommended choice must be `Cancel`" in SKILL_CONTENT
    assert "never replay it automatically" in normalized
    assert "Gateway administration is outside this skill" in normalized
