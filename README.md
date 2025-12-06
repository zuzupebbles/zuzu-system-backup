# zuzu-system-backup

Python-based backup tool for Linux hosts that pushes **snapshots** and **live mirrors** to a remote NAS over SSH/rsync.

- Snapshots are compressed archives (tar + optional zstd) created on the host, then uploaded.
- Mirrors are “single-copy” rsync trees kept in sync with `--delete`.
- Configuration is per-host, in YAML.
- Scheduling is done via systemd timers.

All hostnames, users, and paths in this README use **fictional example values** so it’s safe to publish as-is. Replace them with your own.

---

## 1. High-level architecture

### 1.1 Components

- **Host script**: `zuzu-system-backup.py`
  - Runs on each Linux host.
  - Reads per-host `backup.yaml`.
  - Builds local temp trees, compresses them, and uploads archives.
  - Manages retention (max snapshot count).
- **Remote backup share** (NAS / backup server):
  - Exposed via SSH (and optionally a mounted share).
  - Receives snapshot archives and sync mirrors.

### 1.2 Snapshot vs mirror

The script supports two complementary backup modes:

1. **Snapshots** (versioned)
   - Controlled by:
     - `system.include_paths`
     - `user.include_dirs`
     - `user.include_files`
   - On each run:
     - rsync selected paths into a **local temp tree** per category.
     - compress each category into a single `*.tar` or `*.tar.zst` archive.
     - upload archives to a timestamped directory on the backup share.

2. **Single-copy mirrors** (no history)
   - Controlled by `single_copy_mappings`.
   - Each mapping is `"LOCAL_SRC|REMOTE_DEST"`.
   - rsync with `--delete` keeps the remote tree in sync with local.
   - Intended for large data (databases, model stores, etc.).

---

## 2. Data-flow diagrams

### 2.1 Backup flow on a single host

```mermaid
flowchart LR
    subgraph Host["Linux host (example: orion.example.net)"]
        A[Snapshot sources\nsystem + user] --> B[Local temp trees\nsystem / user-dirs / user-files]
        B --> C[Tar plus optional zstd\ncreate archives]
        D[Mirror sources\nsingle_copy_mappings] --> E[rsync --delete]
    end

subgraph NAS["Backup share (example: backup-nas.local)"]
C --> F[Snapshots directory\n/daily/system-orion/<timestamp>/]
E --> G[Sync mirrors\n/sync/system-orion-*]
end

````

* Snapshots: archives are compressed **locally** and only the archives are sent.
* Mirrors: rsync goes directly from host paths to remote paths, with `--delete`.

### 2.2 Control flow inside the script

```mermaid
flowchart TD
    Start([systemd timer or manual run])
--> LCFG[Load backup.yaml]
--> PREP[Compute paths and compression root]
--> SNAPROOT[Ensure remote snapshots root exists]
--> SNAPNAME[Generate snapshot name\nYYYY-MM-DD_HH-MM-SS]
--> MKDIR_REMOTE[Create remote snapshot directory]

MKDIR_REMOTE --> TMPDIR[Create local temp root]

TMPDIR --> RSYNC_SYS[rsync system.include_paths\ninto temp/system]
TMPDIR --> RSYNC_UDIR[rsync user.include_dirs\ninto temp/user-dirs]
TMPDIR --> RSYNC_UFILE[rsync user.include_files\ninto temp/user-files]

RSYNC_SYS --> COMP_SYS[Compress system category\ncreate system archive]
RSYNC_UDIR --> COMP_UDIR[Compress user-dirs category\ncreate user-dirs archive]
RSYNC_UFILE --> COMP_UFILE[Compress user-files category\ncreate user-files archive]

COMP_SYS --> UPLOAD_SYS[Upload archives to remote snapshot directory]
COMP_UDIR --> UPLOAD_UDIR
COMP_UFILE --> UPLOAD_UFILE

UPLOAD_UFILE --> MIRRORS[Process mirrors\nsingle_copy_mappings with rsync --delete]
MIRRORS --> PRUNE[Prune old snapshots\nkeep at most N]
PRUNE --> CLEANUP[Remove temp tree and excludes file]
CLEANUP --> Done([Exit])

