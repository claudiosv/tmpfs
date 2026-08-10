from __future__ import annotations

import functools
import os
import plistlib
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psutil
from rich.filesize import decimal

from tmpfs.errors import (
    AttachError,
    FormatError,
    LaunchAgentError,
    MountError,
    UnmountError,
)

SECTOR_SIZE = 512
BYTES_PER_MIB = 1024 * 1024
SECTORS_PER_MIB = BYTES_PER_MIB // SECTOR_SIZE  # 2048

LAUNCH_AGENT_LABEL = "local.tmpfs.login-mount"


@functools.cache
def _resolve(executable: str) -> str:
    return shutil.which(executable) or executable


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    resolved = [_resolve(cmd[0]), *cmd[1:]]
    return subprocess.run(resolved, capture_output=True, text=True, check=False)


def attach_ram_device(size_mb: int) -> str:
    sectors = size_mb * SECTORS_PER_MIB
    result = _run([
        "diskutil",
        "image",
        "--plist",
        "attach",
        "--noMount",
        f"ram://{sectors}",
    ])

    device = None
    if result.returncode == 0 and result.stdout:
        try:
            data = plistlib.loads(result.stdout.encode())
        except plistlib.InvalidFileException:
            data = {}
        entities = data.get("system-entities", [])
        if entities:
            dev_entry = entities[0].get("dev-entry")
            if dev_entry:
                device = (
                    dev_entry if dev_entry.startswith("/dev/") else f"/dev/{dev_entry}"
                )

    if result.returncode != 0 or not device:
        msg = f"failed to allocate RAM disk: {result.stderr.strip()}"
        raise AttachError(msg)
    return device


def format_hfs(device: str, volume_name: str) -> None:
    result = _run(["newfs_hfs", "-v", volume_name, device])
    if result.returncode != 0:
        msg = f"failed to format '{device}': {result.stderr.strip()}"
        raise FormatError(msg)


def mount_hfs(
    device: str, mount_point: Path, option_flags: list[str] | None = None
) -> None:
    mount_point.mkdir(parents=True, exist_ok=True)
    result = _run([
        "mount",
        "-t",
        "hfs",
        *(option_flags or []),
        device,
        str(mount_point),
    ])
    if result.returncode != 0:
        msg = f"failed to mount '{device}' at '{mount_point}': {result.stderr.strip()}"
        raise MountError(msg)


def detach_device(device: str, force: bool = True) -> None:
    cmd = ["hdiutil", "detach", device]
    if force:
        cmd.append("-force")
    _run(cmd)


def _mount_line(mount_point: Path) -> str | None:
    result = _run(["mount"])
    needle = f" on {mount_point} "
    for line in result.stdout.splitlines():
        if needle in line:
            return line
    return None


def mounted_device(mount_point: Path) -> str | None:
    line = _mount_line(mount_point)
    return line.split(" on ", 1)[0] if line else None


def mount_entry(mount_point: Path) -> tuple[str, str, set[str]] | None:
    """Return (device, filesystem type, mount options) for a mounted path, as reported by `mount`."""
    line = _mount_line(mount_point)
    if line is None:
        return None
    device, _, remainder = line.partition(" on ")
    _, _, paren = remainder.partition("(")
    inside = paren.rsplit(")", 1)[0]
    parts = [p.strip() for p in inside.split(",")]
    fstype = parts[0] if parts else ""
    options = {p for p in parts[1:] if not p.startswith("mounted by")}
    return device, fstype, options


def unmount_only(mount_point: Path, force: bool = False) -> None:
    """Unmount a path without detaching its backing device (data survives)."""
    cmd = ["umount", "-f", str(mount_point)] if force else ["umount", str(mount_point)]
    result = _run(cmd)
    if result.returncode != 0:
        msg = f"failed to unmount '{mount_point}': {result.stderr.strip()}"
        raise UnmountError(msg)


def eject_mount_point(mount_point: Path, force: bool = False) -> None:
    device = mounted_device(mount_point)
    unmount_only(mount_point, force=force)
    if device is not None:
        detach_device(device)


def rename_volume(device: str, volume_name: str) -> None:
    """Best-effort: relabel the HFS volume on device. Purely cosmetic, failure is non-fatal."""
    _run(["diskutil", "rename", device, volume_name])


