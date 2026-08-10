from __future__ import annotations

from pathlib import Path

import pytest

from tmpfs.config import DiskConfig, LinkEntry
from tmpfs.linker import LinkStatus, find_links_into, reconcile_link, reconcile_links


@pytest.fixture
def disk(tmp_path: Path) -> DiskConfig:
    mount_point = tmp_path / "ramdisk"
    mount_point.mkdir()
    return DiskConfig(name="d", size_mb=10, mount_point=mount_point)


def link_for(disk: DiskConfig, source: Path, target: str = "file.txt") -> LinkEntry:
    return LinkEntry(source=source, target=target)


class TestFirstTimeRedirect:
    def test_moves_real_file_and_symlinks_back(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        source = tmp_path / "real.txt"
        source.write_text("hello")
        link = link_for(disk, source)

        result = reconcile_link(disk, link)

        assert result.status is LinkStatus.REDIRECTED
        assert source.is_symlink()
        assert source.readlink() == disk.resolved_mount_point / "file.txt"
        assert (disk.resolved_mount_point / "file.txt").read_text() == "hello"

    def test_dry_run_does_not_touch_filesystem(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        source = tmp_path / "real.txt"
        source.write_text("hello")
        link = link_for(disk, source)

        result = reconcile_link(disk, link, dry_run=True)

        assert result.status is LinkStatus.WOULD_REDIRECT
        assert not source.is_symlink()
        assert source.read_text() == "hello"

    def test_creates_parent_dirs_for_nested_target(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        source = tmp_path / "real.txt"
        source.write_text("hello")
        link = link_for(disk, source, target="sub/dir/file.txt")

        reconcile_link(disk, link)

        assert (
            disk.resolved_mount_point / "sub" / "dir" / "file.txt"
        ).read_text() == "hello"

    def test_conflict_when_both_source_and_target_exist(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        source = tmp_path / "real.txt"
        source.write_text("hello")
        (disk.resolved_mount_point / "file.txt").write_text("other")
        link = link_for(disk, source)

        result = reconcile_link(disk, link)

        assert result.status is LinkStatus.CONFLICT
        assert not source.is_symlink()


class TestMissingSource:
    def test_missing_with_no_ramdisk_copy(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        link = link_for(disk, tmp_path / "gone.txt")
        result = reconcile_link(disk, link)
        assert result.status is LinkStatus.MISSING

    def test_missing_with_ramdisk_copy_present_gets_linked(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        (disk.resolved_mount_point / "file.txt").write_text("data")
        source = tmp_path / "gone.txt"
        link = link_for(disk, source)

        result = reconcile_link(disk, link)

        assert result.status is LinkStatus.LINKED
        assert source.is_symlink()
        assert source.readlink() == disk.resolved_mount_point / "file.txt"

    def test_dry_run_would_link(self, tmp_path: Path, disk: DiskConfig) -> None:
        (disk.resolved_mount_point / "file.txt").write_text("data")
        source = tmp_path / "gone.txt"
        link = link_for(disk, source)

        result = reconcile_link(disk, link, dry_run=True)

        assert result.status is LinkStatus.WOULD_LINK
        assert not source.exists()


class TestExistingSymlink:
    def test_correct_symlink_with_data_present_is_ok(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        (disk.resolved_mount_point / "file.txt").write_text("data")
        source = tmp_path / "link.txt"
        source.symlink_to(disk.resolved_mount_point / "file.txt")
        link = link_for(disk, source)

        result = reconcile_link(disk, link)

        assert result.status is LinkStatus.OK

    def test_correct_path_but_data_missing_is_missing(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        # e.g. disk was recreated: same mount path, but the file underneath is gone.
        source = tmp_path / "link.txt"
        source.symlink_to(disk.resolved_mount_point / "file.txt")
        link = link_for(disk, source)

        result = reconcile_link(disk, link)

        assert result.status is LinkStatus.MISSING

    def test_stale_symlink_repaired_when_target_exists(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        (disk.resolved_mount_point / "file.txt").write_text("data")
        source = tmp_path / "link.txt"
        source.symlink_to(tmp_path / "somewhere-else")
        link = link_for(disk, source)

        result = reconcile_link(disk, link)

        assert result.status is LinkStatus.REPAIRED
        assert source.readlink() == disk.resolved_mount_point / "file.txt"

    def test_stale_symlink_dry_run_does_not_repair(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        (disk.resolved_mount_point / "file.txt").write_text("data")
        source = tmp_path / "link.txt"
        stale_target = tmp_path / "somewhere-else"
        source.symlink_to(stale_target)
        link = link_for(disk, source)

        result = reconcile_link(disk, link, dry_run=True)

        assert result.status is LinkStatus.WOULD_REPAIR
        assert source.readlink() == stale_target

    def test_stale_symlink_with_no_target_is_ambiguous_and_untouched(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        source = tmp_path / "link.txt"
        stale_target = tmp_path / "somewhere-else"
        source.symlink_to(stale_target)
        link = link_for(disk, source)

        result = reconcile_link(disk, link)

        assert result.status is LinkStatus.AMBIGUOUS
        assert source.readlink() == stale_target


class TestReconcileLinks:
    def test_reconciles_every_configured_link(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        source_a = tmp_path / "a.txt"
        source_a.write_text("a")
        source_b = tmp_path / "b.txt"
        source_b.write_text("b")
        disk.links = [
            link_for(disk, source_a, "a.txt"),
            link_for(disk, source_b, "b.txt"),
        ]

        results = reconcile_links(disk)

        assert {r.status for r in results} == {LinkStatus.REDIRECTED}
        assert len(results) == 2


class TestFindLinksInto:
    def test_finds_symlinks_pointing_into_mount_point(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        scan_dir = tmp_path / "scan"
        scan_dir.mkdir()
        target = disk.resolved_mount_point / "file.txt"
        target.write_text("data")
        (scan_dir / "link.txt").symlink_to(target)
        (scan_dir / "unrelated.txt").write_text("not a link")
        (scan_dir / "elsewhere.txt").symlink_to(tmp_path / "not-in-ramdisk")

        found = find_links_into(scan_dir, disk.resolved_mount_point)

        assert len(found) == 1
        assert found[0].source == scan_dir / "link.txt"
        assert found[0].target == "file.txt"

    def test_finds_symlinks_in_subdirectories(
        self, tmp_path: Path, disk: DiskConfig
    ) -> None:
        scan_dir = tmp_path / "scan"
        (scan_dir / "nested").mkdir(parents=True)
        target = disk.resolved_mount_point / "file.txt"
        target.write_text("data")
        (scan_dir / "nested" / "link.txt").symlink_to(target)

        found = find_links_into(scan_dir, disk.resolved_mount_point)

        assert len(found) == 1
        assert found[0].source == scan_dir / "nested" / "link.txt"

    def test_no_symlinks_returns_empty(self, tmp_path: Path, disk: DiskConfig) -> None:
        scan_dir = tmp_path / "scan"
        scan_dir.mkdir()
        (scan_dir / "plain.txt").write_text("data")

        assert find_links_into(scan_dir, disk.resolved_mount_point) == []
