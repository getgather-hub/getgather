import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Generator

import pytest
from pytest import MonkeyPatch


def pytest_addoption(parser: pytest.Parser) -> None:
    """Runtime inputs for tests that take no configuration from the environment."""
    parser.addoption(
        "--amazon-base-url",
        default="",
        help=(
            "Alternate origin for the Amazon US tools; enables tests/mcp/test_amazon_alt_origin.py."
        ),
    )
    parser.addoption("--amazon-email", default="", help="Sign-in email for --amazon-base-url.")
    parser.addoption(
        "--amazon-password", default="", help="Sign-in password for --amazon-base-url."
    )


def _required_option(request: pytest.FixtureRequest, name: str) -> str:
    value = str(request.config.getoption(name))
    if not value:
        # `pytest.skip()` itself is a callable protocol that `ty` cannot resolve.
        raise pytest.skip.Exception(f"pass {name}=<value> to run against an alternate origin")
    return value


@pytest.fixture
def amazon_base_url(request: pytest.FixtureRequest) -> str:
    return _required_option(request, "--amazon-base-url")


@pytest.fixture
def amazon_credentials(request: pytest.FixtureRequest) -> tuple[str, str]:
    return (
        _required_option(request, "--amazon-email"),
        _required_option(request, "--amazon-password"),
    )


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


@pytest.fixture
def mcp_config() -> dict[str, Any]:
    return {
        "mcpServers": {
            "getgather": {
                "url": f"{os.environ.get('HOST', 'http://localhost:23456')}/mcp",
            }
        }
    }
