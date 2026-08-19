import asyncio
import time
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket
from loguru import logger

from getgather.browsers.backend import BROWSER_SCOPE, Backend
from getgather.cdp_client import open_cdp_url
from getgather.config import settings

CDP_READY_ATTEMPTS = 30
CDP_READY_RETRY_SECONDS = 1.0
CDP_OPEN_TIMEOUT_SECONDS = 10.0
CDP_COMMAND_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class _WinnerRoute:
    backend: Backend
    provider_browser_id: str


@dataclass(frozen=True)
class _Candidate:
    provider: str
    backend: Backend
    provider_browser_id: str
    create_seconds: float
    cdp_ready_seconds: float


class ProviderRaceBackend:
    """Race configured browser providers and proxy the winner behind an opaque public id.

    Routes intentionally live in memory for the first experiment. A restart, or a follow-up
    request landing on another server replica, loses the route. The public API never exposes the
    provider name, provider browser id, or provider CDP URL.
    """

    def __init__(self, fallback: Backend, providers: dict[str, Backend]) -> None:
        if len(providers) < 2:
            raise ValueError("BROWSER_PROVIDER_RACE requires at least two configured providers")
        self._fallback = fallback
        self._providers = providers
        self._routes: dict[str, _WinnerRoute] = {}
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    @property
    def default_best_of_n(self) -> int:
        # The outer race is across providers. Do not also multiply candidates within a provider.
        return 1

    async def create_raced_browser(
        self,
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> None:
        """Create one candidate per provider and register the first CDP-ready winner."""
        race_started = time.monotonic()
        logger.info(f"Provider race started for {browser_id}: candidates={len(self._providers)}")
        tasks = {
            provider: asyncio.create_task(
                self._create_ready_candidate(
                    provider,
                    provider_backend,
                    browser_id,
                    origin_ip,
                    target_domain,
                    browser_type,
                )
            )
            for provider, provider_backend in self._providers.items()
        }

        winner: _Candidate | None = None
        for completed in asyncio.as_completed(tasks.values()):
            try:
                winner = await completed
            except Exception:
                # The candidate logs its provider, phase, duration, and error type before raising.
                continue
            break

        if winner is None:
            raise RuntimeError("No browser provider became CDP-ready")

        self._routes[browser_id] = _WinnerRoute(
            backend=winner.backend,
            provider_browser_id=winner.provider_browser_id,
        )
        logger.info(
            f"Provider-race winner for {browser_id}: provider={winner.provider} "
            f"create_ms={winner.create_seconds * 1000:.0f} "
            f"cdp_ready_ms={winner.cdp_ready_seconds * 1000:.0f} "
            f"race_ms={(time.monotonic() - race_started) * 1000:.0f}"
        )

        cleanup_task = asyncio.create_task(
            self._cleanup_race_losers(tasks, winner_provider=winner.provider)
        )
        self._cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._cleanup_tasks.discard)

    async def _create_ready_candidate(
        self,
        provider: str,
        provider_backend: Backend,
        public_browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> _Candidate:
        provider_browser_id = public_browser_id
        candidate_started = time.monotonic()
        phase = "create"
        try:
            result = await provider_backend.create_browser(
                public_browser_id, origin_ip, target_domain, browser_type
            )
            created_at = time.monotonic()
            result_browser_id = result.get("browser_id")
            if isinstance(result_browser_id, str):
                provider_browser_id = result_browser_id
            phase = "cdp_ready"
            await self._wait_until_cdp_ready(provider_backend, provider_browser_id)
            ready_at = time.monotonic()
            create_seconds = created_at - candidate_started
            cdp_ready_seconds = ready_at - created_at
            logger.info(
                f"Provider-race candidate ready for {public_browser_id}: provider={provider} "
                f"create_ms={create_seconds * 1000:.0f} "
                f"cdp_ready_ms={cdp_ready_seconds * 1000:.0f} "
                f"total_ms={(ready_at - candidate_started) * 1000:.0f}"
            )
            return _Candidate(
                provider,
                provider_backend,
                provider_browser_id,
                create_seconds,
                cdp_ready_seconds,
            )
        except Exception as e:
            logger.warning(
                f"Provider-race candidate failed for {public_browser_id}: provider={provider} "
                f"phase={phase} elapsed_ms={(time.monotonic() - candidate_started) * 1000:.0f} "
                f"error={type(e).__name__}"
            )
            await self._delete_quietly(provider, provider_backend, provider_browser_id)
            raise

    async def _wait_until_cdp_ready(self, provider_backend: Backend, browser_id: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, CDP_READY_ATTEMPTS + 1):
            client = None
            try:
                remote_url = await provider_backend.get_cdp_websocket_remote_url(browser_id)
                if remote_url is None:
                    raise RuntimeError("CDP URL is not available")
                client = await open_cdp_url(remote_url, timeout=CDP_OPEN_TIMEOUT_SECONDS)
                await asyncio.wait_for(
                    client.send("Target.getTargets"), timeout=CDP_COMMAND_TIMEOUT_SECONDS
                )
                return
            except Exception as e:
                last_error = e
                logger.debug(
                    f"Provider-race CDP probe {attempt}/{CDP_READY_ATTEMPTS} failed: "
                    f"{type(e).__name__}"
                )
            finally:
                if client is not None:
                    await client.aclose()
            if attempt < CDP_READY_ATTEMPTS:
                await asyncio.sleep(CDP_READY_RETRY_SECONDS)
        raise RuntimeError(f"Browser {browser_id} did not become CDP-ready: {last_error}")

    async def _cleanup_race_losers(
        self, tasks: dict[str, asyncio.Task[_Candidate]], *, winner_provider: str
    ) -> None:
        for provider, task in tasks.items():
            if provider == winner_provider:
                continue
            try:
                candidate = await task
            except Exception:
                # _create_ready_candidate already performs best-effort cleanup on failure.
                continue
            await self._delete_quietly(
                candidate.provider, candidate.backend, candidate.provider_browser_id
            )

    async def _delete_quietly(
        self, provider: str, provider_backend: Backend, provider_browser_id: str
    ) -> None:
        cleanup_started = time.monotonic()
        try:
            await provider_backend.delete_browser(provider_browser_id)
            logger.info(
                f"Provider-race candidate cleaned up: provider={provider} "
                f"cleanup_ms={(time.monotonic() - cleanup_started) * 1000:.0f}"
            )
        except Exception as e:
            logger.warning(
                f"Provider-race cleanup failed: provider={provider} "
                f"cleanup_ms={(time.monotonic() - cleanup_started) * 1000:.0f} "
                f"error={type(e).__name__}"
            )

    def _route(self, browser_id: str) -> tuple[Backend, str]:
        route = self._routes.get(browser_id)
        if route is None:
            return self._fallback, browser_id
        return route.backend, route.provider_browser_id

    async def shutdown(self) -> None:
        if self._cleanup_tasks:
            await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
        seen: set[int] = set()
        for provider_backend in [self._fallback, *self._providers.values()]:
            identity = id(provider_backend)
            if identity in seen:
                continue
            seen.add(identity)
            await provider_backend.shutdown()

    async def create_browser(
        self,
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> dict[str, Any]:
        provider_backend, provider_browser_id = self._route(browser_id)
        # Preserve a raced browser's provider affinity when /cdp auto-starts it after deletion.
        # Browserbase may assign a fresh provider id, so refresh the internal route from the result.
        result = await provider_backend.create_browser(
            provider_browser_id, origin_ip, target_domain, browser_type
        )
        route = self._routes.get(browser_id)
        result_browser_id = result.get("browser_id")
        if route is not None and isinstance(result_browser_id, str):
            self._routes[browser_id] = _WinnerRoute(provider_backend, result_browser_id)
        return result

    async def get_browser(
        self, browser_id: str, origin_ip: str | None, target_domain: str | None
    ) -> dict[str, Any]:
        provider_backend, provider_browser_id = self._route(browser_id)
        await provider_backend.get_browser(provider_browser_id, origin_ip, target_domain)
        return {"browser_id": browser_id, "status": "created"}

    async def delete_browser(self, browser_id: str) -> dict[str, Any]:
        provider_backend, provider_browser_id = self._route(browser_id)
        await provider_backend.delete_browser(provider_browser_id)
        # Keep the route as a tombstone. Falling through to the default backend after deletion can
        # turn a subsequent GET into an upstream 500 or auto-start the id on a different provider.
        return {"browser_id": browser_id, "status": "deleted"}

    async def list_browser_ids(self, scope: BROWSER_SCOPE = "all") -> list[str]:
        fallback_ids = await self._fallback.list_browser_ids(scope)
        hidden_ids = {route.provider_browser_id for route in self._routes.values()}
        active_routes = {
            browser_id
            for browser_id, route in self._routes.items()
            if await route.backend.browser_exists(route.provider_browser_id)
        }
        return sorted((set(fallback_ids) - hidden_ids) | active_routes)

    async def browser_exists(self, browser_id: str) -> bool:
        provider_backend, provider_browser_id = self._route(browser_id)
        return await provider_backend.browser_exists(provider_browser_id)

    async def cleanup_idle(self) -> list[str]:
        deleted: list[str] = []
        seen: set[int] = set()
        for provider_backend in [self._fallback, *self._providers.values()]:
            identity = id(provider_backend)
            if identity in seen:
                continue
            seen.add(identity)
            deleted.extend(await provider_backend.cleanup_idle())
        return deleted

    async def get_cdp_base_url(self, browser_id: str) -> str:
        provider_backend, provider_browser_id = self._route(browser_id)
        return await provider_backend.get_cdp_base_url(provider_browser_id)

    def cdp_websocket_base(self) -> None:
        # Provider selection is per browser, so there is no single shared relay base.
        return None

    async def get_cdp_websocket_remote_url(self, browser_id: str) -> str | None:
        provider_backend, provider_browser_id = self._route(browser_id)
        return await provider_backend.get_cdp_websocket_remote_url(provider_browser_id)

    def cdp_targets_need_namespacing(self, browser_id: str | None = None) -> bool:
        if browser_id is None:
            return self._fallback.cdp_targets_need_namespacing()
        provider_backend, provider_browser_id = self._route(browser_id)
        return provider_backend.cdp_targets_need_namespacing(provider_browser_id)

    async def get_devtools_websocket_remote_url(
        self, client_ws: WebSocket, browser_id: str, page_id: str
    ) -> str | None:
        provider_backend, provider_browser_id = self._route(browser_id)
        return await provider_backend.get_devtools_websocket_remote_url(
            client_ws, provider_browser_id, page_id
        )

    async def get_vnc_endpoint(self, browser_id: str) -> tuple[str, int] | None:
        provider_backend, provider_browser_id = self._route(browser_id)
        return await provider_backend.get_vnc_endpoint(provider_browser_id)

    async def get_live_view_url(self, browser_id: str) -> str | None:
        provider_backend, provider_browser_id = self._route(browser_id)
        return await provider_backend.get_live_view_url(provider_browser_id)


def create_provider_race_backend(fallback: Backend) -> ProviderRaceBackend:
    """Build the experimental provider set from configured credentials."""
    providers: dict[str, Backend] = {}

    if settings.CHROMEFLEET_URL:
        from getgather.browsers.fleet_browsers import FleetBackend

        providers["fly"] = fallback if isinstance(fallback, FleetBackend) else FleetBackend()

    if settings.DAYTONA_API_KEY and settings.DAYTONA_SNAPSHOT:
        from getgather.browsers.daytona_browsers import DaytonaBackend

        providers["daytona"] = (
            fallback
            if isinstance(fallback, DaytonaBackend)
            else DaytonaBackend(
                settings.DAYTONA_API_KEY, settings.DAYTONA_API_URL, settings.DAYTONA_SNAPSHOT
            )
        )

    if settings.BROWSERBASE_API_KEY:
        from getgather.browsers.browserbase_browsers import BrowserbaseBackend

        providers["browserbase"] = (
            fallback if isinstance(fallback, BrowserbaseBackend) else BrowserbaseBackend()
        )

    return ProviderRaceBackend(fallback, providers)
