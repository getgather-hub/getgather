from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BrowserBackendName = Literal["podman", "daytona", "browserbase"]


class BrowserSettings(BaseSettings):
    """Settings for the browser backends (fleet, podman, daytona).

    The concrete `getgather.config.Settings` inherits this and supplies the `.env`
    file; here we only declare the fields and their validation.
    """

    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    # Port the server listens on; used to build the local fallback Chrome Fleet URL.
    PORT: int = 23456

    # External Chrome Fleet: when set, the browser API is proxied to this upstream fleet
    # (takes precedence over BROWSER_BACKEND).
    CHROMEFLEET_URL: str = ""
    # WebSocket open timeout. Must exceed the Chrome Fleet cold-start (Daytona
    # sandbox create is ~45s p95), otherwise the connect loses the race against a
    # browser that is still launching.
    CHROMEFLEET_CDP_OPEN_TIMEOUT_SECONDS: float = 120.0
    # Timeout for the CDP handshake (set_discover_targets / update_targets) after
    # the WebSocket opens. Also sized past cold-start for the same reason.
    CHROMEFLEET_CDP_HANDSHAKE_TIMEOUT_SECONDS: float = 120.0

    # Local backend selection (ignored when CHROMEFLEET_URL is set)
    BROWSER_BACKEND: BrowserBackendName = "podman"

    # Podman backend
    CONTAINER_IMAGE: str = "ghcr.io/remotebrowser/chromium-live"
    CONTAINER_HOST: str = ""
    MAX_IDLE_MINUTES: int = 15

    # Residential proxy (Massive or Oxylabs) and MaxMind GeoIP
    MASSIVE_PROXY_USERNAME: str = ""
    MASSIVE_PROXY_PASSWORD: str = ""
    OXYLABS_USERNAME: str = ""
    OXYLABS_PASSWORD: str = ""
    DEFAULT_PROXY_TYPE: Literal["massive", "oxylabs"] = "massive"
    MAXMIND_ACCOUNT_ID: int = 0
    MAXMIND_LICENSE_KEY: str = ""

    # Daytona backend (required when BROWSER_BACKEND=daytona; install: uv sync --extra daytona).
    DAYTONA_API_KEY: str = ""
    DAYTONA_API_URL: str = (
        ""  # point at a self-hosted Daytona; empty uses the managed cloud default
    )
    DAYTONA_SNAPSHOT: str = ""

    # Browserbase backend (required when BROWSER_BACKEND=browserbase). Read at runtime from the
    # environment so the server can boot without it when another backend is selected.
    BROWSERBASE_API_KEY: str = ""

    # Best-of-N cold-create: on a fresh browser, race this many candidates in parallel and keep the
    # first whose `create_browser` fully succeeds.
    # Losers are deleted in the background. Set to 1 to disable the race and create a single
    # browser. Applies to every backend; only used by the server-assigned-id endpoint
    # (POST /api/v1/browsers). When unset (None), each backend supplies its own default
    # (see `Backend.default_best_of_n`): Podman=5, Daytona=3, Fleet=1.
    BROWSER_BEST_OF_N: int | None = None

    @property
    def effective_chromefleet_url(self) -> str:
        """Returns CHROMEFLEET_URL if set, otherwise falls back to the local backend."""
        return self.CHROMEFLEET_URL or f"http://127.0.0.1:{self.PORT}"

    @property
    def MASSIVE_PROXY_ENABLED(self) -> bool:
        return bool(self.MASSIVE_PROXY_USERNAME and self.MASSIVE_PROXY_PASSWORD)

    @property
    def OXYLABS_PROXY_ENABLED(self) -> bool:
        return bool(self.OXYLABS_USERNAME and self.OXYLABS_PASSWORD)

    @property
    def MAXMIND_ENABLED(self) -> bool:
        return bool(self.MAXMIND_ACCOUNT_ID and self.MAXMIND_LICENSE_KEY)

    @model_validator(mode="after")
    def validate_daytona_settings(self) -> Self:
        if self.BROWSER_BACKEND == "daytona":
            missing = [
                key for key in ("DAYTONA_API_KEY", "DAYTONA_SNAPSHOT") if not getattr(self, key)
            ]
            if missing:
                raise ValueError(
                    f"Missing settings for BROWSER_BACKEND=daytona: {', '.join(missing)}"
                )
        return self
