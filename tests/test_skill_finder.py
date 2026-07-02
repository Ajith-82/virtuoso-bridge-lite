from __future__ import annotations

import json
import subprocess
from pathlib import Path

import virtuoso_bridge
from virtuoso_bridge.cli import main
from virtuoso_bridge.virtuoso.basic.bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.skill_finder import (
    SKILLFinder,
    _build_doc_finder_walk_script,
    _parse_virtuoso_path,
)


class _FakeSkillClient:
    def __init__(self) -> None:
        self.find_calls: list[tuple[str, str, int, bool]] = []

    def find_skill(self, query: str, *, mode: str = "fuzzy", limit: int = 50, include_desc: bool = False):
        self.find_calls.append((query, mode, limit, include_desc))
        return [
            {
                "name": "dbOpenCellViewByType",
                "syntax": "dbOpenCellViewByType(lib cell view)",
                "description": "Open a cellview.",
                "source_file": "database.fnd",
            }
        ]

    def get_skill_more_info(self, func_name: str):
        return {
            "func_name": func_name,
            "file_path": "$database/db.html",
            "topic": func_name,
            "raw_html": "<h1>dbOpenCellViewByType</h1>",
            "plain_text": "# dbOpenCellViewByType",
        }


def _patch_cli_client(monkeypatch):
    fake = _FakeSkillClient()
    seen_profiles: list[str | None] = []

    class _FakeVirtuosoClient:
        @classmethod
        def from_env(cls, profile=None):
            seen_profiles.append(profile)
            return fake

    monkeypatch.setattr(virtuoso_bridge, "VirtuosoClient", _FakeVirtuosoClient)
    monkeypatch.setattr("virtuoso_bridge.cli._load_cli_env", lambda: None)
    monkeypatch.setattr("virtuoso_bridge.profile.resolve_profile", lambda explicit=None: explicit)
    return fake, seen_profiles


def test_skill_find_json_flag_emits_json(capsys, monkeypatch):
    fake, seen_profiles = _patch_cli_client(monkeypatch)

    rc = main(["skill-find", "dbOpen", "--json", "--mode", "prefix", "--limit", "3"])

    assert rc == 0
    assert fake.find_calls == [("dbOpen", "prefix", 3, False)]
    assert seen_profiles == [None]
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["name"] == "dbOpenCellViewByType"


def test_skill_find_passes_explicit_profile(capsys, monkeypatch):
    _fake, seen_profiles = _patch_cli_client(monkeypatch)

    rc = main(["skill-find", "dbOpen", "-p", "worker1", "--json"])

    assert rc == 0
    assert seen_profiles == ["worker1"]
    assert json.loads(capsys.readouterr().out)[0]["source_file"] == "database.fnd"


def test_skill_find_passes_include_desc_with_explicit_profile(capsys, monkeypatch):
    fake, seen_profiles = _patch_cli_client(monkeypatch)

    rc = main(["skill-find", "open.*cellview", "--mode", "regex", "-p", "worker1", "--json", "--include-desc"])

    assert rc == 0
    assert seen_profiles == ["worker1"]
    assert json.loads(capsys.readouterr().out)[0]["source_file"] == "database.fnd"


def test_skill_info_passes_explicit_profile(capsys, monkeypatch):
    _fake, seen_profiles = _patch_cli_client(monkeypatch)

    rc = main(["skill-info", "dbOpenCellViewByType", "-p", "worker1", "--json"])

    assert rc == 0
    assert seen_profiles == ["worker1"]
    assert json.loads(capsys.readouterr().out)["func_name"] == "dbOpenCellViewByType"


class _LocalTunnel:
    _ssh_runner = None
    _remote_host = "localhost"


def _write_finder_tree(tmp_path):
    doc_root = tmp_path / "ic" / "doc"
    skill_root = doc_root / "finder" / "SKILL" / "database"
    skill_root.mkdir(parents=True)
    (skill_root / "database.fnd").write_text(
        '("dbOpenCellViewByType"\n'
        '"dbOpenCellViewByType(lib cell view)"\n'
        '"Open a cellview.")\n',
        encoding="utf-8",
    )

    more_info_dir = doc_root / "api_more_info"
    more_info_dir.mkdir()
    (more_info_dir / "api_more_info.tgf").write_text(
        "dbOpenCellViewByType $database/db.html NULL HTML\n",
        encoding="utf-8",
    )
    html_dir = doc_root / "database"
    html_dir.mkdir()
    (html_dir / "db.html").write_text(
        "<html><body><h1>dbOpenCellViewByType</h1><p>Open a cellview.</p></body></html>",
        encoding="utf-8",
    )
    return skill_root.parent


