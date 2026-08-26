#!/usr/bin/env python3
"""agent-backup: generic manifest-driven backup for AI coding agents.

Backs up an agent harness's home directory the same way regardless of which
harness it is. Each harness has a profile (JSON) declaring four buckets:
  config    -> small, versioned in git (settings, skills, rules, prompts)
  memory    -> critical, versioned in git (memories, project state)
  sessions  -> large transcripts; compressed usage metrics -> git, and the raw
               transcripts mirror to object storage (opt-in)
  secrets/runtime -> always excluded

Per agent this produces one private git repo at ~/repos/<name>-backup/ with
config/, memory/, usage/ (Parquet ledger), plus optional object-storage mirror
of the raw transcripts.

Usage:
  agent-backup init <name> [--remote <url>]
  agent-backup sync <name>
  agent-backup schedule <name> [--at HH:MM]
  agent-backup status <name>

Profiles live in ./profiles/<name>.json. Object storage is opt-in: set
R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET (+BACKUP_OBJECT_STORE=r2)
to mirror raw transcripts. duckdb (parquet) and boto3 (R2) are optional; the
tool degrades to CSV and skips object storage without them.
"""

import argparse
import csv
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROFILES_DIR = Path(__file__).parent / "profiles"
DEFAULT_REPO_ROOT = Path.home() / "repos"


def log(msg):
    print(msg, flush=True)


def err(msg):
    print(msg, file=sys.stderr, flush=True)


def expand(p):
    return Path(os.path.expanduser(str(p)))


def load_profile(name):
    path = PROFILES_DIR / f"{name}.json"
    if not path.exists():
        candidates = sorted(p.name for p in PROFILES_DIR.glob("*.json"))
        err(f"no profile '{name}'. available: {', '.join(candidates)}")
        sys.exit(1)
    return json.loads(path.read_text("utf-8"))


def resolve_home(profile):
    home = profile.get("home", "")
    envvar = profile.get("home_env")
    if envvar and os.environ.get(envvar):
        home = os.environ[envvar]
    return expand(home)


def pattern_to_paths(home, pattern):
    """Yield absolute paths under home matching a config/memory/session pattern.

    Each matched path is resolved and any path whose resolved form is not under
    the resolved home dir is skipped with a warning (guards against symlink or
    '..' traversal escaping home)."""
    home_r = home.resolve()

    def _inside(path):
        try:
            return path.resolve().is_relative_to(home_r)
        except OSError:
            return False

    p = home / pattern
    if p.exists():
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    if _inside(f):
                        yield f
                    else:
                        log(f"  warning: skipping {f} (outside home)")
        else:
            if _inside(p):
                yield p
            else:
                log(f"  warning: skipping {p} (outside home)")


def load_env(repo):
    """Read <repo>/.env (key=value lines, ignoring comments/blanks) into
    os.environ without overriding already-set variables."""
    env_file = repo / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def excluded(relpath, secrets, runtime):
    """True if a relative path matches any secret/runtime pattern. Uses fnmatch
    glob semantics ('*' crosses path separators) so both file and dir patterns
    work, and a dir pattern excludes its whole subtree."""
    posix = relpath.as_posix()
    for pat in (*secrets, *runtime):
        p = pat.rstrip("/")
        if not p:
            continue
        if (fnmatch.fnmatch(posix, p)          # whole path (file or dir)
                or fnmatch.fnmatch(posix, "**/" + p)      # dir under any prefix
                or fnmatch.fnmatch(posix, "**/" + p + "/*")  # anything under it
                or any(fnmatch.fnmatch(seg, p) for seg in relpath.parts)):
            return True
    return False


def collect(home, patterns, secrets, runtime, prefix=""):
    """Return [(relpath, abspath)] for config/memory items, applying exclusions."""
    out = []
    for pattern in patterns:
        for abspath in pattern_to_paths(home, pattern):
            rel = (abspath.relative_to(home)).as_posix()
            rel_path = Path(prefix + rel)
            if excluded(rel_path, secrets, runtime):
                continue
            out.append((rel_path, abspath))
    return out


