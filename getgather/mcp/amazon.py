import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import zendriver as zd
from loguru import logger

from getgather.browser import get_url, page_query_selector, zen_navigate_with_retry
from getgather.mcp.dpage import (
    remote_zen_dpage_mcp_tool,
    remote_zen_dpage_with_action,
)
from getgather.mcp.registry import MCPTool
from getgather.zen_distill import (
    convert,
    load_distillation_patterns,
    run_distillation_loop,
)


@dataclass(frozen=True)
class AmazonCountry:
    """Configuration for an Amazon country domain."""

    domain: str
    purchase_history_key: str
    watch_history_result_key: str
    watchlist_result_key: str
    prime_library_result_key: str
    watch_history_url: str
    watchlist_url: str
    prime_library_url: str
    browsing_history_url: str
    watchlist_pagination_api_url: str
    watch_history_pagination_api_url: str

    @property
    def base_url(self) -> str:
        # `domain` is the full host, so a mock host with no `www.` works unchanged.
        return f"https://{self.domain}"

    @property
    def signin_url(self) -> str:
        return f"{self.base_url}/ax/account/manage"


AMAZON_US = AmazonCountry(
    domain="amazon-mock.dataportrait.app",
    purchase_history_key="amazon_purchase_history",
    watch_history_result_key="amazon_watch_history",
    watchlist_result_key="amazon_prime_watchlist",
    prime_library_result_key="amazon_prime_library",
    watch_history_url="https://amazon-mock.dataportrait.app/gp/video/settings/watch-history",
    watchlist_url="https://amazon-mock.dataportrait.app/gp/video/mystuff/watchlist",
    prime_library_url="https://amazon-mock.dataportrait.app/gp/video/mystuff/library",
    browsing_history_url="https://amazon-mock.dataportrait.app/gp/history?ref_=nav_AccountFlyout_browsinghistory",
    watchlist_pagination_api_url="https://amazon-mock.dataportrait.app/gp/video/api/paginateCollection",
    watch_history_pagination_api_url="https://amazon-mock.dataportrait.app/gp/video/api/getWatchHistorySettingsPage",
)

AMAZON_CA = AmazonCountry(
    domain="www.amazon.ca",
    purchase_history_key="amazonca_purchase_history",
    watch_history_result_key="amazon_ca_watch_history",
    watchlist_result_key="amazon_ca_prime_watchlist",
    prime_library_result_key="amazon_ca_prime_library",
    watch_history_url="https://www.primevideo.com/region/na/settings/watch-history/ref=atv_set_watch-history",
    watchlist_url="https://www.primevideo.com/region/na/mystuff/watchlist",
    prime_library_url="https://www.primevideo.com/region/na/mystuff/library",
    browsing_history_url="https://www.amazon.ca/gp/history?ref_=nav_AccountFlyout_browsinghistory",
    watchlist_pagination_api_url="https://www.primevideo.com/region/na/api/paginateCollection",
    watch_history_pagination_api_url="https://www.primevideo.com/region/fe/api/getWatchHistorySettingsPage",
)


def _normalize_order_id(order_id: str | list[str] | None) -> str | list[str] | None:
    if order_id is None:
        return order_id
    if isinstance(order_id, list):
        return order_id
    if order_id.startswith("Order #"):
        return order_id.replace("Order #", "").strip()
    return order_id


async def _search_purchase_history(
    country: AmazonCountry, keyword: str, page_number: int = 1
) -> dict[str, Any]:
    return await remote_zen_dpage_mcp_tool(
        f"{country.base_url}/your-orders/search?page={page_number}&search={keyword}",
        "order_history",
    )


async def _get_purchase_history(
    country: AmazonCountry, year: str | int | None = None, start_index: int = 0
) -> dict[str, Any]:
    if year is None:
        target_year = datetime.now().year
    elif isinstance(year, str):
        try:
            target_year = int(year)
        except ValueError:
            target_year = datetime.now().year
    else:
        target_year = int(year)

    current_year = datetime.now().year
    if not (1900 <= target_year <= current_year + 1):
        raise ValueError(f"Year {target_year} is out of valid range (1900-{current_year + 1})")

    return await remote_zen_dpage_mcp_tool(
        f"{country.base_url}/your-orders/orders?timeFilter=year-{target_year}&startIndex={start_index}",
        country.purchase_history_key,
    )


async def _search_product(country: AmazonCountry, keyword: str) -> dict[str, Any]:
    return await remote_zen_dpage_mcp_tool(
        f"{country.base_url}/s?k={keyword}",
        "product_list",
    )


