from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tmpfs import wizard
from tmpfs.config import TmpfsConfig, load_config


def queued(values: list[Any]):
    remaining = list(values)

    def _ask(*_args: object, **_kwargs: object) -> Any:
        return remaining.pop(0)

    return _ask


class TestRunAddWizard:
    def test_happy_path_no_links(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wizard.IntPrompt, "ask", queued([50]))
        monkeypatch.setattr(
            wizard.Prompt, "ask", queued(["hfs", "/private/tmp/testdisk"])
        )
        monkeypatch.setattr(
            wizard.Confirm,
            "ask",
            queued([
                True,  # hidden
                False,  # access_times
                False,  # restore
                False,  # on_login
                False,  # add a link? -> no
                True,  # save?
            ]),
        )

        wizard.run_add_wizard("testdisk", TmpfsConfig())

        config = load_config()
        disk = config.get("testdisk")
        assert disk is not None
        assert disk.size_mb == 50
        assert disk.mount_point == Path("/private/tmp/testdisk")
        assert disk.options.hidden is True
        assert disk.options.access_times is False
        assert disk.links == []

    def test_declining_save_writes_nothing(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wizard.IntPrompt, "ask", queued([50]))
        monkeypatch.setattr(
            wizard.Prompt, "ask", queued(["hfs", "/private/tmp/testdisk"])
        )
        monkeypatch.setattr(
            wizard.Confirm,
            "ask",
            queued([True, False, False, False, False, False]),  # save? -> no
        )

        wizard.run_add_wizard("testdisk", TmpfsConfig())

        assert load_config().get("testdisk") is None

    def test_duplicate_name_exits(self, fake_home: Path) -> None:
        existing = TmpfsConfig()
        from tmpfs.config import add_disk

        config = add_disk(existing, wizard.DiskConfig(name="dup", size_mb=10))

        with pytest.raises(SystemExit):
            wizard.run_add_wizard("dup", config)

    def test_adds_a_link(
        self, fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "real.txt"
        source.write_text("data")

        monkeypatch.setattr(wizard.IntPrompt, "ask", queued([50]))
        monkeypatch.setattr(
            wizard.Prompt,
            "ask",
            queued([
                "hfs",  # filesystem
                "/private/tmp/testdisk",  # mount point
                str(source),  # link source path
                "real.txt",  # link target
            ]),
        )
        monkeypatch.setattr(
            wizard.Confirm,
            "ask",
            queued([
                True,  # hidden
                False,  # access_times
                False,  # restore
                False,  # on_login
                True,  # add a link? -> yes
                False,  # add another link? -> no
                True,  # save?
            ]),
        )

        wizard.run_add_wizard("testdisk", TmpfsConfig())

        disk = load_config().get("testdisk")
        assert disk is not None
        assert len(disk.links) == 1
        assert disk.links[0].source == source
        assert disk.links[0].target == "real.txt"
