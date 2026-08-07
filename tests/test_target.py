"""Unit tests for Target purchase-detail URL/identifier resolution.

Pure-function tests only — no browser or live server involved.
"""

# Importing declarative_mcp runs create_declarative_mcp_tools() at import time,
# registering all brands (incl. target) and importing the target custom
# module — which must happen before `from getgather.mcp.target import ...`
# resolves, since that module looks up MCPTool.registry["target"] at import.
import asyncio
from typing import Any
from unittest.mock import AsyncMock

from getgather.mcp import declarative_mcp

assert "target" in declarative_mcp.MCPTool.registry

import getgather.mcp.target as target_module  # noqa: E402
from getgather.mcp.target import (  # noqa: E402
    _DETAIL_BASE,  # pyright: ignore[reportPrivateUsage]
    _ORDER_ID_FIELD,  # pyright: ignore[reportPrivateUsage]
    _detail_url,  # pyright: ignore[reportPrivateUsage]
    _fetch_all_details,  # pyright: ignore[reportPrivateUsage]
    _get_purchases,  # pyright: ignore[reportPrivateUsage]
)


def test_online_detail_url_is_unchanged():
    url = _detail_url("ONLINE", "1234567890")
    assert url == f"{_DETAIL_BASE}/1234567890"


def test_store_detail_url_has_orders_and_store_suffix():
    url = _detail_url("STORE", "5228-0324-0172-6691")
    assert url == f"{_DETAIL_BASE}/orders/5228-0324-0172-6691/store"


def test_order_id_field_mapping():
    assert _ORDER_ID_FIELD == {"ONLINE": "order_number", "STORE": "store_receipt_id"}


class _FakeTab:
    """Captures the JS handed to evaluate() and returns a canned result."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.last_js_code: str | None = None

    async def evaluate(self, js_code: str, return_by_value: bool = True) -> Any:
        self.last_js_code = js_code
        return self.result


def test_fetch_all_details_builds_store_url_in_js():
    fake_page = _FakeTab([{"store_receipt_id": "R1", "unit_price": "9.99"}])
    result = asyncio.run(
        _fetch_all_details(fake_page, "STORE", ["5228-0324-0172-6691"], "test-api-key")  # type: ignore[arg-type]
    )
    assert result == [{"store_receipt_id": "R1", "unit_price": "9.99"}]
    assert fake_page.last_js_code is not None
    assert "/orders/5228-0324-0172-6691/store" in fake_page.last_js_code


def test_fetch_all_details_builds_online_url_in_js():
    fake_page = _FakeTab([{"order_number": "O1"}])
    result = asyncio.run(
        _fetch_all_details(fake_page, "ONLINE", ["1234567890"], "test-api-key")  # type: ignore[arg-type]
    )
    assert result == [{"order_number": "O1"}]
    assert fake_page.last_js_code is not None
    assert "post_orders/v1/1234567890" in fake_page.last_js_code
    assert "/orders/" not in fake_page.last_js_code


async def _run_get_purchases(
    monkeypatch: Any, order_purchase_type: str, orders: list[dict[str, Any]]
):
    monkeypatch.setattr(target_module, "zen_navigate_with_retry", AsyncMock(return_value=None))

    # ONLINE + page 1 reuses this intercepted page1 payload directly (see _get_purchases);
    # every other case calls the mocked _fetch_list_page below instead.
    intercept_page = _FakeTab({
        "page1": {"orders": orders, "total_pages": 1},
        "x_api_key": "test-api-key",
    })

    fetch_list_page_mock = AsyncMock(return_value={"orders": orders, "total_pages": 1})
    monkeypatch.setattr(target_module, "_fetch_list_page", fetch_list_page_mock)

    fetch_all_details_mock = AsyncMock(return_value=[{**o, "unit_price": "9.99"} for o in orders])
    monkeypatch.setattr(target_module, "_fetch_all_details", fetch_all_details_mock)

    result = await _get_purchases(intercept_page, 1, order_purchase_type)  # type: ignore[arg-type]
    return result, fetch_all_details_mock


def test_store_purchases_now_hit_detail_fetch(monkeypatch: Any):
    orders = [{"store_receipt_id": "R1"}, {"store_receipt_id": "R2"}]
    result, fetch_all_details_mock = asyncio.run(_run_get_purchases(monkeypatch, "STORE", orders))

    # full replace: target_purchases comes from the detail fetch, not the raw list orders
    assert result["target_purchases"] == [
        {"store_receipt_id": "R1", "unit_price": "9.99"},
        {"store_receipt_id": "R2", "unit_price": "9.99"},
    ]
    fetch_all_details_mock.assert_awaited_once()
    call_args = fetch_all_details_mock.await_args
    assert call_args is not None
    _page, order_purchase_type, identifiers, _x_api_key = call_args.args
    assert order_purchase_type == "STORE"
    assert identifiers == ["R1", "R2"]


def test_online_purchases_still_use_order_number(monkeypatch: Any):
    orders = [{"order_number": "O1"}]
    result, fetch_all_details_mock = asyncio.run(_run_get_purchases(monkeypatch, "ONLINE", orders))

    assert result["target_purchases"] == [{"order_number": "O1", "unit_price": "9.99"}]
    call_args = fetch_all_details_mock.await_args
    assert call_args is not None
    _page, order_purchase_type, identifiers, _x_api_key = call_args.args
    assert order_purchase_type == "ONLINE"
    assert identifiers == ["O1"]


def test_store_with_no_orders_skips_detail_fetch(monkeypatch: Any):
    result, fetch_all_details_mock = asyncio.run(_run_get_purchases(monkeypatch, "STORE", []))

    assert result["target_purchases"] == []
    fetch_all_details_mock.assert_not_awaited()
