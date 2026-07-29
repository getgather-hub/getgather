from typing import Any, cast

import zendriver as zd
from loguru import logger

from getgather.mcp.dpage import remote_zen_dpage_with_action
from getgather.mcp.registry import MCPTool

walgreens_mcp = MCPTool.registry["walgreens"]

ORDERS_UI_URL = "https://www.walgreens.com/orderhistory/orders-ui"
SEARCH_URL = "https://www.walgreens.com/orderhistory/v1/orders/search"

# Walgreens' purchase-history page requests up to 100 orders per page and exposes
# order history in 6-month buckets going back ~2 years. The /orders/search backend
# accepts an arbitrary date window, so a single 2-year window covers full history.
PAGE_SIZE = 100
YEARS_BACK = 2


def build_purchases_response(data: dict[str, Any], page_number: int) -> dict[str, Any]:
    """Turn a successful ``/orders/search`` JSON body into the tool's response.

    ``orders`` are preserved as raw Walgreens objects (no normalization). The
    Walgreens response has no total-count field, so ``total_pages`` is DERIVED and
    is ``None`` whenever it cannot be known:
      - empty page 1        -> 0 (the account has no orders)
      - partial page (<size)-> this is the last page, so total_pages = page_number
      - full page           -> more may exist, total unknown -> None
      - empty page > 1       -> past the end / indeterminate -> None
    """
    orders_raw = data.get("orders", [])
    orders: list[dict[str, Any]]
    if isinstance(orders_raw, list):
        orders = [item for item in cast(list[Any], orders_raw) if isinstance(item, dict)]
    else:
        orders = []

    count = len(orders)
    total_pages: int | None
    if count == 0:
        total_pages = 0 if page_number == 1 else None
    elif count < PAGE_SIZE:
        total_pages = page_number
    else:
        total_pages = None

    return {
        "walgreens_purchases": orders,
        "pagination": {
            "current_page": page_number,
            "total_pages": total_pages,
            "page_size": PAGE_SIZE,
        },
    }


@walgreens_mcp.tool
async def get_purchases(page_number: int = 1) -> dict[str, Any]:
    """Get online order history from a user's Walgreens account.

    Args:
        page_number: Page to fetch (1-indexed). Default 1.

    Returns a dict with ``walgreens_purchases`` (the raw Walgreens order objects,
    unmodified) and a ``pagination`` object. Note: the Walgreens
    ``/orderhistory/v1/orders/search`` response carries no total-count field, so
    ``total_pages`` is DERIVED and is ``None`` whenever it cannot be known: 0 for an
    empty first page, the current page number when a short (< page size) page is
    returned (last page), and ``None`` when a full page is returned (more may exist)
    or an empty page beyond the first is requested. ``page_size`` is the request
    size (100).
    """
    if page_number < 1:
        raise ValueError(f"page_number must be >= 1, got {page_number}")

    async def action(page: zd.Tab, browser: zd.Browser) -> dict[str, Any]:
        logger.info(f"Walgreens: signed in, fetching order history (page {page_number})")

        # Walgreens uses the Spring Security CSRF meta-tag pattern: the token lives
        # in <meta name="_csrf"> and the header name in <meta name="_csrfHeader">.
        # The XSRF-TOKEN cookie itself is httpOnly, so it must be read from the meta
        # tag. The request is issued from the page context with credentials so the
        # session cookies are attached.
        js_code = f"""
            (async () => {{
                const token = document.querySelector('meta[name=_csrf]')?.content;
                const header = document.querySelector('meta[name=_csrfHeader]')?.content
                    || 'X-XSRF-TOKEN';
                if (!token) {{
                    return {{ tokenPresent: false }};
                }}
                const now = new Date();
                const pad = n => String(n).padStart(2, '0');
                const day = pad(now.getDate());
                const month = pad(now.getMonth() + 1);
                const year = now.getFullYear();
                const body = {{
                    filter: {{
                        type: ['ALL'],
                        fromDate: {{ day, month, year: year - {YEARS_BACK} }},
                        toDate: {{ day, month, year }},
                        filterType: 'ALL',
                        tab: 'ONLINE',
                        p: {page_number},
                        s: {PAGE_SIZE},
                        sortColumn: 'purchaseDate',
                        sortType: 'DESC',
                    }},
                }};
                const res = await fetch('{SEARCH_URL}', {{
                    method: 'POST',
                    credentials: 'include',
                    headers: {{
                        'Accept': 'application/json, text/plain, */*',
                        'Content-Type': 'application/json; charset=UTF-8',
                        [header]: token,
                    }},
                    body: JSON.stringify(body),
                }});
                const text = await res.text();
                let json = null;
                try {{ json = JSON.parse(text); }} catch (e) {{}}
                return {{
                    tokenPresent: true,
                    status: res.status,
                    json: json,
                    isJson: json !== null,
                    textHead: json === null ? text.slice(0, 200) : null,
                }};
            }})()
        """
        result = cast(dict[str, Any], await page.evaluate(js_code, await_promise=True))

        if not result.get("tokenPresent", False):
            raise Exception("Walgreens: CSRF token missing — user is not signed in")

        status = int(result.get("status", 0))
        if status in (401, 403):
            raise Exception(f"Walgreens: order search returned {status} — user is not signed in")

        if not result.get("isJson", False):
            # A non-JSON body (e.g. an Akamai HTML challenge) indicates bot detection
            # or an unexpected error rather than a valid empty result.
            head = result.get("textHead") or ""
            raise Exception(
                f"Walgreens: order search returned non-JSON (status {status}); "
                f"possible bot-detection/challenge. Body starts: {head!r}"
            )

        data = cast(dict[str, Any], result.get("json") or {})
        call_status = data.get("callStatus")
        if call_status != "SUCCESS":
            messages = data.get("messages")
            raise Exception(
                f"Walgreens: order search callStatus={call_status!r} (status {status}); "
                f"messages={messages!r}"
            )

        return build_purchases_response(data, page_number)

    return await remote_zen_dpage_with_action(ORDERS_UI_URL, action=action)
