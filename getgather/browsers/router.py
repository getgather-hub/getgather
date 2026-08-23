import asyncio
import html
import json
from typing import Any

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketState
from loguru import logger
from websockets.exceptions import ConnectionClosed

from getgather.browsers import daytona_probe
from getgather.browsers.backend import (
    Backend,
    BrowserNotFound,
    best_of_n,
    create_backend,
    new_browser_id,
)
from getgather.cdp_client import CDPClient, open_cdp_url
from getgather.config import settings

router = APIRouter()

backend: Backend = create_backend()
logger.info(f"Using browser backend: {backend.__class__.__name__}")


async def open_browser_cdp_client(browser_id: str) -> CDPClient | None:
    """Open a browser-level CDP client for `browser_id`, usable for direct commands like
    Target.getTargets. Returns None when the backend has no browser-level socket the router
    can open directly (Fleet relays /cdp and /devtools to an external proxy instead).

    - Browserbase: open the per-session connectUrl (signing key in the query string).
    - Podman / Daytona: /json/version discovers the browser webSocketDebuggerUrl, then open it.
    - Fleet: cdp_websocket_base() is the shared relay; never enumerated here.
    """
    if backend.cdp_websocket_base() is not None:
        return None  # Fleet: relay-only, no browser-level socket to open directly.
    ws_url = await backend.get_cdp_websocket_remote_url(browser_id)
    if ws_url is None:
        return None
    return await open_cdp_url(ws_url, timeout=10.0)


async def get_page_list(browser_id: str) -> list[str]:
    # Page enumeration is done over the browser-level CDP socket by sending Target.getTargets
    # and filtering for type == "page" — the same call pages_api_router.list_pages uses. This
    # works uniformly for every backend that exposes a browser-level socket (Browserbase via
    # connectUrl, Podman/Daytona via /json/version discovery); Fleet relays /devtools to its
    # own proxy and never reaches this path, so open_browser_cdp_client returns None there.
    try:
        client = await open_browser_cdp_client(browser_id)
    except (
        BrowserNotFound,
        httpx.HTTPStatusError,
        httpx.TimeoutException,
        websockets.exceptions.InvalidStatus,
    ) as e:
        # A Daytona sandbox that is stopped or archived answers 400/404 on its signed
        # preview URL, and a deleted one raises BrowserNotFound (Browserbase answers 410
        # for an ended session). A sandbox that is up but unresponsive times out on the
        # /json/version probe (httpx.TimeoutException), or its CDP websocket gets rejected
        # with a 502 from the fleet's front proxy (websockets.exceptions.InvalidStatus). In
        # every case the browser has no reachable pages, so skip it and let find_browser_id
        # keep scanning the rest.
        logger.info(f"[CDP] browser {browser_id} unreachable: {type(e).__name__}")
        return []
    if client is None:
        return []
    try:
        result = await client.send("Target.getTargets")
    except Exception as e:
        logger.warning(f"[CDP] Target.getTargets failed for {browser_id}: {type(e).__name__}: {e}")
        return []
    finally:
        await client.aclose()
    target_infos: list[dict[str, Any]] = result.get("targetInfos", [])  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    return [str(info["targetId"]) for info in target_infos if info.get("type") == "page"]


async def find_browser_id(page_id: str) -> str | None:
    # Live-only: a stopped or archived browser cannot host the page, so probing it would only
    # buy a round-trip and an error.
    browser_ids = await backend.list_browser_ids("live")
    logger.debug(f"[CDP] scanning {len(browser_ids)} browser(s) for page_id={page_id}")
    for browser_id in browser_ids:
        page_ids = await get_page_list(browser_id)
        if page_id in page_ids:
            return browser_id

    return None


def strip_browser_id_from_target_id(target_id: str) -> str:
    if "@" not in target_id:
        return target_id
    return target_id.split("@", 1)[1]


def prepend_browser_id_to_target_id(target_id: str, browser_id: str) -> str:
    return browser_id + "@" + target_id


CDP_TARGET_METHODS_STRIP_ID = (
    "Target.attachToTarget",
    "Target.closeTarget",
    "Target.getTargetInfo",
)


