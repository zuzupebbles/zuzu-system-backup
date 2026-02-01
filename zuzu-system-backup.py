#!/usr/bin/env python3
"""zuzu-system-backup (restic edition)

This is a refactor of the original script to use restic for snapshots.

Design:
  1) "Snapshots" (system + user selections) -> restic repo on the NAS via SSH (sftp backend).
  2) "Single-copy mirrors" -> rsync to the NAS (unchanged from prior behavior).

Why this is saner than tar/compress:
  - Restic keeps content-addressed packs + dedupe; only changed data is uploaded.
  - No giant archives that break incremental behavior.
  - Time-based retention is built-in (forget --keep-daily/weekly/monthly/...).

Prereqs on the workstation (Agamemnon):
  - python3, PyYAML
  - restic installed in PATH
  - SSH access to NAS works non-interactively (your ssh config already covers this)
  - A restic password provided via RESTIC_PASSWORD or RESTIC_PASSWORD_FILE

Config file is YAML (backup.yaml). This script keeps backward compat with:
  retention.snapshots: <N>  (old behavior -> maps to restic --keep-last N)

Preferred new retention schema:
  retention:
    keep_daily: 7
    keep_weekly: 4
    keep_monthly: 12
    keep_yearly: 3
    prune: true

Repository placement:
  - default: sftp:<remote.user>@<remote.host>:<remote.base>/<remote.host_dir>/restic-repo
  - override: restic.repository: "sftp:zocalo:/share/.../restic-repo"

"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml


@dataclass
class Remote:
    user: str
    host: str
    port: int
    key: Optional[str]
    base: str
    host_dir: str


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def run(cmd: List[str], *, env: Optional[Dict[str, str]] = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command, echoing it in a copy/paste-friendly form."""
    printable = " ".join(shlex.quote(c) for c in cmd)
    print(f"$ {printable}")
    return subprocess.run(cmd, env=env, check=check)