```

---

## 3. Backup share layout

This project assumes a structured backup share on the NAS or backup server, for example:

```text
/srv/backup/
  automated/
    daily/
      system-orion/
        2025-01-01_03-00-01/
          system.tar.zst
          user-dirs.tar.zst
          user-files.tar.zst
        2025-01-02_03-00-01/
          ...
      system-pegasus/
      system-hera/

    sync/
      system-orion-databases/
        ... live rsync mirror ...
      system-orion-models/
        ...
      system-pegasus-luns/
        ...

  manual/
    installer-isos/
    http-bookmarks/
    license-keys/
    ...
```

Typical patterns:

* **automated/daily/system-<host>/**
  Date-based snapshot directories, each containing only a few archives.
* **automated/sync/system-<host>-<purpose>/**
  One dir per mirror, updated in place.
* **manual/**
  Hand-managed backups, not touched by this tool.

You can adapt the root (`/srv/backup`) and naming (`system-orion`, etc.) to match your environment.

---

## 4. Features

* Python 3 script, no shell gymnastics.
* YAML configuration, per-host.
* Snapshot categories:

    * `system.include_paths` (root-owned config and service dirs)
    * `user.include_dirs` (full user trees, relative to `user.home`)
    * `user.include_files` (one-off important files)
* Compression modes:

    * `high` (zstd `-19`)
    * `light` (zstd `-3`)
    * `none` (plain `.tar`)
* Local compression only:

    * No dependency on the NAS having zstd or GNU tar.
* Single-copy mirrors:

    * Declarative `"LOCAL_SRC|REMOTE_DEST"` mappings.
* Retention:

    * Keep at most `retention.snapshots` snapshot directories per host.
* Systemd integration:

    * Ones-shot service + timer per host.
* Logging:

    * Structured, timestamped logs via `journalctl`.

---

## 5. Dependencies

On each host:

* `python` (3.x)
* `python-yaml` (PyYAML)
* `rsync`
* `openssh`
* `tar`
* `zstd` (optional but strongly recommended if using `compression.mode: high|light`)

Example (Arch-based host):

```bash
sudo pacman -S python python-yaml rsync openssh tar zstd
```

---

## 6. Configuration (`backup.yaml`)

Each host has its own `backup.yaml` next to the script.

### 6.1 Schema overview

* `remote`: where to send backups
* `retention`: how many snapshots to keep
* `compression`: how to compress snapshot archives
* `rsync`: extra rsync flags
* `system`: system-level include paths
* `user`: user home and per-user include paths/files
* `exclude_patterns`: rsync-style excludes
* `single_copy_mappings`: one-way mirrors (no history)

### 6.2 Example `backup.yaml` (for host `orion`)

```yaml
remote:
  user: backupuser
  host: backup-nas.local
  port: 22
  key: /home/backupuser/.ssh/id_ed25519-orion
  base: /srv/backup/automated
  host_dir: system-orion

retention:
  # Max number of snapshot directories to keep on NAS
  snapshots: 7

compression:
  # high | light | none
  mode: high
  # Optional: where local temp trees and archives live
  path: /srv/tmp/backups

rsync:
  extra_opts:
    - --numeric-ids
    - --info=progress2
    - --protect-args

system:
  include_paths:
    - /etc/nftables.conf
    - /etc/snapper/configs
    - /etc/NetworkManager/system-connections
    - /etc/chromium/policies/managed
    - /etc/fstab
    - /etc/systemd/system/*.mount
    - /etc/systemd/system/*.automount
    - /etc/nut/nut.conf
    - /etc/nut/upsmon.conf

user:
  home: /home/devuser

  include_dirs:
    - .ssh
    - .gnupg
    - .local/share/wallpapers
    - projects
    - pkgbuilds
    - venvs

  include_files:
    - .config/chromium/Default/Preferences
    - .config/chromium/Default/Bookmarks
    - .config/vlc/vlcrc
    - .gitconfig
    - .bashrc
    - .bash_profile
    - .local/share/user-places.xbel

exclude_patterns:
  # Caches (generic)
  - "**/Cache/**"
  - "**/GPUCache/**"
  - "**/shadercache/**"
  - "**/ShaderCache/**"
  - "**/Code Cache/**"

  # SSH ControlMaster sockets
  - "${USER_HOME}/.ssh/ctl-*"
  - "**/.ssh/ctl-*"

  # JetBrains bulk (plugins + Toolbox app bundles)
  - "${USER_HOME}/.local/share/JetBrains/**/plugins/**"
  - "${USER_HOME}/.local/share/JetBrains/Toolbox/apps/**"
  - "${USER_HOME}/.cache/JetBrains/**"

  # Chromium bulk (we include only specific files above)
  - "${USER_HOME}/.config/chromium/**"

