from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tmpfs.config import (
    DiskConfig,
    Filesystem,
    LinkEntry,
    MountOptions,
    TmpfsConfig,
    add_disk,
    default_config_path,
    load_config,
    mount_options_from_tokens,
    remove_disk,
    replace_disk,
    save_config,
)
from tmpfs.errors import ConfigError


def make_disk(name: str = "scratch", **overrides: object) -> DiskConfig:
    fields: dict[str, object] = {"name": name, "size_mb": 100}
    fields.update(overrides)
    return DiskConfig(**fields)  # type: ignore[arg-type]


class TestLinkEntry:
    def test_rejects_absolute_target(self) -> None:
        with pytest.raises(ValidationError):
            LinkEntry(source=Path("/tmp/foo"), target="/etc/passwd")

    def test_rejects_dotdot_target(self) -> None:
        with pytest.raises(ValidationError):
            LinkEntry(source=Path("/tmp/foo"), target="../escape")

    def test_accepts_relative_target(self) -> None:
        link = LinkEntry(source=Path("/tmp/foo"), target="sub/file.txt")
        assert link.target == "sub/file.txt"


class TestMountOptions:
    def test_defaults_produce_nobrowse_and_noatime(self) -> None:
        flags = MountOptions().to_mount_flags()
        assert flags == ["-o", "nobrowse,noatime"]

    def test_all_permissive_produces_no_flags(self) -> None:
        options = MountOptions(hidden=False, access_times=True)
        assert options.to_mount_flags() == []

    def test_read_only_and_noexec(self) -> None:
        options = MountOptions(
            hidden=False, access_times=True, allow_exec=False, read_only=True
        )
        flags = options.to_mount_flags()
        assert flags[0] == "-o"
        assert set(flags[1].split(",")) == {"noexec", "rdonly"}


class TestMountOptionsFromTokens:
    def test_round_trips_controllable_flags(self) -> None:
        tokens = {"nobrowse", "noatime", "noexec"}
        options = mount_options_from_tokens(tokens)
        assert options.hidden is True
        assert options.access_times is False
        assert options.allow_exec is False
        assert options.read_only is False

    def test_read_only_alias(self) -> None:
        assert mount_options_from_tokens({"read-only"}).read_only is True
        assert mount_options_from_tokens({"rdonly"}).read_only is True

    def test_nodev_nosuid_forced_by_os_still_parsed(self) -> None:
        # macOS forces nodev/nosuid on non-root local mounts regardless of intent;
        # the parser still reflects whatever tokens it's given.
        options = mount_options_from_tokens({"nodev", "nosuid"})
        assert options.allow_dev is False
        assert options.allow_setuid is False


class TestDiskConfig:
    def test_resolved_mount_point_defaults_to_private_tmp_name(self) -> None:
        disk = make_disk(name="foo")
        assert disk.resolved_mount_point == Path("/private/tmp/foo")

    def test_resolved_mount_point_honors_explicit_override(self) -> None:
        disk = make_disk(mount_point=Path("/private/tmp/custom"))
        assert disk.resolved_mount_point == Path("/private/tmp/custom")

    def test_size_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            DiskConfig(name="x", size_mb=0)

    def test_defaults(self) -> None:
        disk = make_disk()
        assert disk.filesystem is Filesystem.HFS
        assert disk.restore is False
        assert disk.enabled is True
        assert disk.on_login is False
        assert disk.links == []


class TestTmpfsConfigGet:
    def test_get_returns_none_when_missing(self) -> None:
        assert TmpfsConfig().get("nope") is None

    def test_get_finds_by_name(self) -> None:
        disk = make_disk(name="a")
        config = TmpfsConfig(disks=[disk])
        assert config.get("a") is disk


class TestConfigPersistence:
    def test_default_config_path_under_home_config_tmpfs(self, fake_home: Path) -> None:
        assert default_config_path() == fake_home / ".config" / "tmpfs" / "tmpfs.toml"

    def test_load_missing_file_returns_empty_config(self, fake_home: Path) -> None:
        config = load_config()
        assert config.disks == []

    def test_save_then_load_round_trips(self, fake_home: Path) -> None:
        disk = make_disk(
            name="roundtrip",
            size_mb=256,
            links=[LinkEntry(source=Path("/tmp/a"), target="a")],
        )
        save_config(TmpfsConfig(disks=[disk]))

        loaded = load_config()
        assert len(loaded.disks) == 1
        assert loaded.disks[0].name == "roundtrip"
        assert loaded.disks[0].size_mb == 256
        assert loaded.disks[0].links[0].target == "a"

    def test_save_preserves_hand_written_comments(self, fake_home: Path) -> None:
        path = default_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# a hand-written comment\ndisks = []\n")

        save_config(add_disk(load_config(), make_disk(name="new")))

        text = path.read_text()
        assert "# a hand-written comment" in text
        assert "new" in text

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "tmpfs.toml"
        save_config(TmpfsConfig(disks=[make_disk()]), path=path)
        assert path.exists()


class TestAddRemoveReplaceDisk:
    def test_add_disk(self) -> None:
        config = add_disk(TmpfsConfig(), make_disk(name="a"))
        assert config.get("a") is not None

    def test_add_duplicate_name_raises(self) -> None:
        config = add_disk(TmpfsConfig(), make_disk(name="a"))
        with pytest.raises(ConfigError):
            add_disk(config, make_disk(name="a"))

    def test_remove_disk(self) -> None:
        config = add_disk(TmpfsConfig(), make_disk(name="a"))
        config = remove_disk(config, "a")
        assert config.get("a") is None

    def test_remove_missing_disk_raises(self) -> None:
        with pytest.raises(ConfigError):
            remove_disk(TmpfsConfig(), "nope")

    def test_replace_disk_renames(self) -> None:
        config = add_disk(TmpfsConfig(), make_disk(name="a"))
        renamed = make_disk(name="b")
        config = replace_disk(config, "a", renamed)
        assert config.get("a") is None
        assert config.get("b") is not None

    def test_replace_missing_old_name_raises(self) -> None:
        with pytest.raises(ConfigError):
            replace_disk(TmpfsConfig(), "nope", make_disk(name="b"))

    def test_replace_onto_existing_new_name_raises(self) -> None:
        config = add_disk(TmpfsConfig(), make_disk(name="a"))
        config = add_disk(config, make_disk(name="b"))
        with pytest.raises(ConfigError):
            replace_disk(config, "a", make_disk(name="b"))