def extra_home_items(profile, category, secrets, runtime):
    """Collect items from extra_homes (namespaced under <basename>/)."""
    out = []
    for extra in profile.get("extra_homes", []):
        eh = expand(extra["path"])
        base = eh.name or "extra"
        for pattern in extra.get(category, []):
            for abspath in pattern_to_paths(eh, pattern):
                rel = (abspath.relative_to(eh)).as_posix()
                rel_path = Path(f"{base}/{rel}")
                if excluded(rel_path, secrets, runtime):
                    continue
                out.append((rel_path, abspath))
    return out


def copy_items(items, dest_root):
    for rel, abspath in items:
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abspath, dst)


def reconcile(dest_dir, kept_rels):
    """Delete files under dest_dir that are not in kept_rels (rsync-style
    mirror), so deleted source files are pruned from the backup repo."""
    if not dest_dir.exists():
        return
    for f in sorted(dest_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(dest_dir).as_posix()
        if rel not in kept_rels:
            try:
                f.unlink()
                log(f"  prune: removed {rel}")
            except OSError as e:
                err(f"  prune: could not remove {rel}: {e}")
    for d in sorted((p for p in dest_dir.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass


def run_git(repo, *args, check=True):
    res = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr}")
    return res


def git_sync(repo, commit_msg):
    """Commit if changed (or if a commit is unpushed), then pull --rebase + push
    if an upstream exists. Commits locally when no remote is configured yet.
    Returns True if anything was pushed/committed, else False."""
    run_git(repo, "add", "-A")
    staged = run_git(repo, "diff", "--cached", "--quiet", check=False).returncode != 0
    has_upstream = run_git(repo, "rev-parse", "--verify", "-q", "@{u}",
                           check=False).returncode == 0
    unpushed = 0
    if has_upstream:
        unpushed = int(run_git(repo, "rev-list", "--count", "@{u}..HEAD").stdout.strip())
    if not staged and unpushed == 0:
        log("  no changes, nothing to push")
        return False
    if staged:
        run_git(repo, "commit", "-q", "-m", commit_msg)
    if not has_upstream:
        remotes = run_git(repo, "remote", check=False).stdout.split()
        if "origin" in remotes:
            if run_git(repo, "push", "-u", "origin", "HEAD", "-q",
                       check=False).returncode != 0:
                raise RuntimeError("first push failed; commit stays local")
            log("  pushed new branch to origin")
            return True
        log("  no remote/upstream configured; committed locally")
        return True
    if run_git(repo, "pull", "--rebase", "--autostash", "-q", check=False).returncode != 0:
        run_git(repo, "rebase", "--abort", check=False)
        raise RuntimeError("rebase failed; aborted (commit stays local)")
    if run_git(repo, "push", "-q", check=False).returncode != 0:
        raise RuntimeError("push failed; commit stays local")
    log("  pushed update")
    return True


# --------------------------------------------------------------------------
# Usage extraction (compact metrics from session transcripts)
# --------------------------------------------------------------------------

TS_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
              "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")


def to_epoch_ms(ts):
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    s = str(ts)
    for fmt in TS_FORMATS:
        try:
            return int(datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return None


def num(node, key):
    v = node.get(key)
    return float(v) if isinstance(v, (int, float)) else 0.0


def find_usage(node, ts=None, model=None, provider=None, out=None):
    """omp-style usage dicts: {"input","output","cacheRead","cost":{...}}."""
    if out is None:
        out = []
    if isinstance(node, dict):
        ts = node.get("timestamp", ts)
        model = node.get("model") or model
        provider = node.get("provider") or provider
        u = node.get("usage")
        if isinstance(u, dict) and isinstance(u.get("cost"), dict):
            out.append((to_epoch_ms(ts), model, provider, u))
        for v in node.values():
            find_usage(v, ts, model, provider, out)
    elif isinstance(node, list):
        for v in node:
            find_usage(v, ts, model, provider, out)
    return out


def find_token_usage(node, ts=None, model=None, out=None):
    """codex-style: input_tokens/output_tokens/total_tokens on an event dict."""
    if out is None:
        out = []
    if isinstance(node, dict):
        ts = node.get("timestamp", ts)
        model = node.get("model") or model
        if ("input_tokens" in node or "output_tokens" in node or
                "total_tokens" in node):
            out.append((to_epoch_ms(ts), model, node))
        for v in node.values():
            find_token_usage(v, ts, model, out)
    elif isinstance(node, list):
        for v in node:
            find_token_usage(v, ts, model, out)
    return out


def extract_usage(profile, home, out_dir):
    fmt = profile.get("usage", {}).get("format", "none")
    if fmt != "jsonl":
        return 0
    has_cost = profile.get("usage", {}).get("has_cost", False)
    rows = []
    for pattern in profile.get("sessions", []):
        for abspath in pattern_to_paths(home, pattern):
            if abspath.suffix != ".jsonl":
                continue
            with open(abspath, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if has_cost:
                        for ts_ms, model, provider, u in find_usage(obj):
                            c = u["cost"]
                            rows.append({
                                "ts_ms": ts_ms, "model": model or "",
                                "input_tokens": int(num(u, "input")),
                                "output_tokens": int(num(u, "output")),
                                "cache_read_tokens": int(num(u, "cacheRead")),
                                "cache_write_tokens": int(num(u, "cacheWrite")),
                                "total_tokens": int(num(u, "totalTokens")),
                                "cost_total": num(c, "total"),
                            })
                    else:
                        for ts_ms, model, u in find_token_usage(obj):
                            rows.append({
                                "ts_ms": ts_ms, "model": model or "",
                                "input_tokens": int(num(u, "input_tokens")),
                                "output_tokens": int(num(u, "output_tokens")),
                                "cache_read_tokens": 0, "cache_write_tokens": 0,
                                "total_tokens": int(num(u, "total_tokens")),
                                "cost_total": 0.0,
                            })
    out_dir.mkdir(parents=True, exist_ok=True)
    if not rows:
        log("  no usage events")
        return 0
    try:
        import duckdb
        import pandas as pd
        con = duckdb.connect()
        con.register("u", pd.DataFrame(rows))
        con.execute(f"COPY u TO '{out_dir / 'usage.parquet'}' "
                    f"(FORMAT PARQUET, CODEC ZSTD)")
        con.execute(
            f"""COPY (
              SELECT strftime(to_timestamp(ts_ms/1000.0), '%Y-%m-%d') AS date, model,
                     count(*) AS events, sum(total_tokens) AS total_tokens,
                     sum(cost_total) AS cost_total
              FROM u GROUP BY 1,2 ORDER BY 1,2
            ) TO '{out_dir / 'daily.parquet'}' (FORMAT PARQUET, CODEC ZSTD)""")
        log(f"  usage -> {len(rows)} events in Parquet")
    except ImportError:
        with open(out_dir / "usage.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            for r in rows:
                w.writerow(r)
        log(f"  usage -> {len(rows)} events in CSV (duckdb not installed)")
    meta = {
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "agent": profile["name"], "events": len(rows),
        "models": sorted({r["model"] for r in rows if r["model"]}),
    }
    meta_path = out_dir / "meta.json"
    # Preserve the previous extracted_at when nothing substantive changed, so a
    # re-run with no new data produces no diff (and therefore no empty commit).
    if meta_path.exists():
        try:
            prev = json.loads(meta_path.read_text("utf-8"))
            if (prev.get("agent") == meta["agent"]
                    and prev.get("events") == meta["events"]
                    and prev.get("models") == meta["models"]):
                meta["extracted_at"] = prev["extracted_at"]
        except Exception:
            pass
    meta_path.write_text(json.dumps(meta, indent=2), "utf-8")
    return len(rows)


# --------------------------------------------------------------------------
# Object storage mirror (opt-in)
# --------------------------------------------------------------------------

def mirror_sessions(profile, home):
    account = os.environ.get("R2_ACCOUNT_ID", "").strip()
    key = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    bucket = os.environ.get("R2_BUCKET", "").strip()
    store = os.environ.get("BACKUP_OBJECT_STORE", "").strip().lower()
    if store != "r2" or not all([account, key, secret, bucket]):
        log("  sessions: object store not configured; skipping (set "
            "BACKUP_OBJECT_STORE=r2 + R2_* to enable)")
        return
    try:
        import boto3
        from botocore.client import Config
    except ImportError:
        log("  sessions: boto3 not installed; skipping object store")
        return
    endpoint = os.environ.get("R2_ENDPOINT", "").strip() or \
        f"https://{account}.r2.cloudflarestorage.com"
    client = boto3.client("s3", endpoint_url=endpoint, region_name="auto",
                          aws_access_key_id=key, aws_secret_access_key=secret,
                          config=Config(signature_version="s3v4"))
    prefix = f"{profile['name']}/raw/"
    manifest_path = Path(os.environ.get("BACKUP_MANIFEST", "~/.agent-backup-manifest.json"))
    manifest_path = expand(manifest_path)
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text("utf-8"))
        except Exception:
            manifest = {}
    uploaded = 0
    for pattern in profile.get("sessions", []):
        for abspath in pattern_to_paths(home, pattern):
            if not abspath.is_file():
                continue
            rel = abspath.relative_to(home).as_posix()
            key = f"{profile['name']}/{rel}"
            st = abspath.stat()
            prev = manifest.get(key)
            if prev and prev.get("size") == st.st_size and prev.get("mtime") == st.st_mtime:
                continue
            client.upload_file(str(abspath), bucket, prefix + rel)
            manifest[key] = {"size": st.st_size, "mtime": st.st_mtime}
            uploaded += 1
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), "utf-8")
    log(f"  sessions: mirrored {uploaded} new/changed files to R2")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init(args):
    profile = load_profile(args.name)
    home = resolve_home(profile)
    if not home.exists():
        err(f"home '{home}' does not exist for agent '{args.name}'")
        sys.exit(1)
    repo = args.dir / f"{args.name}-backup"
    if repo.exists():
        err(f"repo already exists: {repo}")
        sys.exit(1)
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "main")
    # repo-local fallback identity so the first commit works when none is set
    if run_git(repo, "config", "--local", "user.name",
               check=False).stdout.strip() == "":
        run_git(repo, "config", "--local", "user.name", "agent-backup")
    if run_git(repo, "config", "--local", "user.email",
               check=False).stdout.strip() == "":
        run_git(repo, "config", "--local", "user.email", "agent-backup@localhost")
    (repo / ".gitattributes").write_text("# store verbatim (LF), no CRLF conversion\n* -text\n")
    ignored = sorted(set(profile.get("secrets", []) + profile.get("runtime", [])))
    (repo / ".gitignore").write_text(
        "# never back these up (secrets + runtime state)\n" +
        "".join(f"/{p.rstrip('/')}/\n" if p.endswith("/") else f"/{p}\n" for p in ignored) +
        "__pycache__/\n"
        "# local secrets / state files never committed\n"
        ".env\n.env.*\nr2_manifest.json\n")
    if args.remote:
        run_git(repo, "remote", "add", "origin", args.remote)
    log(f"initialized {repo} for '{args.name}' (home: {home})")
    log("next: agent-backup sync " + args.name)


