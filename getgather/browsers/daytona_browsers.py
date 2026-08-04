import asyncio
import time
from typing import Any

from daytona import (
    AsyncDaytona,
    AsyncSandbox,
    CreateSandboxFromSnapshotParams,
    DaytonaConfig,
    DaytonaConflictError,
    DaytonaNotFoundError,
    ListSandboxesQuery,
)
from fastapi import WebSocket
from loguru import logger

from getgather.browsers.backend import (
    BROWSER_NAME_PREFIX,
    BrowserNotFound,
    ProxyVerificationError,
    get_browser_websocket_debugger_url,
    get_page_websocket_debugger_url,
)
from getgather.browsers.residential_proxy import get_proxy_config
from getgather.config import settings

CDP_PORT = 9222
VNC_PORT = 8080

# The chromium s6 service reads this env var on first boot to pick the browser (Chrome default,
# `cloak` for CloakBrowser). Set at sandbox-create time so a fresh sandbox boots straight into the
# selected browser — no post-start swap. See chrome-live chromium/run.
ACTIVE_BROWSER_ENV = "ACTIVE_BROWSER"

DEFAULT_BEST_OF_N = 3

# Chrome stores last_visit_time as microseconds since 1601-01-01; this offset converts to unix epoch.
CHROMIUM_EPOCH_OFFSET_SECONDS = 11644473600
CHROME_HISTORY_PATH = "/home/user/chrome-profile/Default/History"

# Daytona auto-stops a sandbox after this many idle minutes (no sandbox event), parking it at
# $0 compute. Once it has been continuously stopped for TTL_MINUTES, Daytona's native
# auto_delete_interval (set at create time) deletes it.
AUTO_STOP_MINUTES = 15
TTL_MINUTES = 60

# CDP is reached over a Daytona signed preview URL: a self-authenticating HTTPS reverse proxy to
# the sandbox's port 9222 (https://9222-<token>.<proxy-domain>).
#
# SECURITY: this URL is PUBLICLY reachable on the internet. The sandbox is created public=False,
# but a signed preview URL bypasses that — the only credential is the token embedded in the URL
# itself, so anyone who obtains the cdp_url can drive the browser's CDP until the token expires.
# Treat cdp_url as a bearer secret. The token is stateless and survives sandbox restarts.
SIGNED_URL_TTL_SECONDS = 3600

# An open live-view iframe streams noVNC over port 8080, which Daytona counts as sandbox activity
# and uses to reset auto_stop_interval. The dashboard embeds a live view per browser, so without a
# gate just viewing it would keep every sandbox alive indefinitely (defeating auto-stop). Only hand
# out the live URL when the browser has had real Chrome activity within this window; otherwise let
# the idle sandbox auto-stop. A sandbox with no history yet (e.g. a fresh sign-in) is treated as
# active so the primary "watch the sign-in" flow keeps working.
LIVE_VIEW_MAX_IDLE_SECONDS = 3600

LABEL_FLEET = "fleet"


def _sandbox_name(browser_id: str) -> str:
    return f"{BROWSER_NAME_PREFIX}{browser_id}"


async def _configure_sandbox_proxy(sandbox: AsyncSandbox, proxy_url: str) -> bool:
    stripped = proxy_url.removeprefix("http://")
    cmds = [
        "sed -i '/^Upstream http/d' /app/tinyproxy.conf",
        f"sed -i '$ a\\Upstream http {stripped}' /app/tinyproxy.conf",
        "sudo /command/s6-svc -r /run/service/tinyproxy",
        "while ! curl -s -o /dev/null -x http://localhost:8119 http://tinyproxy.stats; do sleep 0.1; done",
    ]
    for cmd in cmds:
        try:
            response = await sandbox.process.exec(cmd)
        except Exception as e:
            logger.warning(f"Proxy config failed on {sandbox.name}: {type(e).__name__}: {e}")
            return False
        if response.exit_code != 0:
            logger.warning(
                f"Proxy config exit={response.exit_code} on {sandbox.name}: cmd={cmd!r} stderr={response.result!r}"
            )
            return False
    return True