def patch_cdp_target(message: str, browser_id: str) -> str:
    if "targetId" not in message:
        return message

    try:
        data: Any = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return message

    if isinstance(data, dict):
        if data.get("method") == "Target.targetCreated":  # pyright: ignore[reportUnknownMemberType]
            params: Any = data.get("params")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            if isinstance(params, dict):
                target_info: Any = params.get("targetInfo")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                if isinstance(target_info, dict) and "targetId" in target_info:
                    target_info["targetId"] = prepend_browser_id_to_target_id(
                        str(target_info["targetId"]),  # pyright: ignore[reportUnknownArgumentType]
                        browser_id,
                    )
                    return json.dumps(data)
        elif data.get("method") in CDP_TARGET_METHODS_STRIP_ID:  # pyright: ignore[reportUnknownMemberType]
            params = data.get("params")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            if isinstance(params, dict) and "targetId" in params:
                params["targetId"] = strip_browser_id_from_target_id(str(params["targetId"]))  # pyright: ignore[reportUnknownArgumentType]
                return json.dumps(data)
        elif "result" in data:
            result: Any = data.get("result")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            if isinstance(result, dict) and "targetId" in result:
                result["targetId"] = prepend_browser_id_to_target_id(
                    str(result["targetId"]),  # pyright: ignore[reportUnknownArgumentType]
                    browser_id,
                )
                return json.dumps(data)

    return message


async def websocket_proxy(
    client_ws: WebSocket, remote_url: str, browser_id: str, patch: bool = True
) -> None:
    # The probe wrapper is observation only: on Daytona this relay is a live connection through the
    # sandbox's preview proxy, so how long it stays open is a candidate answer for "what keeps the
    # sandbox from auto-stopping". See daytona_probe.
    with daytona_probe.connection(browser_id, "cdp-relay"):
        await _websocket_proxy(client_ws, remote_url, browser_id, patch)


async def _websocket_proxy(
    client_ws: WebSocket, remote_url: str, browser_id: str, patch: bool = True
) -> None:
    # `patch` rewrites target ids to namespace them by browser_id (local backends multiplex many
    # browsers behind one proxy). The external fleet already does this on its own /cdp proxy, so
    # when relaying to it we pass `patch=False` to avoid double-prefixing target ids.
    try:
        async with websockets.connect(
            remote_url,
            ping_interval=60,
            ping_timeout=30,
            close_timeout=10,
            max_size=10 * 1024 * 1024,
        ) as remote_ws:
            logger.info("[CDP] Connected to remote WebSocket")

            async def client_to_remote() -> None:
                try:
                    while True:
                        message = await client_ws.receive_text()
                        if patch:
                            message = patch_cdp_target(message, browser_id)
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
                        if patch:
                            msg_text = patch_cdp_target(msg_text, browser_id)
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

    except OSError as e:
        logger.error(f"[CDP] Could not connect to remote: {e}")
        if client_ws.client_state == WebSocketState.CONNECTED:
            await client_ws.close(code=4502, reason="Remote server unreachable")
    except Exception as e:
        logger.error(f"[CDP] Unexpected error: {type(e).__name__}: {e}")
        if client_ws.client_state == WebSocketState.CONNECTED:
            await client_ws.close(code=4500, reason="Internal proxy error")
    finally:
        if client_ws.client_state == WebSocketState.CONNECTED:
            await client_ws.close()


@router.post("/api/v1/browsers")
async def create_browser_auto_endpoint(request: Request) -> dict[str, Any]:
    # Server-assigned id + best-of-N policy lives here (not per-backend): every backend only owns
    # per-browser CRUD keyed by a caller-supplied id. When the effective N > 1 it races that many
    # candidates via best_of_n and returns the winner; otherwise it creates a single browser
    # directly. The effective N is BROWSER_BEST_OF_N when explicitly set, else the backend's own
    # default (see `Backend.default_best_of_n`).
    logger.info("Starting browser (server-assigned id)...")
    try:
        origin_ip = request.headers.get("x-origin-ip")
        target_domain = request.headers.get("x-target-domains")
        browser_type = request.headers.get("x-browser-type")
        explicit = settings.BROWSER_BEST_OF_N
        n = max(1, explicit if explicit is not None else backend.default_best_of_n)
        if n == 1:
            browser_id = new_browser_id()
            result = await backend.create_browser(
                browser_id, origin_ip, target_domain, browser_type
            )
        else:
            browser_id, result = await best_of_n(backend, n, origin_ip, target_domain, browser_type)
        logger.info(f"Browser {browser_id} is started.")
        return {"browser_id": browser_id, **result}
    except Exception as e:
        detail = "Unable to start browser!"
        logger.error(f"{detail} Exception={e}")
        raise HTTPException(status_code=500, detail=detail)