single_copy_mappings:
  # Example mirrors:
  - "/srv/data/postgres|/srv/backup/automated/sync/system-orion-postgres"
  - "/srv/data/models|/srv/backup/automated/sync/system-orion-models"
```

Notes:

* `user.include_dirs` and `user.include_files` are **relative to `user.home`** unless they start with `/`.
* `${USER_HOME}` and `${HOME}` in `exclude_patterns` are expanded to `user.home` by the script.
* `single_copy_mappings` paths are *not* expanded; use absolute paths.

---

## 7. Script usage

### 7.1 Manual run

From the directory where the script lives, or via its full path:

```bash
sudo /usr/local/sbin/zuzu-system-backup/zuzu-system-backup.py
```

(or whatever path you install it to)

Logs go to stderr; under systemd, they land in `journalctl`.

### 7.2 Systemd service & timer

Example service:

```ini
# /etc/systemd/system/host-backup.service
[Unit]
Description=Host backup to NAS via zuzu-system-backup
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/zuzu-system-backup/zuzu-system-backup.py
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
```

Example timer:

```ini
# /etc/systemd/system/host-backup.timer
[Unit]
Description=Nightly host backup to NAS

[Timer]
OnCalendar=*-*-* 03:15:00
RandomizedDelaySec=20min
Persistent=true

[Install]
WantedBy=timers.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now host-backup.timer
```

Check status:

```bash
systemctl list-timers 'host-backup*'
journalctl -u host-backup.service -n 50
```

---

## 8. Retention policy

Retention is implemented as “keep at most **N snapshots**” for each host:

* `retention.snapshots: 7` → keep the newest 7 snapshot directories under `REMOTE_BASE/host_dir/snapshots`.
* Snapshot directory names are timestamps: `YYYY-MM-DD_HH-MM-SS`.
* Older snapshot dirs are deleted entirely (`rm -rf` on the NAS via SSH).

No time math on `mtime`, just count-based retention by sorted timestamp name; simple and predictable.

---

## 9. Restore basics

### 9.1 Restoring from a snapshot archive

On the NAS or after copying archives locally:

```bash
# Example: restore system snapshot for 2025-01-02 from host "orion"
cd /restore/target

# If compressed
zstd -d /srv/backup/automated/daily/system-orion/2025-01-02_03-00-01/system.tar.zst -o system.tar
tar -xf system.tar

# If compression.mode was "none"
tar -xf /srv/backup/automated/daily/system-orion/2025-01-02_03-00-01/system.tar
```

Repeat for `user-dirs.tar(.zst)` and `user-files.tar(.zst)` as needed.

### 9.2 Restoring from a mirror

Mirrors are just rsynced trees; you can restore them with rsync or cp:

```bash
# rsync mirror back to host
rsync -aHAX --numeric-ids \
  backupuser@backup-nas.local:/srv/backup/automated/sync/system-orion-postgres/ \
  /srv/data/postgres/
```

Always test restores on a non-production target first.

---

## 10. Safety notes

* **Mirrors are destructive:**

    * `single_copy_mappings` use rsync `--delete`.
    * Deletes on the host will remove files on the backup side in the next run.
* **Snapshots are immutable per run:**

    * Each run creates a new directory, writes archives, and then retention may remove older snapshot dirs.
* **Local compression uses space:**

    * `compression.path` should point at a filesystem with enough free space to hold a full snapshot’s uncompressed temp trees **plus** the compressed archives.
* **Permissions:**

    * The script expects to be run as root (or with enough privileges) to read system paths and user homes.
* **SSH keys:**

    * Use dedicated SSH keys per host with restricted accounts on the NAS where possible.

---
