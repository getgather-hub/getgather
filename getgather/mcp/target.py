import asyncio
import json
from typing import Any, cast

import zendriver as zd
from loguru import logger

from getgather.browser import zen_navigate_with_retry
from getgather.mcp.dpage import remote_zen_dpage_with_action
from getgather.mcp.registry import MCPTool

target_mcp = MCPTool.registry["target"]

BASE_URL = "https://www.target.com"
API_BASE = "https://api.target.com"
LIST_PAGE_SIZE = 10

_FETCH_TIMEOUT_SECONDS = 30.0
_FETCH_RETRIES = 3

# ONLINE now returns fully-populated orders from a single call; STORE still uses
# the legacy lean list + per-order detail fetch.
_ONLINE_LIST_URL = (
    f"{API_BASE}/post_orders/v1/orders/history"
    f"?page_size={LIST_PAGE_SIZE}&order_purchase_type=ONLINE"
)
_STORE_LIST_URL = (
    f"{API_BASE}/guest_order_aggregations/v1/order_history"
    f"?page_size={LIST_PAGE_SIZE}&pending_order=true&shipt_status=true"
    f"&order_purchase_type=STORE"
)
_DETAIL_BASE = f"{API_BASE}/post_orders/v1"

# Matched against the app's own natural request so the x-api-key header can be read off it.
_LIST_PATH_MARKERS = (
    "/post_orders/v1/orders/history",
    "/guest_order_aggregations/v1/order_history",
)

_JS_HEADERS = """
    'accept': 'application/json',
    'origin': 'https://www.target.com',
    'referer': 'https://www.target.com/',
"""


def _list_url(order_purchase_type: str, page_number: int) -> str:
    base = _STORE_LIST_URL if order_purchase_type == "STORE" else _ONLINE_LIST_URL
    return f"{base}&page_number={page_number}"


async def _capture_api_key(page: zd.Tab) -> str:
    """Read x-api-key off the page's own order-history request (it rotates, so never hardcode)."""
    markers_json = json.dumps(list(_LIST_PATH_MARKERS))
    js_code = f"""
        (async () => {{
            const markers = {markers_json};
            const args = await new Promise(resolve => {{
                const originalFetch = window.fetch;
                window.fetch = function (...args) {{
                    const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
                    if (markers.some(m => url.includes(m))) {{
                        window.fetch = originalFetch;
                        resolve(args);
                    }}
                    return originalFetch.apply(this, args);
                }};
            }});
            const headers = (args[1] || {{}}).headers || {{}};
            return headers['x-api-key'] || headers['X-Api-Key'] || '';
        }})()
    """
    return str(await page.evaluate(js_code, True))


async def _fetch_json(page: zd.Tab, url: str, x_api_key: str) -> dict[str, Any]:
    js_code = f"""
        (async () => {{
            const res = await fetch({json.dumps(url)}, {{
                credentials: 'include',
                headers: {{
                    {_JS_HEADERS}
                    'x-api-key': {json.dumps(x_api_key)},
                }},
            }});
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return await res.json();
        }})()
    """
    return cast(dict[str, Any], await page.evaluate(js_code, True))


