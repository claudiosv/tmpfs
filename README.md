# tmpfs

Declarative multi-disk RAM disk manager for macOS. Replaces `mount-tmp` and
`ramdisk.sh` with a single tool driven by `~/.config/tmpfs/tmpfs.toml`.

## Install

```sh
uv tool install .
```

Or run it in place with `uv run tmpfs ...`.

## Commands

### `tmpfs add NAME`

Interactively define a new named ramdisk: prompts for size, filesystem, mount
point, and any files/directories to symlink into it.

```sh
tmpfs add scratch
```

### `tmpfs mount [NAME]`

Mount configured RAM disks and reconcile their links. Omit `NAME` to mount
every enabled disk.

```sh
tmpfs mount           # mount all enabled disks
tmpfs mount scratch   # mount just "scratch"
```

### `tmpfs apply [NAME]`

Reconcile already-mounted disks with the current config (size, mount
options, links), without a full unmount/remount unless required.

```sh
tmpfs apply                    # apply to all configured disks
tmpfs apply scratch            # apply to one disk
tmpfs apply scratch --recreate # allow destroying+remounting if size/fs changed
tmpfs apply scratch --recreate --yes   # skip the confirmation prompt
tmpfs apply scratch --force    # force-unmount even if files are open
```

Options:
- `--recreate` — allow recreating a mounted disk whose size or filesystem
  changed (destroys current RAM contents not covered by tracked links).
- `--yes` / `-y` — skip the confirmation before recreating a disk.
- `--force` / `-f` — force unmount even if files are open (may corrupt
  in-progress writes).

### `tmpfs unmount [NAME]`

Eject a disk (or all configured disks if `NAME` is omitted).

```sh
tmpfs unmount                # unmount everything configured
tmpfs unmount scratch        # unmount just "scratch"
tmpfs unmount scratch --restore      # copy files back before ejecting
tmpfs unmount scratch --no-restore   # leave dangling symlinks, don't restore
tmpfs unmount scratch --force        # force even if files are open
```

Options:
- `--restore` / `--no-restore` — copy files back to their original location
  before ejecting. Defaults to the disk's configured `restore` setting.
- `--force` / `-f` — force unmount even if files are open (may corrupt
  in-progress writes).

### `tmpfs list [NAME]`

Show configured disks, live mount state, and link health.

```sh
tmpfs list
tmpfs list scratch
```

### `tmpfs info NAME`

Show detailed info for one configured disk, including its resolved mount
options.

```sh
tmpfs info scratch
```

### `tmpfs links [NAME]`

Show configured links and their status. Omit `NAME` to show links for every
configured disk.

```sh
tmpfs links
tmpfs links scratch
```

### `tmpfs link NAME SOURCE`

Add a file/directory to an existing disk's links, both in the config and (if
the disk is mounted) live.

```sh
tmpfs link scratch ~/.cache/foo
tmpfs link scratch ~/.cache/foo --target foo-cache   # rename inside the ramdisk
```