def is_mounted(mount_point: Path) -> bool:
    return mounted_device(mount_point) is not None


def get_disk_size(mount_point: Path) -> str | None:
    try:
        usage = shutil.disk_usage(mount_point)
    except OSError:
        return None
    return decimal(usage.total)


@dataclass
class RamDiskEntry:
    device: str
    blockcount: int
    mount_point: Path | None


def _ram_disk_entries() -> list[RamDiskEntry]:
    """Query every attached ram:// image via `hdiutil info -plist` (structured, not text-parsed)."""
    result = _run(["hdiutil", "info", "-plist"])
    if result.returncode != 0 or not result.stdout:
        return []
    try:
        data = plistlib.loads(result.stdout.encode())
    except plistlib.InvalidFileException:
        return []

    entries: list[RamDiskEntry] = []
    for image in data.get("images", []):
        if not str(image.get("image-path", "")).startswith("ram://"):
            continue
        blockcount = image.get("blockcount")
        if blockcount is None:
            continue
        for entity in image.get("system-entities", []):
            device = entity.get("dev-entry")
            if device is None:
                continue
            mount_point_str = entity.get("mount-point")
            mount_point = Path(mount_point_str) if mount_point_str else None
            entries.append(RamDiskEntry(device, blockcount, mount_point))
    return entries


def find_ram_devices() -> list[str]:
    """Return whole-disk identifiers (e.g. /dev/disk9) currently backed by a ram:// image."""
    return [entry.device for entry in _ram_disk_entries()]


def ram_device_size_mb(device: str) -> int | None:
    """Return the size (MB) a ram:// device was allocated with, if it is one."""
    for entry in _ram_disk_entries():
        if entry.device == device:
            return entry.blockcount // SECTORS_PER_MIB
    return None


def device_mount_point(device: str) -> Path | None:
    for entry in _ram_disk_entries():
        if entry.device == device:
            return entry.mount_point
    return None


def find_orphaned_ram_devices(known_mount_points: list[Path]) -> list[str]:
    known = set(known_mount_points)
    return [
        entry.device for entry in _ram_disk_entries() if entry.mount_point not in known
    ]


@dataclass
class OpenedBy:
    pid: int
    name: str
    username: str
    opened_path: str


def processes_with_open_file(path: Path) -> Iterator[OpenedBy]:
    """Yield an OpenedBy for every process holding a handle to the same file as path."""
    try:
        target = path.stat()
    except OSError:
        return

    for proc in psutil.process_iter():
        try:
            for opened in proc.open_files():
                try:
                    st = Path(opened.path).stat()
                except OSError:
                    continue

                if st.st_dev == target.st_dev and st.st_ino == target.st_ino:
                    yield OpenedBy(proc.pid, proc.name(), proc.username(), opened.path)
                    break
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue


def uv_tool_executable(name: str) -> Path:
    """Locate an executable installed via `uv tool install` (e.g. this tool itself)."""
    result = _run(["uv", "tool", "dir", "--bin"])
    if result.returncode != 0:
        msg = f"failed to locate the uv tool bin directory: {result.stderr.strip()}"
        raise LaunchAgentError(msg)

    path = (Path(result.stdout.strip()) / name)
    path = Path(os.path.normpath(path.absolute()))
    if not path.exists():
        msg = f"uv tool executable not found: {path}"
        raise LaunchAgentError(msg)
    return path


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def write_launch_agent(executable: Path, log_dir: Path) -> Path:
    """Write the login LaunchAgent plist. Does not load it into launchd."""
    log_dir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(executable), "mount", "--login"],
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir / "login-mount.log"),
        "StandardErrorPath": str(log_dir / "login-mount.err.log"),
    }
    path = launch_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        plistlib.dump(plist, f)
    return path


def load_launch_agent(path: Path) -> None:
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", f"{domain}/{LAUNCH_AGENT_LABEL}"])
    result = _run(["launchctl", "bootstrap", domain, str(path)])
    if result.returncode != 0:
        msg = f"failed to load launch agent: {result.stderr.strip()}"
        raise LaunchAgentError(msg)


def unload_launch_agent() -> None:
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", f"{domain}/{LAUNCH_AGENT_LABEL}"])
