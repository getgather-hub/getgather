"""Unit tests for Target purchase-detail URL/identifier resolution.

Pure-function tests only — no browser or live server involved.
"""

# Importing declarative_mcp runs create_declarative_mcp_tools() at import time,
# registering all brands (incl. target) and importing the target custom
# module — which must happen before `from getgather.mcp.target import ...`
# resolves, since that module looks up MCPTool.registry["target"] at import.
import asyncio
from typing import Any

from getgather.mcp import declarative_mcp
from getgather.mcp.target import _DETAIL_BASE, _ORDER_ID_FIELD, _detail_url, _fetch_all_details

assert "target" in declarative_mcp.MCPTool.registry


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
