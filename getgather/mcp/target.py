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

_LIST_URL = (
    f"{API_BASE}/guest_order_aggregations/v1/order_history"
    f"?page_size={LIST_PAGE_SIZE}&pending_order=true&shipt_status=true"
)
_DETAIL_BASE = f"{API_BASE}/post_orders/v1"


async def _fetch_list_page(
    page: zd.Tab, page_number: int, x_api_key: str, order_purchase_type: str
) -> dict[str, Any]:
    url = f"{_LIST_URL}&order_purchase_type={order_purchase_type}&page_number={page_number}"
    js_code = f"""
        (async () => {{
            const res = await fetch('{url}', {{
                credentials: 'include',
                headers: {{
                    'accept': 'application/json',
                    'x-api-key': '{x_api_key}',
                }},
            }});
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return await res.json();
        }})()
    """
    return cast(dict[str, Any], await page.evaluate(js_code, True))


async def _fetch_all_details(
    page: zd.Tab, order_numbers: list[str], x_api_key: str
) -> list[dict[str, Any]]:
    numbers_json = json.dumps(order_numbers)
    js_code = f"""
        (async () => {{
            const orderNumbers = {numbers_json};
            const results = await Promise.all(orderNumbers.map(n =>
                fetch('{_DETAIL_BASE}/' + n, {{
                    credentials: 'include',
                    headers: {{
                        'accept': 'application/json',
                        'x-api-key': '{x_api_key}',
                    }},
                }})
                .then(r => r.ok ? r.json() : null)
                .catch(() => null)
            ));
            return results;
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
    intercept_result = cast(
        dict[str, Any],
        await page.evaluate(
            """
        (async () => {
            const httpRequest = await new Promise(resolve => {
                const originalFetch = window.fetch;
                window.fetch = async function (...args) {
                    if (typeof args[0] === 'string' && args[0].includes('/guest_order_aggregations/v1/order_history')) {
                        window.fetch = originalFetch;
                        resolve(args);
                    }
                    return originalFetch.apply(this, args);
                };
            });
            const res = await fetch(httpRequest[0], {...httpRequest[1], credentials: 'include'});
            return {
                page1: await res.json(),
                x_api_key: (httpRequest[1].headers || {})['x-api-key'] ?? ''
            };
        })()
        """,
            True,
        ),
    )

    x_api_key = str(intercept_result.get("x_api_key", ""))

    # The page's own load only fires the ONLINE-type request (intercepted above), so
    # only that case can reuse it as page 1 for free; STORE always needs a manual fetch.
    if order_purchase_type == "ONLINE" and page_number == 1:
        page_data = cast(dict[str, Any], intercept_result.get("page1", {}))
    else:
        page_data = await _fetch_list_page(page, page_number, x_api_key, order_purchase_type)

    total_pages = int(page_data.get("total_pages", 1))
    orders = page_data.get("orders", [])

    pagination = {
        "current_page": page_number,
        "total_pages": total_pages,
        "page_size": LIST_PAGE_SIZE,
    }

    # STORE orders have no order_number and already embed full order_lines
    # (tcin, description, images) in the list response, so no detail fetch needed.
    if order_purchase_type == "STORE":
        return {"target_purchases": orders, "pagination": pagination}

    order_numbers = [o["order_number"] for o in orders if "order_number" in o]

    if not order_numbers:
        return {"target_purchases": [], "pagination": pagination}

    try:
        details = await asyncio.wait_for(
            _fetch_all_details(page, order_numbers, x_api_key), timeout=60.0
        )
    except asyncio.TimeoutError:
        logger.warning("Target: detail fetch timed out")
        details = []

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
