import subprocess
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[1] / "src" / "skt" / "cli.py"

PENDING_COMMANDS = []  # all subcommands implemented as of SKT-5


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True
    )


def test_entry_point_importable():
    sys.path.insert(0, str(CLI.parents[1]))
    from skt.cli import main  # noqa: F401


def test_help_runs_clean():
    result = run("--help")
    assert result.returncode == 0
    assert "skt" in result.stdout


def test_version():
    result = run("--version")
    assert result.returncode == 0
    from skt import __version__ as pkg_version
    assert result.stdout.strip() == f"skt {pkg_version}"


def test_the_two_version_literals_agree():
    """`skt.cli` must keep its own copy, so something has to check it.

    cli.py is stdlib-only by contract -- the skill-script installer runs
    it with the system python3 and no venv -- so it cannot import the
    package to learn the version, and the duplicate literal is deliberate.
    What is NOT acceptable is the duplicate drifting silently: the 0.7.0
    bump moved three manifests and `skt/__init__.py`, left this one at
    0.6.0, and every test still passed because the only assertion read
    the stale copy.
    """
    from skt import __version__ as pkg_version
    from skt.cli import __version__ as cli_version
    assert cli_version == pkg_version, (
        f"skt.cli.__version__ is {cli_version} but the package says "
        f"{pkg_version} — bump both, they cannot import each other"
    )


def test_no_args_prints_help():
    result = run()
    assert result.returncode == 0
    assert "startup report" in result.stdout


@pytest.mark.parametrize("command", PENDING_COMMANDS)
def test_pending_commands_are_honest_stubs(command):
    result = run(command)
    assert result.returncode == 2
    assert "not implemented yet" in result.stderr
    assert "SKT-" in result.stderr


@pytest.mark.parametrize("command", PENDING_COMMANDS)
def test_pending_commands_have_help(command):
    result = run(command, "--help")
    assert result.returncode == 0