@router.post("/api/v1/browsers/{browser_id}")
async def create_browser(browser_id: str, request: Request) -> dict[str, Any]:
    logger.info(f"Starting browser {browser_id}...")
    try:
        origin_ip = request.headers.get("x-origin-ip")
        target_domain = request.headers.get("x-target-domains")
        browser_type = request.headers.get("x-browser-type")
        result = await backend.create_browser(browser_id, origin_ip, target_domain, browser_type)
        logger.info(f"Browser {browser_id} is started.")
        return {"browser_id": browser_id, **result}
    except Exception as e:
        detail = f"Unable to start browser {browser_id}!"
        logger.error(f"{detail} Exception={e}")
        raise HTTPException(status_code=500, detail=detail)


@router.delete("/api/v1/browsers/{browser_id}")
async def delete_browser(browser_id: str) -> dict[str, Any]:
    logger.info(f"Stopping browser {browser_id}...")
    if not await backend.browser_exists(browser_id):
        detail = f"Browser {browser_id} not found!"
        logger.warning(detail)
        raise HTTPException(status_code=404, detail=detail)
    try:
        result = await backend.delete_browser(browser_id)
        logger.info(f"Browser {browser_id} is stopped.")
        return result
    except Exception as e:
        detail = f"Unable to stop browser {browser_id}!"
        logger.error(f"{detail} Exception={e}")
        raise HTTPException(status_code=500, detail=detail)


@router.get("/api/v1/browsers/{browser_id}")
async def get_browser(browser_id: str, request: Request) -> dict[str, Any]:
    logger.info(f"Querying browser {browser_id}...")
    origin_ip = request.headers.get("x-origin-ip")
    target_domain = request.headers.get("x-target-domains")
    try:
        return await backend.get_browser(browser_id, origin_ip, target_domain)
    except BrowserNotFound:
        detail = f"Browser {browser_id} not found!"
        logger.warning(detail)
        raise HTTPException(status_code=404, detail=detail)


@router.get("/api/v1/browsers")
async def list_browsers() -> JSONResponse:
    logger.info("Enumerating all browsers...")
    try:
        return JSONResponse(await backend.list_browser_ids())
    except Exception as e:
        detail = "Unable to list all browsers"
        logger.error(f"{detail} Exception={e}")
        raise HTTPException(status_code=500, detail=detail)


async def relay_browser_cdp(client_ws: WebSocket, browser_id: str, *, patch: bool) -> None:
    """Shared body of the browser-level CDP routes: auto-launch the browser, resolve the
    backend's remote wss URL, then relay. `patch` decides whether target ids are namespaced by
    browser_id on the way through (`/cdp`) or passed verbatim (`/api/v1/browsers/{browser_id}/cdp`)."""
    await client_ws.accept()
    logger.debug("[CDP] WebSocket accepted")

    # (1) Auto-launch the browser if it isn't running yet.
    if not await backend.browser_exists(browser_id):
        logger.info(f"[CDP] Browser {browser_id} not found — launching")
        try:
            origin_ip = client_ws.headers.get("x-origin-ip")
            target_domain = client_ws.headers.get("x-target-domains")
            browser_type = client_ws.headers.get("x-browser-type")
            await backend.create_browser(browser_id, origin_ip, target_domain, browser_type)
            logger.info(f"[CDP] Browser {browser_id} started")
        except Exception as e:
            logger.error(f"[CDP] Failed to auto-start browser {browser_id}: {e}")
            await client_ws.close(code=1008)
            return

    # (2) Resolve the backend-specific remote wss URL — retry up to 10 times. Each backend owns
    # this lookup end-to-end (`get_cdp_websocket_remote_url`); the router is agnostic of
    # whether it came from a connectUrl, an external /cdp relay, or a /json/version probe. Both
    # `None` (not yet resolvable) and exceptions (transient /json/version failure on a boot race)
    # count as a missed attempt; the first non-None result wins.
    remote_url: str | None = None
    for attempt in range(10):
        try:
            remote_url = await backend.get_cdp_websocket_remote_url(browser_id)
        except Exception as e:
            logger.warning(
                f"[CDP] Attempt {attempt + 1}/10 failed to get debugger URL from {browser_id}: {e}"
            )
        if remote_url is not None:
            logger.info(f"[CDP] Got remote URL: {remote_url}")
            break
        if attempt < 9:
            logger.debug("[CDP] Retrying in 3 seconds...")
            await asyncio.sleep(3)
    else:
        logger.error("[CDP] All retry attempts exhausted")
        await client_ws.close(code=4502, reason="Failed to get debugger URL")
        return

    # (3) Relay.
    logger.info(f"[CDP] Client connected, proxying to {remote_url}")
    await websocket_proxy(client_ws, remote_url, browser_id, patch)


