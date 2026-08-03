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

    # Tigris (S3-compatible) storage for browser session recordings
    TIGRIS_BUCKET: str = ""
    TIGRIS_ACCESS_KEY_ID: str = ""
    TIGRIS_SECRET_ACCESS_KEY: str = ""
    TIGRIS_ENDPOINT_URL: str = "https://t3.storage.dev"
    TIGRIS_REGION: str = "auto"

    # How long to keep polling Chrome Fleet for recordings that are still encoding.
    RECORDING_POLL_TIMEOUT_SECONDS: float = 120.0
    RECORDING_POLL_INTERVAL_SECONDS: float = 3.0

    @property
    def data_dir(self) -> Path:
        path = Path(self.DATA_DIR).resolve() if self.DATA_DIR else PROJECT_DIR / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def RECORDING_UPLOAD_ENABLED(self) -> bool:
        return bool(
            self.TIGRIS_BUCKET and self.TIGRIS_ACCESS_KEY_ID and self.TIGRIS_SECRET_ACCESS_KEY
        )

    @property
    def screenshots_dir(self) -> Path:
        path = self.data_dir / "screenshots"
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