def run_capture(cmd: List[str], *, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    printable = " ".join(shlex.quote(c) for c in cmd)
    print(f"$ {printable}")
    return subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def load_config(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_remote(cfg: Dict) -> Remote:
    r = cfg.get("remote", {})
    missing = [k for k in ("user", "host", "port", "base", "host_dir") if not r.get(k)]
    if missing:
        raise SystemExit(f"backup.yaml missing required remote keys: {', '.join(missing)}")
    key = r.get("key")
    return Remote(
        user=str(r["user"]),
        host=str(r["host"]),
        port=int(r["port"]),
        key=str(key) if key else None,
        base=str(r["base"]).rstrip("/"),
        host_dir=str(r["host_dir"]).strip("/"),
    )


def ssh_base(remote: Remote) -> List[str]:
    cmd = ["ssh", "-p", str(remote.port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    # Use an explicit key if provided; otherwise let ssh_config handle it.
    if remote.key:
        cmd += ["-i", remote.key]
    cmd += [f"{remote.user}@{remote.host}"]
    return cmd


def ensure_restic_installed() -> None:
    if shutil.which("restic") is None:
        raise SystemExit(
            "restic is not installed on this host. Install it first (Arch: pacman -S restic) and re-run."
        )


def restic_env(cfg: Dict) -> Dict[str, str]:
    """Build env for restic.

    Accepts:
      - RESTIC_PASSWORD or RESTIC_PASSWORD_FILE from environment
      - restic.password_file in backup.yaml (optional)

    We do NOT read passwords from YAML directly (too easy to accidentally commit).
    """
    env = os.environ.copy()
    if "RESTIC_PASSWORD" in env or "RESTIC_PASSWORD_FILE" in env:
        return env

    r = cfg.get("restic", {})
    pw_file = r.get("password_file")
    if pw_file:
        env["RESTIC_PASSWORD_FILE"] = str(pw_file)
        return env

    raise SystemExit(
        "RESTIC_PASSWORD (or RESTIC_PASSWORD_FILE) is not set. "
        "Set it in the environment or add restic.password_file to backup.yaml."
    )


def restic_repo(cfg: Dict, remote: Remote) -> str:
    r = cfg.get("restic", {})
    repo = r.get("repository")
    if repo:
        return str(repo)

    # Default: repo lives alongside your prior snapshot root.
    # Use sftp backend so restic goes over SSH using your keys + ssh config.
    repo_path = f"{remote.base}/{remote.host_dir}/restic-repo"
    return f"sftp:{remote.user}@{remote.host}:{repo_path}"


def ensure_remote_dir(remote: Remote, path: str) -> None:
    # Ensure the directory exists on the NAS (restic won't always create parent dirs).
    mkdir_cmd = ssh_base(remote) + ["mkdir", "-p", path]
    run(mkdir_cmd)


def ensure_restic_repo_initialized(repo: str, env: Dict[str, str]) -> None:
    # "restic snapshots" is a cheap way to test access and repo validity.
    p = run_capture(["restic", "-r", repo, "snapshots"], env=env)
    if p.returncode == 0:
        return

    combined = (p.stdout or "") + "\n" + (p.stderr or "")
    if "Is there a repository" in combined or "repository does not exist" in combined:
        run(["restic", "-r", repo, "init"], env=env)
        return

    # Wrong password, permission issues, or network.
    eprint(combined.strip())
    raise SystemExit("restic repo check failed (see output above)")


def expand_user_paths(cfg: Dict) -> List[str]:
    """Return absolute paths for user include lists."""
    user = cfg.get("user", {})
    home = str(user.get("home", "")).rstrip("/")
    if not home:
        return []

    paths: List[str] = []
    for d in user.get("include_dirs", []) or []:
        d = str(d).lstrip("/")
        paths.append(f"{home}/{d}")
    for f in user.get("include_files", []) or []:
        f = str(f).lstrip("/")
        paths.append(f"{home}/{f}")
    return paths


def system_paths(cfg: Dict) -> List[str]:
    syscfg = cfg.get("system", {})
    return [str(p) for p in (syscfg.get("include_paths", []) or [])]


def exclude_file(cfg: Dict) -> Optional[str]:
    patterns = cfg.get("exclude_patterns", []) or []
    if not patterns:
        return None

    user_home = str(cfg.get("user", {}).get("home", ""))
    fd, path = tempfile.mkstemp(prefix="restic-exclude-", text=True)
    os.close(fd)
    p = Path(path)

    lines: List[str] = []
    for pat in patterns:
        s = str(pat)
        if "${USER_HOME}" in s:
            s = s.replace("${USER_HOME}", user_home)
        lines.append(s)

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def restic_backup(cfg: Dict, repo: str, env: Dict[str, str], host_tag: str, *, dry_run: bool) -> None:
    paths = system_paths(cfg) + expand_user_paths(cfg)
    paths = [p for p in paths if p]
    if not paths:
        print("No include paths defined (nothing to back up)")
        return

    ex_file = exclude_file(cfg)

    cmd = ["restic", "-r", repo, "backup"]
    cmd += ["--tag", host_tag]
    # Optional additional tags
    for t in (cfg.get("restic", {}) or {}).get("tags", []) or []:
        cmd += ["--tag", str(t)]

    # Safety defaults; can be overridden later if needed.
    r_opts = cfg.get("restic", {}) or {}
    if r_opts.get("one_file_system", False):
        cmd.append("--one-file-system")
    if r_opts.get("exclude_caches", True):
        cmd.append("--exclude-caches")
    if ex_file:
        cmd += ["--exclude-file", ex_file]
    if dry_run:
        cmd.append("--dry-run")

    cmd += paths
    try:
        run(cmd, env=env)
    finally:
        if ex_file:
            try:
                Path(ex_file).unlink(missing_ok=True)
            except Exception:
                pass


def restic_forget(cfg: Dict, repo: str, env: Dict[str, str], host_tag: str, *, dry_run: bool) -> None:
    retention = cfg.get("retention", {}) or {}

    # Backward compatibility: retention.snapshots -> keep-last
    keep_last = retention.get("keep_last")
    if keep_last is None and retention.get("snapshots") is not None:
        keep_last = retention.get("snapshots")

    keep_hourly = retention.get("keep_hourly")
    keep_daily = retention.get("keep_daily")
    keep_weekly = retention.get("keep_weekly")
    keep_monthly = retention.get("keep_monthly")
    keep_yearly = retention.get("keep_yearly")

    prune = retention.get("prune", True)

    cmd = ["restic", "-r", repo, "forget", "--tag", host_tag]

    def add_keep(flag: str, val: object) -> None:
        if val is None:
            return
        cmd.extend([flag, str(int(val))])

    add_keep("--keep-last", keep_last)
    add_keep("--keep-hourly", keep_hourly)
    add_keep("--keep-daily", keep_daily)
    add_keep("--keep-weekly", keep_weekly)
    add_keep("--keep-monthly", keep_monthly)
    add_keep("--keep-yearly", keep_yearly)

    if prune:
        cmd.append("--prune")
    if dry_run:
        cmd.append("--dry-run")

    # If no keep policy is defined, do nothing (safer than pruning everything).
    has_keep = any(
        v is not None
        for v in (keep_last, keep_hourly, keep_daily, keep_weekly, keep_monthly, keep_yearly)
    )
    if not has_keep:
        print("No retention policy configured; skipping restic forget/prune")
        return

    run(cmd, env=env)


def rsync_single_copy(cfg: Dict, remote: Remote, *, dry_run: bool) -> None:
    mappings = cfg.get("single_copy_mappings", []) or []
    extra = (cfg.get("rsync", {}) or {}).get("extra_opts", []) or []
    extra = [str(x) for x in extra if x]

    for m in mappings:
        if not m:
            continue
        if isinstance(m, str):
            raw = m
        else:
            # YAML could have dicts in the future; ignore for now.
            continue

        if "|" not in raw:
            eprint(f"Skipping malformed single_copy_mapping (expected 'SRC|DST'): {raw}")
            continue
        src, dst = raw.split("|", 1)
        src = src.strip().strip('"')
        dst = dst.strip().strip('"')
        if not src or not dst:
            continue

        # remote rsync target: user@host:/path
        target = f"{remote.user}@{remote.host}:{dst.rstrip('/')}"
        cmd = ["rsync", "-aHAX", "--delete", "-e", _rsync_ssh(remote)]
        cmd += extra
        if dry_run:
            cmd.append("--dry-run")
        cmd += [src.rstrip("/") + "/", target + "/"]
        run(cmd)


def _rsync_ssh(remote: Remote) -> str:
    parts = ["ssh", "-p", str(remote.port)]
    if remote.key:
        parts += ["-i", remote.key]
    parts += ["-o", "BatchMode=yes"]
    return " ".join(shlex.quote(p) for p in parts)


def main() -> None:
    ap = argparse.ArgumentParser(description="Backup system+user selections to NAS using restic; optional rsync mirrors")
    ap.add_argument("-c", "--config", default="backup.yaml", help="Path to YAML config (default: backup.yaml)")
    ap.add_argument("--no-rsync", action="store_true", help="Skip single-copy rsync mirrors")
    ap.add_argument("--no-forget", action="store_true", help="Skip retention (restic forget/prune)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would happen without changing anything")
    args = ap.parse_args()

    cfg = load_config(args.config)
    remote = parse_remote(cfg)

    ensure_restic_installed()
    env = restic_env(cfg)

    repo = restic_repo(cfg, remote)
    host_tag = f"host={remote.host_dir}"

    # Ensure repo dir exists on NAS when using the default layout.
    # (If you set restic.repository yourself, you're responsible for its existence.)
    if "restic" not in cfg or not (cfg.get("restic", {}) or {}).get("repository"):
        ensure_remote_dir(remote, f"{remote.base}/{remote.host_dir}/restic-repo")

    ensure_restic_repo_initialized(repo, env)
    restic_backup(cfg, repo, env, host_tag, dry_run=args.dry_run)

    if not args.no_forget:
        restic_forget(cfg, repo, env, host_tag, dry_run=args.dry_run)

    if not args.no_rsync:
        rsync_single_copy(cfg, remote, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
