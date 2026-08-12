from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from getgather.browsers.settings import BrowserSettings

FRIENDLY_CHARS = "23456789abcdefghijkmnpqrstuvwxyz"

PROJECT_DIR = Path(__file__).resolve().parent.parent


class Settings(BrowserSettings, BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env", env_ignore_empty=True, extra="ignore"
    )
    ENVIRONMENT: str = "local"
    GIT_REV: str = ""

    DATA_DIR: str = ""

    # Logging
    LOG_LEVEL: str = "INFO"
    SENTRY_DSN: str = ""
    LOGFIRE_TOKEN: str = ""

    # Memory leak instrumentation (getgather/memory_xray.py). Off by default:
    # the census and tracemalloc tiers cost real CPU, so they are opted into
    # per-deployment rather than carried by every install.
    MEMORY_XRAY: bool = False
    MEMORY_XRAY_INTERVAL: int = 60
    MEMORY_XRAY_TOP_N: int = 10
    MEMORY_XRAY_CENSUS: bool = False
    MEMORY_XRAY_TRACEMALLOC: bool = False

    @property
    def data_dir(self) -> Path:
        path = Path(self.DATA_DIR).resolve() if self.DATA_DIR else PROJECT_DIR / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def screenshots_dir(self) -> Path:
        path = self.data_dir / "screenshots"
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
