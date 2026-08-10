from __future__ import annotations

import shutil
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.filesize import decimal
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from tmpfs import config as config_module
from tmpfs import system
from tmpfs.config import (
    DiskConfig,
    Filesystem,
    LinkEntry,
    TmpfsConfig,
    add_disk,
    load_config,
    mount_options_from_tokens,
    remove_disk,
    replace_disk,
    save_config,
)
from tmpfs.errors import TmpfsError
from tmpfs.linker import LinkStatus, find_links_into, reconcile_link, reconcile_links
from tmpfs.wizard import disk_summary_lines, run_add_wizard

app = typer.Typer(no_args_is_help=True)
console = Console()


def _select_disks(config: TmpfsConfig, name: str | None) -> list[DiskConfig]:
    if name is None:
        return config.disks
    disk = config.get(name)
    if disk is None:
        console.print(f"[red]Error:[/red] no disk named '{name}' in the config.")
        raise typer.Exit(1)
    return [disk]


def _mount_disk(disk: DiskConfig) -> None:
    if disk.filesystem is not Filesystem.HFS:
        console.print(
            f"[red]{disk.name}:[/red] filesystem '{disk.filesystem}' is not yet implemented."
        )
        raise typer.Exit(1)

    if system.is_mounted(disk.resolved_mount_point):
        console.print(
            f"[cyan]{disk.name}:[/cyan] already mounted at {disk.resolved_mount_point}"
        )
        return

    device = None
    try:
        with console.status(f"[{disk.name}] allocating {disk.size_mb}MB RAM device..."):
            device = system.attach_ram_device(disk.size_mb)

        with console.status(f"[{disk.name}] formatting {device}..."):
            system.format_hfs(device, disk.name)

        with console.status(
            f"[{disk.name}] mounting at {disk.resolved_mount_point}..."
        ):
            system.mount_hfs(
                device, disk.resolved_mount_point, disk.options.to_mount_flags()
            )
    except TmpfsError as exc:
        if device is not None:
            system.detach_device(device)
        console.print(f"[red]{disk.name}:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print(
        f"[green]{disk.name}:[/green] mounted ({disk.size_mb}MB) at {disk.resolved_mount_point}"
    )


def _print_link_results(disk: DiskConfig) -> None:
    for result in reconcile_links(disk):
        color = "green" if result.status == LinkStatus.OK else "yellow"
        if result.status in {
            LinkStatus.MISSING,
            LinkStatus.CONFLICT,
            LinkStatus.AMBIGUOUS,
        }:
            color = "red"
        console.print(f"  [{color}]{result.status.value}:[/{color}] {result.detail}")


@app.command()
def mount(
    name: str | None = typer.Argument(
        None, help="Disk name; omit to mount all configured (enabled) disks"
    ),
    login: bool = typer.Option(
        False,
        "--login",
        hidden=True,
        help="Mount only disks with on_login=true (used by the login LaunchAgent)",
    ),
) -> None:
    """Mount configured RAM disks and reconcile their links."""
    config = load_config()

    if login:
        disks = [d for d in config.disks if d.enabled and d.on_login]
        if not disks:
            return
    else:
        disks = _select_disks(config, name)
        if not disks:
            console.print("No disks configured. Use `tmpfs add NAME` first.")
            raise typer.Exit(1)
        if name is None:
            for disk in disks:
                if not disk.enabled:
                    console.print(f"[dim]{disk.name}: disabled, skipping[/dim]")
            disks = [d for d in disks if d.enabled]

    for disk in disks:
        _mount_disk(disk)
        _print_link_results(disk)


def _options_drifted(current_tokens: set[str], desired: DiskConfig) -> bool:
    # nodev/nosuid are ignored: macOS forces them on non-root local mounts
    # regardless of requested flags, so they can't be reliably diffed live.
    current = mount_options_from_tokens(current_tokens)
    desired_options = desired.options
    return (
        current.hidden != desired_options.hidden
        or current.access_times != desired_options.access_times
        or current.allow_exec != desired_options.allow_exec
        or current.read_only != desired_options.read_only
    )


