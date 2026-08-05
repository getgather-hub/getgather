"""Unit tests for Target purchase-detail URL/identifier resolution.

Pure-function tests only — no browser or live server involved.
"""

# Importing declarative_mcp runs create_declarative_mcp_tools() at import time,
# registering all brands (incl. target) and importing the target custom
# module — which must happen before `from getgather.mcp.target import ...`
# resolves, since that module looks up MCPTool.registry["target"] at import.
from getgather.mcp import declarative_mcp
from getgather.mcp.target import _DETAIL_BASE, _ORDER_ID_FIELD, _detail_url

assert "target" in declarative_mcp.MCPTool.registry


def test_online_detail_url_is_unchanged():
    url = _detail_url("ONLINE", "1234567890")
    assert url == f"{_DETAIL_BASE}/1234567890"


def test_store_detail_url_has_orders_and_store_suffix():
    url = _detail_url("STORE", "5228-0324-0172-6691")
    assert url == f"{_DETAIL_BASE}/orders/5228-0324-0172-6691/store"


def test_order_id_field_mapping():
    assert _ORDER_ID_FIELD == {"ONLINE": "order_number", "STORE": "store_receipt_id"}
