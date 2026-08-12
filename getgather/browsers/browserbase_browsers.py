import asyncio
import json
import os
from typing import Any

import httpx
import websockets
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState
from loguru import logger
from websockets.exceptions import ConnectionClosed, InvalidStatus

from getgather.browsers.backend import BROWSER_SCOPE, BrowserNotFound
from getgather.browsers.residential_proxy import get_proxy_config
from getgather.config import settings

DEFAULT_BEST_OF_N = 1

BROWSERBASE_API_URL = "https://api.browserbase.com/v1/sessions"


async def websocket_proxy_attached(
    client_ws: WebSocket,
    remote_url: str,
    target_id: str,
) -> None:
    """Bridge a per-page /devtools WebSocket to a multiplexed browser-level CDP socket
    (a Browserbase connectUrl) by sending Target.attachToTarget with flatten=true, then
    relaying only that session's frames:

    - Outbound (client -> remote): inject "sessionId" into every CDP command (the client speaks
      flat per-page mode, the remote speaks multiplexed "flatten" mode).
    - Inbound  (remote -> client): forward only frames whose "sessionId" matches the one returned
      by attachToTarget, and strip that field before sending so the client sees plain per-page
      frames (responses with `id`, events without).

    This is what Puppeteer/Playwright do internally when connecting to a browser endpoint and
    then driving a single page — here we expose it as an opaque /devtools/<id> socket so clients
    that follow Chrome's "/devtools/page/<id>" URL convention (zendriver's `Tab.websocket_url`)
    keep working unchanged against Browserbase's multiplexed connectUrl.

    Single attempt: by the time a /devtools request arrives for a Browserbase session, the chrome
    inside it has already been confirmed CDP-ready by `BrowserbaseBackend._wait_until_cdp_ready`
    during `create_browser`. Retrying the wss upgrade here would just paper over a race that no
    longer exists; if the connect fails now it's a real failure, surfaced to the client as a
    close (4502).
    """
    attach_req = json.dumps({
        "id": -1,
        "method": "Target.attachToTarget",
        "params": {"targetId": target_id, "flatten": True},
    })

    try:
        async with websockets.connect(
            remote_url,
            ping_interval=60,
            ping_timeout=30,
            close_timeout=7200,
            max_size=10 * 1024 * 1024,
        ) as remote_ws:
            logger.info(f"[CDP] Connected to remote for attach target={target_id}")

            await remote_ws.send(attach_req)
            session_id: str | None = None
            # Wait for the attachToTarget response (id == -1). Any events that arrive first
            # (none expected on a fresh socket before AutoAttach is set) are discarded.
            while session_id is None:
                raw = await remote_ws.recv()
                if not isinstance(raw, str):
                    continue
                try:
                    data: Any = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                if data.get("id") != -1:  # pyright: ignore[reportUnknownMemberType]
                    continue
                if "error" in data:
                    err: Any = data.get("error")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                    logger.error(f"[CDP] attachToTarget failed for {target_id}: {err}")
                    if client_ws.client_state == WebSocketState.CONNECTED:
                        await client_ws.close(code=4502, reason=f"attachToTarget failed: {err}")
                    return
                result: Any = data.get("result")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                if isinstance(result, dict):
                    sid: Any = result.get("sessionId")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                    if isinstance(sid, str):
                        session_id = sid
            logger.info(f"[CDP] Attached to {target_id} as sessionId={session_id}")

            async def client_to_remote() -> None:
                try:
                    while True:
                        message = await client_ws.receive_text()
                        try:
                            parsed: Any = json.loads(message)
                        except (json.JSONDecodeError, TypeError):
                            # Forward non-JSON frames untouched (shouldn't happen on a CDP WS).
                            await remote_ws.send(message)
                            continue
                        if isinstance(parsed, dict) and "sessionId" not in parsed:
                            parsed["sessionId"] = session_id
                            message = json.dumps(parsed)
                        logger.debug(f"[CDP] Client -> Remote: {message[:100]}")
                        await remote_ws.send(message)
                except (WebSocketDisconnect, RuntimeError):
                    logger.info("[CDP] Client disconnected")
                except Exception as e:
                    logger.error(f"[CDP] client_to_remote error: {type(e).__name__}: {e}")

            async def remote_to_client() -> None:
                try:
                    async for message in remote_ws:
                        msg_text = message if isinstance(message, str) else message.decode()
                        try:
                            parsed: Any = json.loads(msg_text)
                        except (json.JSONDecodeError, TypeError):
                            if client_ws.client_state == WebSocketState.CONNECTED:
                                await client_ws.send_text(msg_text)
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        # Only forward frames that belong to our attached session.
                        if parsed.get("sessionId") != session_id:  # pyright: ignore[reportUnknownMemberType]
                            continue
                        parsed.pop("sessionId", None)  # pyright: ignore[reportUnknownMemberType]
                        msg_text = json.dumps(parsed)
                        logger.debug(f"[CDP] Remote -> Client: {msg_text[:100]}")
                        if client_ws.client_state == WebSocketState.CONNECTED:
                            await client_ws.send_text(msg_text)
                        else:
                            logger.debug("[CDP] Client not connected, breaking")
                            break
                except ConnectionClosed as e:
                    logger.info(f"[CDP] Remote disconnected: code={e.code} reason={e.reason}")
                except Exception as e:
                    logger.error(f"[CDP] remote_to_client error: {type(e).__name__}: {e}")

            tasks = [
                asyncio.create_task(client_to_remote()),
                asyncio.create_task(remote_to_client()),
            ]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            return
    except OSError as e:
        logger.warning(f"[CDP] Attach to {target_id} failed (OSError): {e}")
    except InvalidStatus as e:
        logger.warning(f"[CDP] Attach to {target_id} rejected with HTTP {e.response.status_code}")
    except Exception as e:
        logger.warning(f"[CDP] Attach to {target_id} failed ({type(e).__name__}): {e}")

    if client_ws.client_state == WebSocketState.CONNECTED:
        await client_ws.close(code=4502, reason="Remote server unreachable")


