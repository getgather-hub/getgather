"""Unit tests for the Walgreens purchase-history response builder.

These use sanitized, synthetic fixtures derived from the observed
``/orderhistory/v1/orders/search`` response shape (no real personal data). They
exercise the pure pagination/parsing logic without a live browser or server.
"""

from typing import Any

# Importing declarative_mcp runs create_declarative_mcp_tools() at import time,
# registering all brands (incl. walgreens) and importing the walgreens custom
# module — which must happen before `from getgather.mcp.walgreens import ...`
# resolves, since that module looks up MCPTool.registry["walgreens"] at import.
from getgather.mcp import declarative_mcp
from getgather.mcp.walgreens import PAGE_SIZE, build_purchases_response

assert "walgreens" in declarative_mcp.MCPTool.registry


def _empty_response() -> dict[str, Any]:
    """The exact shape observed for an authenticated account with no orders."""
    return {
        "filter": {"filterType": "ALL", "tab": "ONLINE", "p": 1, "s": PAGE_SIZE},
        "failedSvcs": [],
        "callStatus": "SUCCESS",
        "messages": [],
        "orders": [],
    }


def _order(order_id: str) -> dict[str, Any]:
    """A synthetic raw order object (arbitrary fields; not real data)."""
    return {
        "orderId": order_id,
        "orderStatus": "Delivered",
        "purchaseDate": "2026-01-15",
        "channel": "ONLINE",
        "orderTotal": {"amount": "12.34", "currency": "USD"},
        "items": [{"description": "Synthetic Item", "quantity": 1}],
    }


def test_empty_first_page_is_success_not_error():
    result = build_purchases_response(_empty_response(), page_number=1)
    assert result["walgreens_purchases"] == []
    assert result["pagination"] == {"current_page": 1, "total_pages": 0, "page_size": PAGE_SIZE}


def test_empty_page_beyond_first_has_unknown_total():
    result = build_purchases_response(_empty_response(), page_number=3)
    assert result["walgreens_purchases"] == []
    assert result["pagination"] == {"current_page": 3, "total_pages": None, "page_size": PAGE_SIZE}


def test_partial_page_is_last_page():
    data = {"callStatus": "SUCCESS", "orders": [_order("A1"), _order("A2")]}
    result = build_purchases_response(data, page_number=1)
    assert len(result["walgreens_purchases"]) == 2
    assert result["pagination"] == {"current_page": 1, "total_pages": 1, "page_size": PAGE_SIZE}


def test_partial_page_two_reports_that_page_as_total():
    data = {"callStatus": "SUCCESS", "orders": [_order(f"B{i}") for i in range(30)]}
    result = build_purchases_response(data, page_number=2)
    assert result["pagination"] == {"current_page": 2, "total_pages": 2, "page_size": PAGE_SIZE}


def test_full_page_total_is_unknown():
    data = {"callStatus": "SUCCESS", "orders": [_order(f"C{i}") for i in range(PAGE_SIZE)]}
    result = build_purchases_response(data, page_number=1)
    assert len(result["walgreens_purchases"]) == PAGE_SIZE
    assert result["pagination"] == {"current_page": 1, "total_pages": None, "page_size": PAGE_SIZE}


def test_raw_order_objects_are_preserved_unmodified():
    order = _order("D1")
    order["someVendorField"] = {"nested": [1, 2, 3]}
    result = build_purchases_response({"orders": [order]}, page_number=1)
    assert result["walgreens_purchases"][0] == order  # no fields dropped or renamed


def test_non_list_orders_yields_empty():
    result = build_purchases_response({"orders": None}, page_number=1)
    assert result["walgreens_purchases"] == []
    assert result["pagination"]["total_pages"] == 0
