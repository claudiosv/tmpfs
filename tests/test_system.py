from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from tmpfs import system
from tmpfs.errors import (
    AttachError,
    FormatError,
    LaunchAgentError,
    MountError,
    UnmountError,
)


def completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class ScriptedRun:
    """Stand-in for system._run: pops one canned response per call, records every command.

    Once the queue is exhausted, repeats the last response -- functions like
    ram_device_size_mb re-invoke _run on every call, so a single canned
    response must satisfy repeated identical commands too.
    """

    def __init__(self, responses: list[subprocess.CompletedProcess[str]]) -> None:
        self._responses = list(responses)
        self._last = completed()
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if self._responses:
            self._last = self._responses.pop(0)
        return self._last


@pytest.fixture
def patch_run(monkeypatch: pytest.MonkeyPatch):
    def _patch(*responses: subprocess.CompletedProcess[str]) -> ScriptedRun:
        stub = ScriptedRun(list(responses))
        monkeypatch.setattr(system, "_run", stub)
        return stub

    return _patch


def attach_plist(dev_entry: str, size_bytes: int) -> str:
    """Mirrors `diskutil image --plist attach --noMount ram://...` output."""
    return plistlib.dumps({
        "system-entities": [
            {"content-hint": "", "dev-entry": dev_entry, "size": size_bytes}
        ]
    }).decode()


class TestAttachRamDevice:
    def test_returns_device_path(self, patch_run) -> None:
        stub = patch_run(completed(stdout=attach_plist("disk9", 104857600)))
        device = system.attach_ram_device(100)
        assert device == "/dev/disk9"
        assert stub.calls[0][:3] == ["diskutil", "image", "--plist"]
        assert "ram://204800" in stub.calls[0]

    def test_dev_entry_already_prefixed_is_kept_as_is(self, patch_run) -> None:
        patch_run(completed(stdout=attach_plist("/dev/disk9", 104857600)))
        assert system.attach_ram_device(100) == "/dev/disk9"

    def test_raises_on_failure(self, patch_run) -> None:
        patch_run(completed(returncode=1, stderr="boom"))
        with pytest.raises(AttachError):
            system.attach_ram_device(100)

    def test_raises_when_no_device_in_output(self, patch_run) -> None:
        patch_run(completed(returncode=0, stdout=""))
        with pytest.raises(AttachError):
            system.attach_ram_device(100)

    def test_raises_on_malformed_plist(self, patch_run) -> None:
        patch_run(completed(returncode=0, stdout="not a plist"))
        with pytest.raises(AttachError):
            system.attach_ram_device(100)


class TestFormatAndMount:
    def test_format_hfs_raises_on_failure(self, patch_run) -> None:
        patch_run(completed(returncode=1, stderr="format failed"))
        with pytest.raises(FormatError):
            system.format_hfs("/dev/disk9", "myvol")

    def test_mount_hfs_creates_mount_point_and_raises_on_failure(
        self, patch_run, tmp_path: Path
    ) -> None:
        patch_run(completed(returncode=1, stderr="mount failed"))
        mount_point = tmp_path / "nested" / "mnt"
        with pytest.raises(MountError):
            system.mount_hfs("/dev/disk9", mount_point, ["-o", "nobrowse"])
        assert mount_point.exists()

    def test_mount_hfs_success(self, patch_run, tmp_path: Path) -> None:
        stub = patch_run(completed(returncode=0))
        mount_point = tmp_path / "mnt"
        system.mount_hfs("/dev/disk9", mount_point, ["-o", "nobrowse,noatime"])
        assert stub.calls[0] == [
            "mount",
            "-t",
            "hfs",
            "-o",
            "nobrowse,noatime",
            "/dev/disk9",
            str(mount_point),
        ]


MOUNT_OUTPUT = (
    "/dev/disk1s1 on / (apfs, local, journaled)\n"
    "/dev/disk4 on /private/tmp/tmpfs (hfs, local, nodev, nosuid, noatime, nobrowse, mounted by claudio)\n"
)


