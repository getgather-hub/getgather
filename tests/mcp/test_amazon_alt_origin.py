"""Exercise the Amazon US tools against an alternate origin via their runtime `base_url` parameter.

Needs a running server and a reachable stand-in for Amazon; skips without `--amazon-base-url`.
"""

import json
from typing import Any, cast

import pytest
import zendriver as zd
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp.types import TextContent

# (tool name, arguments besides base_url, key the payload must carry)
DATA_TOOLS: list[tuple[str, dict[str, Any], str]] = [
    ("amazon_search_purchase_history", {"keyword": ""}, "order_history"),
    ("amazon_get_purchase_history", {}, "amazon_purchase_history"),
    ("amazon_get_purchase_history_with_details", {}, "amazon_purchase_history"),
    ("amazon_search_product", {"keyword": ""}, "product_list"),
    ("amazon_get_browsing_history", {}, "browsing_history_data"),
    ("amazon_get_watch_history", {}, "amazon_watch_history"),
    ("amazon_get_watchlist", {}, "amazon_prime_watchlist"),
    ("amazon_get_prime_library", {}, "amazon_prime_library"),
    ("amazon_get_watchlist_with_pagination", {"start_index": 0}, "amazon_prime_watchlist"),
    ("amazon_get_watch_history_with_pagination", {}, "amazon_watch_history"),
]


async def _call_json(client: Client[Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert result.content, f"{tool} returned no content"
    assert isinstance(result.content[0], TextContent), (
        f"Expected TextContent from {tool}, got {type(result.content[0])}"
    )
    parsed: object = json.loads(result.content[0].text)
    assert isinstance(parsed, dict), f"Expected a JSON object from {tool}, got {type(parsed)}"
    return cast(dict[str, Any], parsed)


async def _sign_in(signin_url: str, email: str, password: str) -> None:
    browser = await zd.start(no_sandbox=True, headless=True)
    try:
        page = await browser.get(signin_url)
        email_input = await page.wait_for("input[name=email]")
        await email_input.send_keys(email)
        password_input = await page.wait_for("input[name=password]")
        await password_input.send_keys(password)
        submit = await page.select("button[type='submit']")
        await submit.click()
        await page.wait_for(text="Finished!", timeout=30)
    finally:
        await browser.stop()


@pytest.mark.mcp
@pytest.mark.asyncio
async def test_amazon_us_tools_against_an_alternate_origin(
    mcp_config: dict[str, Any],
    amazon_base_url: str,
    amazon_credentials: tuple[str, str],
) -> None:
    """All eleven Amazon US tools work when repointed at another origin at runtime.

    One client session means one `mcp-session-id`, so a single browser and cookie jar is
    shared across every call.
    """
    email, password = amazon_credentials

    async with Client(mcp_config, timeout=180) as client:
        signin = await _call_json(client, "amazon_signin", {"base_url": amazon_base_url})
        if signin.get("signin_id"):
            await _sign_in(str(signin["url"]), email, password)
            checked = await _call_json(client, "check_signin", {"signin_id": signin["signin_id"]})
            assert checked.get("status") == "SUCCESS", checked

        for tool, arguments, result_key in DATA_TOOLS:
            payload = await _call_json(client, tool, {**arguments, "base_url": amazon_base_url})
            # A signin_id here means the override dropped the session, or the call reached
            # real Amazon rather than the alternate origin.
            assert "signin_id" not in payload, f"{tool} asked to sign in again: {payload}"
            assert result_key in payload, f"{tool} did not return {result_key}: {payload}"
            assert payload[result_key], f"{tool} returned an empty {result_key}"


@pytest.mark.mcp
@pytest.mark.asyncio
async def test_invalid_base_url_is_rejected(mcp_config: dict[str, Any]) -> None:
    """The validation message survives the FastMCP boundary. Needs no alternate site."""
    async with Client(mcp_config, timeout=30) as client:
        with pytest.raises(ToolError, match="base_url"):
            await client.call_tool(
                "amazon_search_product", {"keyword": "book", "base_url": "not-an-origin"}
            )