def _apply_disk(disk: DiskConfig, recreate: bool, yes: bool, force: bool) -> None:
    device = system.mounted_device(disk.resolved_mount_point)
    if device is None:
        console.print(
            f"[cyan]{disk.name}:[/cyan] not mounted, nothing to apply "
            f"(run `tmpfs mount {disk.name}` to bring it up)"
        )
        return

    current_size = system.ram_device_size_mb(device)
    entry = system.mount_entry(disk.resolved_mount_point)
    current_fstype = entry[1] if entry else None
    current_tokens = entry[2] if entry else set()

    needs_recreate = (current_size is not None and current_size != disk.size_mb) or (
        current_fstype is not None and current_fstype != disk.filesystem.value
    )

    if needs_recreate:
        if disk.filesystem is not Filesystem.HFS:
            console.print(
                f"[red]{disk.name}:[/red] filesystem '{disk.filesystem}' is not yet implemented."
            )
            return

        message = (
            f"{disk.name}: size/filesystem changed ({current_size}MB {current_fstype} -> "
            f"{disk.size_mb}MB {disk.filesystem.value}). Recreating destroys current RAM "
            "contents not covered by tracked links."
        )
        if not recreate:
            console.print(
                f"[yellow]{message} Skipping (rerun with --recreate to apply).[/yellow]"
            )
            return

        if not yes and not Confirm.ask(f"{message} Recreate now?", default=False):
            console.print(f"[cyan]{disk.name}:[/cyan] skipped")
            return

        try:
            with console.status(f"[{disk.name}] recreating..."):
                system.eject_mount_point(disk.resolved_mount_point, force=force)
                new_device = system.attach_ram_device(disk.size_mb)
                system.format_hfs(new_device, disk.name)
                system.mount_hfs(
                    new_device, disk.resolved_mount_point, disk.options.to_mount_flags()
                )
        except TmpfsError as exc:
            console.print(f"[red]{disk.name}:[/red] {exc}")
            return

        console.print(f"[green]{disk.name}:[/green] recreated ({disk.size_mb}MB)")
        _print_link_results(disk)
        return

    if _options_drifted(current_tokens, disk):
        try:
            with console.status(f"[{disk.name}] remounting with updated options..."):
                system.unmount_only(disk.resolved_mount_point, force=force)
                system.mount_hfs(
                    device, disk.resolved_mount_point, disk.options.to_mount_flags()
                )
        except TmpfsError as exc:
            console.print(f"[red]{disk.name}:[/red] {exc}")
            return
        console.print(f"[green]{disk.name}:[/green] remounted with updated options")

    _print_link_results(disk)


@app.command()
def apply(
    name: str | None = typer.Argument(
        None, help="Disk name; omit to apply to all configured disks"
    ),
    recreate: bool = typer.Option(
        False,
        "--recreate",
        help="Allow recreating mounted disks whose size or filesystem changed "
        "(destroys current RAM contents not covered by tracked links)",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation before recreating a disk"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force unmount even if files are open (may corrupt in-progress writes)",
    ),
) -> None:
    """Reconcile already-mounted disks with the current config (size, options, links)."""
    config = load_config()
    disks = _select_disks(config, name)
    if not disks:
        console.print("No disks configured. Use `tmpfs add NAME` first.")
        raise typer.Exit(1)

    for disk in disks:
        _apply_disk(disk, recreate=recreate, yes=yes, force=force)


