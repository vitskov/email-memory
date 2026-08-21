from __future__ import annotations

from pathlib import Path
import os
import py_compile
import re
import shlex
import shutil
import subprocess
import sys
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = ROOT / "src/email_memory_store/deployment"


def test_public_wheel_owns_hardened_deploy_launcher_and_operational_scripts() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "email-memory-store-deploy" not in project["project"]["scripts"]
    assert (
        "deployment/scripts/*.sh"
        in project["tool"]["setuptools"]["package-data"]["email_memory_store"]
    )
    assert {path.name for path in (DEPLOYMENT / "scripts").glob("*.sh")} == {
        "email_memory_environment.sh",
        "email_memory_store_deploy_launcher.sh",
        "email_memory_store_mcp_launcher.sh",
        "install_email_memory_mcp_launcher.sh",
        "nightly_cron_launcher.sh",
        "nightly_maintenance.sh",
    }


def test_deployment_coordinator_starts_before_project_dependencies_are_installed() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "email_memory_store.deployment.cli",
            "--help",
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(ROOT / "src"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Deploy from a clean public email-memory Git checkout." in completed.stdout
    assert "{bootstrap,doctor,nightly}" in completed.stdout


def test_operational_scripts_load_configuration_from_the_public_module() -> None:
    for name in ("nightly_cron_launcher.sh", "nightly_maintenance.sh"):
        source = (DEPLOYMENT / "scripts" / name).read_text(encoding="utf-8")
        assert "-m email_memory_store.local_config" in source
        assert "load_private_config" not in source


def test_provisioner_builds_exactly_one_public_wheel() -> None:
    source = (ROOT / "scripts/provision_email_memory_environment.sh").read_text(
        encoding="utf-8"
    )

    assert source.count('"$UV_BIN" build --wheel') == 1
    assert "PUBLIC_WHEEL" in source
    assert b"private" + b"/operations" not in source.encode()
    assert "PRIVATE_WHEEL" not in source
    assert "OPERATIONS_WHEEL" not in source


def test_provisioner_is_staging_only_and_has_no_current_mutation_path() -> None:
    source = (ROOT / "scripts/provision_email_memory_environment.sh").read_text(
        encoding="utf-8"
    )

    assert "CURRENT_LINK" not in source
    assert "activated email-memory release" not in source
    assert "staged and verified email-memory release" in source
    assert "--no-activate" in source  # Compatibility no-op for older coordinators.
    assert '--managed-python-install-dir "$PYTHON_INSTALL_DIR"' in source


def test_provisioner_rejects_ambient_uv_and_preserves_current(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    checkout.chmod(0o700)
    scripts.chmod(0o700)
    shutil.copyfile(
        ROOT / "scripts/provision_email_memory_environment.sh",
        scripts / "provision_email_memory_environment.sh",
    )
    (scripts / "provision_email_memory_environment.sh").chmod(0o700)
    for relative in (
        "pyproject.toml",
        "uv.lock",
        "scripts/bootstrap.sh",
        "src/email_memory_store/deployment/scripts/email_memory_store_deploy_launcher.sh",
    ):
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
        path.chmod(0o600)
        for parent in path.parents:
            if not parent.is_relative_to(checkout):
                break
            parent.chmod(0o700)

    deployment = tmp_path / "deployment"
    deployment.mkdir(mode=0o700)
    original = deployment / "original"
    original.mkdir(mode=0o700)
    current = deployment / "current"
    current.symlink_to(original)
    hostile = tmp_path / "hostile"
    hostile.mkdir(mode=0o700)
    marker = tmp_path / "hostile-uv-ran"
    fake_uv = hostile / "uv"
    fake_uv.write_text(
        f"#!/bin/sh\n/usr/bin/touch {marker}\nexit 99\n", encoding="utf-8"
    )
    fake_uv.chmod(0o700)
    bash_env = hostile / "bash-env"
    bash_env.write_text(f"/usr/bin/touch '{marker}'\n", encoding="utf-8")

    completed = subprocess.run(
        [
            str(scripts / "provision_email_memory_environment.sh"),
            "--public-checkout",
            str(checkout),
            "--deployment-root",
            str(deployment),
            "--allow-dirty",
        ],
        env=os.environ
        | {"PATH": str(hostile), "UV_BIN": "uv", "BASH_ENV": str(bash_env)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "trusted uv executable" in completed.stderr
    assert not marker.exists()
    assert current.resolve() == original


def test_deploy_ignores_hostile_path_and_python_injection(tmp_path: Path) -> None:
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    marker = tmp_path / "ambient-tool-ran"
    trusted_marker = tmp_path / "trusted-uv-ran"
    for name in ("uv", "python", "dirname", "git"):
        executable = hostile / name
        executable.write_text(
            f"#!/bin/sh\n/usr/bin/touch {marker}\nexit 99\n", encoding="utf-8"
        )
        executable.chmod(0o700)
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    checkout.chmod(0o700)
    scripts.chmod(0o700)
    shutil.copyfile(ROOT / "scripts/deploy.sh", scripts / "deploy.sh")
    (scripts / "deploy.sh").chmod(0o700)
    trusted_uv = checkout / "trusted-uv"
    trusted_uv.write_text(
        "#!/bin/sh\n"
        '[ -z "${UV_CONFIG_FILE:-}" ] && [ -z "${GIT_DIR:-}" ] && '
        '[ -z "${PIP_CONFIG_FILE:-}" ] && [ -z "${VIRTUAL_ENV:-}" ] && '
        '[ -z "${PYTHONPYCACHEPREFIX:-}" ] && [ -z "${BASH_ENV:-}" ] && '
        '[ "${UV_NO_CONFIG:-}" = 1 ] && [ "${GIT_CONFIG_NOSYSTEM:-}" = 1 ] '
        "|| exit 91\n"
        "/usr/bin/env | /usr/bin/grep -Eq '^(ENV|BASHOPTS|SHELLOPTS)=' && exit 92\n"
        f"/usr/bin/touch {trusted_marker}\nprintf '%s\\n' "
        "'clean public Git checkout'\n",
        encoding="utf-8",
    )
    trusted_uv.chmod(0o700)
    bash_env = hostile / "bash-env"
    bash_env.write_text(f"/usr/bin/touch '{marker}'\n", encoding="utf-8")

    completed = subprocess.run(
        [str(scripts / "deploy.sh"), "--synthetic"],
        env=os.environ
        | {
            "PATH": str(hostile),
            "PYTHONPATH": str(hostile),
            "PYTHONHOME": str(hostile),
            "PYTHONUSERBASE": str(hostile),
            "UV_BIN": str(trusted_uv),
            "UV_CONFIG_FILE": str(hostile / "uv.toml"),
            "GIT_DIR": str(hostile / "repository"),
            "PIP_CONFIG_FILE": str(hostile / "pip.conf"),
            "VIRTUAL_ENV": str(hostile / "venv"),
            "BASH_ENV": str(bash_env),
            "PYTHONPYCACHEPREFIX": str(hostile / "pycache"),
            "ENV": str(hostile / "env"),
            "BASHOPTS": "extglob",
            "SHELLOPTS": "braceexpand",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert trusted_marker.exists()
    assert not marker.exists()


def test_deployed_launcher_clears_hostile_python_and_uv_environment(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release_bin = release / "bin"
    venv_bin = release / "venv/bin"
    python_bin = release / "python/3.14/bin"
    for directory in (
        release,
        release_bin,
        release / "venv",
        venv_bin,
        release / "python",
        release / "python/3.14",
        python_bin,
    ):
        directory.mkdir(exist_ok=True)
        directory.chmod(0o700)
    launcher = release_bin / "email-memory-store-deploy"
    shutil.copyfile(
        DEPLOYMENT / "scripts/email_memory_store_deploy_launcher.sh", launcher
    )
    launcher.chmod(0o700)
    marker = tmp_path / "hostile-module-ran"
    invocation = tmp_path / "invocation"
    python = python_bin / "python"
    python.write_text(
        "#!/bin/bash\n"
        f'[[ -z "${{PYTHONPATH:-}}" && -z "${{PYTHONHOME:-}}" && '
        f'-z "${{UV_CONFIG_FILE:-}}" && -z "${{PYTHONPYCACHEPREFIX:-}}" ]] '
        f"|| {{ /usr/bin/touch '{marker}'; exit 90; }}\n"
        f"/usr/bin/env | /usr/bin/grep -Eq '^(BASH_ENV|ENV|BASHOPTS|SHELLOPTS)=' "
        f"&& {{ /usr/bin/touch '{marker}'; exit 91; }}\n"
        "if [[ ${1:-} == -I && ${2:-} == - ]]; then /usr/bin/cat >/dev/null; exit 0; fi\n"
        f"printf '%s\\n' \"$*\" > '{invocation}'\n",
        encoding="utf-8",
    )
    python.chmod(0o700)
    (venv_bin / "python").symlink_to(Path("../../python/3.14/bin/python"))
    pyvenv = release / "venv/pyvenv.cfg"
    pyvenv.write_text(f"home = {python_bin}\n", encoding="utf-8")
    pyvenv.chmod(0o600)
    marker_file = release / ".email-memory-release"
    marker_file.write_text(
        "public_revision=0123456789ab\npython=3.14\naccelerator_request=cpu\n",
        encoding="utf-8",
    )
    marker_file.chmod(0o600)
    receipt = release / ".deployment-readiness.json"
    receipt.write_text('{"status":"ready"}\n', encoding="utf-8")
    receipt.chmod(0o600)
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()\n", encoding="utf-8"
    )
    bash_env = hostile / "bash-env"
    bash_env.write_text(f"/usr/bin/touch '{marker}'\n", encoding="utf-8")

    completed = subprocess.run(
        [str(launcher), "doctor"],
        env=os.environ
        | {
            "PYTHONPATH": str(hostile),
            "PYTHONHOME": str(hostile),
            "UV_CONFIG_FILE": str(hostile / "uv.toml"),
            "BASH_ENV": str(bash_env),
            "PYTHONPYCACHEPREFIX": str(hostile / "pycache"),
            "ENV": str(hostile / "env"),
            "BASHOPTS": "extglob",
            "SHELLOPTS": "braceexpand",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()
    assert invocation.read_text(encoding="utf-8").strip() == (
        "-I -m email_memory_store.deployment.cli doctor"
    )


def test_production_environment_helper_ignores_hostile_home_and_xdg(
    tmp_path: Path,
) -> None:
    hostile = tmp_path / "hostile-home"
    hostile_data = hostile / "data"
    release = hostile_data / "email-memory-store/envs/hostile"
    for directory in (release / "venv/bin", release / "python/3.14/bin"):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    (hostile_data / "email-memory-store/current").symlink_to(release)
    for name in ("python", "email-memory-store", "email-memory-store-mcp"):
        executable = release / "venv/bin" / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o700)
    pyvenv = release / "venv/pyvenv.cfg"
    pyvenv.write_text(f"home = {release / 'python/3.14/bin'}\n", encoding="utf-8")
    pyvenv.chmod(0o600)
    helper = DEPLOYMENT / "scripts/email_memory_environment.sh"

    completed = subprocess.run(
        ["/bin/bash", "-c", f'source "{helper}"; printf %s "$EMAIL_MEMORY_DATA_HOME"'],
        env=os.environ | {"HOME": str(hostile), "XDG_DATA_HOME": str(hostile_data)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.stdout != str(hostile_data)


def test_environment_helper_blocks_malicious_python_pycache_prefix(
    tmp_path: Path,
) -> None:
    module_root = tmp_path / "modules"
    module_root.mkdir()
    victim = module_root / "victim.py"
    malicious_source = tmp_path / "malicious.py"
    marker = tmp_path / "malicious-bytecode-ran"
    malicious_source.write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    malicious_stat = malicious_source.stat()
    victim.write_text("VALUE = 1\n".ljust(malicious_stat.st_size), encoding="utf-8")
    os.utime(victim, (malicious_stat.st_mtime, malicious_stat.st_mtime))
    cache_prefix = tmp_path / "hostile-pycache"
    cache_query = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.util,sys; print(importlib.util.cache_from_source(sys.argv[1]))",
            str(victim),
        ],
        env=os.environ | {"PYTHONPYCACHEPREFIX": str(cache_prefix)},
        capture_output=True,
        text=True,
        check=True,
    )
    malicious_cache = Path(cache_query.stdout.strip())
    malicious_cache.parent.mkdir(parents=True)
    py_compile.compile(str(malicious_source), cfile=str(malicious_cache), doraise=True)
    import_command = (
        f"import sys; sys.path.insert(0, {str(module_root)!r}); import victim"
    )

    subprocess.run(
        [sys.executable, "-c", import_command],
        env=os.environ | {"PYTHONPYCACHEPREFIX": str(cache_prefix)},
        check=True,
    )
    assert marker.exists()
    marker.unlink()

    helper = DEPLOYMENT / "scripts/email_memory_environment.sh"
    completed = subprocess.run(
        [
            "/bin/bash",
            "-p",
            "-c",
            f'source "{helper}"; exec "{sys.executable}" -c {shlex.quote(import_command)}',
        ],
        env=os.environ
        | {
            "EMAIL_MEMORY_TEST_MODE": "1",
            "HOME": str(tmp_path),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "PYTHONPYCACHEPREFIX": str(cache_prefix),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()


def test_mcp_launcher_forces_production_environment_resolution() -> None:
    source = (DEPLOYMENT / "scripts/email_memory_store_mcp_launcher.sh").read_text(
        encoding="utf-8"
    )

    assert source.index("unset EMAIL_MEMORY_TEST_MODE") < source.index(
        'source "$ENVIRONMENT_HELPER"'
    )


@pytest.mark.parametrize(
    "name", ["email_memory_store_mcp_launcher.sh", "nightly_cron_launcher.sh"]
)
def test_runtime_launchers_ignore_bash_env(tmp_path: Path, name: str) -> None:
    marker = tmp_path / "bash-env-ran"
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(f"/usr/bin/touch '{marker}'\n", encoding="utf-8")
    launcher = tmp_path / name
    shutil.copyfile(DEPLOYMENT / "scripts" / name, launcher)
    launcher.chmod(0o700)

    subprocess.run(
        [str(launcher)],
        env=os.environ | {"BASH_ENV": str(bash_env)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert not marker.exists()


@pytest.mark.parametrize(
    "name", ["email_memory_store_mcp_launcher.sh", "nightly_cron_launcher.sh"]
)
def test_runtime_launcher_children_receive_clean_environment(
    tmp_path: Path, name: str
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir(mode=0o700)
    launcher = scripts / name
    shutil.copyfile(DEPLOYMENT / "scripts" / name, launcher)
    launcher.chmod(0o700)
    captured = tmp_path / "child-environment"
    captured_args = tmp_path / "child-arguments"
    child = tmp_path / "child"
    if name == "email_memory_store_mcp_launcher.sh":
        config = tmp_path / "config/email-memory-store"
        config.mkdir(parents=True, mode=0o700)
        runtime = config / "runtime.toml"
        runtime.write_text("synthetic\n", encoding="utf-8")
        runtime.chmod(0o600)
        child.write_text(
            f"#!/bin/sh\n/usr/bin/env >'{captured}'\n"
            f"printf '%s\\n' \"$@\" >'{captured_args}'\nexit 0\n",
            encoding="utf-8",
        )
        helper_source = (
            f"EMAIL_MEMORY_STORE_MCP_COMMAND='{child}'\n"
            f"XDG_CONFIG_HOME='{tmp_path / 'config'}'\n"
            "export EMAIL_MEMORY_STORE_MCP_COMMAND XDG_CONFIG_HOME\n"
        )
    else:
        child.write_text(
            f"#!/bin/sh\n/usr/bin/env >'{captured}'\nexit 1\n", encoding="utf-8"
        )
        helper_source = (
            f"EMAIL_MEMORY_OPERATIONAL_PYTHON='{child}'\n"
            "export EMAIL_MEMORY_OPERATIONAL_PYTHON\n"
        )
    child.chmod(0o700)
    helper = scripts / "email_memory_environment.sh"
    helper.write_text(helper_source, encoding="utf-8")
    helper.chmod(0o600)
    hostile = tmp_path / "hostile"
    hostile.write_text("ignored\n", encoding="utf-8")

    subprocess.run(
        [str(launcher)],
        env=os.environ
        | {
            "BASH_ENV": str(hostile),
            "ENV": str(hostile),
            "BASHOPTS": "extglob",
            "SHELLOPTS": "braceexpand",
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
            "HIMALAYA_CONFIG": str(tmp_path / "hostile-himalaya.toml"),
            "HIMALAYA_TEST_MODE": "1",
            "HERMES_HOME": str(tmp_path / "hostile-hermes-home"),
            "HERMES_PROFILE": "hostile-profile",
            "HERMES_MODEL": "hostile-model",
            "HERMES_PROVIDER": "hostile-provider",
            "HERMES_PLUGIN_PATH": str(tmp_path / "hostile-plugins"),
            "HERMES_TEST_MODE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    child_keys = {
        line.partition("=")[0]
        for line in captured.read_text(encoding="utf-8").splitlines()
    }
    assert (
        not {
            "BASH_ENV",
            "ENV",
            "BASHOPTS",
            "SHELLOPTS",
            "PYTHONPYCACHEPREFIX",
            "HIMALAYA_CONFIG",
            "HIMALAYA_TEST_MODE",
            "HERMES_HOME",
            "HERMES_PROFILE",
            "HERMES_MODEL",
            "HERMES_PROVIDER",
            "HERMES_PLUGIN_PATH",
            "HERMES_TEST_MODE",
        }
        & child_keys
    )
    if name == "email_memory_store_mcp_launcher.sh":
        assert captured_args.read_text(encoding="utf-8") == "\n"
        child_environment = captured.read_text(encoding="utf-8")
        assert (
            f"EMAIL_MEMORY_STORE_RUNTIME_CONFIG={runtime}\n" in child_environment
        )
        assert str(runtime) not in captured_args.read_text(encoding="utf-8")


def test_all_public_deployment_shell_entrypoints_use_privileged_bash() -> None:
    entrypoints = [
        ROOT / "scripts/deploy.sh",
        ROOT / "scripts/provision_email_memory_environment.sh",
        ROOT / "scripts/bootstrap.sh",
        *sorted((DEPLOYMENT / "scripts").glob("*.sh")),
    ]

    for entrypoint in entrypoints:
        assert (
            entrypoint.read_text(encoding="utf-8").splitlines()[0] == "#!/bin/bash -p"
        )


def test_deploy_rejects_untrusted_checkout_ancestry(tmp_path: Path) -> None:
    untrusted_checkout = tmp_path / "untrusted"
    scripts = untrusted_checkout / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/deploy.sh", scripts / "deploy.sh")
    (scripts / "deploy.sh").chmod(0o700)
    untrusted_checkout.chmod(0o777)

    completed = subprocess.run(
        [str(scripts / "deploy.sh"), "--invalid"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "public checkout ancestry is not trusted" in completed.stderr


def test_deploy_help_requires_a_clean_public_git_checkout() -> None:
    completed = subprocess.run(
        [str(ROOT / "scripts/deploy.sh"), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "clean public Git checkout" in completed.stdout


def test_standalone_bootstrap_rejects_relative_uv_and_hostile_path(
    tmp_path: Path,
) -> None:
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    marker = tmp_path / "hostile-tool-ran"
    for name in ("uv", "uname", "dirname", "git"):
        executable = hostile / name
        executable.write_text(
            f"#!/bin/sh\n/usr/bin/touch '{marker}'\nexit 0\n", encoding="utf-8"
        )
        executable.chmod(0o700)

    completed = subprocess.run(
        [str(ROOT / "scripts/bootstrap.sh"), "--environment", str(tmp_path / "venv")],
        env=os.environ | {"PATH": str(hostile), "UV_BIN": "uv"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "trusted absolute uv executable" in completed.stderr
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable"])
def test_standalone_bootstrap_rejects_untrusted_uv_file(
    tmp_path: Path, kind: str
) -> None:
    marker = tmp_path / "uv-ran"
    real_uv = tmp_path / "real-uv"
    real_uv.write_text(
        f"#!/bin/sh\n/usr/bin/touch '{marker}'\nexit 0\n", encoding="utf-8"
    )
    real_uv.chmod(0o700)
    candidate = tmp_path / "candidate-uv"
    if kind == "symlink":
        candidate.symlink_to(real_uv)
    elif kind == "hardlink":
        os.link(real_uv, candidate)
    else:
        candidate = real_uv
        candidate.chmod(0o722)

    completed = subprocess.run(
        [str(ROOT / "scripts/bootstrap.sh"), "--environment", str(tmp_path / "venv")],
        env=os.environ | {"UV_BIN": str(candidate)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "trusted absolute uv executable" in completed.stderr
    assert not marker.exists()


def test_standalone_bootstrap_scrubs_hostile_uv_python_git_and_shell_config(
    tmp_path: Path,
) -> None:
    captured = tmp_path / "uv-environment"
    marker = tmp_path / "bash-env-ran"
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(f"/usr/bin/touch '{marker}'\n", encoding="utf-8")
    trusted_uv = tmp_path / "trusted-uv"
    trusted_uv.write_text(
        f"#!/bin/sh\n/usr/bin/env >'{captured}'\nexit 99\n", encoding="utf-8"
    )
    trusted_uv.chmod(0o700)

    completed = subprocess.run(
        [str(ROOT / "scripts/bootstrap.sh"), "--environment", str(tmp_path / "venv")],
        env=os.environ
        | {
            "UV_BIN": str(trusted_uv),
            "UV_CONFIG_FILE": str(tmp_path / "uv.toml"),
            "UV_PYTHON_INSTALL_DIR": str(tmp_path / "redirected-python"),
            "UV_PROJECT_ENVIRONMENT": str(tmp_path / "redirected"),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
            "GIT_DIR": str(tmp_path / "git-dir"),
            "BASH_ENV": str(bash_env),
            "ENV": str(tmp_path / "env"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert not marker.exists()
    child_env = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in captured.read_text(encoding="utf-8").splitlines()
    }
    assert child_env["UV_NO_CONFIG"] == "1"
    assert child_env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "venv")
    assert child_env["PYTHONNOUSERSITE"] == "1"
    assert child_env["GIT_CONFIG_NOSYSTEM"] == "1"
    hostile_keys = {
        "UV_CONFIG_FILE",
        "UV_PYTHON_INSTALL_DIR",
        "PYTHONPYCACHEPREFIX",
        "GIT_DIR",
        "BASH_ENV",
        "ENV",
        "BASHOPTS",
        "SHELLOPTS",
    } & child_env.keys()
    assert not hostile_keys, hostile_keys


def test_standalone_bootstrap_sets_validated_release_local_python_controls(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    environment = release / "venv"
    python_install_dir = release / "python"
    captured = tmp_path / "uv-environment"
    trusted_uv = tmp_path / "trusted-uv"
    trusted_uv.write_text(
        f"#!/bin/sh\n/usr/bin/env >'{captured}'\nexit 99\n", encoding="utf-8"
    )
    trusted_uv.chmod(0o700)

    completed = subprocess.run(
        [
            str(ROOT / "scripts/bootstrap.sh"),
            "--environment",
            str(environment),
            "--managed-python-install-dir",
            str(python_install_dir),
        ],
        env=os.environ
        | {
            "UV_BIN": str(trusted_uv),
            "UV_MANAGED_PYTHON": "0",
            "UV_PYTHON_INSTALL_DIR": str(tmp_path / "hostile-python"),
            "UV_LINK_MODE": "hardlink",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    child_env = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in captured.read_text(encoding="utf-8").splitlines()
    }
    assert child_env["UV_MANAGED_PYTHON"] == "1"
    assert child_env["UV_PYTHON_INSTALL_DIR"] == str(python_install_dir)
    assert child_env["UV_LINK_MODE"] == "copy"


def test_deployment_tree_has_no_service_or_gateway_lifecycle_commands() -> None:
    sources = [
        DEPLOYMENT / "cli.py",
        ROOT / "scripts/deploy.sh",
        ROOT / "scripts/provision_email_memory_environment.sh",
        *sorted((DEPLOYMENT / "scripts").rglob("*.sh")),
    ]
    forbidden = re.compile(
        r"(?m)(?:^|[;&|]\s*)(?:/[^\s]+/)?"
        r"(?:systemctl|service|pkill|killall|kill)\b|"
        r"\bhermes\s+(?:gateway\s+)?(?:start|stop|restart|reload)\b|"
        r"\bgateway\s+(?:start|stop|restart|reload)\b",
        re.IGNORECASE,
    )
    for path in sources:
        assert forbidden.search(path.read_text(encoding="utf-8")) is None, path


def test_deployment_shell_uses_only_hermes_chat_and_send_subcommands() -> None:
    invocation = re.compile(r"""["']?\$(?:\{)?HERMES(?:\})?["']?\s+([a-z-]+)""")
    observed: set[str] = set()
    for path in sorted((DEPLOYMENT / "scripts").rglob("*.sh")):
        observed.update(invocation.findall(path.read_text(encoding="utf-8")))

    assert observed == {"chat", "send"}


def test_nightly_launcher_is_the_only_maintenance_lock_owner() -> None:
    scripts = sorted((DEPLOYMENT / "scripts").rglob("*.sh"))
    owners = {
        path.name
        for path in scripts
        if "nightly_maintenance.lock" in path.read_text(encoding="utf-8")
    }

    assert owners == {"nightly_cron_launcher.sh"}
