import asyncio
from typing import Any, Literal, Protocol, cast, runtime_checkable

import httpx
from fastapi import WebSocket
from loguru import logger
from nanoid import generate

from getgather.config import FRIENDLY_CHARS, settings

# Shared name prefix: a browser with id `abc` is a podman container / Daytona sandbox named
# `chromium-abc`. Both local backends derive names and parse ids from this single prefix.
BROWSER_NAME_PREFIX = "chromium-"

RECENTLY_DELETED_SECONDS = 60.0
_deleted_at: dict[str, float] = {}


def mark_browser_deleting(browser_id: str) -> None:
    """Remember `browser_id` as deleted. Call before awaiting the delete, not after."""
    now = asyncio.get_running_loop().time()
    for stale in [b for b, t in _deleted_at.items() if now - t > RECENTLY_DELETED_SECONDS]:
        del _deleted_at[stale]
    _deleted_at[browser_id] = now


def was_browser_recently_deleted(browser_id: str) -> bool:
    deleted_at = _deleted_at.get(browser_id)
    if deleted_at is None:
        return False
    if asyncio.get_running_loop().time() - deleted_at > RECENTLY_DELETED_SECONDS:
        del _deleted_at[browser_id]
        return False
    return True


# Which browsers a listing should include. `all` is the full inventory, including ones a
# backend can resume on demand; `live` is only those able to serve CDP right now.
BROWSER_SCOPE = Literal["all", "live"]


def rewrite_ws_url(ws_url: str, cdp_base_url: str) -> str:
    """Rewrite a CDP webSocketDebuggerUrl to use the cdp_base_url's scheme/host/port.

    Chrome reports webSocketDebuggerUrl against the Host it saw (e.g. ws://localhost:9222/...),
    which is unreachable when CDP is fronted by a reverse proxy (a Daytona signed preview URL).
    This points the websocket at the same scheme+host+port we reached CDP on (https -> wss),
    keeping the path/query. For the local podman backend the host already matches, so it is a no-op.
    """
    base = httpx.URL(cdp_base_url)
    scheme = "wss" if base.scheme == "https" else "ws"
    return str(httpx.URL(ws_url).copy_with(scheme=scheme, host=base.host, port=base.port))


async def get_browser_websocket_debugger_url(cdp_base_url: str) -> str:
    """Discover the browser-level `webSocketDebuggerUrl` from a CDP endpoint's
    `/json/version`, rewritten to ride over the same scheme/host/port as `cdp_base_url`.

    Used by the local backends (Podman / Daytona) that expose a per-browser HTTP CDP endpoint
    but no pre-baked wss connect URL: their `get_cdp_websocket_remote_url` resolves the
    cdp base URL (per browser) and then this helper does the standard /json/version probe.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{cdp_base_url}/json/version")
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        logger.debug(f"[CDP] CDP json version gives {data}")
        return rewrite_ws_url(str(data["webSocketDebuggerUrl"]), cdp_base_url)


async def get_page_websocket_debugger_url(cdp_base_url: str, page_id: str) -> str | None:
    """Discover the per-page `webSocketDebuggerUrl` for `page_id` from a CDP endpoint's
    `/json/list`, rewritten to ride over the same scheme/host/port as `cdp_base_url`.

    Used by the local backends (Podman / Daytona) that expose per-page webSocketDebuggerUrls
    over HTTP — their `get_devtools_websocket_remote_url` resolves the cdp base URL (per
    browser) and then this helper does the standard /json/list probe. Returns None when the
    page id is not present in the listing (page already closed or not yet registered).
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{cdp_base_url}/json/list")
        response.raise_for_status()
        raw: Any = response.json()
        if not isinstance(raw, list):
            return None
        for item in cast(list[dict[str, Any]], raw):
            if item.get("id") == page_id:
                ws_url = item.get("webSocketDebuggerUrl")
                return rewrite_ws_url(str(ws_url), cdp_base_url) if ws_url else None
        return None


def new_browser_id() -> str:
    """A server-assigned browser id. `B`-prefixed so it is distinguishable from client-supplied ids."""
    return "B" + generate(FRIENDLY_CHARS, 8)


class BrowserNotFound(Exception):
    """Raised by a backend when a browser does not exist; endpoints map it to HTTP 404."""