async def _get_browsing_history(country: AmazonCountry) -> dict[str, Any]:
    async def get_browsing_history_action(page: zd.Tab, _) -> dict[str, Any]:
        current_url = await get_url(page)
        logger.info(f"Getting browsing history from {current_url}")
        if current_url is None or "signin" in current_url:
            raise Exception("User is not signed in")

        is_empty = await page_query_selector(
            page, "//span[contains(., 'You have no recently viewed items.')]"
        )
        logger.info(f"is_empty: {is_empty}")
        if is_empty:
            logger.info(f"No browsing history")
            return {"browsing_history_data": []}

        logger.info(f"Navigating to {current_url}")

        await zen_navigate_with_retry(page, country.browsing_history_url, wait_for_ready=False)
        logger.info("Intercepting XHR and extracting browsing history data")

        captured = cast(
            dict[str, Any],
            await page.evaluate(
                """
                (async () => {
                    const httpRequest = await new Promise((resolve, reject) => {
                        const timer = setTimeout(
                            () => reject(new Error('XHR intercept timeout after 15s')), 15000
                        );
                        const originalOpen = XMLHttpRequest.prototype.open;
                        const originalSend = XMLHttpRequest.prototype.send;
                        const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;

                        XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                            this._interceptMethod = method;
                            this._interceptUrl = url;
                            this._interceptHeaders = {};
                            return originalOpen.call(this, method, url, ...rest);
                        };

                        XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
                            if (!this._interceptHeaders) this._interceptHeaders = {};
                            this._interceptHeaders[name] = value;
                            return originalSetRequestHeader.call(this, name, value);
                        };

                        XMLHttpRequest.prototype.send = function(body) {
                            const url = String(this._interceptUrl || '');
                            if (url.includes('browsing-history')) {
                                clearTimeout(timer);
                                XMLHttpRequest.prototype.open = originalOpen;
                                XMLHttpRequest.prototype.send = originalSend;
                                XMLHttpRequest.prototype.setRequestHeader = originalSetRequestHeader;
                                resolve([url, { headers: this._interceptHeaders }]);
                            }
                            return originalSend.call(this, body);
                        };
                    });

                    const rawAttribute = await new Promise((resolve) => {
                        const check = () => {
                            const el = document.querySelector('div[data-client-recs-list]');
                            if (el) {
                                resolve(el.getAttribute('data-client-recs-list'));
                            } else {
                                setTimeout(check, 100);
                            }
                        };
                        setTimeout(check, 500);
                    });

                    return {
                        url: httpRequest[0],
                        headers: httpRequest[1].headers,
                        rawAttribute
                    };
                })()
                """,
                True,
            ),
        )

        browsing_history_api_url = cast(str, captured["url"])
        request_headers = cast(dict[str, Any], captured["headers"])
        raw_attribute = captured.get("rawAttribute")
        logger.info(f"Found browsing history API URL: {browsing_history_api_url}")
        logger.debug(
            f"Request headers captured: {len(request_headers) if request_headers else 0} headers"
        )
        logger.info("Extracting browsing history IDs from data-client-recs-list attribute")
        logger.info(
            f"Raw attribute value: {raw_attribute[:200] if raw_attribute and len(str(raw_attribute)) > 200 else raw_attribute}"
        )
        raw_attribute_str = str(raw_attribute) if raw_attribute is not None else "[]"
        output = [json.dumps(item) for item in json.loads(raw_attribute_str)]
        logger.info(f"Extracted {len(output)} browsing history IDs")

        async def get_browsing_history_batch(start_index: int, end_index: int):
            logger.info(
                f"Getting browsing history batch: indices {start_index} to {end_index} (batch size: {end_index - start_index})"
            )

            headers_js = json.dumps(request_headers or {})
            ids_js = json.dumps(output[start_index:end_index])
            logger.info(
                f"Requesting {len(output[start_index:end_index])} items from API: {browsing_history_api_url}"
            )

            try:
                html = await page.evaluate(
                    f"""
                    (async () => {{
                        const headers = {headers_js};
                        const ids = {ids_js};
                        const res = await fetch('{browsing_history_api_url}', {{
                            method: 'POST',
                            headers: headers,
                            credentials: 'include',
                            body: JSON.stringify({{"ids": ids}})
                        }});
                        if (!res.ok) {{
                            throw new Error(`HTTP error! status: ${{res.status}}`);
                        }}
                        return await res.text();
                    }})()
                """,
                    True,
                )
            except Exception as e:
                logger.info(f"Error fetching browsing history batch {start_index}-{end_index}: {e}")
                raise
            distilled = f"""
                <html gg-domain="amazon">
                    <body>
                        {html}
                    </body>
                    <script type="application/json" id="browsing_history">
                        {{
                            "rows": "div#gridItemRoot",
                            "columns": [
                                {{
                                    "name": "name",
                                    "selector": "a.a-link-normal > span > div"
                                }},
                                {{
                                    "name": "url",
                                    "selector": "div[class*='uncoverable-faceout'] a[class='a-link-normal aok-block']",
                                    "attribute": "href"
                                }},
                                {{
                                    "name": "image_url",
                                    "selector": "a img.a-dynamic-image",
                                    "attribute": "src"
                                }},
                                {{
                                    "name": "rating",
                                    "selector": "div.a-icon-row > a > i > span"
                                }},
                                {{
                                    "name": "rating_count",
                                    "selector": "div.a-icon-row > a > span"
                                }},
                                {{
                                    "name": "price",
                                    "selector": "span.a-color-price > span"
                                }},
                                {{
                                    "name": "price_unit",
                                    "selector": "span[class='a-size-mini a-color-price aok-nowrap']"
                                }},
                                {{
                                    "name": "delivery_message",
                                    "selector": "div.udm-primary-delivery-message"
                                }}
                            ]
                        }}
                    </script>
                </html>
            """
            logger.debug(f"Converting distilled HTML for batch {start_index}-{end_index}")
            converted = await convert(distilled)
            if converted is not None:
                logger.info(
                    f"Converted batch {start_index}-{end_index}: found {len(converted)} items"
                )
                for item in converted:
                    item["url"] = f"{country.base_url}{item.get('url', '')}"
            else:
                logger.warning(f"Conversion returned None for batch {start_index}-{end_index}")
            return converted

        num_batches = (len(output) + 99) // 100
        logger.info(f"Fetching browsing history in {num_batches} batch(es) of up to 100 items each")
        browsing_history_list = await asyncio.gather(*[
            get_browsing_history_batch(i, i + 100) for i in range(0, len(output), 100)
        ])
        flattened_history: list[Any] = []
        for idx, batch in enumerate(browsing_history_list):
            if batch is not None:
                logger.debug(f"Adding batch {idx} with {len(batch)} items to flattened history")
                flattened_history.extend(batch)
            else:
                logger.warning(f"Batch {idx} was None, skipping")

        logger.info(f"Total browsing history items collected: {len(flattened_history)}")
        return {"browsing_history_data": flattened_history}

    return await remote_zen_dpage_with_action(
        country.browsing_history_url,
        action=get_browsing_history_action,
    )


