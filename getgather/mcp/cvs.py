from typing import Any, cast

import zendriver as zd
from loguru import logger

from getgather.browser import retry_with_navigation, zen_navigate_with_retry
from getgather.mcp.dpage import (
    remote_zen_dpage_mcp_tool,
    remote_zen_dpage_with_action,
)
from getgather.mcp.registry import MCPTool

cvs_mcp = MCPTool.registry["cvs"]


@cvs_mcp.tool
async def signin() -> dict[str, Any]:
    """Signin to a user's CVS account (local zen)."""
    return await remote_zen_dpage_mcp_tool("https://www.cvs.com/account-login", "cvs_signin")


async def get_perscription_history_action(tab: zd.Tab, _) -> dict[str, Any]:
    """Get the perscription history from a user's CVS account (local zen)."""

    logger.info("Starting get_perscription_history_action")

    async def fetch_orders() -> dict[str, Any]:
        orders = None

        await zen_navigate_with_retry(tab, "https://www.cvs.com/pharmacy/rx/prescriptions", wait_for_ready=False)
        orders = await tab.evaluate(
            f"""
                (async () => {{
                    const httpRequest = await new Promise(resolve => {{
                        const originalFetch = window.fetch;
                        window.fetch = async function (...args) {{
                            if(args[0].includes('mcapi/client/experience/v2/load') && args[0].includes('ice_plp_web_retail_blocks') && args[1].method === 'POST'){{
                                window.fetch = originalFetch;
                                resolve(args);
                            }}
                            const response = await originalFetch.apply(this, args);
                            return response;
                        }};
                    }})
                    
                    const url = httpRequest[0]
                    const headers = httpRequest[1].headers
                    const originalBody = JSON.parse(httpRequest[1].body);
                    const endDateObj = new Date(originalBody.data.endDate);
                    const startDateObj = new Date(endDateObj);
                    startDateObj.setMonth(startDateObj.getMonth() - 24);
                    const startDate = String(originalBody.data.endDate).includes("T")
                        ? startDateObj.toISOString()
                        : startDateObj.toISOString().slice(0, 10);
                    const body = {{
                        ...originalBody,
                        data: {{
                            ...originalBody.data,
                            startDate,
                        }}
                    }};
                    
                    const res = await fetch(url, {{
                        method: 'POST',
                        credentials: 'include',
                        headers,
                        body: JSON.stringify(body)
                    }});
                    if (!res.ok) {{
                        const error_text = await res.text();
                        throw new Error(`HTTP error! status: ${{res.status}} - ${{error_text}}`);
                    }}
                    return await res.json();
                }})()
            """,
            True,
        )
        return cast(dict[str, Any], orders)

    return await retry_with_navigation(
        tab=tab,
        operation=fetch_orders,
        max_retries=3,
        exceptions=(Exception,),
        re_raise_on_max_retries=True,
        timeout_seconds=30,
        operation_name="get_perscription_history_action",
    )


@cvs_mcp.tool
async def get_perscription_history() -> dict[str, Any]:
    """Get the perscription history from a user's CVS account."""
    return await remote_zen_dpage_with_action(
        "https://www.cvs.com/pharmacy/rx/prescriptions",
        get_perscription_history_action,
    )