class TestMountedDeviceAndEntry:
    def test_mounted_device_found(self, patch_run) -> None:
        patch_run(completed(stdout=MOUNT_OUTPUT))
        assert system.mounted_device(Path("/private/tmp/tmpfs")) == "/dev/disk4"

    def test_mounted_device_not_found(self, patch_run) -> None:
        patch_run(completed(stdout=MOUNT_OUTPUT))
        assert system.mounted_device(Path("/private/tmp/nope")) is None

    def test_is_mounted(self, patch_run) -> None:
        patch_run(completed(stdout=MOUNT_OUTPUT))
        assert system.is_mounted(Path("/private/tmp/tmpfs")) is True

    def test_mount_entry_parses_device_fstype_and_options(self, patch_run) -> None:
        patch_run(completed(stdout=MOUNT_OUTPUT))
        entry = system.mount_entry(Path("/private/tmp/tmpfs"))
        assert entry is not None
        device, fstype, options = entry
        assert device == "/dev/disk4"
        assert fstype == "hfs"
        assert options == {"local", "nodev", "nosuid", "noatime", "nobrowse"}

    def test_mount_entry_none_when_not_mounted(self, patch_run) -> None:
        patch_run(completed(stdout=MOUNT_OUTPUT))
        assert system.mount_entry(Path("/private/tmp/nope")) is None


class TestUnmountAndEject:
    def test_unmount_only_raises_on_failure(self, patch_run) -> None:
        patch_run(completed(returncode=1, stderr="Resource busy"))
        with pytest.raises(UnmountError):
            system.unmount_only(Path("/private/tmp/tmpfs"))

    def test_unmount_only_force_passes_dash_f(self, patch_run) -> None:
        stub = patch_run(completed(returncode=0))
        system.unmount_only(Path("/private/tmp/tmpfs"), force=True)
        assert stub.calls[0] == ["umount", "-f", "/private/tmp/tmpfs"]

    def test_eject_mount_point_detaches_backing_device(self, patch_run) -> None:
        stub = patch_run(
            completed(stdout=MOUNT_OUTPUT),  # mounted_device lookup
            completed(returncode=0),  # umount
            completed(returncode=0),  # hdiutil detach
        )
        system.eject_mount_point(Path("/private/tmp/tmpfs"))
        assert stub.calls[1][0] == "umount"
        assert stub.calls[2][:2] == ["hdiutil", "detach"]
        assert "/dev/disk4" in stub.calls[2]


class TestGetDiskSize:
    def test_returns_none_for_missing_path(self) -> None:
        assert system.get_disk_size(Path("/nonexistent/path/for/tmpfs/tests")) is None

    def test_returns_decimal_size_for_real_path(self, tmp_path: Path) -> None:
        result = system.get_disk_size(tmp_path)
        assert result is not None
        assert "B" in result


def hdiutil_plist(*images: dict[str, object]) -> str:
    """Build a `hdiutil info -plist`-shaped payload, matching hdiutil's real structure."""
    return plistlib.dumps({
        "framework": "701",
        "revision": "701",
        "vendor": "Apple",
        "images": list(images),
    }).decode()


def ram_image(
    device: str, blockcount: int, mount_point: str | None = None
) -> dict[str, object]:
    entity: dict[str, object] = {"content-hint": "", "dev-entry": device}
    if mount_point is not None:
        entity["mount-point"] = mount_point
    return {
        "autodiskmount": False,
        "blockcount": blockcount,
        "blocksize": 512,
        "image-path": f"ram://{blockcount}",
        "image-type": "read/write disk image",
        "removable": True,
        "system-entities": [entity],
        "writeable": True,
    }


# Mirrors real `hdiutil info -plist` output: disk4 mounted, disk5 attached-but-unmounted.
HDIUTIL_INFO_PLIST = hdiutil_plist(
    ram_image("/dev/disk4", 204800, mount_point="/private/tmp/tmpfs"),
    ram_image("/dev/disk5", 20480),
)