class ProxyVerificationError(Exception):
    """Raised when a configured (mandatory) proxy fails to apply or the egress IP is unchanged.

    Also used as the generic "no Best-of-N candidate succeeded" signal: `best_of_n` raises it when
    every candidate's `create_browser` failed, so the endpoint maps it to 500 and the client can
    retry rather than receive an unproxied / partial browser."""


@runtime_checkable
class _CleanupBackend(Protocol):
    """The slice of `Backend` that loser cleanup needs: check a candidate exists, delete it."""

    async def delete_browser(self, browser_id: str) -> dict[str, Any]: ...

    async def browser_exists(self, browser_id: str) -> bool: ...


@runtime_checkable
class _BestOfNBackend(_CleanupBackend, Protocol):
    """The slice of `Backend` that the best-of-N race needs: create a candidate, plus the cleanup
    slice. Every concrete `Backend` satisfies this structurally, so the helper accepts any backend
    while tests can pass a minimal fake."""

    async def create_browser(
        self,
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> dict[str, Any]: ...


async def best_of_n(
    backend: _BestOfNBackend,
    n: int,
    origin_ip: str | None,
    target_domain: str | None,
    browser_type: str | None,
) -> tuple[str, dict[str, Any]]:
    """Race `n` cold-create candidates; keep the first that fully succeeds, delete the rest.

    Each candidate is an independent browser with its own server-assigned id (so their names never
    collide). A candidate "succeeds" when `backend.create_browser` returns; one that raises (a
    Daytona proxy-verification failure, a podman container-launch failure, an upstream 5xx on the
    Fleet backend) is treated as a loser. The first to succeed wins; the rest are torn down in the
    background. If every candidate fails, this raises `ProxyVerificationError` rather than
    returning an unproxied / partial browser, so the client can retry.
    """
    ids = [new_browser_id() for _ in range(n)]
    logger.info(f"Best-of-{n} browser race: {ids}")

    async def _candidate(bid: str) -> tuple[str, dict[str, Any]]:
        return bid, await backend.create_browser(bid, origin_ip, target_domain, browser_type)

    tasks = [asyncio.create_task(_candidate(b)) for b in ids]
    winner: tuple[str, dict[str, Any]] | None = None
    for coro in asyncio.as_completed(tasks):
        try:
            winner = await coro
        except Exception as e:
            logger.warning(f"Best-of-N candidate failed: {type(e).__name__}: {e}")
            continue
        break

    for task in tasks:
        if not task.done():
            task.cancel()

    if winner is None:
        raise ProxyVerificationError("Best-of-N: no browser candidate started successfully")
    logger.info(f"Best-of-N winner: {winner[0]}")
    asyncio.create_task(_cleanup_losers(backend, ids, winner_id=winner[0]))
    return winner


async def _cleanup_losers(backend: _CleanupBackend, ids: list[str], *, winner_id: str) -> None:
    """Best-effort delete every losing candidate; the winner is kept.

    A losing browser may still be materializing server-side (or visible but mid state-change) when
    its create task was cancelled, so each loser is retried until it settles (appear -> delete) so
    we don't leak it. A loser that never reached the backend's resource allocation (its create was
    cancelled before anything was created) will spin the retry window and then give up; a final
    best-effort `delete_browser` is still issued (and failures swallowed) so backends that keep
    per-id handles outside the browser itself (e.g. Daytona's per-id lock) get to release them.
    The backend's native idle cleanup is the final backstop for anything left."""
    for browser_id in ids:
        if browser_id == winner_id:
            continue
        deleted = False
        for _ in range(8):
            if await backend.browser_exists(browser_id):
                try:
                    mark_browser_deleting(browser_id)
                    await backend.delete_browser(browser_id)
                    logger.info(f"Best-of-N: deleted losing candidate {browser_id}")
                    deleted = True
                    break
                except Exception as e:
                    logger.debug(f"Best-of-N: delete retry for loser {browser_id}: {e}")
            await asyncio.sleep(5)
        if not deleted:
            # Never confirmed it existed; still issue one idempotent delete so per-id backend
            # state (e.g. Daytona's lock dict) is cleaned up. Swallow the not-found failure.
            try:
                mark_browser_deleting(browser_id)
                await backend.delete_browser(browser_id)
            except Exception as e:
                logger.debug(f"Best-of-N: final delete for loser {browser_id} failed: {e}")
            logger.warning(f"Best-of-N: gave up deleting loser {browser_id}")


@runtime_checkable
class Backend(Protocol):
    """A backend creates a browser on demand and tears it down afterwards.

    Three implementations select at startup via `create_backend()`:
    - `FleetBackend`: proxy the REST API to an external Chrome Fleet (`CHROMEFLEET_URL`).
    - `DaytonaBackend`: on-demand Daytona sandboxes (`BROWSER_BACKEND=daytona`).
    - `PodmanBackend`: local podman containers (default).

    Backends only own per-browser CRUD keyed by a caller-supplied `browser_id`. The
    server-assigned-id + best-of-N policy lives inline in the `POST /api/v1/browsers` endpoint
    in `router.py`, which races `create_browser` across several ids; the REST endpoints depend on
    these methods plus that policy.
    """

    async def shutdown(self) -> None: ...

    @property
    def default_best_of_n(self) -> int: ...

    async def create_browser(
        self,
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> dict[str, Any]: ...

    async def get_browser(
        self, browser_id: str, origin_ip: str | None, target_domain: str | None
    ) -> dict[str, Any]: ...

    async def delete_browser(self, browser_id: str) -> dict[str, Any]: ...

    async def list_browser_ids(self, scope: BROWSER_SCOPE = "all") -> list[str]:
        """Every browser this backend knows about.

        Pass `"live"` when you intend to reach one (page lookup, CDP relay) rather than
        report inventory. Backends whose listing already holds only reachable browsers
        return the same ids either way; the distinction matters for Daytona, which keeps
        stopped and archived sandboxes in its listing.
        """
        ...

    async def browser_exists(self, browser_id: str) -> bool: ...

    async def cleanup_idle(self) -> list[str]: ...

    async def get_cdp_base_url(self, browser_id: str) -> str: ...

    def cdp_websocket_base(self) -> str | None: ...

    async def get_cdp_websocket_remote_url(self, browser_id: str) -> str | None:
        """The single wss URL the router should relay a /cdp/{browser_id} client socket to.
        Owned end-to-end by each backend, so the router can stay agnostic of how the URL is
        derived (per-session connectUrl for Browserbase, the external fleet's /cdp relay for
        Fleet, the per-browser /json/version discovery for Podman/Daytona). Returns None when
        the URL can't (yet) be resolved."""

    def cdp_targets_need_namespacing(self) -> bool:
        """Whether `websocket_proxy` should rewrite target ids to namespace them by `browser_id`
        when relaying to this backend's URL. True for backends that hand back a single browser's
        raw CDP socket (Podman / Daytona); False for the Fleet relay, whose /cdp proxy already
        namespaces target ids itself (so the router patching again would double-prefix)."""
        ...

    async def get_devtools_websocket_remote_url(
        self, client_ws: WebSocket, browser_id: str, page_id: str
    ) -> str | None:
        """Resolve the remote wss URL the router should relay a /devtools/{path} client socket
        to, or self-relay the socket and return a sentinel "" telling the router to skip its own
        `websocket_proxy`. Owned end-to-end by each backend.

        Returns:
          - a non-empty URL string: the router relays via `websocket_proxy(client_ws, url, …)`.
            Fleet returns the external /devtools relay URL; Podman/Daytona return the per-page
            `/json/list` webSocketDebuggerUrl.
          - "": the backend already relayed `client_ws`, the router must skip `websocket_proxy`.
          - None: the URL can't (yet) be resolved — the router retries up to 10 times before
            giving up and closing the client with 4502.
        """
        ...

    async def get_vnc_endpoint(self, browser_id: str) -> tuple[str, int] | None: ...

    async def get_live_view_url(self, browser_id: str) -> str | None: ...


def create_backend() -> Backend:
    if settings.CHROMEFLEET_URL:
        from getgather.browsers.fleet_browsers import FleetBackend

        return FleetBackend()

    if settings.BROWSER_BACKEND == "daytona":
        from getgather.browsers.daytona_browsers import DaytonaBackend

        return DaytonaBackend(
            settings.DAYTONA_API_KEY, settings.DAYTONA_API_URL, settings.DAYTONA_SNAPSHOT
        )

    if settings.BROWSER_BACKEND == "browserbase":
        from getgather.browsers.browserbase_browsers import BrowserbaseBackend

        return BrowserbaseBackend()

    from getgather.browsers.podman_browsers import PodmanBackend

    return PodmanBackend()
