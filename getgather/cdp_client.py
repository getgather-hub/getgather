import asyncio
import json
from typing import Any, cast

import httpx
import websockets
import websockets.asyncio.client

from getgather.browser import get_remote_browser_cdp_url
from getgather.config import settings


class CDPError(Exception):
    """Base class for CDP client errors that callers (e.g. HTTP routers) may
    translate into protocol-specific responses."""


class PageNotFoundError(CDPError):
    """Raised when a requested page target does not exist in the browser."""


class PageAttachError(CDPError):
    """Raised when attaching to a page target fails."""


class BrowserNotFoundError(CDPError):
    """Raised when the browser genuinely does not exist in Chrome Fleet."""


async def open_cdp(browser_id: str) -> "CDPClient":
    """Locate the browser and return a connected CDP client. Raises
    BrowserNotFoundError if the browser genuinely does not exist, or CDPError
    if resolving/connecting fails transiently."""
    try:
        cdp_websocket_url = await get_remote_browser_cdp_url(browser_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise BrowserNotFoundError(f"Browser {browser_id} not found") from e
        raise CDPError(f"ChromeFleet error resolving {browser_id}: {e}") from e
    except Exception as e:
        raise CDPError(f"Failed to resolve browser {browser_id}: {e}") from e
    ws = await websockets.connect(
        cdp_websocket_url,
        open_timeout=settings.CHROMEFLEET_CDP_OPEN_TIMEOUT_SECONDS,
    )
    return CDPClient(ws)


async def open_cdp_url(ws_url: str, timeout: float | None = None) -> "CDPClient":
    """Open a CDP client against a direct wss URL Used by router helpers that
    need to enumerate page targets on backends that have no /json/list HTTP endpoint."""
    ws = await websockets.connect(
        ws_url,
        open_timeout=timeout
        if timeout is not None
        else settings.CHROMEFLEET_CDP_OPEN_TIMEOUT_SECONDS,
    )
    return CDPClient(ws)


class CDPClient:
    def __init__(self, ws: websockets.asyncio.client.ClientConnection) -> None:
        self._ws: Any = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            while True:
                raw = await self._ws.recv()
                if not isinstance(raw, str):
                    continue
                try:
                    loaded: Any = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(loaded, dict):
                    continue
                data: dict[str, Any] = cast(dict[str, Any], loaded)
                msg_id: Any = data.get("id")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                if isinstance(msg_id, int) and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if future.done():
                        continue
                    if "error" in data:
                        error_info: Any = data["error"]  # pyright: ignore[reportUnknownVariableType]
                        if isinstance(error_info, dict):
                            message = str(
                                error_info.get("message", "CDP error")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                            )
                        else:
                            message = f"CDP error: {error_info}"
                        future.set_exception(Exception(message))
                    else:
                        result: Any = data.get("result", {})  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                        if isinstance(result, dict):
                            future.set_result(
                                result  # pyright: ignore[reportUnknownArgumentType]
                            )
                        else:
                            future.set_result({})
        except Exception:
            pass
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(Exception("CDP connection closed"))
        self._pending.clear()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self._id += 1
        msg_id = self._id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future
        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        await self._ws.send(json.dumps(msg))
        return await future

    async def aclose(self) -> None:
        if not self._reader_task.done():
            self._reader_task.cancel()
        try:
            await self._reader_task
        except (asyncio.CancelledError, Exception):
            pass
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(Exception("CDP client closed"))
        self._pending.clear()
        try:
            await self._ws.close()
        except Exception:
            pass

    async def find_page_target(self, page_id: str) -> dict[str, Any]:
        """Return the TargetInfo dict for the given page_id, or raise PageNotFoundError."""
        # Deferred import: getgather.browsers.router imports this module at load time, so a
        # top-level import here would be circular.
        from getgather.browsers.router import strip_browser_id_from_target_id

        result = await self.send("Target.getTargets")
        target_infos: list[dict[str, Any]] = result.get("targetInfos", [])  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        for info in target_infos:
            info_target_id = strip_browser_id_from_target_id(str(info.get("targetId", "")))
            if info_target_id == page_id and info.get("type") == "page":
                return info
        raise PageNotFoundError(f"Page {page_id} not found in browser")

    async def attach_to_page(self, page_id: str) -> "CDPPage":
        target_info = await self.find_page_target(page_id)
        attach = await self.send(
            "Target.attachToTarget", {"targetId": target_info["targetId"], "flatten": True}
        )
        session_id = attach.get("sessionId")  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        if not isinstance(session_id, str):
            raise PageAttachError("Failed to attach to page target")
        return CDPPage(self, session_id)


class CDPPage:
    def __init__(self, client: CDPClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id

    async def navigate(self, url: str) -> dict[str, Any]:
        return await self._client.send(
            "Page.navigate",
            {"url": url},
            session_id=self._session_id,
        )

    async def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        result = await self._client.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            session_id=self._session_id,
        )
        if "exceptionDetails" in result:
            exc: Any = result.get("exceptionDetails") or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
            if isinstance(exc, dict):
                text = str(exc.get("text", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                exception: Any = exc.get("exception", {})  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(exception, dict):
                    description = str(
                        exception.get("description", "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                    )
                else:
                    description = ""
            else:
                text = ""
                description = ""
            raise Exception(f"JS error: {text}: {description}")
        remote_result: Any = result.get("result", {}) or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        if isinstance(remote_result, dict):
            return remote_result.get("value")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
        return None