async def _get_sandbox_public_ip(
    sandbox: AsyncSandbox, *, retries: int = 5, retry_delay: float = 2.0
) -> str | None:
    cmd = "curl -s --max-time 10 --proxy http://127.0.0.1:8119 https://ip.fly.dev"
    for attempt in range(1, retries + 1):
        try:
            response = await sandbox.process.exec(cmd)
            ip = response.result.strip() if response.exit_code == 0 else ""
            if ip:
                return ip
        except Exception as e:
            logger.debug(f"IP check {attempt}/{retries} on {sandbox.name} failed: {e}")
        if attempt < retries:
            await asyncio.sleep(retry_delay)
    logger.warning(f"IP check on {sandbox.name} failed after {retries} attempts")
    return None


async def _configure_remote_sandbox(
    sandbox: AsyncSandbox,
    browser_id: str,
    origin_ip: str | None,
    target_domain: str | None,
) -> None:
    """Apply the residential proxy to the sandbox and verify egress IP changed.

    The proxy is mandatory: if one is configured it MUST apply and change the egress IP, otherwise
    this raises ProxyVerificationError. If no proxy is configured, this is a no-op (proxy is not
    required). Callers let the error propagate; best-of-N treats a raising candidate as failed."""
    proxy_config = await get_proxy_config(origin_ip, target_domain, settings)
    proxy_url = proxy_config.get_proxy_url(browser_id) if proxy_config else None

    if not proxy_url:
        return  # no proxy configured; proxy is not required for this browser

    ip_before = await _get_sandbox_public_ip(sandbox)
    logger.debug(f"Sandbox {sandbox.name} IP before proxy: {ip_before}")

    ok = await _configure_sandbox_proxy(sandbox, proxy_url)
    if not ok:
        raise ProxyVerificationError(f"Proxy failed to apply on {sandbox.name}")

    ip_after = await _get_sandbox_public_ip(sandbox)
    if ip_after is None:
        # Distinct from "unchanged": the egress IP check itself failed (curl/exec timeout),
        # so we cannot confirm the proxy either way. Fail the candidate with an accurate reason.
        raise ProxyVerificationError(
            f"Could not verify egress IP on {sandbox.name} (IP check failed)"
        )
    if ip_before is not None and ip_before == ip_after:
        raise ProxyVerificationError(
            f"Sandbox {sandbox.name} IP unchanged after proxy: {ip_before}"
        )
    logger.info(f"Sandbox {sandbox.name} IP after proxy: {ip_after} (before: {ip_before})")
    return


def _browser_id_from_name(name: str) -> str:
    return name.removeprefix(BROWSER_NAME_PREFIX)