@router.websocket("/cdp/{browser_id}")
async def cdp_browser_websocket_proxy(client_ws: WebSocket, browser_id: str) -> None:
    # `cdp_targets_need_namespacing` lets each backend tell the router whether the URL it returned
    # already namespaces target ids (Fleet's external /cdp proxy) — in which case we do not patch
    # again (would double-prefix browser_id).
    logger.debug(f"[CDP] Entered cdp_browser_websocket_proxy for browser_id={browser_id}")
    await relay_browser_cdp(client_ws, browser_id, patch=backend.cdp_targets_need_namespacing())
    logger.debug("[CDP] cdp_browser_websocket_proxy exiting")


@router.websocket("/api/v1/browsers/{browser_id}/cdp")
async def cdp_browser_websocket_proxy_raw(client_ws: WebSocket, browser_id: str) -> None:
    """Same relay as `/cdp/{browser_id}`, but byte-for-byte: no target-id namespacing at all, in
    either direction. Clients see the browser's own raw target ids (`abc1234567`, not
    `xyz@abc1234567`) and any id they send is forwarded untouched.

    Note this route does not undo namespacing applied upstream: with `CHROMEFLEET_URL` set we
    relay to the external fleet's own `/cdp` proxy, which namespaces on its side."""
    logger.debug(f"[CDP] Entered cdp_browser_websocket_proxy_raw for browser_id={browser_id}")
    await relay_browser_cdp(client_ws, browser_id, patch=False)
    logger.debug("[CDP] cdp_browser_websocket_proxy_raw exiting")


@router.websocket("/devtools/{path:path}")
async def cdp_devtools_websocket_proxy(client_ws: WebSocket, path: str) -> None:
    logger.debug(f"[CDP] Entered cdp_devtools_websocket_proxy for path={path}")
    await client_ws.accept()
    logger.debug("[CDP] WebSocket accepted")

    # Resolve the page id from the path's last segment (Chrome's /devtools/page/<id> URL form,
    # possibly namespaced by /cdp as `<browser_id>@<page_id>`). When the `@` form is present the
    # browser_id is carried in the segment; otherwise scan every backend browser's targets via
    # Target.getTargets to locate the page (Fleet relays /devtools verbatim and never reaches
    # this branch — it returns a relay URL from `get_devtools_websocket_remote_url` for any
    # page_id, so the find_browser_id fallback only fires for the local backends that report raw
    # un-namespaced target ids to clients).
    parts = path.split("/")
    page_id = parts[-1] if parts else None
    if not page_id:
        logger.error("[CDP] No page_id in path")
        await client_ws.close(code=4000, reason="No page_id in path")
        return

    if "@" in page_id:
        browser_id, page_id = page_id.split("@", 1)
        logger.debug(f"[CDP] browser_id={browser_id} page_id={page_id}")
    elif backend.cdp_websocket_base() is None:
        logger.debug(f"[CDP] Looking for page_id={page_id}")
        browser_id = await find_browser_id(page_id)
        if browser_id:
            logger.debug(f"[CDP] Found page {page_id} in browser {browser_id}")
        else:
            logger.error(f"[CDP] Page {page_id} not found in any browser")
            await client_ws.close(code=4000, reason="Page not found in any browser")
            return
    else:
        # Fleet relay: page_id wasn't namespaced by /cdp (the fleet's own /cdp already namespaces
        # target ids, and we patch=False on that route). Pass the namespaced form through to the
        # backend by leaving browser_id empty; see `get_devtools_websocket_remote_url`.
        browser_id = ""

    # Resolve the backend-specific remote wss URL — retry up to 10 times. Each backend owns this
    # end-to-end (`get_devtools_websocket_remote_url`): Fleet returns the external /devtools
    # relay URL, Podman/Daytona return the per-page /json/list webSocketDebuggerUrl, and
    # Browserbase self-relays the client socket against its multiplexed connectUrl and returns ""
    # — the sentinel that tells this router to skip its own websocket_proxy (a plain relay can't
    # drive a single page off a multiplexed socket). Both `None` (not yet resolvable) and
    # exceptions (a transient /json/list failure on a boot race) count as a missed attempt; the
    # first non-None result wins.
    remote_url: str | None = None
    for attempt in range(10):
        try:
            remote_url = await backend.get_devtools_websocket_remote_url(
                client_ws, browser_id, page_id
            )
        except Exception as e:
            logger.warning(
                f"[CDP] Attempt {attempt + 1}/10 failed to get page URL for "
                f"{browser_id}/{page_id}: {e}"
            )
        if remote_url is not None:
            if remote_url != "":
                logger.info(f"[CDP] Got page remote URL: {remote_url}")
            break
        if attempt < 9:
            logger.debug("[CDP] Retrying in 3 seconds...")
            await asyncio.sleep(3)
    else:
        logger.error(f"[CDP] Could not get websocket URL for page {page_id}")
        await client_ws.close(code=4502, reason="Failed to get page websocket URL")
        return

    # Sentinel: the backend already relayed the client socket itself (Browserbase).
    if remote_url == "":
        logger.debug("[CDP] cdp_devtools_websocket_proxy exiting (backend self-relayed)")
        return

    # Relay. `cdp_targets_need_namespacing` lets each backend tell the router whether the URL it
    # returned already namespaces target ids (Fleet's external /devtools proxy) — in which case
    # we do not patch again (would double-prefix browser_id).
    logger.info(f"[CDP] Connecting to {remote_url}")
    await websocket_proxy(
        client_ws, remote_url, browser_id, patch=backend.cdp_targets_need_namespacing()
    )
    logger.debug("[CDP] cdp_devtools_websocket_proxy exiting")


