#!/usr/bin/env python3
"""
zuzu-system-backup

Snapshot + mirror backups from Arch hosts to NAS over SSH/rsync.

- Snapshot sources:
    INCLUDE_PATHS
    USER_INCLUDE_DIRS
    USER_INCLUDE_FILES

  -> rsync’d into a local temp tree per category
  -> compressed locally (tar + optional zstd)
  -> archives uploaded to NAS snapshot dir

- Single-copy mirrors:
    SINGLE_COPY_MAPPINGS

  -> rsync directly to NAS with --delete
"""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "backup.yaml"

SNAPSHOT_FMT = "%Y-%m-%d_%H-%M-%S"


# ---------------------------------------------------------------------------
# Load config (backup.yaml)
# ---------------------------------------------------------------------------

def _fatal(msg: str) -> None:
    print(f"[FATAL] {msg}", file=sys.stderr)
    sys.exit(1)

try:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
except FileNotFoundError:
    _fatal(f"Config file not found: {CONFIG_PATH}")

remote_cfg = cfg.get("remote", {})
SSH_USER = remote_cfg.get("user")
SSH_HOST = remote_cfg.get("host")
SSH_PORT = int(remote_cfg.get("port", 22))
SSH_KEY = remote_cfg.get("key")
REMOTE_BASE = remote_cfg.get("base")
REMOTE_HOST_DIR = remote_cfg.get("host_dir")

if not all([SSH_USER, SSH_HOST, SSH_KEY, REMOTE_BASE, REMOTE_HOST_DIR]):
    _fatal("remote.{user,host,key,base,host_dir} must all be set in backup.yaml")

# retention
RETENTION_DAYS = int(cfg.get("retention", {}).get("snapshots", 7))

# compression
compression_cfg = cfg.get("compression", {})
COMPRESSION_MODE = (compression_cfg.get("mode") or "high").lower()
if COMPRESSION_MODE not in ("high", "light", "none"):
    COMPRESSION_MODE = "high"

COMPRESSION_PATH = compression_cfg.get("path")

# rsync
RSYNC_EXTRA_OPTS = cfg.get("rsync", {}).get("extra_opts", [])

user_cfg = cfg.get("user", {})
USER_HOME = user_cfg.get("home")


def expand_user_path(p: str) -> str:
    if not p:
        return p
    if p.startswith("/"):
        return p
    if USER_HOME:
        return str(Path(USER_HOME) / p)
    return p


def expand_home_var(s: str) -> str:
    if not isinstance(s, str):
        return s
    if USER_HOME:
        return (
            s.replace("${USER_HOME}", USER_HOME)
            .replace("${HOME}", USER_HOME)
        )
    return s

# System trees
INCLUDE_PATHS = cfg.get("system", {}).get("include_paths", [])

# User trees
USER_INCLUDE_DIRS = [expand_user_path(p) for p in user_cfg.get("include_dirs", [])]
USER_INCLUDE_FILES = [expand_user_path(p) for p in user_cfg.get("include_files", [])]

# Exclude patterns
raw_excl = cfg.get("exclude_patterns", [])
EXCLUDE_PATTERNS = [expand_home_var(p) for p in raw_excl]

# Single-copy mappings
SINGLE_COPY_MAPPINGS = cfg.get("single_copy_mappings", [])

# Derived remote paths
REMOTE_ROOT = f"{REMOTE_BASE.rstrip('/')}/{REMOTE_HOST_DIR}"
REMOTE_SNAPSHOTS_DIR = f"{REMOTE_ROOT}/snapshots"

# Decide where local temp/compression work happens
if COMPRESSION_PATH:
    compression_root = Path(COMPRESSION_PATH)
    try:
        compression_root.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(
            f"[WARN] COMPRESSION_PATH {compression_root} not usable ({e}); "
            "falling back to system temp",
            file=sys.stderr,
        )
        compression_root = Path(tempfile.gettempdir())
else:
    compression_root = Path(tempfile.gettempdir())

COMPRESSION_ROOT = compression_root

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now().strftime(SNAPSHOT_FMT)
    print(f"[{ts}] {msg}", file=sys.stderr)


