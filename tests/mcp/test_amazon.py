import json
from typing import Any, cast

import httpx
import pytest
from fastmcp import Client
from mcp.types import TextContent

from getgather.config import settings

pytestmark = [
    pytest.mark.mcp,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not settings.AMAZON_BASE_URL,
        reason="Set AMAZON_BASE_URL to run the local mock Amazon MCP test",
    ),
]


async def _call_json(
    client: Client[Any], tool_name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = await client.call_tool(tool_name, arguments or {})
    assert result.content
    assert isinstance(result.content[0], TextContent)
    parsed = cast(object, json.loads(result.content[0].text))
    assert isinstance(parsed, dict)
    return cast(dict[str, Any], parsed)


async def _complete_mock_signin(signin_url: str) -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as browser:
        signin_page = await browser.get(signin_url)
        signin_page.raise_for_status()
        assert 'name="email"' in signin_page.text
        assert 'name="password"' in signin_page.text

        completed = await browser.post(
            signin_url,
            data={"email": "joe@example.com", "password": "trustno1"},
        )
        completed.raise_for_status()
        assert "Finished!" in completed.text


async def test_amazon_mock_exercises_all_us_tools(mcp_config: dict[str, Any]) -> None:
    mock_health_url = settings.AMAZON_BASE_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=3) as health_client:
            health = await health_client.get(f"{mock_health_url}/health")
            health.raise_for_status()
    except httpx.HTTPError as error:
        pytest.fail(
            f"Mock Amazon is not healthy at {mock_health_url}; "
            f"start apps/mock-amazon first: {error}"
        )

    async with Client(mcp_config, timeout=180) as client:
        first_signin = await _call_json(client, "amazon_signin")
        assert isinstance(first_signin.get("url"), str)
        assert isinstance(first_signin.get("signin_id"), str)

        await _complete_mock_signin(first_signin["url"])

        checked = await _call_json(
            client,
            "check_signin",
            {"signin_id": first_signin["signin_id"]},
        )
        assert checked.get("status") == "SUCCESS"
        assert checked.get("completed") is True

        list_cases: list[tuple[str, dict[str, Any], str]] = [
            ("amazon_signin", {}, "signin"),
            ("amazon_get_purchase_history", {}, "amazon_purchase_history"),
            (
                "amazon_get_purchase_history_with_details",
                {},
                "amazon_purchase_history",
            ),
            ("amazon_search_purchase_history", {"keyword": ""}, "order_history"),
            ("amazon_search_product", {"keyword": ""}, "product_list"),
            ("amazon_get_browsing_history", {}, "browsing_history_data"),
            ("amazon_get_watch_history", {}, "amazon_watch_history"),
            ("amazon_get_watchlist", {}, "amazon_prime_watchlist"),
            ("amazon_get_prime_library", {}, "amazon_prime_library"),
        ]
        for tool_name, arguments, result_key in list_cases:
            payload = await _call_json(client, tool_name, arguments)
            assert result_key in payload, f"{tool_name} did not return {result_key}"
            assert isinstance(payload[result_key], list)
            assert payload[result_key], f"{tool_name} returned an empty fixture"

        watchlist_page = await _call_json(
            client,
            "amazon_get_watchlist_with_pagination",
            {"start_index": 0},
        )
        assert isinstance(watchlist_page.get("amazon_prime_watchlist"), dict)
        assert watchlist_page["amazon_prime_watchlist"].get("items")

        watch_history_page = await _call_json(
            client,
            "amazon_get_watch_history_with_pagination",
        )
        assert isinstance(watch_history_page.get("amazon_watch_history"), dict)
        assert watch_history_page["amazon_watch_history"].get("watchHistory")
