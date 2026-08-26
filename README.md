# agent-backup

![License](https://img.shields.io/badge/license-MIT-blue)
![Version](https://img.shields.io/badge/version-0.2.0-2b8a3e)
![Python](https://img.shields.io/badge/python-3.10+-3776AB)
![CI](https://github.com/evanokeefe39/agent-backup/actions/workflows/ci.yml/badge.svg)

**Supported OS:** Windows · macOS · Linux

**Supported agents:** Oh My Pi (OMP) · Claude Code · Codex · Pi · Hermes · any custom agent (via profile)

Generic, manifest-driven backup for AI coding agent harnesses — OMP, Claude Code, Codex, Pi, Hermes, or any custom agent. It backs up a harness's **config** and **memories** plus a **compressed usage/cost ledger** to a private git repo, and optionally mirrors the raw **session transcripts** to object storage (Cloudflare R2, S3-compatible API).

It never touches the harness's own directories — it copies the valuable data into your own backup repos, and always excludes secrets (`auth.json`, `.env`) and runtime state (`node_modules`, caches, logs, DBs).

> **Disclaimer:** This skill is **vibe-coded** — built iteratively for personal use and primarily exercised against the **Oh My Pi** harness. OMP is the most-tested target; the Claude Code, Codex, Pi, and Hermes profiles are supported but less battle-tested. It ships without warranty — review it before relying on it, and note that the raw-transcript object-store upload is strictly opt-in. The test suite (see [Testing](#testing)) verifies the claims in this README.

## Install

**Pi** (auto-discovers `skills/agent-backup/SKILL.md`):
```
pi install git:github.com/evanokeefe39/agent-backup
```

**OMP** (marketplace):
```
omp plugin marketplace add evanokeefe39/agent-backup
omp plugin install agent-backup@agent-backup
```

**Claude Code** (plugin marketplace):
```
/plugin marketplace add evanokeefe39/agent-backup
/plugin install agent-backup@agent-backup
```

## Quick start

```bash
cd <skill dir>
python agent-backup.py status omp          # sanity-check a profile's paths
python agent-backup.py init codex --remote https://github.com/<you>/codex-backup.git
python agent-backup.py sync codex          # config+memory+usage -> git
python agent-backup.py schedule codex --at 09:15
```

Commands: `init`, `sync`, `schedule`, `status`, `recover`. Each agent gets one
repo at `~/repos/<name>-backup/` with `config/`, `memory/`, and `usage/`
(Parquet). Object storage is opt-in — set `BACKUP_OBJECT_STORE=r2` plus
`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` (in the
repo's `.env`, which is gitignored) to mirror raw transcripts.

## Restore (agent-driven, approval-gated)

`recover` prints the known recovery paths; restore itself is a gated,
agent-driven flow, not a blind copy:

```bash
python agent-backup.py recover codex
```

The agent gathers the recovery map, **diffs** the latest backup against the
current agent state, produces an **impact assessment** (what changes
irreversibly — files that exist and differ from the backup get overwritten),
snapshots anything it will touch, and then **gates on your explicit approval**
before writing anything. On approval it restores config/memory from the backup
repo (cloned or pulled) and optionally downloads session transcripts from R2
(`<agent>/raw/`). Secrets were never backed up, so none return. If you decline,
nothing changes.

## Adding a new agent

Profiles live in `profiles/<name>.json` — data-driven, no code changes:

```json
{
  "name": "my-agent",
  "home": "~/.my-agent",
  "config": ["settings.json", "skills/"],
  "memory": ["memories/"],
  "sessions": ["sessions/"],
  "usage": { "format": "jsonl", "has_cost": true },
  "secrets": ["auth.json"],
  "runtime": ["cache/", "node_modules", "*.db"]
}
```

## License

MIT

## Platforms

Cross-platform: Windows, macOS, and Linux. The tool is Python 3.10+ stdlib
(no runtime dependencies). `schedule` registers a Windows Task Scheduler job on
Windows; on macOS/Linux it prints a cron line to add yourself. The object-store
mirror (`boto3`) and the Parquet usage ledger (`duckdb` + `pandas`) are optional
dependencies, enabled only when installed.

## Testing

Tests are a **dev-only** concern — the skill itself runs on the Python standard
library with zero runtime dependencies. Install the dev extras to run them:

```bash
pip install -e ".[dev]"     # or: uv sync --extra dev
python -m pytest tests/ -q
```

The suite runs the real `init`/`sync` commands against a synthetic agent home
and verifies config/memory backup, secret and runtime exclusion, that the
harness home is never modified, the DuckDB-queryable usage ledger (per-turn and
per-day), git commit + first-push, idempotent re-runs (no empty commits), the
`.env` gitignore + opt-in object store, and adding a custom agent via profile.