async def _get_purchase_history_with_details(
    country: AmazonCountry,
    year: str | int | None = None,
    start_index: int = 0,
    timeFilter: str | None = None,
) -> dict[str, Any]:
    if year is None:
        target_year = datetime.now().year
    elif isinstance(year, str):
        try:
            target_year = int(year)
        except ValueError:
            target_year = datetime.now().year
    else:
        target_year = int(year)

    if timeFilter is None:
        timeFilter = f"year-{target_year}"

    current_year = datetime.now().year
    if not (1900 <= target_year <= current_year + 1):
        raise ValueError(f"Year {target_year} is out of valid range (1900-{current_year + 1})")

    async def get_order_details_action(page: zd.Tab, browser: zd.Browser) -> dict[str, Any]:
        current_url = await get_url(page)
        logger.info(f"Current URL: {current_url}")
        if current_url is None or "signin" in current_url:
            logger.info(f"User is not signed in")
            raise Exception("User is not signed in")

        path = os.path.join(os.path.dirname(__file__), "patterns", "**/amazon-*.html")

        logger.debug(f"Loading patterns from {path}")
        patterns = load_distillation_patterns(path)
        logger.debug(f"Loaded {len(patterns)} patterns")
        page_index = int((start_index / 10) + 1)
        _, _, orders = await run_distillation_loop(
            location=f"{country.base_url}/your-orders/orders?timeFilter={timeFilter}&startIndex={start_index}#pagination/${page_index}/time/${year}/",
            patterns=patterns,
            browser=browser,
            timeout=10,
            page=page,
        )
        if orders is None:
            return {country.purchase_history_key: []}

        for order in orders:
            order["order_id"] = _normalize_order_id(order.get("order_id")) or ""

        async def get_order_details(order: dict[str, Any]):
            order_id = order["order_id"]
            store_logo = order.get("store_logo")

            product_prices = order.get("product_prices")
            if isinstance(product_prices, list):
                return {"order_id": order_id}

            order_type = "regular"
            if store_logo:
                store_logo_text = str(store_logo).lower()
                if "whole foods" in store_logo_text:
                    order_type = "wholefoods"
                elif "fresh" in store_logo_text:
                    order_type = "fresh"

            match order_type:
                case "wholefoods":
                    url = f"{country.base_url}/fopo/order-details?orderID={order_id}&ref=ppx_yo2ov_dt_b_fed_wwgs_wfm_ATVPDKIKX0DER&page=itemmod"
                    js_code = f"""
                        (async () => {{
                            const res = await fetch('{url}', {{
                                method: 'GET',
                                credentials: 'include',
                            }});
                            const text = await res.text();
                            const parser = new DOMParser();
                            const doc = parser.parseFromString(text, 'text/html');
                            doc.querySelectorAll('script').forEach(s => s.remove());

                            const itemRows = doc.querySelectorAll('div.a-row.a-spacing-base');
                            const prices = [];
                            const productNames = [];
                            const productUrls = [];
                            const imageUrls = [];
                            const quantities = [];

                            itemRows.forEach(row => {{
                                const productLink = row.querySelector('div.a-column.a-span10 > a.a-size-small.a-link-normal');
                                if (productLink) {{
                                    const name = productLink.textContent?.trim();
                                    if (name) {{
                                        productNames.push(name);
                                    }}
                                    const href = productLink.getAttribute('href');
                                    if (href) {{
                                        productUrls.push(href);
                                    }}
                                }}

                                const priceSpan = row.querySelector('div.a-column.a-span2.a-span-last div.a-text-right span.a-size-small');
                                if (priceSpan) {{
                                    prices.push(priceSpan.textContent?.trim() || '');
                                }}

                                const quantitySpan = row.querySelector('div.a-column.a-span10 > div.a-spacing-none:nth-of-type(2) > span.a-size-small');
                                if (quantitySpan) {{
                                    quantities.push(quantitySpan.textContent?.replace("Qty: ", "")?.trim() || '');
                                }}

                                const img = row.querySelector('img.ufpo-itemListWidget-image');
                                if (img) {{
                                    const src = img.getAttribute('src') || img.getAttribute('data-a-hires');
                                    if (src) {{
                                        imageUrls.push(src);
                                    }}
                                }}
                            }});
                            let paymentInfo = "";
                            if (doc.querySelector("span#wfm-0-card-brand")){{
                                paymentInfo = doc.querySelector("span#wfm-0-card-brand")?.textContent?.trim() + " " + doc.querySelector("span#wfm-0-card-tail")?.textContent?.trim();
                            }}
                            return {{
                                prices,
                                productNames,
                                productUrls,
                                imageUrls,
                                paymentInfo,
                                quantities
                            }};
                        }})()
                    """

                case "fresh":
                    url = f"{country.base_url}/uff/your-account/order-details?orderID={order_id}&ref=ppx_yo2ov_dt_b_fed_wwgs_yo_odp_A1VC38T7YXB528&page=itemmod"
                    js_code = f"""
                        (async () => {{
                            const res = await fetch('{url}', {{
                                method: 'GET',
                                credentials: 'include',
                            }});
                            const text = await res.text();
                            const parser = new DOMParser();
                            const doc = parser.parseFromString(text, 'text/html');
                            doc.querySelectorAll('script').forEach(s => s.remove());

                            const itemRows = doc.querySelectorAll('div[id$="-item-grid-row"]');
                            const prices = [];
                            const productNames = [];
                            const productUrls = [];
                            const imageUrls = [];
                            const quantities = []

                            itemRows.forEach(row => {{
                                const priceSpan = row.querySelector('span[id$="-item-total-price"]');
                                if (priceSpan) {{
                                    prices.push(priceSpan.textContent?.trim() || '');
                                }}

                                const quantitySpan = row.querySelector('div.a-span-last div.a-grid-vertical-align div.a-text-center');
                                if (quantitySpan) {{
                                    quantities.push(quantitySpan.textContent?.trim() || '');
                                }}

                                const productLink = row.querySelector('a.a-link-normal.a-text-normal');
                                if (productLink) {{
                                    const nameSpan = productLink.querySelector('span');
                                    if (nameSpan) {{
                                        const name = nameSpan.textContent?.trim();
                                        if (name) {{
                                            productNames.push(name);
                                        }}
                                    }}
                                    const href = productLink.getAttribute('href');
                                    if (href) {{
                                        productUrls.push(href);
                                    }}
                                }}

                                const img = row.querySelector('div.ufpo-item-image-column img');
                                if (img) {{
                                    const src = img.getAttribute('src') || img.getAttribute('data-a-hires');
                                    if (src) {{
                                        imageUrls.push(src);
                                    }}
                                }}
                            }});

                            const paymentInfo = doc.querySelector("li.pmts-payments-instrument-detail-box-paystationpaymentmethod")?.textContent?.trim();
                            const paymentInfoDetail = doc.querySelector("li.pmts-payments-instrument-detail-box-paystationpaymentmethod:nth-of-type(2)")?.textContent?.trim();
                            let paymentMethod = "";
                            let paymentGiftCardAmount = "";
                            if (paymentInfoDetail?.includes("gift card")){{
                                paymentMethod = "GIFT_CARD";
                                paymentGiftCardAmount = doc.querySelector("span#ufpo-giftCardAmount-amount")?.textContent?.trim();
                            }}

                            return {{
                                prices,
                                productNames,
                                productUrls,
                                imageUrls,
                                paymentInfo,
                                paymentInfoDetail,
                                quantities
                            }};
                        }})()
                    """

                case _:
                    url = f"{country.base_url}/gp/css/summary/print.html?orderID={order_id}&ref=ppx_yo2ov_dt_b_fed_invoice_pos"
                    js_code = f"""
                        (async () => {{
                            const res = await fetch('{url}', {{
                                method: 'GET',
                                credentials: 'include',
                            }});
                            const text = await res.text();
                            const parser = new DOMParser();
                            const doc = parser.parseFromString(text, 'text/html');
                            doc.querySelectorAll('script').forEach(s => s.remove());

                            const rows = doc.querySelectorAll("div.a-fixed-left-grid");
                            const prices = Array.from(rows)
                                .map(row => row.querySelector("span.a-price span.a-offscreen")?.textContent?.trim())
                                .filter(Boolean);
                            const quantities = Array.from(rows)
                                .map(row => row.querySelector("div[data-component='itemImage']"))
                                .filter(Boolean)
                                .map(row => row?.querySelector("div[class='od-item-view-qty']")?.textContent?.trim() || "1");

                            const paymentElement = doc.querySelector("div.pmts-payment-instrument-billing-address");
                            const paymentInfoElements = Array.from(doc.querySelectorAll("span.pmts-payments-instrument-detail-box-paystationpaymentmethod"));

                            const isGiftCard = !!paymentInfoElements?.find(el => el.textContent?.toLowerCase().includes("gift card"));

                            const bnplElement = paymentElement?.querySelector("span.pmts-payments-instrument-supplemental-box-paystationpaymentmethod");

                            let paymentInfo = paymentInfoElements[0]?.textContent?.trim();
                            let paymentInfoDetail = "";
                            let paymentGiftCardAmount = "";
                            let paymentMethod = "";


                            if (bnplElement) {{
                                paymentInfoDetail = bnplElement?.textContent?.trim();
                                paymentMethod = "BNPL";
                            }} else if (isGiftCard) {{
                                paymentInfoDetail = paymentInfoElements[0]?.textContent?.trim();
                                paymentInfo = paymentInfoElements[1]?.textContent?.trim();
                                if (paymentInfo?.includes("gift card")){{
                                    paymentInfoDetail = paymentInfoElements[1]?.textContent?.trim();
                                    paymentInfo = paymentInfoElements[0]?.textContent?.trim();
                                }}
                                paymentGiftCardAmount = Array.from(doc.querySelectorAll("div#od-subtotals span.a-list-item"))
                                                            .find(el => el.textContent?.includes("Gift Card"))
                                                            ?.querySelector("div.a-span-last")
                                                            ?.textContent
                                                            ?.trim();
                                paymentMethod = "GIFT_CARD";
                            }}

                            return {{
                                prices,
                                paymentInfo,
                                paymentInfoDetail,
                                paymentGiftCardAmount,
                                paymentMethod,
                                quantities
                            }};
                        }})()
                    """

            result = await page.evaluate(js_code, True)
            return {"order_id": order_id, **cast(dict[str, Any], result)}

        try:
            order_details_list = await asyncio.gather(
                *[get_order_details(order) for order in orders], return_exceptions=True
            )

            for i, item in enumerate(order_details_list):
                if isinstance(item, BaseException):
                    order_id = orders[i]["order_id"]
                    logger.warning(f"Error getting order details for order: {order_id}: {item}")

            order_details = {
                item["order_id"]: item
                for item in order_details_list
                if not isinstance(item, BaseException)
            }
            for order in orders:
                details = order_details.get(order["order_id"])
                if details is None:
                    continue
                if details.get("prices") is not None:
                    order["product_prices"] = details["prices"]
                if details.get("quantities") is not None:
                    order["quantities"] = details["quantities"]
                if order.get("store_logo") and details.get("productNames"):
                    order["product_names"] = details["productNames"]
                    order["product_urls"] = details["productUrls"]
                    order["image_urls"] = details["imageUrls"]
                order["payment_info"] = details.get("paymentInfo") or ""
                order["payment_info_detail"] = details.get("paymentInfoDetail") or ""
                order["payment_method"] = details.get("paymentMethod") or ""
                order["payment_gift_card_amount"] = details.get("paymentGiftCardAmount") or ""
        except Exception as e:
            logger.error(f"Error getting order details for order: {e}")
            pass
        return {country.purchase_history_key: orders}

    return await remote_zen_dpage_with_action(
        f"{country.base_url}/your-orders/orders?timeFilter={timeFilter}&startIndex={start_index}",
        action=get_order_details_action,
    )


