from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt

from tmpfs.config import (
    DiskConfig,
    Filesystem,
    LinkEntry,
    MountOptions,
    TmpfsConfig,
    add_disk,
    save_config,
)

console = Console()


def disk_summary_lines(disk: DiskConfig) -> list[str]:
    link_lines = [f"  {link.source} -> {link.target}" for link in disk.links] or [
        "  (none)"
    ]
    return [
        f"name: {disk.name}",
        f"size_mb: {disk.size_mb}",
        f"filesystem: {disk.filesystem}",
        f"mount_point: {disk.resolved_mount_point}",
        f"hidden: {disk.options.hidden}",
        f"access_times: {disk.options.access_times}",
        f"restore: {disk.restore}",
        f"enabled: {disk.enabled}",
        f"on_login: {disk.on_login}",
        "links:",
        *link_lines,
    ]


def _prompt_link() -> LinkEntry | None:
    source_str = Prompt.ask("Source path (on the real disk)")
    source = Path(source_str).expanduser()

    if not source.exists() and not Confirm.ask(
        f"'{source}' does not exist yet — add it anyway?", default=False
    ):
        return None

    if source.is_symlink() and not Confirm.ask(
        f"'{source}' is already a symlink — add it anyway?", default=False
    ):
        return None

    target = Prompt.ask("Target filename inside the ramdisk", default=source.name)

    try:
        return LinkEntry(source=source, target=target)
    except ValidationError as exc:
        console.print(f"[red]Invalid link:[/red] {exc}")
        return None


def run_add_wizard(name: str, config: TmpfsConfig) -> None:
    if config.get(name) is not None:
        console.print(
            f"[red]Error:[/red] a disk named '{name}' already exists. Run `tmpfs remove {name}` first."
        )
        raise SystemExit(1)

    size_mb = IntPrompt.ask("RAM disk size (MB)", default=100)

    fs_str = Prompt.ask("Filesystem", choices=["hfs", "apfs"], default="hfs")
    if fs_str == "apfs":
        console.print(
            "[yellow]Note:[/yellow] APFS is not yet implemented by `tmpfs mount` (config only)."
        )
        if not Confirm.ask("Continue with apfs anyway?", default=False):
            fs_str = "hfs"
    filesystem = Filesystem(fs_str)

    mount_point = Path(
        Prompt.ask("Mount point", default=f"/private/tmp/{name}")
    ).expanduser()

    hidden = Confirm.ask("Hide from Finder (nobrowse)?", default=True)
    access_times = Confirm.ask(
        "Track file access times (slower; omit noatime)?", default=False
    )
    options = MountOptions(hidden=hidden, access_times=access_times)

    restore = Confirm.ask(
        "On unmount, copy linked files back to their original location "
        "(instead of leaving dangling symlinks)?",
        default=False,
    )

    on_login = Confirm.ask(
        "Mount this disk automatically at login (requires `tmpfs install`)?",
        default=False,
    )

    links: list[LinkEntry] = []
    while Confirm.ask("Add a file/directory to link into this ramdisk?", default=False):
        link = _prompt_link()
        if link is not None:
            links.append(link)

    disk = DiskConfig(
        name=name,
        size_mb=size_mb,
        filesystem=filesystem,
        mount_point=mount_point,
        options=options,
        restore=restore,
        on_login=on_login,
        links=links,
    )

    summary = "\n".join(disk_summary_lines(disk))
    console.print(Panel(summary, title=f"New disk: {name}"))

    if not Confirm.ask("Save this configuration?", default=True):
        console.print("Aborted, nothing was written.")
        return

    updated = add_disk(config, disk)
    save_config(updated)
    console.print(f"[green]Saved.[/green] Run `tmpfs mount {name}` to activate it now.")
