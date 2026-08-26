"""Tests that verify the claims made in the agent-backup README against the
actual skill code.

Run:  python -m pytest tests/ -q        (from the repo root)

The skill under test is loaded directly from skills/agent-backup/agent-backup.py.
Each test builds a synthetic agent home in a temp dir and runs the real
`init`/`sync` commands against it, then asserts the README's claims.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = REPO_ROOT / "skills" / "agent-backup" / "agent-backup.py"

_spec = importlib.util.spec_from_file_location("agent_backup", TOOL_PATH)
agent_backup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_backup)

SESSION_LINES = "\n".join([
    json.dumps({
        "timestamp": "2026-08-01T10:00:00.000Z", "model": "test-model", "provider": "x",
        "usage": {"input": 100, "output": 50, "cacheRead": 0, "cacheWrite": 0,
                  "totalTokens": 150, "reasoningTokens": 0,
                  "cost": {"input": 0.001, "output": 0.002, "cacheRead": 0,
                           "cacheWrite": 0, "total": 0.003}},
    }),
    json.dumps({
        "timestamp": "2026-08-01T11:00:00.000Z", "model": "test-model", "provider": "x",
        "usage": {"input": 200, "output": 100, "cacheRead": 10, "cacheWrite": 0,
                  "totalTokens": 310, "reasoningTokens": 5,
                  "cost": {"input": 0.002, "output": 0.004, "cacheRead": 0.001,
                           "cacheWrite": 0, "total": 0.007}},
    }),
])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("BACKUP_OBJECT_STORE", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
              "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(v, raising=False)


@pytest.fixture
def agent_home(tmp_path):
    """A synthetic agent home with config, memory, sessions, secrets, runtime."""
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "settings.json").write_text('{"model": "test"}')
    (home / "skills" / "my-skill").mkdir(parents=True)
    (home / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: test skill\n---\nbody\n")
    (home / "memory").mkdir()
    (home / "memory" / "MEMORY.md").write_text("memory content")
    (home / "sessions").mkdir()
    (home / "sessions" / "s1.jsonl").write_text(SESSION_LINES)
    # secrets (must never be backed up)
    (home / "auth.json").write_text('{"token": "sekrit"}')
    (home / ".env").write_text("SECRET=1\n")
    # runtime (must never be backed up)
    (home / "node_modules" / "pkg").mkdir(parents=True)
    (home / "node_modules" / "pkg" / "index.js").write_text("x")
    (home / "cache").mkdir()
    (home / "cache" / "x.tmp").write_text("x")
    (home / "agent.db").write_bytes(b"\x00\x01")
    (home / "logs").mkdir()
    (home / "logs" / "app.log").write_text("log")
    return home


@pytest.fixture
def profile_dir(tmp_path, agent_home):
    d = tmp_path / "profiles"
    d.mkdir()
    (d / "testagent.json").write_text(json.dumps({
        "name": "testagent",
        "home": str(agent_home),
        "config": ["config/settings.json", "skills/"],
        "memory": ["memory/"],
        "sessions": ["sessions/"],
        "usage": {"format": "jsonl", "has_cost": True},
        "secrets": ["auth.json", ".env"],
        "runtime": ["node_modules", "cache/", "agent.db", "logs/"],
    }))
    return d


def _home_files(home):
    return {str(p.relative_to(home)) for p in home.rglob("*") if p.is_file()}


def run_init(profile_dir, repo_root, remote=None, name="testagent"):
    agent_backup.PROFILES_DIR = profile_dir
    agent_backup.cmd_init(argparse.Namespace(name=name, dir=repo_root, remote=remote))
    return repo_root / f"{name}-backup"


def run_sync(profile_dir, repo_root, name="testagent"):
    agent_backup.PROFILES_DIR = profile_dir
    agent_backup.cmd_sync(argparse.Namespace(name=name, dir=repo_root))


# --- README: "backs up a harness's config and memories ... to a git repo" ---

def test_config_and_memory_copied_to_repo(agent_home, profile_dir, tmp_path):
    repo = run_init(profile_dir, tmp_path / "repos")
    run_sync(profile_dir, tmp_path / "repos")
    assert (repo / "config" / "config" / "settings.json").exists()
    assert (repo / "config" / "skills" / "my-skill" / "SKILL.md").exists()
    assert (repo / "memory" / "memory" / "MEMORY.md").exists()


# --- README: "always excludes secrets (auth.json, .env)" ---

def test_secrets_excluded(agent_home, profile_dir, tmp_path):
    run_init(profile_dir, tmp_path / "repos")
    run_sync(profile_dir, tmp_path / "repos")
    repo = tmp_path / "repos" / "testagent-backup"
    leaked = [p for p in repo.rglob("auth.json")] + [p for p in repo.rglob(".env")]
    assert leaked == [], f"secrets leaked into backup: {leaked}"


# --- README: "always excludes ... runtime state (node_modules, caches, logs, DBs)" ---

def test_runtime_excluded(agent_home, profile_dir, tmp_path):
    run_init(profile_dir, tmp_path / "repos")
    run_sync(profile_dir, tmp_path / "repos")
    repo = tmp_path / "repos" / "testagent-backup"
    names = {str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_file()}
    assert not any("node_modules" in n or "cache" in n or ".db" in n or ".log" in n
                   for n in names), f"runtime leaked into backup: {names}"


# --- README: "never touches the harness's own directories" ---

def test_harness_home_untouched(agent_home, profile_dir, tmp_path):
    before = _home_files(agent_home)
    run_init(profile_dir, tmp_path / "repos")
    run_sync(profile_dir, tmp_path / "repos")
    after = _home_files(agent_home)
    assert before == after, "sync modified the harness home directory"


# --- README: "compressed usage/cost ledger", "query with DuckDB per-turn/per-day" ---

def test_usage_ledger_created_and_queryable(agent_home, profile_dir, tmp_path):
    run_init(profile_dir, tmp_path / "repos")
    run_sync(profile_dir, tmp_path / "repos")
    repo = tmp_path / "repos" / "testagent-backup"
    usage = repo / "usage" / "usage.parquet"
    daily = repo / "usage" / "daily.parquet"
    assert usage.exists() and daily.exists()

    turns = duckdb.sql(f"SELECT * FROM read_parquet('{usage.as_posix()}')").df()
    assert len(turns) == 2                       # per-turn rows
    assert int(turns["total_tokens"].sum()) == 460
    assert abs(turns["cost_total"].sum() - 0.010) < 1e-9

    by_day = duckdb.sql(f"SELECT * FROM read_parquet('{daily.as_posix()}')").df()
    assert len(by_day) == 1                      # one day
    assert by_day["date"].iloc[0] == "2026-08-01"
    assert by_day["model"].iloc[0] == "test-model"
    assert int(by_day["events"].iloc[0]) == 2
    assert int(by_day["total_tokens"].iloc[0]) == 460
    assert abs(by_day["cost_total"].iloc[0] - 0.010) < 1e-9


# --- README: "backs up ... to a private git repo" (a commit is created) ---

def test_git_commit_created(agent_home, profile_dir, tmp_path):
    repo = run_init(profile_dir, tmp_path / "repos")
    run_sync(profile_dir, tmp_path / "repos")
    res = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True)
    assert res.returncode == 0 and int(res.stdout.strip()) >= 1


# --- README: "private git repo" (first sync pushes a new branch to a remote) ---

def test_first_sync_pushes_to_remote(agent_home, profile_dir, tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    run_init(profile_dir, tmp_path / "repos", remote=str(bare))
    run_sync(profile_dir, tmp_path / "repos")
    res = subprocess.run(["git", "--git-dir", str(bare), "log", "main", "--oneline", "-1"],
                         capture_output=True, text=True)
    assert res.returncode == 0 and "backup" in res.stdout.lower()


# --- README: sync is idempotent (a re-run makes no empty commit) ---

def test_rerun_is_idempotent(agent_home, profile_dir, tmp_path):
    repo = run_init(profile_dir, tmp_path / "repos")
    run_sync(profile_dir, tmp_path / "repos")
    before = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    run_sync(profile_dir, tmp_path / "repos")
    after = subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    assert before == after, "re-running sync created a new commit"


# --- README: ".env, which is gitignored" + object store is opt-in ---

def test_init_gitignore_excludes_secrets_and_env(agent_home, profile_dir, tmp_path):
    repo = run_init(profile_dir, tmp_path / "repos")
    gi = (repo / ".gitignore").read_text()
    for needle in (".env", "auth.json", "node_modules", "agent.db"):
        assert needle in gi, f".gitignore missing '{needle}':\n{gi}"


def test_object_store_opt_in_skips_without_creds(agent_home, profile_dir, tmp_path,
                                                 capsys):
    run_init(profile_dir, tmp_path / "repos")
    run_sync(profile_dir, tmp_path / "repos")   # must not raise / must not upload
    out = capsys.readouterr().out
    assert "skipping" in out.lower() or "not configured" in out.lower()


# --- README: "Adding a new agent" (data-driven profile, no code change) ---

def test_custom_profile_adds_new_agent(tmp_path):
    home = tmp_path / "mini"
    (home / "conf").mkdir(parents=True)
    (home / "conf" / "settings.json").write_text("{}")
    prof = tmp_path / "profiles"
    prof.mkdir()
    (prof / "custom-agent.json").write_text(json.dumps({
        "name": "custom-agent", "home": str(home),
        "config": ["conf/settings.json"], "memory": [], "sessions": [],
        "usage": {"format": "none"}, "secrets": [], "runtime": [],
    }))
    repo = run_init(prof, tmp_path / "repos", name="custom-agent")
    run_sync(prof, tmp_path / "repos", name="custom-agent")
    assert (repo / "config" / "conf" / "settings.json").exists()


# --- README/robustness: schedule is Python-native (no git-bash/cygpath) on Windows ---

def test_schedule_uses_python_native_launcher_on_windows(agent_home, profile_dir,
                                                         tmp_path, monkeypatch):
    # Force the Windows branch and capture the schtasks invocation.
    monkeypatch.setattr(agent_backup.os, "name", "nt")
    run_init(profile_dir, tmp_path / "repos")
    captured = {}

    class _FakeResult:
        stdout, stderr = "SUCCESS", ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _FakeResult()

    monkeypatch.setattr(agent_backup.subprocess, "run", fake_run)
    agent_backup.cmd_schedule(
        argparse.Namespace(name="testagent", dir=tmp_path / "repos", at="09:15"))

    launcher = tmp_path / "repos" / "testagent-backup" / "sync.cmd"
    assert launcher.exists(), "expected a .cmd launcher to be written"
    text = launcher.read_text()
    assert agent_backup.sys.executable in text
    assert "sync" in text

    cmd = " ".join(captured["cmd"])
    assert "schtasks" in cmd and "/create" in cmd
    assert "bash" not in cmd.lower() and "cygpath" not in cmd.lower(), \
        f"Windows scheduling still depends on bash/cygpath: {cmd}"
    assert "sync.cmd" in cmd

def test_usage_csv_fallback_without_duckdb(agent_home, profile_dir, tmp_path,
                                           monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "duckdb":
            raise ImportError("no duckdb")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    run_init(profile_dir, tmp_path / "repos")
    run_sync(profile_dir, tmp_path / "repos")
    repo = tmp_path / "repos" / "testagent-backup"
    assert (repo / "usage" / "usage.csv").exists()


def test_recover_prints_recovery_map(agent_home, profile_dir, tmp_path, capsys):
    run_init(profile_dir, tmp_path / "repos")
    run_sync(profile_dir, tmp_path / "repos")
    agent_backup.cmd_recover(
        argparse.Namespace(name="testagent", dir=tmp_path / "repos"))
    out = capsys.readouterr().out
    assert "backup repo:" in out and "testagent-backup" in out
    assert "git remote:" in out
    assert "usage:" in out and "usage.parquet" in out
    assert "object store:" in out
