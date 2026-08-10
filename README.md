# tmpfs

Declarative multi-disk RAM disk manager for macOS. Replaces `mount-tmp` and
`ramdisk.sh` with a single tool driven by `~/.config/tmpfs/tmpfs.toml`.

## Usage

```sh
uv run tmpfs add NAME       # wizard: define a new ramdisk + files to symlink into it
uv run tmpfs mount [NAME]   # mount all (or one) configured disks, reconcile links
uv run tmpfs unmount [NAME] # eject a disk; --restore copies files back before ejecting
uv run tmpfs list [NAME]    # show configured disks, live mount state, link health
uv run tmpfs remove NAME    # unmount (unless --keep-mounted) and drop from config
uv run tmpfs orphan         # find ram:// devices not tracked by any configured disk
```

Or install it on `$PATH` with `uv tool install .`.

## Config

Each `[[disks]]` entry has a `name`, `size_mb`, `filesystem` (`hfs` only for
now), an optional `mount_point` (defaults to `/private/tmp/<name>`), and a
list of `[[disks.links]]` — `source` (real path) / `target` (relative path
inside the ramdisk) pairs that get moved onto the ramdisk and symlinked back
on `mount`.