async def _fetch_list_page(
    page: zd.Tab, page_number: int, x_api_key: str, order_purchase_type: str
) -> dict[str, Any]:
    """Fetch one list page, retrying on timeout/error (the request can hang if the page stalls)."""
    url = _list_url(order_purchase_type, page_number)
    for attempt in range(1, _FETCH_RETRIES + 1):
        try:
            return await asyncio.wait_for(
                _fetch_json(page, url, x_api_key), timeout=_FETCH_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - retry on timeout or JS-side failure
            logger.warning(
                "Target list fetch attempt %s/%s failed type=%s page=%s: %s",
                attempt,
                _FETCH_RETRIES,
                order_purchase_type,
                page_number,
                exc,
            )
    raise RuntimeError(
        f"Target: list fetch failed after {_FETCH_RETRIES} attempts "
        f"type={order_purchase_type} page={page_number}"
    )


async def _fetch_all_store_details(
    page: zd.Tab, identifiers: list[str], x_api_key: str
) -> list[dict[str, Any]]:
    urls = [f"{_DETAIL_BASE}/orders/{identifier}/store" for identifier in identifiers]
    js_code = f"""
        (async () => {{
            const urls = {json.dumps(urls)};
            return await Promise.all(urls.map(u =>
                fetch(u, {{
                    credentials: 'include',
                    headers: {{
                        {_JS_HEADERS}
                        'x-api-key': {json.dumps(x_api_key)},
                    }},
                }})
                .then(r => r.ok ? r.json() : null)
                .catch(() => null)
            ));
        }})()
    """
    raw = await page.evaluate(js_code, True)
    if not isinstance(raw, list):
        return []
    return [cast(dict[str, Any], item) for item in cast(list[Any], raw) if isinstance(item, dict)]


async def _get_purchases(
    page: zd.Tab, page_number: int, order_purchase_type: str
) -> dict[str, Any]:
    logger.info(
        f"Target: signed in, fetching {order_purchase_type.lower()} "
        f"purchase history (page {page_number})"
    )

    await zen_navigate_with_retry(page, f"{BASE_URL}/orders", wait_for_ready=False)

    try:
        x_api_key = await asyncio.wait_for(_capture_api_key(page), timeout=_FETCH_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise RuntimeError(
            "Target: x-api-key capture timed out - no order-history request fired "
            "(user is likely not signed in)"
        ) from exc

    if not x_api_key:
        raise RuntimeError(
            f"Target: x-api-key missing from order-history request type={order_purchase_type}"
        )

    page_data = await _fetch_list_page(page, page_number, x_api_key, order_purchase_type)
    orders = cast(list[dict[str, Any]], page_data.get("orders", []))
    pagination = {
        "current_page": page_number,
        "total_pages": int(page_data.get("total_pages", 1)),
        "page_size": LIST_PAGE_SIZE,
    }

    # ONLINE list response is already fully populated - no per-order detail fetch.
    if order_purchase_type == "ONLINE":
        logger.info(f"Target ONLINE purchases fetched count={len(orders)} page={page_number}")
        return {"target_purchases": orders, "pagination": pagination}

    identifiers = [o["store_receipt_id"] for o in orders if "store_receipt_id" in o]
    if not identifiers:
        logger.info(f"Target STORE list empty page={page_number}")
        return {"target_purchases": [], "pagination": pagination}

    try:
        details = await asyncio.wait_for(
            _fetch_all_store_details(page, identifiers, x_api_key), timeout=60.0
        )
    except asyncio.TimeoutError:
        logger.warning("Target: detail fetch timed out")
        details = []

    logger.info(f"Target STORE purchases fetched count={len(details)} page={page_number}")
    return {"target_purchases": details, "pagination": pagination}


@target_mcp.tool
async def get_purchases(page_number: int = 1) -> dict[str, Any]:
    """Get online purchase history from a user's Target account.

    Args:
        page_number: Page to fetch (1-indexed). Default 1.
    """

    async def action(page: zd.Tab, browser: zd.Browser) -> dict[str, Any]:
        return await _get_purchases(page, page_number, "ONLINE")

    return await remote_zen_dpage_with_action(
        f"{BASE_URL}/orders",
        action=action,
    )


@target_mcp.tool
async def get_purchases_in_store(page_number: int = 1) -> dict[str, Any]:
    """Get in-store purchase history from a user's Target account.

    Args:
        page_number: Page to fetch (1-indexed). Default 1.
    """

    async def action(page: zd.Tab, browser: zd.Browser) -> dict[str, Any]:
        return await _get_purchases(page, page_number, "STORE")

    return await remote_zen_dpage_with_action(
        f"{BASE_URL}/orders",
        action=action,
    )