async def _signin(country: AmazonCountry) -> dict[str, Any]:
    return await remote_zen_dpage_mcp_tool(
        country.signin_url,
        "signin",
    )


async def _get_watch_history(country: AmazonCountry) -> dict[str, Any]:
    return await remote_zen_dpage_mcp_tool(
        country.watch_history_url,
        country.watch_history_result_key,
    )


async def _get_watchlist(country: AmazonCountry) -> dict[str, Any]:
    return await remote_zen_dpage_mcp_tool(
        country.watchlist_url,
        country.watchlist_result_key,
    )


async def _get_prime_library(country: AmazonCountry) -> dict[str, Any]:
    return await remote_zen_dpage_mcp_tool(
        country.prime_library_url,
        country.prime_library_result_key,
    )


async def _get_watchlist_with_pagination(
    country: AmazonCountry, start_index: int = 0
) -> dict[str, Any]:
    async def get_watchlist_with_pagination_action(
        page: zd.Tab, browser: zd.Browser
    ) -> dict[str, Any]:
        current_url = await get_url(page)
        if current_url is None or "signin" in current_url:
            logger.info(f"User is not signed in")
            raise Exception("User is not signed in")

        js_code = f"""
            (async () => {{
                const res = await fetch(location.href, {{
                    method: 'GET',
                    credentials: 'include',
                }});
                const text = await res.text();
                const parser = new DOMParser();
                const doc = parser.parseFromString(text, 'text/html');
                const hydrationData = doc.querySelector('#dv-web-page-hydration-data')?.textContent;
                const hydrationDataJson = JSON.parse(hydrationData);
                const paginationTargetId = hydrationDataJson.init.preparations.body.content.baseOutput.containers[0].paginationTargetId;
                const paginationServiceToken = hydrationDataJson.init.preparations.body.content.baseOutput.containers[0].paginationServiceToken;
                const startIndex = {start_index};
                
                const queryParam = {{
                    pageType: "home",
                    pageId: "Watchlist",
                    collectionType: "Container",
                    paginationTargetId: paginationTargetId,
                    serviceToken: paginationServiceToken,
                    startIndex: String(startIndex),
                    actionScheme: "default",
                    payloadScheme: "default",
                    decorationScheme: "web-decoration-asin-v4",
                    featureScheme: "web-features-v6",
                    dynamicFeatures: [
                        "integration",
                        "CLIENT_DECORATION_ENABLE_DAAPI",
                        "ENABLE_DRAPER_CONTENT",
                        "HorizontalPagination",
                        "CleanSlate",
                        "EpgContainerPagination",
                        "ENABLE_GPCI",
                        "SupportsImageTextLinkTextInStandardHero",
                        "Remaster",
                        "SupportsChannelWidget",
                        "PromotionalBannerSupported",
                        "RemoveFromContinueWatching",
                        "SearchChannelBundles",
                        "LinearStationInAllCarousels",
                        "SupportChannelItemDecoration",
                        "TvodMovieBundles"
                    ],
                    widgetScheme: "web-explore-v38",
                    variant: "desktopOSX",
                    myStuffViewType: "watchlist",
                    journeyIngressContext: ""
                }};
                
                const params = new URLSearchParams();
            
                for (const [key, value] of Object.entries(queryParam)) {{
                    if (Array.isArray(value)) {{
                        value.forEach(v => params.append(key, v));
                    }} else {{
                        params.append(key, value);
                    }}
                }}
            
                const url = `{country.watchlist_pagination_api_url}?${{params.toString()}}`;
            
                const response = await fetch(url, {{
                    method: 'GET',
                    credentials: 'include',
                    headers: {{
                        accept: '*/*',
                        'x-requested-with': 'XMLHttpRequest'
                    }}
                }});
            
                const data = await response.json();
                const items = data.entities ?? data.container?.entities ?? [];
            
                return {{ items, hasMoreItems: data.hasMoreItems === true, startIndex }};
            }})()
        """

        result = await page.evaluate(js_code, True)

        return {country.watchlist_result_key: result}

    return await remote_zen_dpage_with_action(
        country.watchlist_url,
        action=get_watchlist_with_pagination_action,
    )