def _unmount_disk(
    disk: DiskConfig, restore: bool | None = None, force: bool = False
) -> None:
    if not system.is_mounted(disk.resolved_mount_point):
        console.print(f"[cyan]{disk.name}:[/cyan] not mounted")
        return

    restore = disk.restore if restore is None else restore

    dangling: list[Path] = []
    for link in disk.links:
        source = link.source
        ramdisk_target = disk.resolved_mount_point / link.target
        if not source.is_symlink():
            continue

        if restore:
            if ramdisk_target.exists():
                source.unlink()
                shutil.move(str(ramdisk_target), str(source))
            else:
                dangling.append(source)
        else:
            dangling.append(source)

    try:
        with console.status(f"[{disk.name}] ejecting {disk.resolved_mount_point}..."):
            system.eject_mount_point(disk.resolved_mount_point, force=force)
    except TmpfsError as exc:
        console.print(f"[red]{disk.name}:[/red] {exc}")
        if not force:
            console.print(
                "  Rerun with --force to unmount even if files are open (may corrupt in-progress writes)."
            )
        raise typer.Exit(1) from exc

    console.print(f"[green]{disk.name}:[/green] unmounted")
    if dangling and not restore:
        console.print(
            f"  [yellow]warning:[/yellow] {len(dangling)} symlink(s) now dangling:"
        )
        for path in dangling:
            console.print(f"    {path}")


def _unmount(name: str | None, restore: bool | None, force: bool) -> None:
    config = load_config()
    disks = _select_disks(config, name)
    if not disks:
        console.print("No disks configured.")
        raise typer.Exit(1)
    for disk in disks:
        _unmount_disk(disk, restore, force)


@app.command(name="unmount")
def unmount_cmd(
    name: str | None = typer.Argument(None),
    restore: bool | None = typer.Option(
        None,
        "--restore/--no-restore",
        help="Copy files back to their original location before ejecting "
        "(defaults to each disk's configured `restore` setting)",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force unmount even if files are open (may corrupt in-progress writes)",
    ),
) -> None:
    """Unmount configured RAM disks."""
    _unmount(name, restore, force)


@app.command(name="umount", hidden=True)
def umount_cmd(
    name: str | None = typer.Argument(None),
    restore: bool | None = typer.Option(None, "--restore/--no-restore"),
    force: bool = typer.Option(False, "--force", "-f"),
) -> None:
    """Alias for `unmount`."""
    _unmount(name, restore, force)


@app.command()
def add(name: str = typer.Argument(..., help="Name for the new ramdisk")) -> None:
    """Interactively define a new named ramdisk."""
    config = load_config()
    run_add_wizard(name, config)


