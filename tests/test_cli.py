from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tmpfs import cli, system
from tmpfs.config import (
    DiskConfig,
    LinkEntry,
    TmpfsConfig,
    load_config,
    save_config,
)
from tmpfs.errors import LaunchAgentError

if TYPE_CHECKING:
    from typer.testing import CliRunner


class FakeSystem:
    """In-memory stand-in for tmpfs.system's subprocess-backed functions."""

    def __init__(self) -> None:
        self.mounted: dict[Path, str] = {}
        self.device_size_mb: dict[str, int] = {}
        self.device_fstype: dict[str, str] = {}
        self.device_options: dict[str, set[str]] = {}
        # A real ram:// device keeps its data when remounted at a new path
        # (same block device, different mount point). Simulate that by
        # symlinking mount_point at a persistent per-device backing dir,
        # instead of creating a fresh empty directory on every mount_hfs call.
        self.device_backing: dict[str, Path] = {}
        self.detached: list[str] = []
        self._next_device = 10

    def _new_device(self) -> str:
        device = f"/dev/disk{self._next_device}"
        self._next_device += 1
        return device

    def attach_ram_device(self, size_mb: int) -> str:
        device = self._new_device()
        self.device_size_mb[device] = size_mb
        self.device_backing[device] = Path(
            tempfile.mkdtemp(prefix="tmpfs-fake-device-")
        )
        return device

    def format_hfs(self, device: str, volume_name: str) -> None:
        self.device_fstype[device] = "hfs"

    def mount_hfs(
        self, device: str, mount_point: Path, option_flags: list[str] | None = None
    ) -> None:
        mount_point.parent.mkdir(parents=True, exist_ok=True)
        if mount_point.is_symlink():
            mount_point.unlink()
            mount_point.symlink_to(self.device_backing[device])
        elif not mount_point.exists():
            mount_point.symlink_to(self.device_backing[device])
        # else: mount_point already exists as a real directory (e.g. simulating
        # an already-mounted disk in an import test) -- adopt it as-is.
        tokens: set[str] = set()
        if option_flags:
            tokens = set(option_flags[1].split(","))
        self.device_options[device] = tokens
        self.mounted[mount_point] = device

    def detach_device(self, device: str, force: bool = True) -> None:
        self.detached.append(device)
        self.device_size_mb.pop(device, None)
        self.device_fstype.pop(device, None)
        self.device_options.pop(device, None)
        backing = self.device_backing.pop(device, None)
        if backing is not None:
            shutil.rmtree(backing, ignore_errors=True)

    def mounted_device(self, mount_point: Path) -> str | None:
        return self.mounted.get(mount_point)

    def mount_entry(self, mount_point: Path):
        device = self.mounted.get(mount_point)
        if device is None:
            return None
        return (
            device,
            self.device_fstype.get(device, "hfs"),
            self.device_options.get(device, set()),
        )

    def is_mounted(self, mount_point: Path) -> bool:
        return mount_point in self.mounted

    def unmount_only(self, mount_point: Path, force: bool = False) -> None:
        self.mounted.pop(mount_point, None)

    def eject_mount_point(self, mount_point: Path, force: bool = False) -> None:
        device = self.mounted.pop(mount_point, None)
        if device is not None:
            self.detach_device(device, force=True)

    def rename_volume(self, device: str, volume_name: str) -> None:
        pass

    def get_disk_size(self, mount_point: Path) -> str | None:
        device = self.mounted.get(mount_point)
        if device is None:
            return None
        return f"{self.device_size_mb.get(device, 0)}MB"

    def ram_device_size_mb(self, device: str) -> int | None:
        return self.device_size_mb.get(device)

    def device_mount_point(self, device: str) -> Path | None:
        for mount_point, candidate in self.mounted.items():
            if candidate == device:
                return mount_point
        return None

    def find_ram_devices(self) -> list[str]:
        return list(self.device_size_mb.keys())

    def find_orphaned_ram_devices(self, known_mount_points: list[Path]) -> list[str]:
        known = set(known_mount_points)
        return [
            device
            for device in self.device_size_mb
            if self.device_mount_point(device) not in known
        ]

    def write_launch_agent(self, executable: Path, log_dir: Path) -> Path:
        log_dir.mkdir(parents=True, exist_ok=True)
        return Path("/fake/LaunchAgents/agent.plist")

    def load_launch_agent(self, path: Path) -> None:
        self.loaded_agent = path

    def unload_launch_agent(self) -> None:
        self.loaded_agent = None

    def launch_agent_path(self) -> Path:
        return Path("/fake/LaunchAgents/agent.plist")