async def _get_watch_history_with_pagination(
    country: AmazonCountry, nextToken: str | None = None
) -> dict[str, Any]:
    async def get_watch_history_with_pagination_action(
        page: zd.Tab, browser: zd.Browser
    ) -> dict[str, Any]:
        current_url = await get_url(page)
        if current_url is None or "signin" in current_url:
            logger.info(f"User is not signed in")
            raise Exception("User is not signed in")

        # await asyncio.sleep(10)
        js_code = f"""
            (async () => {{
                const res = await fetch(location.href, {{
                    method: "GET",
                    credentials: "include",
                }});
                const text = await res.text();
                const parser = new DOMParser();
                const doc = parser.parseFromString(text, "text/html");
                const hydrationData = [...doc.querySelectorAll("script[type='text/template']")]
                    .map((el) => el.textContent)
                    .find(
                    (content) =>
                        content?.includes("props") &&
                        content.includes("metadata") &&
                        content.includes("availability") &&
                        content.includes("notifications"),
                    );
                const hydrationDataJson = JSON.parse(hydrationData);
                const firstNextToken = hydrationDataJson.props.widgets[2].content.content.nextToken;
                const requestId = hydrationDataJson.args.context.requestID;
                const watchHistory = hydrationDataJson.props.widgets[2].content.content;

                if ("{nextToken}" !== "None") {{
                    const queryString = 'widgetArgs=' + encodeURIComponent(JSON.stringify({{nextToken: "{nextToken}"}}));
                    const paginationApiResponse = await fetch(`{country.watch_history_pagination_api_url}?${{queryString}}`, {{
                        "headers": {{
                            "accept": "*/*",
                            "x-amzn-requestid": requestId,
                            "Referer": "{country.watch_history_url}",
                            "x-requested-with": "XMLHttpRequest"
                        }},
                        "body": null,
                        "method": "GET",
                        "mode": "cors",
                        "credentials": "include"
                    }});
                 
                    const response = await paginationApiResponse.json();
                    return {{ watchHistory: response.widgets[2].content.content.titles[0].titles, nextToken: response.widgets[2].content.content.nextToken }};
                }}

                return {{ watchHistory: watchHistory.titles[0].titles, nextToken: firstNextToken, requestId }};
            }})()
        """

        result = await page.evaluate(js_code, True)
        # await asyncio.sleep(30)

        return {country.watch_history_result_key: result}

    return await remote_zen_dpage_with_action(
        country.watch_history_url,
        action=get_watch_history_with_pagination_action,
    )


