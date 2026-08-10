from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import tomlkit
from pydantic import BaseModel, Field, field_validator

from tmpfs.errors import ConfigError

DEFAULT_MOUNT_ROOT = Path("/private/tmp")


class Filesystem(StrEnum):
    HFS = "hfs"
    APFS = "apfs"


class LinkEntry(BaseModel):
    source: Path
    target: str

    @field_validator("target")
    @classmethod
    def target_must_be_relative(cls, v: str) -> str:
        p = Path(v)
        if p.is_absolute() or ".." in p.parts:
            msg = f"link target must be a relative path with no '..': {v!r}"
            raise ValueError(msg)
        return v


class MountOptions(BaseModel):
    hidden: bool = True
    """Hide the volume from Finder/GUI (mount -o nobrowse)."""
    access_times: bool = False
    """Track file access times (omit for mount -o noatime; faster without)."""
    allow_dev: bool = True
    """Honor device special files on the volume (mount -o nodev if False)."""
    allow_setuid: bool = True
    """Honor setuid/setgid bits on the volume (mount -o nosuid if False)."""
    allow_exec: bool = True
    """Allow executing binaries from the volume (mount -o noexec if False)."""
    read_only: bool = False
    """Mount read-only (mount -o rdonly)."""

    def to_mount_flags(self) -> list[str]:
        options: list[str] = []
        if self.hidden:
            options.append("nobrowse")
        if not self.access_times:
            options.append("noatime")
        if not self.allow_dev:
            options.append("nodev")
        if not self.allow_setuid:
            options.append("nosuid")
        if not self.allow_exec:
            options.append("noexec")
        if self.read_only:
            options.append("rdonly")
        return ["-o", ",".join(options)] if options else []


def mount_options_from_tokens(tokens: set[str]) -> MountOptions:
    """Build MountOptions from the option tokens `mount` reports for a live volume."""
    return MountOptions(
        hidden="nobrowse" in tokens,
        access_times="noatime" not in tokens,
        allow_dev="nodev" not in tokens,
        allow_setuid="nosuid" not in tokens,
        allow_exec="noexec" not in tokens,
        read_only=bool({"rdonly", "read-only"} & tokens),
    )


class DiskConfig(BaseModel):
    name: str
    size_mb: int = Field(gt=0)
    filesystem: Filesystem = Filesystem.HFS
    mount_point: Path | None = None
    options: MountOptions = Field(default_factory=MountOptions)
    restore: bool = False
    """On unmount, copy linked files back to their original location instead of leaving dangling symlinks."""
    enabled: bool = True
    """If False, `tmpfs mount` (with no disk named explicitly) skips this disk."""
    on_login: bool = False
    """If True, the login LaunchAgent installed by `tmpfs install` mounts this disk."""
    links: list[LinkEntry] = Field(default_factory=list)

    @property
    def resolved_mount_point(self) -> Path:
        return self.mount_point or DEFAULT_MOUNT_ROOT / self.name


class TmpfsConfig(BaseModel):
    disks: list[DiskConfig] = Field(default_factory=list)

    def get(self, name: str) -> DiskConfig | None:
        return next((d for d in self.disks if d.name == name), None)


def default_config_path() -> Path:
    return Path.home() / ".config" / "tmpfs" / "tmpfs.toml"


def load_config(path: Path | None = None) -> TmpfsConfig:
    path = path or default_config_path()
    if not path.exists():
        return TmpfsConfig()
    document = tomlkit.parse(path.read_text())
    return TmpfsConfig.model_validate(document.unwrap())


def save_config(config: TmpfsConfig, path: Path | None = None) -> None:
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    document = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    document["disks"] = tomlkit.item(
        config.model_dump(mode="json", exclude_none=True)["disks"]
    )
    path.write_text(tomlkit.dumps(document))


def add_disk(config: TmpfsConfig, disk: DiskConfig) -> TmpfsConfig:
    if config.get(disk.name) is not None:
        msg = f"a disk named '{disk.name}' already exists in the config"
        raise ConfigError(msg)
    return TmpfsConfig(disks=[*config.disks, disk])


def remove_disk(config: TmpfsConfig, name: str) -> TmpfsConfig:
    if config.get(name) is None:
        msg = f"no disk named '{name}' in the config"
        raise ConfigError(msg)
    return TmpfsConfig(disks=[d for d in config.disks if d.name != name])


def replace_disk(config: TmpfsConfig, old_name: str, disk: DiskConfig) -> TmpfsConfig:
    if config.get(old_name) is None:
        msg = f"no disk named '{old_name}' in the config"
        raise ConfigError(msg)
    if disk.name != old_name and config.get(disk.name) is not None:
        msg = f"a disk named '{disk.name}' already exists in the config"
        raise ConfigError(msg)
    return TmpfsConfig(disks=[disk if d.name == old_name else d for d in config.disks])
