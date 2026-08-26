---
name: agent-backup
license: MIT
version: 0.1.0
description: >
  Generic manifest-driven backup for AI coding agent harnesses (OMP, Claude
  Code, Codex, Pi, Hermes, and any custom agent). Backs up a harness's config,
  memories, and compressed usage metrics to a private git repo, and optionally
  mirrors raw session transcripts to object storage (R2, S3-compatible API).
  Use when asked to back up, replicate, version, or schedule backups of an
  agent's config/data, or to set up a new harness for backup.
---

# agent-backup

A single tool that backs up any AI coding agent's home directory the same way,
regardless of which harness it is. It codifies a pattern proven on OMP: keep
the small, valuable data in a private git repo, keep the big raw transcripts
in object storage, and never pollute the harness's own directories.

## The unified model

Every agent harness keeps a **home root** (a base dir, sometimes with a
profile/agent subdir) holding four buckets:

| Bucket | Examples | Backup target |
|---|---|---|
| `config` | settings, skills, rules, commands, prompts, agents | git (small, versioned) |
| `memory` | memories, project state, long-term context | git (critical, versioned) |
| `sessions` | JSONL transcripts, project history | usage metrics → git; raw → object storage (opt-in) |
| `secrets` / `runtime` | auth.json, .env, node_modules, caches, logs, DBs | never backed up |

Profiles (in `profiles/<name>.json`) declare exactly which paths fall in each
bucket. Everything under `secrets` + `runtime` is excluded automatically,
plus a built-in default that always skips `**/node_modules`, `.git/`,
`__pycache__`.

## Usage

```bash
cd ~/.omp/skills/agent-backup

# 1. Initialize a repo for an agent (creates ~/repos/<name>-backup/)
python agent-backup.py init codex --remote https://github.com/<you>/codex-backup.git

# 2. Back it up (copy config+memory, extract usage Parquet, commit/push)
python agent-backup.py sync codex

# 3. Schedule it daily. On Windows this registers a Task Scheduler job
#    honoring --at HH:MM; on unix it prints a cron line to add yourself.
python agent-backup.py schedule codex --at 09:15

# 4. Inspect what a profile resolves to
python agent-backup.py status codex
```

Commands: `init`, `sync`, `schedule`, `status`. `--dir <root>` overrides the
repo root (default `~/repos`). Without a `--remote`, `sync` commits locally
and reports the missing upstream.

## Built-in profiles

- `omp` — `~/.omp/agent` (+ `~/.omp` extras: rules, skills, plugins)
- `claude-code` — `~/.claude`
- `codex` — `~/.codex`
- `pi` — `~/.pi/agent` (+ `~/.pi` extras), respects `PI_CODING_AGENT_DIR`
- `hermes` — `~/.hermes`, respects `HERMES_HOME`

Profiles are data-driven JSON — fix a path or add a pattern without touching
the tool. Each supports `home_env` (an env var that overrides the home dir,
e.g. `HERMES_HOME`) and `extra_homes` (additional roots namespaced under their
folder name, e.g. `config/.omp/rules`).

## Usage metrics (compressed)

`sync` runs a generic JSONL usage miner over the session transcripts and
writes a tiny Parquet ledger (`usage/usage.parquet`, `usage/daily.parquet`) —
per-turn and per-day token/cost. It handles two shapes:
- OMP/Pi style: `"usage": {"input",...,"cost":{...}}` (has cost)
- Codex style: `input_tokens` / `output_tokens` / `total_tokens` (no cost)

Query it with DuckDB: `SELECT * FROM 'usage/daily.parquet' ORDER BY date`.
If `duckdb`/`pandas` are unavailable it falls back to CSV.

## Object storage (opt-in full-session backup)

To mirror the raw transcripts to Cloudflare R2 (S3-compatible API), set env
vars (or put them in the repo's `.env`):

```
BACKUP_OBJECT_STORE=r2
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=...
```

Transcripts upload under `<agent>/raw/<path>`, incremental (only new/changed
files, tracked by a manifest). Without this config, `sync` skips sessions
cleanly — config/memory/usage still back up.

## Adding a new agent

```bash
cat > profiles/my-agent.json <<'EOF'
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
EOF
python agent-backup.py init my-agent && python agent-backup.py sync my-agent
```

Run `status` first to confirm the profile resolves the right paths and that
the file counts are sane (a config count in the tens of thousands usually means
a vendored dir like `node_modules` is leaking in — add it to `runtime`).