def test_find_skill_uses_local_discovery_when_tunnel_has_no_ssh_runner(monkeypatch, tmp_path):
    skill_root = _write_finder_tree(tmp_path)

    def fake_discover(self, remote_runner=None, profile=None):
        assert remote_runner is None
        return skill_root

    monkeypatch.setattr(SKILLFinder, "discover", fake_discover)

    client = VirtuosoClient(tunnel=_LocalTunnel())
    results = client.find_skill("dbOpenCellViewByType", mode="exact")

    assert results == [
        {
            "name": "dbOpenCellViewByType",
            "syntax": "dbOpenCellViewByType(lib cell view)",
            "description": "Open a cellview.",
            "source_file": "database.fnd",
        }
    ]


def test_skill_more_info_uses_local_discovery_when_tunnel_has_no_ssh_runner(monkeypatch, tmp_path):
    skill_root = _write_finder_tree(tmp_path)

    def fake_discover(self, remote_runner=None, profile=None):
        assert remote_runner is None
        return skill_root

    monkeypatch.setattr(SKILLFinder, "discover", fake_discover)

    client = VirtuosoClient(tunnel=_LocalTunnel())
    result = client.get_skill_more_info("dbOpenCellViewByType", cache_dir=tmp_path / "cache")

    assert result is not None
    assert result["func_name"] == "dbOpenCellViewByType"
    assert "Open a cellview." in result["plain_text"]


# ---------------------------------------------------------------------------
# _discover_remote: virtuoso path parsing (F2, F6) and probe composition (F9)
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeRemoteRunner:
    """Records commands and returns queued canned results in order."""

    def __init__(self, results):
        self.commands: list[str] = []
        self._results = list(results)

    def run_command(self, command, timeout=None):
        self.commands.append(command)
        return self._results.pop(0)


def _clear_cadence_env(monkeypatch):
    for var in ("VB_LMOD_MODULES", "VB_CADENCE_CSHRC", "VB_MENTOR_CSHRC"):
        monkeypatch.delenv(var, raising=False)


# -- _parse_virtuoso_path ----------------------------------------------------


def test_parse_virtuoso_path_accepts_absolute_path():
    stdout = "/tools/cadence/IC618/tools/bin/virtuoso\n"
    assert _parse_virtuoso_path(stdout) == "/tools/cadence/IC618/tools/bin/virtuoso"


def test_parse_virtuoso_path_extracts_absolute_path_from_csh_alias():
    stdout = "virtuoso: aliased to /tools/cadence/IC618/tools/bin/virtuoso -leaf\n"
    assert _parse_virtuoso_path(stdout) == "/tools/cadence/IC618/tools/bin/virtuoso"


def test_parse_virtuoso_path_ignores_alias_with_no_absolute_path():
    # e.g. a shell-function-backed alias with no real path in the definition.
    stdout = "virtuoso: aliased to runVirtuosoWrapper\n"
    assert _parse_virtuoso_path(stdout) == ""


def test_parse_virtuoso_path_rejects_bare_token():
    assert _parse_virtuoso_path("virtuoso\n") == ""


def test_parse_virtuoso_path_rejects_relative_path():
    assert _parse_virtuoso_path("bin/virtuoso\n") == ""
    assert _parse_virtuoso_path("tools/bin/virtuoso\n") == ""


def test_parse_virtuoso_path_no_match_returns_empty():
    assert _parse_virtuoso_path("command not found\n") == ""


# -- _discover_remote integration -------------------------------------------


def test_discover_remote_accepts_csh_alias_and_walks_up(monkeypatch):
    _clear_cadence_env(monkeypatch)
    runner = _FakeRemoteRunner([
        _Result(stdout="virtuoso: aliased to /tools/IC618/tools/bin/virtuoso -leaf\n", returncode=0),
        _Result(stdout="/tools/IC618/doc/finder/SKILL\n", returncode=0),
    ])

    finder = SKILLFinder()
    result = finder._discover_remote(runner, None)

    assert result == Path("/tools/IC618/doc/finder/SKILL")
    # The walk script must be seeded with the extracted absolute path.
    assert "/tools/IC618/tools/bin/virtuoso" in runner.commands[1]