amazon_us_mcp = MCPTool(brand_id="amazon", name="Amazon MCP")
amazon_ca_mcp = MCPTool(brand_id="amazonca", name="Amazon CA MCP")


@amazon_us_mcp.tool("search_purchase_history")
async def amazon_us_search_purchase_history(keyword: str, page_number: int = 1) -> dict[str, Any]:
    """Search purchase history from amazon."""
    return await _search_purchase_history(AMAZON_US, keyword, page_number)


@amazon_us_mcp.tool("get_purchase_history")
async def amazon_us_get_purchase_history(
    year: str | int | None = None, start_index: int = 0
) -> dict[str, Any]:
    """Get purchase/order history of a amazon with dpage."""
    return await _get_purchase_history(AMAZON_US, year, start_index)


@amazon_us_mcp.tool("search_product")
async def amazon_us_search_product(keyword: str) -> dict[str, Any]:
    """Search product on amazon."""
    return await _search_product(AMAZON_US, keyword)


@amazon_us_mcp.tool("get_browsing_history")
async def amazon_us_get_browsing_history() -> dict[str, Any]:
    """Get browsing history from amazon."""
    return await _get_browsing_history(AMAZON_US)


@amazon_us_mcp.tool("get_purchase_history_with_details")
async def amazon_us_get_purchase_history_with_details(
    year: str | int | None = None, start_index: int = 0, timeFilter: str | None = None
) -> dict[str, Any]:
    """Get purchase/order history of a amazon with dpage."""
    return await _get_purchase_history_with_details(AMAZON_US, year, start_index, timeFilter)