@router.get("/live/{browser_id}", response_model=None)
async def vnc_live_viewer(browser_id: str) -> HTMLResponse:
    try:
        live_url = await backend.get_live_view_url(browser_id)
    except BrowserNotFound:
        raise HTTPException(status_code=404, detail=f"Browser {browser_id} not found!")

    if live_url:
        safe_url = html.escape(live_url, quote=True)
        page = f"""<!DOCTYPE html>
<html>
<head>
    <title>{browser_id} - Live View</title>
    <style>
        html, body {{ margin: 0; height: 100%; overflow: hidden; background: #000; }}
        iframe {{ border: none; width: 100%; height: 100%; }}
    </style>
</head>
<body>
    <iframe src="{safe_url}" allow="clipboard-read; clipboard-write"></iframe>
</body>
</html>"""
        return HTMLResponse(page)

    if await backend.get_vnc_endpoint(browser_id) is not None:
        page = f"""<!DOCTYPE html>
<html>
<head>
    <title>{browser_id} - Live View</title>
    <style>
        body {{ margin: 0; background: #000; }}
        #screen {{ width: 100vw; height: 100vh; }}
    </style>
</head>
<body>
    <div id="screen"></div>
    <script type="module">
        import RFB from '/rfb.min.js';

        const wsScheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
        const wsUrl = wsScheme + '://' + window.location.host + '/websockify/{browser_id}';

        const rfb = new RFB(
            document.getElementById('screen'),
            wsUrl
        );
        rfb.scaleViewport = true;
    </script>
</body>
</html>"""
        return HTMLResponse(page)

    # No external live URL and no local VNC port: either a backend without live view, or an idle
    # sandbox gated off so it can auto-stop. Show a placeholder instead of a dead noVNC screen.
    page = f"""<!DOCTYPE html>
<html>
<head>
    <title>{browser_id} - Live View</title>
    <style>
        html, body {{
            margin: 0; height: 100%;
            display: flex; align-items: center; justify-content: center;
            background: #111; color: #aaa;
            font-family: system-ui, sans-serif; text-align: center;
        }}
        .msg {{ padding: 1rem; font-size: 0.9rem; line-height: 1.5; }}
    </style>
</head>
<body>
    <div class="msg">Live view unavailable<br>The browser is idle or stopped. Use it to resume.</div>
</body>
</html>"""
    return HTMLResponse(page)


@router.websocket("/websockify/{browser_id}")
async def websockify_proxy(websocket: WebSocket, browser_id: str) -> None:
    endpoint = await backend.get_vnc_endpoint(browser_id)
    if endpoint is None:
        await websocket.close()
        return
    host, vnc_port = endpoint

    client_subprotocol = websocket.headers.get("sec-websocket-protocol")
    if client_subprotocol and "binary" in [p.strip() for p in client_subprotocol.split(",")]:
        await websocket.accept(subprotocol="binary")
    else:
        await websocket.accept()

    try:
        reader, writer = await asyncio.open_connection(host, vnc_port)
    except Exception:
        await websocket.close()
        return

    async def ws_to_vnc() -> None:
        try:
            while True:
                data = await websocket.receive_bytes()
                writer.write(data)
                await writer.drain()
        except Exception:
            pass

    async def vnc_to_ws() -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await websocket.send_bytes(data)
        except Exception:
            pass

    await asyncio.gather(ws_to_vnc(), vnc_to_ws())
    writer.close()
    await writer.wait_closed()