def cmd_sync(args):
    profile = load_profile(args.name)
    home = resolve_home(profile)
    repo = args.dir / f"{args.name}-backup"
    if not repo.exists():
        err(f"repo not initialized: {repo} (run 'agent-backup init {args.name}')")
        sys.exit(1)
    secrets = profile.get("secrets", [])
    runtime = profile.get("runtime", []) + [
        "**/node_modules", ".git/", "**/__pycache__", "**/.git/"
    ]
    log(f"syncing '{args.name}' (home: {home})")

    try:
        config_items = collect(home, profile.get("config", []), secrets, runtime)
        config_items += extra_home_items(profile, "config", secrets, runtime)
        copy_items(config_items, repo / "config")
        reconcile(repo / "config", {r.as_posix() for r, _ in config_items})
        log(f"  config: {len(config_items)} files")
    except Exception as e:
        err(f"sync: ERROR: config copy failed: {e}")

    try:
        memory_items = collect(home, profile.get("memory", []), secrets, runtime)
        memory_items += extra_home_items(profile, "memory", secrets, runtime)
        copy_items(memory_items, repo / "memory")
        reconcile(repo / "memory", {r.as_posix() for r, _ in memory_items})
        log(f"  memory: {len(memory_items)} files")
    except Exception as e:
        err(f"sync: ERROR: memory copy failed: {e}")

    try:
        extract_usage(profile, home, repo / "usage")
    except Exception as e:
        err(f"sync: ERROR: usage extraction failed: {e}")

    log("  git:")
    git_sync(repo, f"backup: {args.name} config+memory+usage {datetime.now():%F %H:%M}")

    try:
        load_env(repo)
        mirror_sessions(profile, home)
    except Exception as e:
        err(f"sync: ERROR: mirror failed: {e}")
    log("done")