@amazon_us_mcp.tool("signin")
async def amazon_us_signin() -> dict[str, Any]:
    """Signin to amazon."""
    return await _signin(AMAZON_US)


@amazon_us_mcp.tool("get_watch_history")
async def amazon_us_get_watch_history() -> dict[str, Any]:
    """Get video watch history from Amazon."""
    return await _get_watch_history(AMAZON_US)


@amazon_us_mcp.tool("get_watchlist")
async def amazon_us_get_watchlist() -> dict[str, Any]:
    """Get Prime Video watchlist from Amazon."""
    return await _get_watchlist(AMAZON_US)


@amazon_us_mcp.tool("get_prime_library")
async def amazon_us_get_prime_library() -> dict[str, Any]:
    """Get Prime Video purchases and rentals library from Amazon."""
    return await _get_prime_library(AMAZON_US)


@amazon_us_mcp.tool("get_watchlist_with_pagination")
async def amazon_us_get_watchlist_with_pagination(start_index: int = 0) -> dict[str, Any]:
    """Get Prime Video watchlist from Amazon US with pagination."""
    return await _get_watchlist_with_pagination(AMAZON_US, start_index)


@amazon_us_mcp.tool("get_watch_history_with_pagination")
async def amazon_us_get_watch_history_with_pagination(
    nextToken: str | None = None,
) -> dict[str, Any]:
    """Get Prime Video watchlist from Amazon US with pagination."""
    return await _get_watch_history_with_pagination(AMAZON_US, nextToken)