def test_discover_remote_returns_none_for_alias_with_no_absolute_path(monkeypatch):
    _clear_cadence_env(monkeypatch)
    runner = _FakeRemoteRunner([
        _Result(stdout="virtuoso: aliased to runVirtuosoWrapper\n", returncode=0),
    ])

    finder = SKILLFinder()
    result = finder._discover_remote(runner, None)

    assert result is None
    # No garbage path should ever reach the walk-script stage.
    assert len(runner.commands) == 1


def test_discover_remote_returns_none_for_bare_virtuoso_token(monkeypatch):
    _clear_cadence_env(monkeypatch)
    runner = _FakeRemoteRunner([_Result(stdout="virtuoso\n", returncode=0)])

    finder = SKILLFinder()
    result = finder._discover_remote(runner, None)

    assert result is None
    assert len(runner.commands) == 1


def test_discover_remote_returns_none_for_relative_path(monkeypatch):
    _clear_cadence_env(monkeypatch)
    runner = _FakeRemoteRunner([_Result(stdout="bin/virtuoso\n", returncode=0)])

    finder = SKILLFinder()
    result = finder._discover_remote(runner, None)

    assert result is None
    assert len(runner.commands) == 1


def test_discover_remote_uses_shared_probe_composition(monkeypatch):
    # A configured cshrc makes cadence_env_setup_csh() non-empty, which
    # routes _discover_remote through remote_tool_probe (F9).
    _clear_cadence_env(monkeypatch)
    monkeypatch.setenv("VB_CADENCE_CSHRC", "/opt/cadence/cshrc.csh")
    runner = _FakeRemoteRunner([
        _Result(stdout="/tools/bin/virtuoso\n", returncode=0),
        _Result(stdout="", returncode=1),
    ])

    finder = SKILLFinder()
    finder._discover_remote(runner, None)

    probe_cmd = runner.commands[0]
    assert probe_cmd.startswith("bash -l -c ")
    assert "which virtuoso" in probe_cmd
    # F1/F9: the csh fallback must be hermetic `csh -f -c` — ~/.cshrc holds
    # no tool/project variables in the Lmod use case; env comes only from
    # the per-project module/cshrc config, identically to real runs.
    assert "csh -f -c" in probe_cmd


def test_discover_remote_skips_probe_composition_when_no_cadence_env_configured(monkeypatch):
    _clear_cadence_env(monkeypatch)
    runner = _FakeRemoteRunner([_Result(stdout="", returncode=1)])

    finder = SKILLFinder()
    result = finder._discover_remote(runner, None)

    assert result is None
    probe_cmd = runner.commands[0]
    assert probe_cmd.startswith("bash -l -c ")
    assert "csh" not in probe_cmd


# -- walk-script termination hardening (F6) ----------------------------------


def test_walk_script_contains_termination_guard():
    script = _build_doc_finder_walk_script("/tools/bin/virtuoso")
    assert 'parent=$(dirname "$p")' in script
    assert '"$parent" = "$p"' in script


def test_discover_remote_walk_script_command_has_termination_guard(monkeypatch):
    _clear_cadence_env(monkeypatch)
    runner = _FakeRemoteRunner([
        _Result(stdout="/tools/bin/virtuoso\n", returncode=0),
        _Result(stdout="", returncode=1),
    ])

    finder = SKILLFinder()
    finder._discover_remote(runner, None)

    walk_cmd = runner.commands[1]
    assert 'parent=$(dirname "$p")' in walk_cmd
    assert '"$parent" = "$p"' in walk_cmd


def test_walk_script_terminates_quickly_for_relative_path():
    """Defense in depth: even if fed a relative/degenerate path, the loop
    itself must not spin forever (dirname("bin/virtuoso") -> "." -> "."...).
    """
    script = _build_doc_finder_walk_script("bin/virtuoso")

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_walk_script_terminates_quickly_for_dot_path():
    """Same guard exercised directly with the degenerate "." path that a
    bare relative single-component input dirnames down to."""
    script = _build_doc_finder_walk_script(".")

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 1
