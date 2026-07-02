"""Tests for the Cadence/Spectre csh environment prelude (Lmod + cshrc)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from virtuoso_bridge.spectre.runner import (
    DEFAULT_LMOD_INIT_CSH,
    cadence_env_import_bash,
    cadence_env_setup_csh,
    remote_tool_probe,
)

_ENV_VARS = (
    "VB_LMOD_MODULES",
    "VB_LMOD_INIT",
    "VB_CADENCE_CSHRC",
    "VB_MENTOR_CSHRC",
    "VB_LMOD_MODULES_worker1",
    "VB_CADENCE_CSHRC_worker1",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_empty_when_nothing_configured():
    assert cadence_env_setup_csh() == ""


def test_lmod_modules_use_default_init(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "cadence/IC25.1 spectre/23.1")
    out = cadence_env_setup_csh()
    assert f"source {DEFAULT_LMOD_INIT_CSH}" in out
    # Load via the Lmod backend ($LMOD_CMD), not the parse-time `module` alias.
    assert "eval `$LMOD_CMD csh load cadence/IC25.1 spectre/23.1`" in out
    assert "module load" not in out
    # Guarded source so a wrong path is harmless when Lmod is already defined.
    assert out.startswith(f"if ( -f {DEFAULT_LMOD_INIT_CSH} )")
    assert "if ( $?LMOD_CMD )" in out


def test_lmod_modules_accept_comma_separator(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "cadence/IC25.1, spectre/23.1")
    assert "csh load cadence/IC25.1 spectre/23.1`" in cadence_env_setup_csh()


def test_lmod_init_override(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "spectre/23.1")
    monkeypatch.setenv("VB_LMOD_INIT", "/opt/lmod/init/csh")
    out = cadence_env_setup_csh()
    assert "/opt/lmod/init/csh" in out
    assert DEFAULT_LMOD_INIT_CSH not in out


def test_modules_then_cshrc_layering(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "spectre/23.1")
    monkeypatch.setenv("VB_CADENCE_CSHRC", "/home/x/cad.cshrc")
    monkeypatch.setenv("VB_MENTOR_CSHRC", "/home/x/mentor.cshrc")
    out = cadence_env_setup_csh()
    # Modules load first, then cshrc files source (later layers win).
    assert out.index("csh load") < out.index("source /home/x/cad.cshrc")
    assert out.index("/home/x/cad.cshrc") < out.index("/home/x/mentor.cshrc")


def test_cshrc_only_without_modules(monkeypatch):
    monkeypatch.setenv("VB_CADENCE_CSHRC", "/home/x/cad.cshrc")
    out = cadence_env_setup_csh()
    assert out == "source /home/x/cad.cshrc"
    assert "module load" not in out


def test_profile_suffix_overrides_with_fallback(monkeypatch):
    # Suffixed modules win; unsuffixed cshrc still falls through.
    monkeypatch.setenv("VB_LMOD_MODULES", "spectre/base")
    monkeypatch.setenv("VB_LMOD_MODULES_worker1", "spectre/24")
    monkeypatch.setenv("VB_CADENCE_CSHRC", "/home/x/cad.cshrc")
    out = cadence_env_setup_csh("_worker1")
    assert "csh load spectre/24`" in out
    assert "spectre/base" not in out
    assert "source /home/x/cad.cshrc" in out


def test_paths_with_spaces_are_quoted(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "spectre/23.1")
    monkeypatch.setenv("VB_LMOD_INIT", "/opt/my lmod/init/csh")
    out = cadence_env_setup_csh()
    assert "'/opt/my lmod/init/csh'" in out


def test_stray_quote_in_modules_does_not_raise(monkeypatch):
    # A whitespace split (not shlex) must tolerate an unbalanced quote in the
    # user-supplied value without raising ValueError.
    monkeypatch.setenv("VB_LMOD_MODULES", "spectre/23.1 bad'module")
    out = cadence_env_setup_csh()  # must not raise
    assert "csh load" in out
    assert "spectre/23.1" in out


def test_not_initialized_warning_present_when_modules_set(monkeypatch):
    monkeypatch.setenv("VB_LMOD_MODULES", "spectre/23.1")
    out = cadence_env_setup_csh()
    # A guarded warning surfaces (on stdout, merged with 2>&1) when Lmod never
    # gets initialized, instead of silently no-op'ing the module load.
    assert "if ( ! $?LMOD_CMD )" in out
    assert "Lmod is not initialized" in out
    # The warning prefix can't be mistaken for any marker/env parser line.
    assert "virtuoso-bridge:" in out


def test_no_warning_when_no_modules(monkeypatch):
    monkeypatch.setenv("VB_CADENCE_CSHRC", "/home/x/cad.cshrc")
    out = cadence_env_setup_csh()
    assert "Lmod is not initialized" not in out


def test_env_import_bash_empty_when_no_setup():
    assert cadence_env_import_bash("", "^CDSHOME=") == ""


def test_env_import_bash_single_quotes_exports():
    frag = cadence_env_import_bash("source /x/cad.cshrc", "^CDSHOME=")
    # Runs the csh prelude's env through grep/sed and eval's single-quoted
    # exports (values with spaces/$ survive verbatim).
    assert "csh -f -c" in frag
    assert "grep -E" in frag
    assert "'^CDSHOME='" in frag
    assert frag.rstrip().endswith(";")
    # The sed program emits `export NAME='VALUE'` (single-quoted the value),
    # not the old unquoted `s/^/export /`.  The literal single quotes are
    # shell-escaped by shlex.quote, so the backref rewrite appears as
    # `export \1=` (the round-trip test below proves the quoting is correct).
    assert "export \\1=" in frag
    assert "s/^/export /" not in frag


def test_env_import_bash_value_round_trips_through_sed_and_bash():
    """F5: the generated sed program must single-quote imported values so a
    value with spaces and a ``$`` round-trips exactly through real sed+bash."""
    if not (shutil.which("bash") and shutil.which("sed")):
        pytest.skip("bash/sed not available")

    frag = cadence_env_import_bash("source /x/cad.cshrc", "^CDSHOME=")
    # Extract just the `sed '<program>'` portion and drive it directly, feeding
    # a synthetic env line with spaces, a `$`, and backticks.
    import re
    m = re.search(r"\| sed (?P<sed>'.*')\)", frag)
    assert m, frag
    sed_arg = m.group("sed")

    env_line = "CDSHOME=/opt/my tools/$weird `x`"
    import shlex
    script = (
        "eval \"$(printf %s " + shlex.quote(env_line)
        + " | grep -E '^CDSHOME=' | sed " + sed_arg + ")\"; "
        'printf "%s" "$CDSHOME"'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "/opt/my tools/$weird `x`"


def test_remote_tool_probe_composes_fast_then_slow_hermetic_csh():
    cmd = remote_tool_probe("which spectre", "setup; which spectre")
    # Fused single round-trip: bash fast path OR csh fallback.
    assert cmd.startswith("bash -l -c ")
    assert "which spectre" in cmd
    # F1: the csh fallback must be hermetic `csh -f -c` (~/.cshrc holds no
    # tool/project variables in the Lmod use case; env comes only from the
    # per-project module/cshrc config, same as real runs); stderr is merged.
    assert "csh -f -c" in cmd
    assert "2>&1" in cmd