@amazon_ca_mcp.tool("search_purchase_history")
async def amazon_ca_search_purchase_history(keyword: str, page_number: int = 1) -> dict[str, Any]:
    """Search purchase history from amazon."""
    return await _search_purchase_history(AMAZON_CA, keyword, page_number)


@amazon_ca_mcp.tool("get_purchase_history")
async def amazon_ca_get_purchase_history(
    year: str | int | None = None, start_index: int = 0
) -> dict[str, Any]:
    """Get purchase/order history of a amazon with dpage."""
    return await _get_purchase_history(AMAZON_CA, year, start_index)


@amazon_ca_mcp.tool("search_product")
async def amazon_ca_search_product(keyword: str) -> dict[str, Any]:
    """Search product on amazon."""
    return await _search_product(AMAZON_CA, keyword)


@amazon_ca_mcp.tool("get_browsing_history")
async def amazon_ca_get_browsing_history() -> dict[str, Any]:
    """Get browsing history from amazon."""
    return await _get_browsing_history(AMAZON_CA)


@amazon_ca_mcp.tool("get_purchase_history_with_details")
async def amazon_ca_get_purchase_history_with_details(
    year: str | int | None = None, start_index: int = 0, timeFilter: str | None = None
) -> dict[str, Any]:
    """Get purchase/order history of a amazon with dpage."""
    return await _get_purchase_history_with_details(AMAZON_CA, year, start_index, timeFilter)


@amazon_ca_mcp.tool("signin")
async def amazon_ca_signin() -> dict[str, Any]:
    """Signin to amazon."""
    return await _signin(AMAZON_CA)


@amazon_ca_mcp.tool("get_watch_history")
async def amazon_ca_get_watch_history() -> dict[str, Any]:
    """Get video watch history from Amazon Canada Prime Video."""
    return await _get_watch_history(AMAZON_CA)


@amazon_ca_mcp.tool("get_watchlist")
async def amazon_ca_get_watchlist() -> dict[str, Any]:
    """Get Prime Video watchlist from Amazon Canada."""
    return await _get_watchlist(AMAZON_CA)


@amazon_ca_mcp.tool("get_prime_library")
async def amazon_ca_get_prime_library() -> dict[str, Any]:
    """Get Prime Video purchases and rentals library from Amazon Canada."""
    return await _get_prime_library(AMAZON_CA)


@amazon_ca_mcp.tool("get_watchlist_with_pagination")
async def amazon_ca_get_watchlist_with_pagination(start_index: int = 0) -> dict[str, Any]:
    """Get Prime Video watchlist from Amazon Canada with pagination."""
    return await _get_watchlist_with_pagination(AMAZON_CA, start_index)


@amazon_ca_mcp.tool("get_watch_history_with_pagination")
async def amazon_ca_get_watch_history_with_pagination(
    nextToken: str | None = None,
) -> dict[str, Any]:
    """Get Prime Video watchlist from Amazon Canada with pagination."""
    return await _get_watch_history_with_pagination(AMAZON_CA, nextToken)