class DaytonaBackend:
    """Launch a Daytona sandbox per browser on demand (no pool).

    The browser_id -> sandbox mapping is the deterministic sandbox name plus labels, so there is
    no local state. CDP is reached over a Daytona signed preview URL: a public, internet-reachable,
    self-authenticating HTTPS reverse proxy to port 9222 (no inbound networking into the sandbox is
    required). Live view embeds the snapshot's built-in noVNC on port 8080 via a signed preview URL.
    Residential proxy/geo-IP are supported via tinyproxy (preconfigured in the snapshot, same as podman).

    Sandbox teardown is owned by Daytona: auto_stop_interval stops idle sandboxes and
    auto_delete_interval deletes them once continuously stopped past the TTL (both set at create
    time in _create).
    """

    def __init__(self, api_key: str, api_url: str, snapshot: str) -> None:
        self.snapshot = snapshot
        self.client = AsyncDaytona(DaytonaConfig(api_key=api_key, api_url=api_url or None))
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def default_best_of_n(self) -> int:
        return DEFAULT_BEST_OF_N

    async def shutdown(self) -> None:
        await self.client.close()

    async def create_browser(
        self,
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> dict[str, Any]:
        lock = self._locks.setdefault(browser_id, asyncio.Lock())
        async with lock:
            sandbox = await self._ensure(browser_id, browser_type)
            # Proxy is mandatory when configured: let ProxyVerificationError propagate (the endpoint
            # maps it to 500) so the client can retry rather than get an unproxied browser.
            await _configure_remote_sandbox(sandbox, browser_id, origin_ip, target_domain)
            return await self._get_info(sandbox)

    async def get_browser(
        self, browser_id: str, origin_ip: str | None, target_domain: str | None
    ) -> dict[str, Any]:
        sandbox = await self._get(_sandbox_name(browser_id))
        if sandbox is None:
            raise BrowserNotFound(browser_id)
        return await self._get_info(sandbox)

    async def delete_browser(self, browser_id: str) -> dict[str, Any]:
        name = _sandbox_name(browser_id)
        sandbox = await self._get(name)
        self._locks.pop(browser_id, None)
        if sandbox is None:
            return {"status": "not found"}
        await sandbox.delete()
        return {"status": "deleted"}

    async def browser_exists(self, browser_id: str) -> bool:
        return await self._get(_sandbox_name(browser_id)) is not None

    async def list_browser_ids(self) -> list[str]:
        browser_ids: list[str] = []
        async for sandbox in self.client.list(ListSandboxesQuery(labels={LABEL_FLEET: "1"})):
            # A just-deleted sandbox lingers briefly as `DESTROYED_<name>_<ts>`; only our names count.
            if sandbox.name and sandbox.name.startswith(BROWSER_NAME_PREFIX):
                browser_ids.append(_browser_id_from_name(sandbox.name))
        return browser_ids

    async def cleanup_idle(self) -> list[str]:
        # Teardown is owned by Daytona's native auto_stop_interval + auto_delete_interval (set in
        # _create), so there is no reconcile sweep to run here.
        return []

    async def get_cdp_base_url(self, browser_id: str) -> str:
        sandbox = await self._get(_sandbox_name(browser_id))
        if sandbox is None:
            raise BrowserNotFound(browser_id)
        signed = await sandbox.create_signed_preview_url(
            CDP_PORT, expires_in_seconds=SIGNED_URL_TTL_SECONDS
        )
        return signed.url

    def cdp_websocket_base(self) -> None:
        # CDP is reached per-sandbox over a signed HTTPS preview URL via get_cdp_base_url +
        # /json/version, not a shared websocket proxy, so there is no relay base.
        return None

    async def get_cdp_websocket_remote_url(self, browser_id: str) -> str | None:
        # Discover the browser-level webSocketDebuggerUrl over the signed preview proxy's
        # /json/version (get_cdp_base_url → /json/version). May raise BrowserNotFound if the
        # sandbox was torn down underneath the router, or transiently on a boot race — the router
        # retries 10x before giving up.
        cdp_base_url = await self.get_cdp_base_url(browser_id)
        return await get_browser_websocket_debugger_url(cdp_base_url)

    def cdp_targets_need_namespacing(self) -> bool:
        # The per-sandbox socket reports raw target ids; the router namespaces them by browser_id
        # so the devtools route can route /devtools/{browser_id@page_id} back to this sandbox.
        return True

    async def get_devtools_websocket_remote_url(
        self, client_ws: WebSocket, browser_id: str, page_id: str
    ) -> str | None:
        # Discover the per-page webSocketDebuggerUrl over the signed preview proxy's /json/list
        # (get_cdp_base_url → /json/list). May return None transiently (page just registered /
        # already closed) or raise BrowserNotFound if the sandbox was torn down — the router
        # retries 10x before giving up.
        del client_ws
        cdp_base_url = await self.get_cdp_base_url(browser_id)
        return await get_page_websocket_debugger_url(cdp_base_url, page_id)

    async def get_vnc_endpoint(self, browser_id: str) -> tuple[str, int] | None:
        return None  # no raw VNC port; live view uses get_live_view_url (noVNC on VNC_PORT)

    async def get_live_view_url(self, browser_id: str) -> str | None:
        sandbox = await self._get(_sandbox_name(browser_id))
        if sandbox is None:
            raise BrowserNotFound(browser_id)
        if sandbox.state != "started":
            return None  # a stopped sandbox has nothing to show and must not be woken to watch it

        # Gate on recent activity so an embedded live view can't keep an idle sandbox alive. A
        # missing timestamp (no history yet / read error) is treated as active to avoid hiding the
        # live view for a fresh sign-in.
        last_activity = await self._get_last_activity(sandbox)
        if last_activity is not None:
            idle_seconds = time.time() - last_activity
            if idle_seconds > LIVE_VIEW_MAX_IDLE_SECONDS:
                logger.info(
                    f"Skipping live view for idle sandbox {sandbox.name}: "
                    f"last activity {idle_seconds:.0f}s ago (> {LIVE_VIEW_MAX_IDLE_SECONDS}s)"
                )
                return None

        signed = await sandbox.create_signed_preview_url(
            VNC_PORT, expires_in_seconds=SIGNED_URL_TTL_SECONDS
        )
        return signed.url

    async def _ensure(self, browser_id: str, browser_type: str | None = None) -> AsyncSandbox:
        name = _sandbox_name(browser_id)
        sandbox = await self._get(name)
        if sandbox is None:
            sandbox = await self._create(name, browser_type)

        # Browser selection is baked into the sandbox at create time via the ACTIVE_BROWSER env var
        # (see _create), so both a fresh create and a resumed start boot the right browser directly —
        # no post-start swap here.
        if sandbox.state != "started":
            logger.info(f"Starting Daytona sandbox {name} (state={sandbox.state})")
            await sandbox.start()

        return sandbox

    async def _get_info(self, sandbox: AsyncSandbox) -> dict[str, Any]:
        signed = await sandbox.create_signed_preview_url(
            CDP_PORT, expires_in_seconds=SIGNED_URL_TTL_SECONDS
        )
        return {
            "hostname": sandbox.name,
            "cdp_url": signed.url,  # public, internet-reachable; bearer secret (see SIGNED_URL_TTL_SECONDS)
            "app_state": sandbox.state,
            "last_activity_timestamp": await self._get_last_activity(sandbox),
        }

    async def _get_last_activity(self, sandbox: AsyncSandbox) -> float | None:
        """Most recent Chrome visit time as a unix timestamp, read from the browser history DB.

        Mirrors PodmanBackend's get_container_last_activity: copy the (Chrome-locked) History DB,
        then query it with sqlite3. Requires a running sandbox; returns None otherwise or on error.
        """
        if sandbox.state != "started":
            return None
        command = (
            f"cp {CHROME_HISTORY_PATH} /tmp/cf-history.db && "
            "sqlite3 /tmp/cf-history.db 'select MAX(last_visit_time) from urls;'"
        )
        try:
            response = await sandbox.process.exec(command)
        except Exception as e:
            logger.warning(
                f"Unexpected error fetching last activity for {sandbox.name}: {type(e).__name__}: {e}"
            )
            return None
        output = response.result.strip()
        if response.exit_code != 0 or not output:
            logger.warning(
                f"Failed to fetch last activity for {sandbox.name}: exit={response.exit_code} result={output!r}"
            )
            return None
        try:
            chromium_time = float(output)
        except ValueError:
            return None
        return (chromium_time / 1_000_000) - CHROMIUM_EPOCH_OFFSET_SECONDS

    async def _get(self, name: str) -> AsyncSandbox | None:
        try:
            return await self.client.get(name)
        except DaytonaNotFoundError:
            return None

    async def _create(self, name: str, browser_type: str | None = None) -> AsyncSandbox:
        # Select the browser at boot via env; the chromium s6 service reads ACTIVE_BROWSER on first
        # boot (chrome-live). Driven by the per-request `browser_type` (x-browser-type header); Chrome
        # is the default. Only set the env for a non-Chrome pick: Chrome is the snapshot default, so
        # a None env_vars keeps the create call identical to a Chrome-only snapshot (older Daytona
        # backends reject env_vars they don't expect).
        env_vars: dict[str, str] | None = None
        sandbox_env: dict[str, str] = {}
        if browser_type and browser_type != "chrome":
            sandbox_env[ACTIVE_BROWSER_ENV] = browser_type
        if browser_type == "cloak" and settings.CLOAKBROWSER_LICENSE_KEY:
            sandbox_env["CLOAKBROWSER_LICENSE_KEY"] = settings.CLOAKBROWSER_LICENSE_KEY
        if sandbox_env:
            env_vars = sandbox_env
        safe_env_vars = (
            {
                key: ("***" if key == "CLOAKBROWSER_LICENSE_KEY" else value)
                for key, value in env_vars.items()
            }
            if env_vars
            else None
        )
        logger.info(f"Daytona sandbox env_vars for {name}: {safe_env_vars}")
        params = CreateSandboxFromSnapshotParams(
            snapshot=self.snapshot,
            name=name,
            labels={LABEL_FLEET: "1"},
            env_vars=env_vars,
            public=False,
            auto_stop_interval=AUTO_STOP_MINUTES,
            # delete after TTL_MINUTES continuously stopped; Daytona owns teardown
            auto_delete_interval=TTL_MINUTES,
        )
        logger.info(f"Creating Daytona sandbox {name} from snapshot {self.snapshot}")
        try:
            return await self.client.create(params, timeout=400)
        except DaytonaConflictError:
            # Lost a concurrent create race (same deterministic name); adopt the winner.
            logger.info(f"Daytona sandbox {name} already exists, adopting")
            existing = await self._get(name)
            if existing is None:
                raise
            return existing