def run(cmd, check: bool = True, capture_output: bool = False, text: bool = True):
    if isinstance(cmd, list):
        debug_cmd = " ".join(shlex.quote(str(c)) for c in cmd)
    else:
        debug_cmd = cmd
    log(f"run: {debug_cmd}")
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture_output,
        text=text,
    )

def remote_shell(cmd_str: str, **kwargs):
    ssh_cmd = [
        "ssh",
        "-i", SSH_KEY,
        "-p", str(SSH_PORT),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{SSH_USER}@{SSH_HOST}",
        cmd_str,
    ]
    return run(ssh_cmd, **kwargs)


def build_excludes_file() -> str:
    fd, path = tempfile.mkstemp(prefix="arch-rsync-backup-excludes-", text=True)
    with os.fdopen(fd, "w") as fh:
        for pat in EXCLUDE_PATTERNS:
            fh.write(str(pat) + "\n")
    return path


def ensure_remote_snapshot_root() -> None:
    remote_shell(f"mkdir -p {shlex.quote(REMOTE_SNAPSHOTS_DIR)}")


def snapshot_name() -> str:
    return datetime.now().strftime(SNAPSHOT_FMT)


def list_remote_snapshots():
    cmd = f"ls -1 {shlex.quote(REMOTE_SNAPSHOTS_DIR)} || true"
    result = remote_shell(cmd, capture_output=True)
    names = []
    if result.stdout:
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                names.append(line)
    return names


def prune_old_snapshots() -> None:
    """
    Retention policy: keep at most RETENTION_DAYS snapshots
    (by timestamp order). Older ones are deleted.
    """
    max_keep = RETENTION_DAYS
    if max_keep <= 0:
        log("RETENTION_DAYS <= 0; skipping pruning")
        return

    names = list_remote_snapshots()
    parsed = []
    for name in names:
        try:
            dt = datetime.strptime(name, SNAPSHOT_FMT)
        except ValueError:
            # non-standard dirs, ignore
            continue
        parsed.append((dt, name))

    parsed.sort()
    if len(parsed) <= max_keep:
        log(f"prune: {len(parsed)} snapshots <= {max_keep}; nothing to delete")
        return

    to_delete = [name for _, name in parsed[:-max_keep]]
    base_q = shlex.quote(REMOTE_SNAPSHOTS_DIR)
    del_str = " ".join(shlex.quote(n) for n in to_delete)
    log(f"prune: deleting old snapshots: {', '.join(to_delete)}")
    remote_shell(f"cd {base_q} && rm -rf -- {del_str}")


# ---------------------------------------------------------------------------
# Snapshot: build local tree per category, compress locally, upload archive
# ---------------------------------------------------------------------------

def rsync_to_local_category(category_root: Path, sources, excludes_file: str) -> None:
    category_root.mkdir(parents=True, exist_ok=True)

    for src in sources:
        src = str(src).rstrip()
        if not src:
            continue
        if not os.path.exists(src):
            log(f"skip missing snapshot source: {src}")
            continue

        cmd = [
            "rsync",
            "-aHAXR",
            "--relative",
            "--human-readable",
            f"--exclude-from={excludes_file}",
            src,
            str(category_root) + "/",
            ]
        # insert extra options after -aHAXR
        cmd[5:5] = RSYNC_EXTRA_OPTS
        run(cmd)