@app.command(name="import")
def import_disk(
    mount_point: Path = typer.Argument(
        ..., help="Already-mounted ramdisk to import, e.g. /private/tmp/tmpfs"
    ),
    scan_dir: Path = typer.Argument(
        ...,
        help="Directory to scan for symlinks pointing into the ramdisk, e.g. ~/.codex",
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Name for the imported disk (default: mount point's basename)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Import an already-mounted ramdisk (e.g. from mount-tmp/ramdisk.sh) into the config."""
    mount_point = mount_point.expanduser()
    scan_dir = scan_dir.expanduser()

    entry = system.mount_entry(mount_point)
    if entry is None:
        console.print(f"[red]Error:[/red] '{mount_point}' is not currently mounted.")
        raise typer.Exit(1)
    device, fstype, mount_opts = entry

    try:
        filesystem = Filesystem(fstype)
    except ValueError:
        console.print(f"[red]Error:[/red] unsupported filesystem '{fstype}'.")
        raise typer.Exit(1) from None

    size_mb = system.ram_device_size_mb(device)
    if size_mb is None:
        console.print(
            f"[yellow]Warning:[/yellow] '{device}' isn't a ram:// backed device; "
            "estimating size from current disk usage."
        )
        size_mb = max(1, shutil.disk_usage(mount_point).total // (1024 * 1024))

    options = mount_options_from_tokens(mount_opts)

    disk_name = name or mount_point.name
    config = load_config()
    if config.get(disk_name) is not None:
        console.print(
            f"[red]Error:[/red] a disk named '{disk_name}' already exists. Use --name to choose another."
        )
        raise typer.Exit(1)

    if not scan_dir.is_dir():
        console.print(f"[red]Error:[/red] '{scan_dir}' is not a directory.")
        raise typer.Exit(1)

    links = find_links_into(scan_dir, mount_point)

    disk = DiskConfig(
        name=disk_name,
        size_mb=size_mb,
        filesystem=filesystem,
        mount_point=mount_point,
        options=options,
        links=links,
    )

    summary = "\n".join(disk_summary_lines(disk))
    console.print(Panel(summary, title=f"Imported disk: {disk_name}"))

    if not yes and not Confirm.ask("Save this configuration?", default=True):
        console.print("Aborted, nothing was written.")
        raise typer.Exit(0)

    updated = add_disk(config, disk)
    save_config(updated)
    console.print(f"[green]Saved.[/green] '{disk_name}' added to the config.")


def _format_bool(value: bool | None) -> str:
    if value is None:
        return "-"
    return "[green]yes[/green]" if value else "[red]no[/red]"


@app.command()
def info(name: str = typer.Argument(..., help="Disk name")) -> None:
    """Show detailed info for one configured ramdisk, including its mount options."""
    config = load_config()
    disk = config.get(name)
    if disk is None:
        console.print(f"[red]Error:[/red] no disk named '{name}' in the config.")
        raise typer.Exit(1)

    device = system.mounted_device(disk.resolved_mount_point)
    mounted = device is not None
    entry = system.mount_entry(disk.resolved_mount_point) if mounted else None
    current_fstype = entry[1] if entry else None
    live_options = mount_options_from_tokens(entry[2]) if entry else None

    actual_size_mb = system.ram_device_size_mb(device) if device else None
    actual_size = system.get_disk_size(disk.resolved_mount_point) if mounted else None

    summary_lines = [
        f"name: {disk.name}",
        f"device: {device.removeprefix('/dev/') if device else '-'}",
        f"mount_point: {disk.resolved_mount_point}",
        f"mounted: {'yes' if mounted else 'no'}",
        f"configured size: {disk.size_mb}MB",
        "actual size: "
        + (f"{actual_size_mb}MB" if actual_size_mb is not None else "-")
        + (f" ({actual_size})" if actual_size else ""),
        f"filesystem: configured={disk.filesystem.value} live={current_fstype or '-'}",
        f"restore on unmount: {disk.restore}",
        f"enabled: {disk.enabled}",
        f"on_login: {disk.on_login}",
        f"links: {len(disk.links)}",
    ]
    console.print(Panel("\n".join(summary_lines), title=f"tmpfs disk: {name}"))

    table = Table(title="Mount Options")
    table.add_column("Option")
    table.add_column("Configured")
    table.add_column("Live")

    rows = [
        (
            "hidden (nobrowse)",
            disk.options.hidden,
            live_options.hidden if live_options else None,
        ),
        (
            "access_times (atime)",
            disk.options.access_times,
            live_options.access_times if live_options else None,
        ),
        ("allow_dev (nodev)", disk.options.allow_dev, None),
        ("allow_setuid (nosuid)", disk.options.allow_setuid, None),
        (
            "allow_exec (noexec)",
            disk.options.allow_exec,
            live_options.allow_exec if live_options else None,
        ),
        (
            "read_only (rdonly)",
            disk.options.read_only,
            live_options.read_only if live_options else None,
        ),
    ]
    for label, configured, live in rows:
        table.add_row(label, _format_bool(configured), _format_bool(live))

    console.print(table)
    if mounted:
        console.print(
            "[dim]Note: allow_dev/allow_setuid can't be read back from a live mount reliably -- "
            "macOS forces nodev/nosuid on non-root local mounts regardless of these flags.[/dim]"
        )


@app.command(name="list")
def list_disks(name: str | None = typer.Argument(None)) -> None:
    """Show configured ramdisks and their live status."""
    config = load_config()
    disks = _select_disks(config, name)
    if not disks:
        console.print("No disks configured. Use `tmpfs add NAME` first.")
        raise typer.Exit(1)

    table = Table()
    table.add_column("Name")
    table.add_column("Device")
    table.add_column("Size")
    table.add_column("FS")
    table.add_column("Mount Point")
    table.add_column("Mounted")
    table.add_column("Actual Size")
    table.add_column("Links")
    table.add_column("Flags")

    for disk in disks:
        device = system.mounted_device(disk.resolved_mount_point)
        mounted = device is not None
        actual_size = (
            system.get_disk_size(disk.resolved_mount_point) if mounted else "-"
        )
        if disk.links:
            results = reconcile_links(disk, dry_run=True)
            ok_count = sum(1 for r in results if r.status == LinkStatus.OK)
            links_summary = f"{ok_count}/{len(results)} ok"
        else:
            links_summary = "-"

        flags = []
        if not disk.enabled:
            flags.append("[red]disabled[/red]")
        if disk.on_login:
            flags.append("[cyan]on_login[/cyan]")

        table.add_row(
            disk.name,
            device.removeprefix("/dev/") if device else "-",
            f"{disk.size_mb}MB",
            disk.filesystem.value,
            str(disk.resolved_mount_point),
            "[green]yes[/green]" if mounted else "[red]no[/red]",
            actual_size or "-",
            links_summary,
            " ".join(flags) or "-",
        )

    console.print(table)


def _link_size(disk: DiskConfig, link: LinkEntry) -> str:
    for path in (link.source, disk.resolved_mount_point / link.target):
        try:
            return decimal(path.stat().st_size)
        except OSError:
            continue
    return "-"


@app.command()
def links(
    name: str | None = typer.Argument(
        None, help="Disk name; omit to show links for all configured disks"
    ),
) -> None:
    """Show configured links and their status."""
    config = load_config()
    disks = _select_disks(config, name)
    if not disks:
        console.print("No disks configured. Use `tmpfs add NAME` first.")
        raise typer.Exit(1)

    table = Table()
    table.add_column("Disk")
    table.add_column("Source")
    table.add_column("Target")
    table.add_column("Size")
    table.add_column("Status")

    any_links = False
    for disk in disks:
        for result in reconcile_links(disk, dry_run=True):
            any_links = True
            color = "green" if result.status == LinkStatus.OK else "yellow"
            if result.status in {
                LinkStatus.MISSING,
                LinkStatus.CONFLICT,
                LinkStatus.AMBIGUOUS,
            }:
                color = "red"
            table.add_row(
                disk.name,
                str(result.link.source),
                result.link.target,
                _link_size(disk, result.link),
                f"[{color}]{result.status.value}[/{color}]",
            )

    if not any_links:
        console.print("No links configured.")
        return

    console.print(table)


def _print_reconcile_result(disk: DiskConfig, link: LinkEntry) -> None:
    result = reconcile_link(disk, link)
    color = "green" if result.status == LinkStatus.OK else "yellow"
    if result.status in {LinkStatus.MISSING, LinkStatus.CONFLICT, LinkStatus.AMBIGUOUS}:
        color = "red"
    console.print(f"  [{color}]{result.status.value}:[/{color}] {result.detail}")


@app.command()
def link(
    name: str = typer.Argument(..., help="Disk name"),
    source: Path = typer.Argument(
        ..., help="Path on the real disk to link into the ramdisk"
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help="Filename inside the ramdisk (default: source's basename)",
    ),
) -> None:
    """Add a file/directory to an existing disk's links (config, and the live mount if mounted)."""
    config = load_config()
    disk = config.get(name)
    if disk is None:
        console.print(f"[red]Error:[/red] no disk named '{name}' in the config.")
        raise typer.Exit(1)

    source = source.expanduser()
    target = target or source.name

    if any(existing.source == source for existing in disk.links):
        console.print(f"[red]Error:[/red] '{source}' is already linked on '{name}'.")
        raise typer.Exit(1)
    if any(existing.target == target for existing in disk.links):
        console.print(
            f"[red]Error:[/red] target '{target}' is already used by another link on '{name}'."
        )
        raise typer.Exit(1)

    try:
        new_link = LinkEntry(source=source, target=target)
    except ValidationError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None

    updated_disk = disk.model_copy(update={"links": [*disk.links, new_link]})
    save_config(replace_disk(config, name, updated_disk))
    console.print(f"[green]Added[/green] link '{source}' -> {target} on '{name}'.")

    if system.is_mounted(updated_disk.resolved_mount_point):
        _print_reconcile_result(updated_disk, new_link)
    else:
        console.print(f"  Not mounted; run `tmpfs mount {name}` to apply.")


@app.command()
def unlink(
    name: str = typer.Argument(..., help="Disk name"),
    source: Path = typer.Argument(..., help="Source path to unlink, as configured"),
    restore: bool = typer.Option(
        False,
        "--restore",
        help="Copy the file back to its original location instead of leaving a dangling symlink",
    ),
) -> None:
    """Remove a file/directory from an existing disk's links (config, and the live mount)."""
    config = load_config()
    disk = config.get(name)
    if disk is None:
        console.print(f"[red]Error:[/red] no disk named '{name}' in the config.")
        raise typer.Exit(1)

    source = source.expanduser()
    matched = next(
        (existing for existing in disk.links if existing.source == source), None
    )
    if matched is None:
        console.print(f"[red]Error:[/red] '{source}' is not linked on '{name}'.")
        raise typer.Exit(1)

    if restore and source.is_symlink():
        ramdisk_target = disk.resolved_mount_point / matched.target
        if ramdisk_target.exists():
            source.unlink()
            shutil.move(str(ramdisk_target), str(source))
            console.print(f"[green]Restored[/green] '{source}' from the ramdisk.")
        else:
            console.print(
                f"[yellow]Warning:[/yellow] '{ramdisk_target}' does not exist; nothing to restore."
            )

    updated_disk = disk.model_copy(
        update={
            "links": [existing for existing in disk.links if existing.source != source]
        }
    )
    save_config(replace_disk(config, name, updated_disk))
    console.print(f"[green]Removed[/green] link '{source}' from '{name}'.")


@app.command()
def opened(
    name: str | None = typer.Argument(
        None, help="Disk name; omit to check links for all configured disks"
    ),
) -> None:
    """List processes that have this disk's linked files open."""
    config = load_config()
    disks = _select_disks(config, name)
    if not disks:
        console.print("No disks configured. Use `tmpfs add NAME` first.")
        raise typer.Exit(1)

    table = Table()
    table.add_column("Disk")
    table.add_column("Link")
    table.add_column("PID", justify="right")
    table.add_column("Process")
    table.add_column("User")
    table.add_column("Opened Path")

    any_links = False
    for disk in disks:
        for link in disk.links:
            any_links = True
            candidate = disk.resolved_mount_point / link.target
            if not candidate.exists():
                candidate = link.source

            found = False
            for opened_by in system.processes_with_open_file(candidate):
                found = True
                table.add_row(
                    disk.name,
                    link.target,
                    str(opened_by.pid),
                    opened_by.name,
                    opened_by.username,
                    opened_by.opened_path,
                )
            if not found:
                table.add_row(disk.name, link.target, "[red]None[/red]", "", "", "")

    if not any_links:
        console.print("No links configured.")
        return

    console.print(table)


@app.command()
def remove(
    name: str,
    keep_mounted: bool = typer.Option(
        False, "--keep-mounted", help="Don't unmount before removing"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force unmount even if files are open (may corrupt in-progress writes)",
    ),
) -> None:
    """Remove a ramdisk from the config."""
    config = load_config()
    disk = config.get(name)
    if disk is None:
        console.print(f"[red]Error:[/red] no disk named '{name}' in the config.")
        raise typer.Exit(1)

    if not yes and not typer.confirm(
        f"Remove '{name}' from the config?", default=False
    ):
        raise typer.Exit(0)

    if not keep_mounted and system.is_mounted(disk.resolved_mount_point):
        _unmount_disk(disk, force=force)

    updated = remove_disk(config, name)
    save_config(updated)
    console.print(f"[green]Removed[/green] '{name}' from the config.")


@app.command()
def rename(
    old_name: str,
    new_name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Rename a configured ramdisk, relocating its mount point and repairing links if live."""
    config = load_config()
    disk = config.get(old_name)
    if disk is None:
        console.print(f"[red]Error:[/red] no disk named '{old_name}' in the config.")
        raise typer.Exit(1)
    if config.get(new_name) is not None:
        console.print(f"[red]Error:[/red] a disk named '{new_name}' already exists.")
        raise typer.Exit(1)

    old_mount_point = disk.resolved_mount_point
    default_old_mount_point = config_module.DEFAULT_MOUNT_ROOT / old_name
    if old_mount_point == default_old_mount_point:
        new_mount_point = config_module.DEFAULT_MOUNT_ROOT / new_name
    else:
        new_mount_point = old_mount_point

    relocating = new_mount_point != old_mount_point
    message = f"Rename '{old_name}' -> '{new_name}'"
    if relocating:
        message += f" (mount point {old_mount_point} -> {new_mount_point})"
    if not yes and not Confirm.ask(f"{message}?", default=True):
        raise typer.Exit(0)

    new_disk = disk.model_copy(
        update={"name": new_name, "mount_point": new_mount_point}
    )

    device = system.mounted_device(old_mount_point)
    if device is not None and relocating:
        try:
            with console.status(
                f"relocating {old_mount_point} -> {new_mount_point}..."
            ):
                system.unmount_only(old_mount_point)
                system.mount_hfs(
                    device, new_mount_point, new_disk.options.to_mount_flags()
                )
                system.rename_volume(device, new_name)
        except TmpfsError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc

        for result in reconcile_links(new_disk):
            console.print(f"  [cyan]{result.status.value}:[/cyan] {result.detail}")

    updated = replace_disk(config, old_name, new_disk)
    save_config(updated)
    console.print(f"[green]Renamed[/green] '{old_name}' -> '{new_name}'.")


@app.command()
def orphan(
    detach: bool = typer.Option(False, "--detach", help="Detach orphaned RAM devices"),
) -> None:
    """List RAM-backed devices not tracked by any configured disk."""
    config = load_config()
    known_mount_points = [disk.resolved_mount_point for disk in config.disks]
    orphaned = system.find_orphaned_ram_devices(known_mount_points)

    if not orphaned:
        console.print("No orphaned RAM devices found.")
        return

    table = Table()
    table.add_column("Device")
    table.add_column("Size")
    table.add_column("Filesystem")
    table.add_column("Mount Point")
    table.add_column("Mounted")

    for device in orphaned:
        size_mb = system.ram_device_size_mb(device)
        mount_point = system.device_mount_point(device)
        fstype = "-"
        if mount_point is not None:
            entry = system.mount_entry(mount_point)
            if entry is not None:
                _, fstype, _ = entry

        table.add_row(
            device,
            f"{size_mb}MB" if size_mb is not None else "-",
            fstype,
            str(mount_point) if mount_point else "-",
            "[green]yes[/green]" if mount_point else "[red]no[/red]",
        )
    console.print(table)

    if detach:
        for device in orphaned:
            system.detach_device(device)
            console.print(f"[green]Detached[/green] {device}")


@app.command()
def install() -> None:
    """Install a LaunchAgent that mounts on_login disks at user login."""
    try:
        executable = system.uv_tool_executable("tmpfs")
    except TmpfsError as exc:
        console.print(
            f"[red]Error:[/red] {exc} Install it first with `uv tool install .` "
            "from the project directory, then rerun `tmpfs install`."
        )
        raise typer.Exit(1) from exc

    log_dir = Path.home() / "Library" / "Logs" / "tmpfs"
    try:
        path = system.write_launch_agent(executable, log_dir)
        system.load_launch_agent(path)
    except TmpfsError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc

    on_login_disks = [d.name for d in load_config().disks if d.on_login]
    console.print(f"[green]Installed[/green] LaunchAgent at {path}")
    console.print(f"Bin location: {executable}")
    if on_login_disks:
        console.print(f"Will mount on login: {', '.join(on_login_disks)}")
    else:
        console.print(
            "No disks have on_login = true yet -- nothing will mount at login "
            "until you set it (e.g. edit the config, or `tmpfs add` a new disk)."
        )


@app.command()
def uninstall() -> None:
    """Remove the login LaunchAgent installed by `tmpfs install`."""
    path = system.launch_agent_path()
    system.unload_launch_agent()
    if path.exists():
        path.unlink()
        console.print(f"[green]Removed[/green] {path}")
    else:
        console.print("LaunchAgent was not installed.")


if __name__ == "__main__":
    app()