def _api_key() -> str:
    return settings.BROWSERBASE_API_KEY or os.environ["BROWSERBASE_API_KEY"]


class BrowserbaseBackend:
    """A backend that creates remote browsers via the Browserbase API.

    Only `create_browser` is implemented: it POSTs to the Browserbase `/v1/sessions`
    endpoint with the `x-bb-api-key` header (read from the `BROWSERBASE_API_KEY` env var)
    and stores the returned `id -> connectUrl` mapping in an in-memory hashmap so the
    CDP proxy in `router.py` can later route `/cdp/<id>` traffic to the right connectUrl.

    The Browserbase-assigned session `id` is returned as the `browser_id`, ignoring the
    caller-supplied id (Browserbase creates its own). The router's `POST /api/v1/browsers`
    endpoint spreads the result over `{"browser_id": <server id>, **result}`, and because
    `result` contains `browser_id` the Browserbase UUID wins, so the client receives the
    real session id and can then point its CDP client at `/cdp/<that id>`.
    """

    def __init__(self) -> None:
        # browser_id -> connectUrl (wss://connect.usw2.browserbase.com/?signingKey=...)
        self._sessions: dict[str, str] = {}

    async def shutdown(self) -> None:
        return None

    @property
    def default_best_of_n(self) -> int:
        return DEFAULT_BEST_OF_N

    async def create_browser(
        self,
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,  # not supported by Browserbase; always their hosted Chrome
    ) -> dict[str, Any]:
        del browser_type  # not supported by Browserbase; always their hosted Chrome
        headers = {"Content-Type": "application/json", "x-bb-api-key": _api_key()}
        # keepAlive keeps the session running between CDP connections / after disconnects.
        # Without it, Browserbase ends the session the moment the first WS drops, and any
        # subsequent connect to the same signingKey returns HTTP 410 Gone.
        body: dict[str, Any] = {"keepAlive": True}
        proxy_config = await get_proxy_config(origin_ip, target_domain, settings)
        if proxy_config:
            proxy_url = proxy_config.get_proxy_url(browser_id)
            body["proxies"] = [{"type": "external", "server": proxy_url}]
            logger.info(
                f"Browserbase session proxy: provider={proxy_config.type_} "
                f"country={proxy_config.location.country}"
            )
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(BROWSERBASE_API_URL, headers=headers, json=body)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        bb_id = str(data["id"])
        connect_url = str(data["connectUrl"])
        self._sessions[bb_id] = connect_url
        logger.info(f"Browserbase session created: id={bb_id} connectUrl={connect_url}")
        # `timeout` is unset in the request body, so expiresAt reflects the project's
        # defaultTimeout (300s on project d50de03f). Nothing evicts `_sessions` on expiry, so
        # log the cache size here to watch dead entries accumulate across runs.
        logger.info(
            f"[BB-STATE] created id={bb_id} expiresAt={data.get('expiresAt')} "
            f"keepAlive={data.get('keepAlive')} cached={len(self._sessions)}"
        )
        await self._wait_until_cdp_ready(connect_url, bb_id)
        return {"browser_id": bb_id, "status": "created", "ip": None}

    async def _wait_until_cdp_ready(self, connect_url: str, browser_id: str) -> None:
        """Poll the session's multiplexed CDP endpoint until the chrome inside is up.

        Browserbase's POST /v1/sessions returns immediately with status RUNNING, but the chrome
        process inside the session is still booting (PENDING). The wss upgrade against
        wss://connect.*.browserbase.com surfaces that as HTTP 410 / OSError until ready.
        We probe by opening the socket, sending `Target.getTargets` (a browser-level command
        that needs no sessionId on a multiplexed connectUrl) and waiting for its `id`-matched
        response — that confirms CDP is established end-to-end. Retries/backoff are fixed
        here (independent of the /cdp upgrade retry policy in the router) so create-time
        readiness probing is governed by its own budget.

        If the budget is exhausted we release the session (so we don't leak a never-ready remote
        browser) and raise, so the caller surfaces the failure rather than handing back a dead id.
        """
        # Fixed readiness budget — separate from the router's /cdp upgrade retry policy so the
        # two flows can be tuned independently.
        attempts = 20
        delay = 1.0
        probe_id = -2
        probe_req = json.dumps({"id": probe_id, "method": "Target.getTargets"})
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                async with websockets.connect(
                    connect_url,
                    ping_interval=60,
                    ping_timeout=30,
                    close_timeout=30,
                    max_size=10 * 1024 * 1024,
                ) as remote_ws:
                    await remote_ws.send(probe_req)
                    # Read frames until the matching response arrives; any events that arrive
                    # first on a fresh socket are discarded.
                    while True:
                        raw = await remote_ws.recv()
                        if not isinstance(raw, str):
                            continue
                        try:
                            data: Any = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("id") != probe_id:  # pyright: ignore[reportUnknownMemberType]
                            continue
                        if "error" in data:
                            err: Any = data.get("error")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                            raise RuntimeError(f"Target.getTargets rejected: {err}")
                        logger.info(
                            f"[CDP] Browser {browser_id} ready after {attempt} probe attempt(s)"
                        )
                        return
            except OSError as e:
                last_exc = e
                logger.warning(
                    f"[CDP] Readiness probe {attempt}/{attempts} for {browser_id} "
                    f"failed (OSError): {e}"
                )
            except InvalidStatus as e:
                last_exc = e
                logger.warning(
                    f"[CDP] Readiness probe {attempt}/{attempts} for {browser_id} "
                    f"rejected with HTTP {e.response.status_code}"
                )
            except Exception as e:
                last_exc = e
                logger.warning(
                    f"[CDP] Readiness probe {attempt}/{attempts} for {browser_id} "
                    f"failed ({type(e).__name__}): {e}"
                )

            if attempt < attempts:
                await asyncio.sleep(delay)

        logger.error(f"[CDP] Browser {browser_id} not ready after {attempts} probes: {last_exc}")
        # Don't hand back a dead id — release the upstream session and surface the failure.
        try:
            await self.delete_browser(browser_id)
        except Exception as e:
            logger.warning(
                f"[CDP] Cleanup of never-ready {browser_id} failed: {type(e).__name__}: {e}"
            )
        raise RuntimeError(
            f"Browserbase session {browser_id} never became CDP-ready after {attempts} probes"
        )

    async def get_cdp_websocket_remote_url(self, browser_id: str) -> str | None:
        # Browserbase's per-session connectUrl (with signingKey in the query string) is the
        # multiplexed CDP socket the router relays to directly; there is no /json/version
        # discovery step. None for an unknown / already-released session.
        return self._sessions.get(browser_id)

    def cdp_targets_need_namespacing(self) -> bool:
        # The connectUrl is a single browser's socket; the router namespaces its target ids by
        # browser_id so the devtools route can route /devtools/{browser_id@page_id} back here.
        return True

    async def get_devtools_websocket_remote_url(
        self, client_ws: WebSocket, browser_id: str, page_id: str
    ) -> str | None:
        """Relay the /devtools socket to the session's multiplexed connectUrl by sending
        Target.attachToTarget and relaying only that session's frames (see
        `websocket_proxy_attached`), then return the "" sentinel so the router skips its own
        plain websocket_proxy (which can't drive a single page off a multiplexed socket).

        Single attempt — chrome inside the session has already been confirmed CDP-ready by
        `_wait_until_cdp_ready` during `create_browser`, so no boot-race retry is needed.

        Returns None for an unknown / already-released session (the router then retries the
        lookup 10x and finally closes with 4502)."""
        connect_url = self._sessions.get(browser_id)
        if connect_url is None:
            return None
        logger.info(f"[CDP] Bridging /devtools to connectUrl for {browser_id}/{page_id}")
        await websocket_proxy_attached(client_ws, connect_url, page_id)
        return ""  # self-relayed; sentinel tells the router to skip websocket_proxy

    async def get_browser(
        self, browser_id: str, origin_ip: str | None, target_domain: str | None
    ) -> dict[str, Any]:
        if browser_id not in self._sessions:
            raise BrowserNotFound(browser_id)
        return {"browser_id": browser_id, "status": "created", "ip": None}

    async def delete_browser(self, browser_id: str) -> dict[str, Any]:
        # Browserbase has no DELETE endpoint; sessions are closed by POSTing
        # `{"status": "REQUEST_RELEASE"}` to /v1/sessions/{id} (see
        # https://docs.browserbase.com/reference/api/update-a-session). Best-effort: always
        # clear the local id -> connectUrl mapping, regardless of whether the upstream call
        # succeeds (a 404 means the session is already gone, which is the desired state).
        self._sessions.pop(browser_id, None)
        logger.info(f"[BB-STATE] released id={browser_id} cached={len(self._sessions)}")
        try:
            headers = {"Content-Type": "application/json", "x-bb-api-key": _api_key()}
            body = {"status": "REQUEST_RELEASE"}
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{BROWSERBASE_API_URL}/{browser_id}", headers=headers, json=body
                )
                # 404 is not an error here — the session was already closed/expired upstream.
                if response.status_code != 404:
                    response.raise_for_status()
            logger.info(f"Browserbase session released: id={browser_id}")
        except Exception as e:
            logger.warning(f"Browserbase release failed for {browser_id}: {type(e).__name__}: {e}")
        return {"browser_id": browser_id, "status": "deleted"}

    async def browser_exists(self, browser_id: str) -> bool:
        return browser_id in self._sessions

    async def list_browser_ids(self, scope: BROWSER_SCOPE = "all") -> list[str]:
        return list(self._sessions.keys())

    async def log_session_state(self, browser_id: str, context: str) -> None:
        """Log Browserbase's own view of a session. Called by the router when a probe fails; the
        local `_sessions` map has no expiry, so only the API can say whether an id is still live.
        """
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(
                    f"{BROWSERBASE_API_URL}/{browser_id}", headers={"x-bb-api-key": _api_key()}
                )
            data: dict[str, Any] = response.json()
            logger.error(
                f"[BB-STATE] {context}: id={browser_id} http={response.status_code} "
                f"status={data.get('status')} startedAt={data.get('startedAt')} "
                f"endedAt={data.get('endedAt')} expiresAt={data.get('expiresAt')} "
                f"keepAlive={data.get('keepAlive')} cached={len(self._sessions)} "
                f"cached_ids={list(self._sessions.keys())}"
            )
        except Exception as e:
            logger.warning(
                f"[BB-STATE] {context}: id={browser_id} lookup failed: {type(e).__name__}: {e}"
            )

    async def cleanup_idle(self) -> list[str]:
        return []

    async def get_cdp_base_url(self, browser_id: str) -> str:
        raise NotImplementedError(
            "BrowserbaseBackend serves CDP via its connectUrl, not the /json/version flow"
        )

    def cdp_websocket_base(self) -> str | None:
        # Each session has its own connectUrl with query params (the signing key), so
        # there is no shared relay base. The router resolves the connectUrl per browser_id via
        # `get_cdp_websocket_remote_url` instead.
        return None

    async def get_vnc_endpoint(self, browser_id: str) -> tuple[str, int] | None:
        return None

    async def get_live_view_url(self, browser_id: str) -> str | None:
        if browser_id not in self._sessions:
            return None
        headers = {"x-bb-api-key": _api_key()}
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                response = await client.get(
                    f"{BROWSERBASE_API_URL}/{browser_id}/debug", headers=headers
                )
                response.raise_for_status()
            except httpx.HTTPError as e:
                logger.warning(f"Browserbase live view lookup failed for {browser_id}: {e}")
                return None
        data: dict[str, Any] = response.json()
        url: Any = data.get("debuggerFullscreenUrl")  # pyright: ignore[reportUnknownMemberType]
        return url if isinstance(url, str) else None