def compress_category_local(category_name: str, category_root: Path, tmp_root: Path,
                            remote_snapshot_dir: str) -> None:
    # If directory is empty, nothing to do
    if not category_root.exists():
        log(f"category {category_name}: no data, skipping")
        return

    any_content = False
    for _ in category_root.rglob("*"):
        any_content = True
        break
    if not any_content:
        log(f"category {category_name}: empty tree, skipping")
        return

    mode = COMPRESSION_MODE
    if mode == "none":
        archive_name = f"{category_name}.tar"
    else:
        archive_name = f"{category_name}.tar.zst"

    archive_path = tmp_root / archive_name

    if mode == "none":
        # plain tar, no compression
        cmd = [
            "tar",
            "-cf", str(archive_path),
            "--ignore-failed-read",
            "-C", str(category_root),
            ".",
        ]
        run(cmd)
    else:
        level = 19 if mode == "high" else 3
        # Use shell for tar | zstd pipeline
        shell_cmd = (
            f"cd {shlex.quote(str(category_root))} && "
            f"tar -cf - --ignore-failed-read . "
            f"| zstd -T0 -{level} -o {shlex.quote(str(archive_path))}"
        )
        run(["sh", "-c", shell_cmd])

    log(f"category {category_name}: archive created at {archive_path}")

    # Upload archive to remote snapshot dir
    rsync_cmd = [
        "rsync",
        "-a",
        "--human-readable",
        "-e",
        f"ssh -i {SSH_KEY} -p {SSH_PORT} -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
        str(archive_path),
        f"{SSH_USER}@{SSH_HOST}:{remote_snapshot_dir}/",
    ]
    # insert extra opts after -a
    rsync_cmd[3:3] = RSYNC_EXTRA_OPTS
    run(rsync_cmd)

    log(f"category {category_name}: archive uploaded to {remote_snapshot_dir}")


# ---------------------------------------------------------------------------
# Single-copy mirrors (unchanged)
# ---------------------------------------------------------------------------

def rsync_single_copy(src: str, dest_remote: str, excludes_file: str) -> None:
    src = src.rstrip("/")
    if not os.path.exists(src):
        log(f"skip missing single-copy src: {src}")
        return

    dest_remote = dest_remote.rstrip("/")
    remote_shell(f"mkdir -p {shlex.quote(dest_remote)}")

    cmd = [
        "rsync",
        "-aHAX",
        "--delete",
        "--human-readable",
        f"--exclude-from={excludes_file}",
        "-e",
        f"ssh -i {SSH_KEY} -p {SSH_PORT} -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
        f"{src}/",
        f"{SSH_USER}@{SSH_HOST}:{dest_remote}/",
    ]
    cmd[4:4] = RSYNC_EXTRA_OPTS
    run(cmd)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log("==== arch-rsync-backup (local compression): start ====")
    ensure_remote_snapshot_root()

    snap = snapshot_name()
    remote_snapshot_dir = f"{REMOTE_SNAPSHOTS_DIR}/{snap}"
    remote_shell(f"mkdir -p {shlex.quote(remote_snapshot_dir)}")
    log(f"snapshot: {snap} -> {remote_snapshot_dir}")

    excludes_file = build_excludes_file()

    tmp_root = Path(
        tempfile.mkdtemp(
            prefix=f"arch-rsync-backup-{snap}-",
            dir=str(COMPRESSION_ROOT),
        )
    )

    try:
        # 1) Build local category trees
        system_root = tmp_root / "system"
        user_dirs_root = tmp_root / "user-dirs"
        user_files_root = tmp_root / "user-files"

        rsync_to_local_category(system_root, INCLUDE_PATHS, excludes_file)
        rsync_to_local_category(user_dirs_root, USER_INCLUDE_DIRS, excludes_file)
        rsync_to_local_category(user_files_root, USER_INCLUDE_FILES, excludes_file)

        # 2) Compress locally and upload archives
        compress_category_local("system", system_root, tmp_root, remote_snapshot_dir)
        compress_category_local("user-dirs", user_dirs_root, tmp_root, remote_snapshot_dir)
        compress_category_local("user-files", user_files_root, tmp_root, remote_snapshot_dir)

        # 3) Single-copy mirrors (unchanged)
        for mapping in SINGLE_COPY_MAPPINGS:
            if not mapping:
                continue
            if "|" not in mapping:
                log(f"skip malformed mapping (no '|'): {mapping}")
                continue
            src, dest = mapping.split("|", 1)
            src = src.strip()
            dest = dest.strip()
            if not src or not dest:
                log(f"skip malformed mapping (empty src/dest): {mapping}")
                continue
            rsync_single_copy(src, dest, excludes_file)

        # 4) Retention
        prune_old_snapshots()

        log(f"Backup complete: {snap}")
    finally:
        try:
            os.remove(excludes_file)
        except FileNotFoundError:
            pass
        # clean up local temp tree
        shutil.rmtree(tmp_root, ignore_errors=True)
        log("==== arch-rsync-backup: end ====")


if __name__ == "__main__":
    main()