Options:
- `--target` — filename inside the ramdisk (default: the source's basename).

### `tmpfs unlink NAME SOURCE`

Remove a file/directory from an existing disk's links, both in the config
and live.

```sh
tmpfs unlink scratch ~/.cache/foo
tmpfs unlink scratch ~/.cache/foo --restore   # copy the file back instead of leaving a dangling symlink
```

Options:
- `--restore` — copy the file back to its original location instead of
  leaving a dangling symlink.

### `tmpfs opened [NAME]`

List processes that currently have a disk's linked files open — useful
before an `unmount` or `apply --recreate`.

```sh
tmpfs opened
tmpfs opened scratch
```

### `tmpfs remove NAME`

Unmount (unless `--keep-mounted`) and drop a disk from the config.

```sh
tmpfs remove scratch
tmpfs remove scratch --keep-mounted   # drop from config, leave it mounted
tmpfs remove scratch --yes            # skip confirmation
tmpfs remove scratch --force          # force unmount even if files are open
```

Options:
- `--keep-mounted` — don't unmount before removing.
- `--yes` / `-y` — skip confirmation.
- `--force` / `-f` — force unmount even if files are open (may corrupt
  in-progress writes).

### `tmpfs rename OLD_NAME NEW_NAME`

Rename a configured disk, relocating its mount point and repairing links if
it's currently live.

```sh
tmpfs rename scratch scratch2
tmpfs rename scratch scratch2 --yes     # skip confirmation
tmpfs rename scratch scratch2 --force   # force unmount even if files are open
```

Options:
- `--yes` / `-y` — skip confirmation.
- `--force` / `-f` — force unmount even if files are open (may corrupt
  in-progress writes).

### `tmpfs import MOUNT_POINT SCAN_DIR`

Import an already-mounted ramdisk (e.g. from the legacy `mount-tmp/ramdisk.sh`
scripts) into the config, scanning `SCAN_DIR` for symlinks that point into it.

```sh
tmpfs import /private/tmp/tmpfs ~/.codex
tmpfs import /private/tmp/tmpfs ~/.codex --name legacy   # custom disk name
tmpfs import /private/tmp/tmpfs ~/.codex --yes           # skip confirmation
```

Options:
- `--name` — name for the imported disk (default: the mount point's
  basename).
- `--yes` / `-y` — skip confirmation.

### `tmpfs orphan`

List RAM-backed devices not tracked by any configured disk.

```sh
tmpfs orphan
tmpfs orphan --detach   # detach the orphaned RAM devices
```

Options:
- `--detach` — detach orphaned RAM devices.

### `tmpfs install`

Install a LaunchAgent that mounts every disk with `on_login = true` at user
login.

```sh
tmpfs install
```

### `tmpfs uninstall`

Remove the login LaunchAgent installed by `tmpfs install`.

```sh
tmpfs uninstall
```

## Config file

Location: `~/.config/tmpfs/tmpfs.toml` (created automatically the first time
you run `tmpfs add` or `tmpfs mount`).

The file is a list of `[[disks]]` tables:

```toml
[[disks]]
name = "scratch"
size_mb = 2048
filesystem = "hfs"
mount_point = "/private/tmp/scratch"
restore = false
enabled = true
on_login = true

[disks.options]
hidden = true
access_times = false
allow_dev = true
allow_setuid = true
allow_exec = true
read_only = false

[[disks.links]]
source = "/Users/me/.cache/foo"
target = "foo"

[[disks.links]]
source = "/Users/me/.npm"
target = "npm"
```

### Disk fields (`[[disks]]`)

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | — (required) | Unique disk name; also the default mount point basename. |
| `size_mb` | integer > 0 | — (required) | Disk size in megabytes. |
| `filesystem` | `"hfs"` \| `"apfs"` | `"hfs"` | Filesystem to format the disk with. Only `hfs` is fully supported; `apfs` prompts a warning in the `add` wizard. |
| `mount_point` | path | `/private/tmp/<name>` | Where the disk is mounted. |
| `restore` | bool | `false` | On unmount, copy linked files back to their original location instead of leaving dangling symlinks. Overridable per-call with `tmpfs unmount --restore/--no-restore`. |
| `enabled` | bool | `true` | If `false`, `tmpfs mount` (with no disk named explicitly) skips this disk. |
| `on_login` | bool | `false` | If `true`, the login LaunchAgent installed by `tmpfs install` mounts this disk. |
| `options` | table | see below | Mount options, see `[disks.options]`. |
| `links` | array of tables | `[]` | Files/directories moved onto the ramdisk and symlinked back, see `[[disks.links]]`. |

### Mount options (`[disks.options]`)

| Field | Type | Default | Effect when set to its non-default value |
|---|---|---|---|
| `hidden` | bool | `true` | `false` shows the volume in Finder/GUI (mounts without `nobrowse`). |
| `access_times` | bool | `false` | `true` tracks file access times (omits `noatime`; slightly slower). |
| `allow_dev` | bool | `true` | `false` disallows device special files on the volume (mounts with `nodev`). |
| `allow_setuid` | bool | `true` | `false` disallows setuid/setgid bits on the volume (mounts with `nosuid`). |
| `allow_exec` | bool | `true` | `false` disallows executing binaries from the volume (mounts with `noexec`). |
| `read_only` | bool | `false` | `true` mounts the volume read-only (`rdonly`). |

Example: a hidden, read-only, no-exec cache disk:

```toml
[[disks]]
name = "readonly-cache"
size_mb = 512

[disks.options]
allow_exec = false
read_only = true
```

### Link entries (`[[disks.links]]`)

| Field | Type | Description |
|---|---|---|
| `source` | path | Real path on disk that gets moved onto the ramdisk. |
| `target` | relative path string | Filename/path inside the ramdisk (must be relative, no `..`). A symlink is created at `source` pointing here after `mount`. |

```toml
[[disks.links]]
source = "/Users/me/.cache/pip"
target = "pip"
```
</content>
