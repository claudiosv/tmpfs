from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME (and therefore the default tmpfs config path) into tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    # Wide terminal so rich Tables don't truncate cell text in assertions.
    return CliRunner(env={"COLUMNS": "220"})
