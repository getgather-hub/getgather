"""Guards Target's order-history extraction after the ONLINE endpoint change.

_get_purchases navigates to /orders, captures the x-api-key off the page's own
order-history request, then fetches the list page itself (with retry + per-attempt
timeout).

ONLINE uses /post_orders/v1/orders/history and its response is already fully
populated, so its "orders" array is returned verbatim - no detail fetch.
STORE keeps the legacy lean list + per-order /orders/{id}/store detail fetch.

Pure-function / fake-tab tests only - no browser or live server involved.
"""

# Importing declarative_mcp runs create_declarative_mcp_tools() at import time,
# registering all brands (incl. target) and importing the target custom
# module — which must happen before `from getgather.mcp.target import ...`
# resolves, since that module looks up MCPTool.registry["target"] at import.
import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from getgather.mcp import declarative_mcp

assert "target" in declarative_mcp.MCPTool.registry

import getgather.mcp.target as target_module  # noqa: E402
from getgather.mcp.target import (  # noqa: E402
    _get_purchases,  # pyright: ignore[reportPrivateUsage]
)


class _FakeTab:
    """Returns queued evaluate() results in order, recording the JS handed to each call."""

    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.evaluate_calls: list[str] = []

    async def evaluate(self, js_code: str, return_by_value: bool = True) -> Any:
        self.evaluate_calls.append(js_code)
        return self.results.pop(0)


async def _run_get_purchases(
    monkeypatch: Any, order_purchase_type: str, orders: list[dict[str, Any]]
):
    monkeypatch.setattr(target_module, "zen_navigate_with_retry", AsyncMock(return_value=None))
    tab = _FakeTab(["test-api-key", {"orders": orders, "total_pages": 1}])
    result = await _get_purchases(tab, 1, order_purchase_type)  # type: ignore[arg-type]
    return result, tab


def test_key_capture_watches_both_list_endpoints(monkeypatch: Any):
    _result, tab = asyncio.run(_run_get_purchases(monkeypatch, "ONLINE", []))

    capture_js = tab.evaluate_calls[0]
    assert "/post_orders/v1/orders/history" in capture_js
    assert "/guest_order_aggregations/v1/order_history" in capture_js
    assert "x-api-key" in capture_js


def test_online_uses_new_post_orders_history_endpoint_with_captured_key(monkeypatch: Any):
    _result, tab = asyncio.run(_run_get_purchases(monkeypatch, "ONLINE", []))

    list_js = tab.evaluate_calls[1]
    assert "api.target.com/post_orders/v1/orders/history" in list_js
    assert "order_purchase_type=ONLINE" in list_js
    assert "test-api-key" in list_js
    assert "credentials: 'include'" in list_js
    assert "https://www.target.com/" in list_js


def test_online_returns_list_orders_verbatim_without_detail_fetch(monkeypatch: Any):
    order = {"order_number": "123", "order_lines": [{"item": {"tcin": "t1"}}]}
    result, tab = asyncio.run(_run_get_purchases(monkeypatch, "ONLINE", [order]))

    assert result["target_purchases"] == [order]
    # Only key-capture + list fetch ran - no detail fetch.
    assert len(tab.evaluate_calls) == 2


def test_store_still_fetches_detail_per_order(monkeypatch: Any):
    monkeypatch.setattr(target_module, "zen_navigate_with_retry", AsyncMock(return_value=None))
    tab = _FakeTab([
        "test-api-key",
        {"orders": [{"store_receipt_id": "R1"}], "total_pages": 1},
        [{"store_receipt_id": "R1", "unit_price": "9.99"}],
    ])

    result = asyncio.run(_get_purchases(tab, 1, "STORE"))  # type: ignore[arg-type]

    assert result["target_purchases"] == [{"store_receipt_id": "R1", "unit_price": "9.99"}]
    detail_js = tab.evaluate_calls[2]
    assert "orders/R1/store" in detail_js


def test_store_with_no_orders_skips_detail_fetch(monkeypatch: Any):
    result, tab = asyncio.run(_run_get_purchases(monkeypatch, "STORE", []))

    assert result["target_purchases"] == []
    # Only key-capture + list fetch ran - no orders means no detail fetch.
    assert len(tab.evaluate_calls) == 2


def test_missing_api_key_raises(monkeypatch: Any):
    monkeypatch.setattr(target_module, "zen_navigate_with_retry", AsyncMock(return_value=None))
    tab = _FakeTab(["", {"orders": [], "total_pages": 1}])

    with pytest.raises(RuntimeError, match="x-api-key missing"):
        asyncio.run(_get_purchases(tab, 1, "ONLINE"))  # type: ignore[arg-type]


def test_list_fetch_retries_then_raises_on_repeated_failure(monkeypatch: Any):
    monkeypatch.setattr(target_module, "zen_navigate_with_retry", AsyncMock(return_value=None))

    class _FailingTab(_FakeTab):
        async def evaluate(self, js_code: str, return_by_value: bool = True) -> Any:
            self.evaluate_calls.append(js_code)
            if len(self.evaluate_calls) == 1:
                return "test-api-key"
            raise RuntimeError("HTTP 500")

    tab = _FailingTab([])
    with pytest.raises(RuntimeError, match="list fetch failed after"):
        asyncio.run(_get_purchases(tab, 1, "ONLINE"))  # type: ignore[arg-type]

    # 1 key-capture call + 3 retried list-fetch attempts.
    assert len(tab.evaluate_calls) == 1 + target_module._FETCH_RETRIES  # pyright: ignore[reportPrivateUsage]