@pytest.fixture
def fake_system(monkeypatch: pytest.MonkeyPatch):
    fake = FakeSystem()
    for name in (
        "attach_ram_device",
        "format_hfs",
        "mount_hfs",
        "detach_device",
        "mounted_device",
        "mount_entry",
        "is_mounted",
        "unmount_only",
        "eject_mount_point",
        "rename_volume",
        "get_disk_size",
        "ram_device_size_mb",
        "device_mount_point",
        "find_ram_devices",
        "find_orphaned_ram_devices",
        "write_launch_agent",
        "load_launch_agent",
        "unload_launch_agent",
        "launch_agent_path",
    ):
        monkeypatch.setattr(system, name, getattr(fake, name))
    yield fake
    for backing in fake.device_backing.values():
        shutil.rmtree(backing, ignore_errors=True)


def make_disk(name: str, mount_point: Path, **overrides: object) -> DiskConfig:
    fields: dict[str, object] = {
        "name": name,
        "size_mb": 10,
        "mount_point": mount_point,
    }
    fields.update(overrides)
    return DiskConfig(**fields)  # type: ignore[arg-type]


class TestMount:
    def test_no_disks_configured_exits_nonzero(
        self, fake_home: Path, fake_system: FakeSystem, runner: CliRunner
    ) -> None:
        result = runner.invoke(cli.app, ["mount"])
        assert result.exit_code == 1

    def test_mounts_a_configured_disk(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))

        result = runner.invoke(cli.app, ["mount", "scratch"])

        assert result.exit_code == 0
        assert mount_point in fake_system.mounted
        assert "mounted" in result.output

    def test_mounting_already_mounted_disk_is_idempotent(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(cli.app, ["mount", "scratch"])

        assert result.exit_code == 0
        assert "already mounted" in result.output

    def test_reconciles_links_on_mount(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        source = tmp_path / "real.txt"
        source.write_text("hello")
        disk = make_disk(
            "scratch", mount_point, links=[LinkEntry(source=source, target="real.txt")]
        )
        save_config(TmpfsConfig(disks=[disk]))

        result = runner.invoke(cli.app, ["mount", "scratch"])

        assert result.exit_code == 0
        assert source.is_symlink()
        assert (mount_point / "real.txt").read_text() == "hello"

    def test_bulk_mount_skips_disabled_disks(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        disabled = make_disk("disabled-disk", tmp_path / "a", enabled=False)
        enabled = make_disk("enabled-disk", tmp_path / "b")
        save_config(TmpfsConfig(disks=[disabled, enabled]))

        result = runner.invoke(cli.app, ["mount"])

        assert result.exit_code == 0
        assert "disabled-disk: disabled, skipping" in result.output
        assert (tmp_path / "a") not in fake_system.mounted
        assert (tmp_path / "b") in fake_system.mounted

    def test_explicit_name_mounts_disabled_disk_anyway(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        disabled = make_disk("disabled-disk", tmp_path / "a", enabled=False)
        save_config(TmpfsConfig(disks=[disabled]))

        result = runner.invoke(cli.app, ["mount", "disabled-disk"])

        assert result.exit_code == 0
        assert (tmp_path / "a") in fake_system.mounted

    def test_login_flag_mounts_only_on_login_disks(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        plain = make_disk("plain", tmp_path / "a")
        login_disk = make_disk("login-disk", tmp_path / "b", on_login=True)
        save_config(TmpfsConfig(disks=[plain, login_disk]))

        result = runner.invoke(cli.app, ["mount", "--login"])

        assert result.exit_code == 0
        assert (tmp_path / "a") not in fake_system.mounted
        assert (tmp_path / "b") in fake_system.mounted

    def test_login_flag_with_disabled_on_login_disk_skips_it(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        disk = make_disk("login-disk", tmp_path / "a", on_login=True, enabled=False)
        save_config(TmpfsConfig(disks=[disk]))

        result = runner.invoke(cli.app, ["mount", "--login"])

        assert result.exit_code == 0
        assert (tmp_path / "a") not in fake_system.mounted


class TestUnmount:
    def test_unmount_ejects_mounted_disk(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(cli.app, ["unmount", "scratch"])

        assert result.exit_code == 0
        assert mount_point not in fake_system.mounted

    def test_unmount_not_mounted_is_a_no_op(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        save_config(TmpfsConfig(disks=[make_disk("scratch", tmp_path / "ramdisk")]))

        result = runner.invoke(cli.app, ["unmount", "scratch"])

        assert result.exit_code == 0
        assert "not mounted" in result.output

    def test_unmount_warns_about_dangling_symlinks_by_default(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        source = tmp_path / "real.txt"
        source.write_text("hello")
        disk = make_disk(
            "scratch", mount_point, links=[LinkEntry(source=source, target="real.txt")]
        )
        save_config(TmpfsConfig(disks=[disk]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(cli.app, ["unmount", "scratch"])

        assert result.exit_code == 0
        assert "dangling" in result.output
        assert source.is_symlink()

    def test_unmount_restore_copies_files_back(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        source = tmp_path / "real.txt"
        source.write_text("hello")
        disk = make_disk(
            "scratch", mount_point, links=[LinkEntry(source=source, target="real.txt")]
        )
        save_config(TmpfsConfig(disks=[disk]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(cli.app, ["unmount", "scratch", "--restore"])

        assert result.exit_code == 0
        assert not source.is_symlink()
        assert source.read_text() == "hello"

    def test_umount_alias_works(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(cli.app, ["umount", "scratch"])

        assert result.exit_code == 0
        assert mount_point not in fake_system.mounted


class TestAdd:
    def test_delegates_to_wizard(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
    ) -> None:
        calls = []
        monkeypatch.setattr(
            cli, "run_add_wizard", lambda name, config: calls.append((name, config))
        )

        result = runner.invoke(cli.app, ["add", "newdisk"])

        assert result.exit_code == 0
        assert calls[0][0] == "newdisk"


class TestImport:
    def test_imports_mounted_disk_with_links(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "existing"
        mount_point.mkdir()
        device = fake_system.attach_ram_device(64)
        fake_system.format_hfs(device, "existing")
        fake_system.mount_hfs(device, mount_point, ["-o", "nobrowse,noatime"])

        scan_dir = tmp_path / "scan"
        scan_dir.mkdir()
        target = mount_point / "file.txt"
        target.write_text("data")
        (scan_dir / "link.txt").symlink_to(target)

        result = runner.invoke(
            cli.app,
            ["import", str(mount_point), str(scan_dir), "--name", "imported", "--yes"],
        )

        assert result.exit_code == 0
        disk = load_config().get("imported")
        assert disk is not None
        assert disk.size_mb == 64
        assert len(disk.links) == 1
        assert disk.links[0].target == "file.txt"

    def test_errors_when_not_mounted(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(
            cli.app, ["import", str(tmp_path / "nope"), str(tmp_path), "--yes"]
        )
        assert result.exit_code == 1

    def test_errors_on_duplicate_name(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "existing"
        mount_point.mkdir()
        device = fake_system.attach_ram_device(64)
        fake_system.format_hfs(device, "existing")
        fake_system.mount_hfs(device, mount_point)
        save_config(TmpfsConfig(disks=[make_disk("existing", mount_point)]))

        result = runner.invoke(
            cli.app, ["import", str(mount_point), str(tmp_path), "--yes"]
        )
        assert result.exit_code == 1


class TestList:
    def test_shows_mounted_and_link_status(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point, size_mb=42)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(cli.app, ["list"])

        assert result.exit_code == 0
        assert "scratch" in result.output
        assert "42MB" in result.output

    def test_flags_column_shows_disabled_and_on_login(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        save_config(
            TmpfsConfig(
                disks=[
                    make_disk("d1", tmp_path / "a", enabled=False),
                    make_disk("d2", tmp_path / "b", on_login=True),
                ]
            )
        )

        result = runner.invoke(cli.app, ["list"])

        assert "disabled" in result.output
        assert "on_login" in result.output


class TestLinks:
    def test_shows_link_status(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        source = tmp_path / "real.txt"
        source.write_text("hi")
        disk = make_disk(
            "scratch", mount_point, links=[LinkEntry(source=source, target="real.txt")]
        )
        save_config(TmpfsConfig(disks=[disk]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(cli.app, ["links"])

        assert result.exit_code == 0
        assert "ok" in result.output

    def test_no_links_configured(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        save_config(TmpfsConfig(disks=[make_disk("scratch", tmp_path / "ramdisk")]))
        result = runner.invoke(cli.app, ["links"])
        assert "No links configured" in result.output


class TestLink:
    def test_adds_link_and_redirects_when_mounted(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        source = tmp_path / "real.txt"
        source.write_text("hi")

        result = runner.invoke(cli.app, ["link", "scratch", str(source)])

        assert result.exit_code == 0
        disk = load_config().get("scratch")
        assert disk is not None
        assert disk.links[0].source == source
        assert disk.links[0].target == "real.txt"
        assert source.is_symlink()
        assert (mount_point / "real.txt").read_text() == "hi"

    def test_adds_link_when_not_mounted_writes_config_only(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        save_config(TmpfsConfig(disks=[make_disk("scratch", tmp_path / "ramdisk")]))
        source = tmp_path / "real.txt"
        source.write_text("hi")

        result = runner.invoke(cli.app, ["link", "scratch", str(source)])

        assert result.exit_code == 0
        assert "Not mounted" in result.output
        assert not source.is_symlink()
        disk = load_config().get("scratch")
        assert disk is not None
        assert disk.links[0].source == source

    def test_custom_target(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        save_config(TmpfsConfig(disks=[make_disk("scratch", tmp_path / "ramdisk")]))
        source = tmp_path / "real.txt"
        source.write_text("hi")

        result = runner.invoke(
            cli.app, ["link", "scratch", str(source), "--target", "renamed.txt"]
        )

        assert result.exit_code == 0
        disk = load_config().get("scratch")
        assert disk is not None
        assert disk.links[0].target == "renamed.txt"

    def test_missing_disk_errors(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(cli.app, ["link", "nope", str(tmp_path / "x")])
        assert result.exit_code == 1

    def test_duplicate_source_errors(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        source = tmp_path / "real.txt"
        source.write_text("hi")
        disk = make_disk(
            "scratch",
            tmp_path / "ramdisk",
            links=[LinkEntry(source=source, target="real.txt")],
        )
        save_config(TmpfsConfig(disks=[disk]))

        result = runner.invoke(cli.app, ["link", "scratch", str(source)])

        assert result.exit_code == 1
        assert "already linked" in result.output

    def test_duplicate_target_errors(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        existing_source = tmp_path / "a.txt"
        existing_source.write_text("a")
        disk = make_disk(
            "scratch",
            tmp_path / "ramdisk",
            links=[LinkEntry(source=existing_source, target="shared.txt")],
        )
        save_config(TmpfsConfig(disks=[disk]))

        new_source = tmp_path / "b.txt"
        new_source.write_text("b")

        result = runner.invoke(
            cli.app, ["link", "scratch", str(new_source), "--target", "shared.txt"]
        )

        assert result.exit_code == 1
        assert "already used" in result.output


class TestUnlink:
    def test_removes_link_leaves_symlink_dangling_by_default(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))
        runner.invoke(cli.app, ["mount", "scratch"])
        source = tmp_path / "real.txt"
        source.write_text("hi")
        runner.invoke(cli.app, ["link", "scratch", str(source)])

        result = runner.invoke(cli.app, ["unlink", "scratch", str(source)])

        assert result.exit_code == 0
        disk = load_config().get("scratch")
        assert disk is not None
        assert disk.links == []
        assert source.is_symlink()

    def test_restore_copies_file_back(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))
        runner.invoke(cli.app, ["mount", "scratch"])
        source = tmp_path / "real.txt"
        source.write_text("hi")
        runner.invoke(cli.app, ["link", "scratch", str(source)])

        result = runner.invoke(cli.app, ["unlink", "scratch", str(source), "--restore"])

        assert result.exit_code == 0
        assert not source.is_symlink()
        assert source.read_text() == "hi"

    def test_missing_disk_errors(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(cli.app, ["unlink", "nope", str(tmp_path / "x")])
        assert result.exit_code == 1

    def test_unknown_source_errors(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        save_config(TmpfsConfig(disks=[make_disk("scratch", tmp_path / "ramdisk")]))
        result = runner.invoke(
            cli.app, ["unlink", "scratch", str(tmp_path / "never.txt")]
        )
        assert result.exit_code == 1
        assert "not linked" in result.output


class TestOpened:
    def test_none_row_when_nothing_has_file_open(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        source = tmp_path / "real.txt"
        source.write_text("hi")
        disk = make_disk(
            "scratch", mount_point, links=[LinkEntry(source=source, target="real.txt")]
        )
        save_config(TmpfsConfig(disks=[disk]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(cli.app, ["opened", "scratch"])

        assert result.exit_code == 0
        assert "None" in result.output


class TestRemove:
    def test_removes_disk_and_unmounts(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(cli.app, ["remove", "scratch", "--yes"])

        assert result.exit_code == 0
        assert load_config().get("scratch") is None
        assert mount_point not in fake_system.mounted

    def test_keep_mounted_leaves_disk_mounted(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(
            cli.app, ["remove", "scratch", "--yes", "--keep-mounted"]
        )

        assert result.exit_code == 0
        assert mount_point in fake_system.mounted

    def test_missing_disk_errors(
        self, fake_home: Path, fake_system: FakeSystem, runner: CliRunner
    ) -> None:
        result = runner.invoke(cli.app, ["remove", "nope", "--yes"])
        assert result.exit_code == 1


class TestRename:
    def test_renames_config_entry(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "private" / "tmp" / "oldname"
        disk = DiskConfig(name="oldname", size_mb=10, mount_point=mount_point)
        save_config(TmpfsConfig(disks=[disk]))

        result = runner.invoke(cli.app, ["rename", "oldname", "newname", "--yes"])

        assert result.exit_code == 0
        config = load_config()
        assert config.get("oldname") is None
        assert config.get("newname") is not None

    def test_relocates_default_mount_point_and_repairs_links(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # rename only relocates a disk whose mount_point still follows the
        # default DEFAULT_MOUNT_ROOT/<name> convention; point that root at a
        # tmp_path-backed directory so the test never touches the real filesystem.
        fake_root = tmp_path / "private" / "tmp"
        monkeypatch.setattr(cli.config_module, "DEFAULT_MOUNT_ROOT", fake_root)

        old_mount = fake_root / "oldname"
        new_mount = fake_root / "newname"
        source = tmp_path / "real.txt"
        source.write_text("hi")
        disk = DiskConfig(
            name="oldname",
            size_mb=10,
            links=[LinkEntry(source=source, target="real.txt")],
        )
        save_config(TmpfsConfig(disks=[disk]))
        device = fake_system.attach_ram_device(10)
        fake_system.format_hfs(device, "oldname")
        fake_system.mount_hfs(device, old_mount, ["-o", "nobrowse,noatime"])
        (old_mount / "real.txt").write_text("hi")
        source.unlink()
        source.symlink_to(old_mount / "real.txt")

        result = runner.invoke(cli.app, ["rename", "oldname", "newname", "--yes"])

        assert result.exit_code == 0
        assert new_mount in fake_system.mounted
        assert old_mount not in fake_system.mounted
        assert source.readlink() == new_mount / "real.txt"

    def test_duplicate_new_name_errors(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        save_config(
            TmpfsConfig(
                disks=[
                    make_disk("a", tmp_path / "a"),
                    make_disk("b", tmp_path / "b"),
                ]
            )
        )
        result = runner.invoke(cli.app, ["rename", "a", "b", "--yes"])
        assert result.exit_code == 1


class TestApply:
    def test_reconciles_new_link_without_recreate(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        source = tmp_path / "real.txt"
        source.write_text("hi")
        disk = load_config().get("scratch")
        assert disk is not None
        updated = disk.model_copy(
            update={"links": [LinkEntry(source=source, target="real.txt")]}
        )
        from tmpfs.config import replace_disk

        save_config(replace_disk(load_config(), "scratch", updated))

        result = runner.invoke(cli.app, ["apply", "scratch"])

        assert result.exit_code == 0
        assert source.is_symlink()

    def test_size_change_skipped_without_recreate_flag(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point, size_mb=10)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        disk = load_config().get("scratch")
        assert disk is not None
        from tmpfs.config import replace_disk

        save_config(
            replace_disk(
                load_config(), "scratch", disk.model_copy(update={"size_mb": 99})
            )
        )

        result = runner.invoke(cli.app, ["apply", "scratch"])

        assert result.exit_code == 0
        assert "Skipping" in result.output
        assert fake_system.ram_device_size_mb(fake_system.mounted[mount_point]) == 10

    def test_size_change_recreates_with_recreate_flag(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point, size_mb=10)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        disk = load_config().get("scratch")
        assert disk is not None
        from tmpfs.config import replace_disk

        save_config(
            replace_disk(
                load_config(), "scratch", disk.model_copy(update={"size_mb": 99})
            )
        )

        result = runner.invoke(cli.app, ["apply", "scratch", "--recreate", "--yes"])

        assert result.exit_code == 0
        assert fake_system.ram_device_size_mb(fake_system.mounted[mount_point]) == 99

    def test_not_mounted_disk_is_skipped(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        save_config(TmpfsConfig(disks=[make_disk("scratch", tmp_path / "ramdisk")]))
        result = runner.invoke(cli.app, ["apply", "scratch"])
        assert result.exit_code == 0
        assert "not mounted" in result.output


class TestOrphan:
    def test_no_orphans(
        self, fake_home: Path, fake_system: FakeSystem, runner: CliRunner
    ) -> None:
        result = runner.invoke(cli.app, ["orphan"])
        assert result.exit_code == 0
        assert "No orphaned" in result.output

    def test_lists_and_detaches_orphans(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        device = fake_system.attach_ram_device(50)
        fake_system.format_hfs(device, "orphan")
        # not mounted anywhere tracked -> orphaned

        result = runner.invoke(cli.app, ["orphan", "--detach"])

        assert result.exit_code == 0
        assert device in result.output
        assert device in fake_system.detached


class TestInstallUninstall:
    def test_install_requires_uv_tool_executable(
        self,
        fake_home: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise(name: str) -> Path:
            msg = "uv tool executable not found: /fake/bin/tmpfs"
            raise LaunchAgentError(msg)

        monkeypatch.setattr(system, "uv_tool_executable", _raise)
        result = runner.invoke(cli.app, ["install"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_install_writes_and_loads_agent(
        self,
        fake_home: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            system, "uv_tool_executable", lambda name: Path("/usr/local/bin/tmpfs")
        )
        result = runner.invoke(cli.app, ["install"])
        assert result.exit_code == 0
        assert "Installed" in result.output

    def test_uninstall_removes_agent(
        self,
        fake_home: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        agent_path = tmp_path / "agent.plist"
        agent_path.write_text("fake plist")
        monkeypatch.setattr(system, "launch_agent_path", lambda: agent_path)

        result = runner.invoke(cli.app, ["uninstall"])

        assert result.exit_code == 0
        assert not agent_path.exists()

    def test_uninstall_when_not_installed(
        self,
        fake_home: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            system, "launch_agent_path", lambda: tmp_path / "missing.plist"
        )
        result = runner.invoke(cli.app, ["uninstall"])
        assert result.exit_code == 0
        assert "not installed" in result.output


class TestInfo:
    def test_shows_mount_options_table(
        self,
        fake_home: Path,
        tmp_path: Path,
        fake_system: FakeSystem,
        runner: CliRunner,
    ) -> None:
        mount_point = tmp_path / "ramdisk"
        save_config(TmpfsConfig(disks=[make_disk("scratch", mount_point)]))
        runner.invoke(cli.app, ["mount", "scratch"])

        result = runner.invoke(cli.app, ["info", "scratch"])

        assert result.exit_code == 0
        assert "Mount Options" in result.output
        assert "hidden" in result.output

    def test_missing_disk_errors(
        self, fake_home: Path, fake_system: FakeSystem, runner: CliRunner
    ) -> None:
        result = runner.invoke(cli.app, ["info", "nope"])
        assert result.exit_code == 1
