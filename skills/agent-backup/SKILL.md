---
name: agent-backup
license: MIT
version: 0.2.0
description: >
  Generic manifest-driven backup for AI coding agent harnesses (OMP, Claude
  Code, Codex, Pi, Hermes, and any custom agent). Backs up a harness's config
  and hand-written memory state, plus compressed usage metrics, to a private
  git repo, and optionally mirrors raw session transcripts to object storage
  (R2, S3-compatible API). Memory a harness *generates* is data, not git: its
  profile names the object-store custodian that already holds it, instead of
  this tool copying it. Use when asked to back up, replicate, version, or
  schedule backups of an agent's config/data, or to set up a new harness for
  backup.
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
| `memory` | hand-written notes, project state, long-term context | git (critical, versioned) |
| `memory` — generated | a tree the harness rebuilds from its own sessions | object storage, via the custodian the profile names |
| `sessions` | JSONL transcripts, project history | usage metrics → git; raw → object storage (opt-in) |
| `secrets` / `runtime` | auth.json, .env, node_modules, caches, logs, DBs | never backed up |

Profiles (in `profiles/<name>.json`) declare exactly which paths fall in each
bucket. Everything under `secrets` + `runtime` is excluded automatically,
plus a built-in default that always skips `**/node_modules`, `.git/`,
`__pycache__`.

### Memory is a git bucket only when it is hand-written

`config` and `memory` hold state a person wrote and git should version. Memory a
harness *generates* is derived data, and a harness that generates it normally
mirrors it to object storage already. A second copy in git duplicates that
mirror, and it puts derived data into a history read as a record of what the
agent was told.

A profile declares which it is by naming a `memory_custodian` and leaving the
bucket empty:

```json
"memory": [],
"memory_custodian": "data, not git: <repo> mirrors the tree to R2 under <prefix>/"
```

With one declared, `sync` copies nothing, prunes any `memory/` copy an earlier
sync left in the repo, and writes `config+usage` rather than
`config+memory+usage` as its commit subject; `status` and `recover` print the
custodian line where they would print a file count. That line is the point: a
bare `memory: 0 files` reads as a bucket that failed to copy.

A profile that declares a custodian **and** a non-empty `memory` list says two
opposite things, so `sync`, `status` and `recover` refuse it with the key to
remove rather than picking one — the silent choice would stop backing up notes
that an earlier sync had already put in the repo, and prune them.

OMP is the case in hand — `omp.json` declares a custodian and an empty bucket,
because omp's consolidation pipeline generates `~/.omp/agent/memories/` from the
session transcripts, `example-data` mirrors it to Cloudflare R2 under
`omp/memories/`, and the harness's own config repo (`example-config`) ignores the
tree and versions nothing under it.

## Usage

```bash
cd ~/.omp/skills/agent-backup

# 1. Initialize a repo for an agent (creates ~/repos/<name>-backup/)
python agent-backup.py init codex --remote https://github.com/<you>/codex-backup.git

# 2. Back it up (copy config + any git-backed memory, extract usage Parquet,
#    commit/push)
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

- `omp` — `~/.omp/agent` (+ `~/.omp` extras: rules, skills, plugins); memory is
  **not** in git here (see the memory subsection above — `example-data` mirrors
  the generated tree to R2)
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
cleanly — config and any git-backed memory still back up, and the usage ledger
still extracts.

## Restore (agent-driven, approval-gated)

Restore is a **judgment-driven, gated** procedure — never a blind copy-back.
`recover` prints the facts; the agent decides, assesses, and must get user
approval before writing anything.

```bash
python agent-backup.py recover <name>
```

It reports the backup repo path + git remote, how many config files it holds,
the usage ledger, the object-store bucket/prefix, and — for a profile that
declares one — the memory custodian in place of a memory file count.

### Procedure (follow in order)

1. **Gather** — run `recover`, then inspect the backup: `git log` the repo,
   list what's under `config/` and `memory/`, and the usage ledger; note the
   latest backup commit and the object-store prefix. A profile with a
   `memory_custodian` holds no memory here: recover the tree from the store that
   line names (for OMP, the R2 prefix `omp/memories/`), not from the repo.
2. **Diff** — compare each backed-up file against the current agent state:
   for every backup `config/`/`memory/` file, does the matching home path exist
   and does it differ? Enumerate what would be added, overwritten, or left alone.
   Extra-homes restore from `<basename>/` back to that extra home root.
3. **Impact assessment** — state plainly what changes **irreversibly**: any
   current home file that exists and differs from the backup would be
   overwritten (and its current content lost unless snapshotted). Distinguish
   additive/restorative changes from destructive ones.
4. **Safety plan** — before any write, snapshot the affected current home files
   (e.g. copy them to `<home>/.agent-backup-before-restore-<ts>/`) so nothing is
   lost. Sessions (opt-in) restore from R2 `<agent>/raw/` to the matching home
   paths. Secrets were never backed up, so none return.
5. **GATE** — present the diff + impact assessment to the user and get explicit
   approval before writing. Use the ask/approval mechanism; do **not** restore
   without it. If the user declines, stop — change nothing.
6. **Execute** — on approval: snapshot, then restore config and any git-backed
   memory from the repo (clone or pull), optionally restore sessions from R2 and
   any custodied memory from the store `recover` named, and verify the result.

The agent may clone-vs-reuse the repo, restore selectively, and choose the
order — but never writes to the harness home without the impact assessment and
user approval.

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

If that harness *generates* its memory rather than having you write it, do not
put it in `memory`: set `"memory": []` and declare where it actually lives —
`"memory_custodian": "<who mirrors it, and under what prefix>"` — so `sync` does
not duplicate the mirror and `status`/`recover` say so instead of reporting an
empty bucket. `omp.json` is the worked example.

Run `status` first to confirm the profile resolves the right paths and that
the file counts are sane (a config count in the tens of thousands usually means
a vendored dir like `node_modules` is leaking in — add it to `runtime`).