class TestRamDeviceParsing:
    def test_find_ram_devices(self, patch_run) -> None:
        patch_run(completed(stdout=HDIUTIL_INFO_PLIST))
        assert system.find_ram_devices() == ["/dev/disk4", "/dev/disk5"]

    def test_ram_device_size_mb(self, patch_run) -> None:
        patch_run(completed(stdout=HDIUTIL_INFO_PLIST))
        assert system.ram_device_size_mb("/dev/disk4") == 100
        assert system.ram_device_size_mb("/dev/disk5") == 10

    def test_ram_device_size_mb_unknown_device(self, patch_run) -> None:
        patch_run(completed(stdout=HDIUTIL_INFO_PLIST))
        assert system.ram_device_size_mb("/dev/disk99") is None

    def test_device_mount_point_found(self, patch_run) -> None:
        patch_run(completed(stdout=HDIUTIL_INFO_PLIST))
        assert system.device_mount_point("/dev/disk4") == Path("/private/tmp/tmpfs")

    def test_device_mount_point_unmounted_device(self, patch_run) -> None:
        # attached but never mounted: no "mount-point" key in the plist at all.
        patch_run(completed(stdout=HDIUTIL_INFO_PLIST))
        assert system.device_mount_point("/dev/disk5") is None

    def test_non_ram_images_are_ignored(self, patch_run) -> None:
        plist = hdiutil_plist({
            "image-path": "/Users/me/some.dmg",
            "blockcount": 999999,
            "system-entities": [{"dev-entry": "/dev/disk8"}],
        })
        patch_run(completed(stdout=plist))
        assert system.find_ram_devices() == []

    def test_non_zero_returncode_yields_no_entries(self, patch_run) -> None:
        patch_run(completed(returncode=1, stderr="hdiutil: info failed"))
        assert system.find_ram_devices() == []

    def test_malformed_plist_yields_no_entries(self, patch_run) -> None:
        patch_run(completed(stdout="not a plist"))
        assert system.find_ram_devices() == []


class TestFindOrphanedRamDevices:
    def test_known_mount_point_excluded(self, patch_run) -> None:
        patch_run(completed(stdout=HDIUTIL_INFO_PLIST))
        orphaned = system.find_orphaned_ram_devices([Path("/private/tmp/tmpfs")])
        assert orphaned == ["/dev/disk5"]

    def test_all_orphaned_when_none_known(self, patch_run) -> None:
        patch_run(completed(stdout=HDIUTIL_INFO_PLIST))
        orphaned = system.find_orphaned_ram_devices([])
        assert orphaned == ["/dev/disk4", "/dev/disk5"]


class TestUvToolExecutable:
    def test_returns_path_when_found(self, patch_run, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "tmpfs").write_text("#!/bin/sh\n")
        patch_run(completed(stdout=f"{bin_dir}\n"))

        assert system.uv_tool_executable("tmpfs") == bin_dir / "tmpfs"

    def test_raises_when_uv_command_fails(self, patch_run) -> None:
        patch_run(completed(returncode=1, stderr="uv: command not found"))
        with pytest.raises(LaunchAgentError):
            system.uv_tool_executable("tmpfs")

    def test_raises_when_executable_missing_from_bin_dir(
        self, patch_run, tmp_path: Path
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        patch_run(completed(stdout=f"{bin_dir}\n"))

        with pytest.raises(LaunchAgentError):
            system.uv_tool_executable("tmpfs")


class TestLaunchAgent:
    def test_write_launch_agent_creates_plist(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        path = system.write_launch_agent(Path("/usr/local/bin/tmpfs"), log_dir)

        assert path.exists()
        assert log_dir.exists()

        import plistlib

        data = plistlib.loads(path.read_bytes())
        assert data["Label"] == system.LAUNCH_AGENT_LABEL
        assert data["ProgramArguments"] == ["/usr/local/bin/tmpfs", "mount", "--login"]
        assert data["RunAtLoad"] is True

    def test_load_launch_agent_raises_on_failure(
        self, patch_run, tmp_path: Path
    ) -> None:
        patch_run(
            completed(returncode=0),  # bootout (best-effort, ignored)
            completed(returncode=1, stderr="bootstrap failed"),
        )
        with pytest.raises(LaunchAgentError):
            system.load_launch_agent(tmp_path / "agent.plist")

    def test_unload_launch_agent_does_not_raise(self, patch_run) -> None:
        patch_run(completed(returncode=1, stderr="not found"))
        system.unload_launch_agent()  # should not raise even if not currently loaded


class TestProcessesWithOpenFile:
    def test_returns_empty_for_missing_path(self) -> None:
        results = list(
            system.processes_with_open_file(Path("/nonexistent/for/tmpfs/tests"))
        )
        assert results == []

    def test_finds_process_holding_file_open(self, tmp_path: Path) -> None:
        target = tmp_path / "held.txt"
        target.write_text("data")
        with target.open() as handle:
            results = list(system.processes_with_open_file(target))
        assert any(
            r.opened_path == str(target) or Path(r.opened_path) == target
            for r in results
        )
        del handle
