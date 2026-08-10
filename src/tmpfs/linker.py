from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from tmpfs.config import LinkEntry

if TYPE_CHECKING:
    from pathlib import Path

    from tmpfs.config import DiskConfig


class LinkStatus(StrEnum):
    OK = "ok"
    LINKED = "linked"
    REPAIRED = "repaired"
    REDIRECTED = "redirected"
    WOULD_LINK = "would_link"
    WOULD_REPAIR = "would_repair"
    WOULD_REDIRECT = "would_redirect"
    MISSING = "missing"
    CONFLICT = "conflict"
    AMBIGUOUS = "ambiguous"


@dataclass
class LinkResult:
    link: LinkEntry
    status: LinkStatus
    detail: str


def reconcile_link(
    disk: DiskConfig, link: LinkEntry, dry_run: bool = False
) -> LinkResult:
    source = link.source
    ramdisk_target = disk.resolved_mount_point / link.target

    if source.is_symlink():
        if source.readlink() == ramdisk_target:
            if ramdisk_target.exists():
                return LinkResult(
                    link,
                    LinkStatus.OK,
                    f"'{source}' already links to '{ramdisk_target}'",
                )
            return LinkResult(
                link,
                LinkStatus.MISSING,
                f"'{source}' points at '{ramdisk_target}' but the file no longer "
                "exists there (data lost, e.g. the disk was recreated)",
            )

        if not ramdisk_target.exists():
            return LinkResult(
                link,
                LinkStatus.AMBIGUOUS,
                f"'{source}' is a stale symlink and '{ramdisk_target}' does not exist; leaving as-is",
            )

        if dry_run:
            return LinkResult(
                link, LinkStatus.WOULD_REPAIR, f"'{source}' -> '{ramdisk_target}'"
            )

        source.unlink()
        source.symlink_to(ramdisk_target)
        return LinkResult(
            link, LinkStatus.REPAIRED, f"'{source}' -> '{ramdisk_target}'"
        )

    if not source.exists():
        if ramdisk_target.exists():
            if dry_run:
                return LinkResult(
                    link, LinkStatus.WOULD_LINK, f"'{source}' -> '{ramdisk_target}'"
                )
            source.symlink_to(ramdisk_target)
            return LinkResult(
                link,
                LinkStatus.LINKED,
                f"'{source}' -> '{ramdisk_target}' (data already present)",
            )

        return LinkResult(
            link,
            LinkStatus.MISSING,
            f"'{source}' does not exist and no ramdisk copy was found",
        )

    if ramdisk_target.exists():
        return LinkResult(
            link,
            LinkStatus.CONFLICT,
            f"both '{source}' and '{ramdisk_target}' exist; refusing to overwrite either",
        )

    if dry_run:
        return LinkResult(
            link, LinkStatus.WOULD_REDIRECT, f"'{source}' -> '{ramdisk_target}'"
        )

    ramdisk_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(ramdisk_target))
    source.symlink_to(ramdisk_target)
    return LinkResult(link, LinkStatus.REDIRECTED, f"'{source}' -> '{ramdisk_target}'")


def reconcile_links(disk: DiskConfig, dry_run: bool = False) -> list[LinkResult]:
    return [reconcile_link(disk, link, dry_run=dry_run) for link in disk.links]


def find_links_into(scan_dir: Path, mount_point: Path) -> list[LinkEntry]:
    """Find symlinks under scan_dir that point into mount_point, as LinkEntry pairs."""
    found: list[LinkEntry] = []
    for entry in sorted(scan_dir.rglob("*")):
        if not entry.is_symlink():
            continue

        target = entry.readlink()
        if not target.is_absolute():
            target = entry.parent / target

        try:
            relative = target.relative_to(mount_point)
        except ValueError:
            continue

        found.append(LinkEntry(source=entry, target=str(relative)))
    return found
