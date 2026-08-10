from typing import Any, Literal, cast

import httpx
from fastapi import WebSocket
from fastmcp.server.dependencies import get_http_headers
from httpx_retries import Retry, RetryTransport

from getgather.browsers.backend import BrowserNotFound
from getgather.client_ip import client_ip_var
from getgather.config import settings

HTTP_METHOD = Literal["GET", "POST", "DELETE"]

DEFAULT_BEST_OF_N = 1


def build_chromefleet_headers(*, target_domain: str | None = None) -> dict[str, str]:
    mcp_headers = get_http_headers(include_all=True)
    headers = {
        "x-forwarded-for": mcp_headers.get("x-forwarded-for", None),
        "user-agent": mcp_headers.get("user-agent", None),
        "sec-ch-ua": mcp_headers.get("sec-ch-ua", None),
        "sec-ch-ua-mobile": mcp_headers.get("sec-ch-ua-mobile", None),
        "sec-ch-ua-platform": mcp_headers.get("sec-ch-ua-platform", None),
        "x-origin-ip": mcp_headers.get("x-origin-ip") or client_ip_var.get(),
        "x-origin-id": mcp_headers.get("x-origin-id", None),
        "x-origin-ua": mcp_headers.get("x-origin-ua", None),
        "x-browser-type": mcp_headers.get("x-browser-type", None),
        "x-target-domains": target_domain,
    }
    return {k: v for k, v in headers.items() if v is not None}


async def call_chromefleet_api(
    method: HTTP_METHOD,
    browser_id: str | None = None,
    *,
    target_domain: str | None = None,
    timeout: float = 120.0,
    retries: int = 3,
    raise_for_status: bool = True,
    headers: dict[str, str] | None = None,
) -> httpx.Response | None:
    base_url = settings.effective_chromefleet_url.rstrip("/")
    path = f"/api/v1/browsers/{browser_id}" if browser_id else "/api/v1/browsers"
    url = f"{base_url}{path}"

    if headers is None:
        headers = build_chromefleet_headers(target_domain=target_domain)

    async with httpx.AsyncClient(
        transport=RetryTransport(
            retry=Retry(
                total=retries,
                backoff_factor=1.0,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=[method],
            )
        ),
    ) as client:
        error = None
        response = None
        try:
            response = await client.request(
                method,
                url,
                headers=headers,
                timeout=httpx.Timeout(connect=2.0, pool=None, read=timeout, write=timeout),
            )
        except Exception as e:
            error = e

        if raise_for_status:
            if error:
                raise error
            if response:
                response.raise_for_status()
        return response


def _require(response: httpx.Response | None) -> httpx.Response:
    if response is None:
        raise RuntimeError("Chrome Fleet request failed")
    return response


class FleetBackend:
    """Proxy the browser API to an external Chrome Fleet (`CHROMEFLEET_URL`).

    Each method forwards the corresponding REST call to the upstream fleet. getgather's own MCP
    client connects directly to the external fleet's `/cdp` endpoint (see
    `getgather.browser._setup_cdp_url`), so `get_cdp_base_url` (the per-browser `/json/version`
    flow used by the local backends) is not used. External CDP clients hitting the local
    `/cdp/{id}` and `/devtools/{path}` websocket routes are relayed transparently to the same
    fleet endpoints via `cdp_websocket_base()`; the fleet already owns target-id patching, so the
    relay does not patch again. VNC is not proxied here either (`get_vnc_endpoint` returns None).
    """

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
        browser_type: str | None,
    ) -> dict[str, Any]:
        headers = {"x-origin-ip": origin_ip} if origin_ip else {}
        if browser_type:
            headers["x-browser-type"] = browser_type
        response = _require(await call_chromefleet_api("POST", browser_id, headers=headers))
        return response.json()

    async def get_browser(
        self, browser_id: str, origin_ip: str | None, target_domain: str | None
    ) -> dict[str, Any]:
        headers = {"x-origin-ip": origin_ip} if origin_ip else {}
        response = _require(
            await call_chromefleet_api("GET", browser_id, headers=headers, raise_for_status=False)
        )
        if response.status_code == 404:
            raise BrowserNotFound(browser_id)
        response.raise_for_status()
        return response.json()

    async def delete_browser(self, browser_id: str) -> dict[str, Any]:
        response = _require(await call_chromefleet_api("DELETE", browser_id))
        return response.json()

    async def browser_exists(self, browser_id: str) -> bool:
        response = await call_chromefleet_api(
            "GET", browser_id, headers={}, timeout=5.0, retries=0, raise_for_status=False
        )
        return response is not None and response.status_code == 200

    async def list_browser_ids(self) -> list[str]:
        response = _require(await call_chromefleet_api("GET"))
        data: Any = response.json()
        if not isinstance(data, list):
            return []
        return [str(item) for item in cast(list[Any], data)]

    async def cleanup_idle(self) -> list[str]:
        # The external Chrome Fleet runs its own idle cleanup; nothing to do here.
        return []

    async def get_cdp_base_url(self, browser_id: str) -> str:
        raise NotImplementedError(
            "FleetBackend serves CDP via the external fleet's /cdp endpoint, not the local proxy"
        )

    def cdp_websocket_base(self) -> str:
        # The external fleet exposes its own `/cdp/{id}` and `/devtools/{path}` websocket proxies;
        # the local routes relay to them verbatim. ws:// for http, wss:// for https.
        base = settings.effective_chromefleet_url.rstrip("/")
        return base.replace("https://", "wss://").replace("http://", "ws://")

    async def get_cdp_websocket_remote_url(self, browser_id: str) -> str | None:
        # Relay straight to the external fleet's own /cdp proxy, which already namespaces target
        # ids; the router does not patch again (see cdp_targets_need_namespacing). Deterministic
        # from CHROMEFLEET_URL + browser_id, so this never returns None — no retry needed.
        return f"{self.cdp_websocket_base()}/cdp/{browser_id}"

    def cdp_targets_need_namespacing(self) -> bool:
        # The external fleet's /cdp proxy already namespaces target ids by browser_id; the
        # router patching here would double-prefix them, so opt out.
        return False

    async def get_devtools_websocket_remote_url(
        self, client_ws: WebSocket, browser_id: str, page_id: str
    ) -> str | None:
        # Relay verbatim to the external fleet's own /devtools proxy at the Chrome DevTools
        # `page/` URL form. The router passes the namespaced `<browser_id>@<page_id>` target id
        # when the path carried it (the form /cdp produces); otherwise `browser_id` is empty and
        # we relay the un-split fleet target id untouched. Deterministic, so this never returns
        # None — no retry needed.
        del client_ws
        target = f"{browser_id}@{page_id}" if browser_id else page_id
        return f"{self.cdp_websocket_base()}/devtools/page/{target}"

    async def get_vnc_endpoint(self, browser_id: str) -> tuple[str, int] | None:
        return None

    async def get_live_view_url(self, browser_id: str) -> str | None:
        return None
