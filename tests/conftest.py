import shutil
import tempfile
from pathlib import Path
from typing import Generator

import pytest
from pytest import MonkeyPatch


@pytest.fixture
def temp_project_dir(monkeypatch: MonkeyPatch) -> Generator[Path, None, None]:
    """Create a temporary directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

    monkeypatch.setattr("getgather.config.PROJECT_DIR", temp_path)
    yield temp_path
    # Clean up
    if temp_path.exists():
        shutil.rmtree(temp_path)