def cmd_schedule(args):
    profile = load_profile(args.name)
    repo = args.dir / f"{args.name}-backup"
    if not repo.exists():
        err(f"repo not initialized: {repo}")
        sys.exit(1)
    sync_script = Path(__file__).resolve()
    if os.name == "nt":
        bash = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
        if not bash or not Path(bash).exists():
            err("bash not found; install git-bash or add it to PATH")
            sys.exit(1)
        # wrapper that sets the repo dir and calls sync (LF only; CRLF breaks bash)
        wrapper = repo / "sync.sh"
        wrapper.write_bytes(
            ("#!/usr/bin/env bash\nset -euo pipefail\n"
             f'cd "{repo}"\n"{sys.executable}" "{sync_script}" sync {args.name}\n'
             ).encode("utf-8"))
        try:
            msys = subprocess.check_output(
                ["cygpath", "-m", str(wrapper)], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            msys = "/" + str(wrapper)[0].lower() + str(wrapper)[2:].replace("\\", "/")
        task = f"agent-backup-{args.name}"
        res = subprocess.run(
            ["schtasks", "/create", "/tn", task, "/tr",
             f'"{bash}" -lc "{msys}"', "/sc", "daily", "/st", args.at, "/f"],
            capture_output=True, text=True)
        print(res.stdout.strip() or res.stderr.strip())
        log(f"scheduled task '{task}' daily at {args.at} -> {wrapper}")
    else:
        log("unix: add a cron line, e.g.")
        log(f'30 9 * * * cd "{repo}" && "{sys.executable}" "{sync_script}" sync {args.name}')


def cmd_status(args):
    profile = load_profile(args.name)
    home = resolve_home(profile)
    repo = args.dir / f"{args.name}-backup"
    print(f"agent:        {args.name}")
    print(f"home:         {home}  ({'exists' if home.exists() else 'MISSING'})")
    print(f"repo:         {repo}  ({'initialized' if repo.exists() else 'not initialized'})")
    secrets = profile.get("secrets", [])
    runtime = profile.get("runtime", []) + [
        "**/node_modules", ".git/", "**/__pycache__", "**/.git/"
    ]
    for cat in ("config", "memory"):
        items = collect(home, profile.get(cat, []), secrets, runtime)
        items += extra_home_items(profile, cat, secrets, runtime)
        print(f"  {cat:9s}: {len(items)} files (after exclusions)")
    n_sess = 0
    for pattern in profile.get("sessions", []):
        n_sess += len(list(pattern_to_paths(home, pattern)))
    print(f"  sessions : {n_sess} files")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_REPO_ROOT,
                    help="repo root (default ~/repos)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init")
    p_init.add_argument("name"); p_init.add_argument("--remote")
    p_sync = sub.add_parser("sync"); p_sync.add_argument("name")
    p_sched = sub.add_parser("schedule"); p_sched.add_argument("name")
    p_sched.add_argument("--at", default="09:30")
    p_stat = sub.add_parser("status"); p_stat.add_argument("name")
    args = ap.parse_args()
    {"init": cmd_init, "sync": cmd_sync,
     "schedule": cmd_schedule, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
