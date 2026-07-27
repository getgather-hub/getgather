import os
import urllib.parse
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from getgather.browser import find_browser_tab, get_remote_browser
from getgather.browsers.router import strip_browser_id_from_target_id
from getgather.cdp_client import BrowserNotFoundError, CDPError, PageNotFoundError, open_cdp
from getgather.mcp.dpage import distill_post_loop
from getgather.zen_distill import (
    convert,
    distill,
    load_distillation_patterns,
    zen_report_distill_error,
)

router = APIRouter()


@router.get("/api/v1/browsers/{browser_id}/pages")
async def list_pages(browser_id: str) -> JSONResponse:
    try:
        client = await open_cdp(browser_id)
    except BrowserNotFoundError:
        raise HTTPException(status_code=404, detail=f"Browser {browser_id} not found!")
    except CDPError as e:
        raise HTTPException(
            status_code=503, detail=f"Browser {browser_id} temporarily unavailable: {e}"
        )

    try:
        result = await client.send("Target.getTargets")
    except Exception as e:
        logger.error(f"Error listing pages via CDP for {browser_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to list pages: {e}")
    finally:
        await client.aclose()

    target_infos: list[dict[str, Any]] = result.get("targetInfos", [])  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    page_ids = [str(info["targetId"]) for info in target_infos if info.get("type") == "page"]
    return JSONResponse(page_ids)


@router.get("/api/v1/browsers/{browser_id}/pages/{page_id}/html")
async def get_page_html(browser_id: str, page_id: str) -> HTMLResponse:
    page_id = strip_browser_id_from_target_id(page_id)
    try:
        client = await open_cdp(browser_id)
    except BrowserNotFoundError:
        raise HTTPException(status_code=404, detail=f"Browser {browser_id} not found!")
    except CDPError as e:
        raise HTTPException(
            status_code=503, detail=f"Browser {browser_id} temporarily unavailable: {e}"
        )

    try:
        try:
            page = await client.attach_to_page(page_id)
        except PageNotFoundError:
            raise HTTPException(status_code=404, detail=f"Page {page_id} not found in browser")
        except Exception as e:
            logger.error(f"Failed to attach to {browser_id}/{page_id}: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to get page HTML: {e}")

        try:
            html = await page.evaluate("document.documentElement.outerHTML")
        except Exception as e:
            logger.error(f"Error fetching page HTML for {browser_id}/{page_id}: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to get page HTML: {e}")

        if not isinstance(html, str):
            html = str(html) if html is not None else ""
        return HTMLResponse(content=html)
    finally:
        await client.aclose()


@router.get("/api/v1/browsers/{browser_id}/pages/{page_id}/distilled", response_model=None)
async def get_page_distilled(browser_id: str, page_id: str) -> JSONResponse | HTMLResponse:
    page_id = strip_browser_id_from_target_id(page_id)
    try:
        client = await open_cdp(browser_id)
    except BrowserNotFoundError:
        raise HTTPException(status_code=404, detail=f"Browser {browser_id} not found!")
    except CDPError as e:
        raise HTTPException(
            status_code=503, detail=f"Browser {browser_id} temporarily unavailable: {e}"
        )

    try:
        try:
            page = await client.attach_to_page(page_id)
        except PageNotFoundError:
            raise HTTPException(status_code=404, detail=f"Page {page_id} not found in browser")
        except Exception as e:
            logger.error(f"Failed to attach to {browser_id}/{page_id}: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to distill page: {e}")

        try:
            current_url = str(await page.evaluate("window.location.href", await_promise=True))
            hostname = urllib.parse.urlparse(current_url).hostname or ""

            path = os.path.join(os.path.dirname(__file__), "mcp", "patterns", "*.html")
            patterns = load_distillation_patterns(path)
            if not patterns:
                raise HTTPException(status_code=502, detail="No patterns found for '*.html'")

            match = await distill(hostname, page, patterns)  # type: ignore[arg-type]
            if not match:
                error = HTTPException(status_code=502, detail="No matching pattern found for page")
                browser = await get_remote_browser(browser_id)
                tab = find_browser_tab(browser, page_id) if browser else None
                await zen_report_distill_error(
                    error=error,
                    page=tab,
                    profile_id=browser_id,
                    location="get_page_distilled",
                    hostname=hostname,
                    iteration=0,
                )
                raise error

            converted = await convert(match.distilled, pattern_path=match.name)
            if converted:
                return JSONResponse(converted)

            return HTMLResponse(content=match.distilled)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error distilling page for {browser_id}/{page_id}: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to distill page: {e}")
    finally:
        await client.aclose()


@router.post("/api/v1/browsers/{browser_id}/pages/{page_id}/distill")
async def post_page_distill(
    browser_id: str,
    page_id: str,
    request: Request,
) -> HTMLResponse:
    page_id = strip_browser_id_from_target_id(page_id)
    browser = await get_remote_browser(browser_id)
    if browser is None:
        raise HTTPException(status_code=404, detail=f"Browser {browser_id} not found!")

    page = find_browser_tab(browser, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"Page {page_id} not found in browser")

    logger.info(f"POST /distill for browser: {browser_id}  page: {page_id}")
    form_data = await request.form()
    fields: dict[str, str] = {k: str(v) for k, v in form_data.items()}
    action = f"/api/v1/browsers/{browser_id}/pages/{page_id}/distill"
    return await distill_post_loop(page, page_id, fields, action, timeout=15)


@router.post("/api/v1/browsers/{browser_id}/pages/{page_id}/navigate")
@router.get("/api/v1/browsers/{browser_id}/pages/{page_id}/navigate")
async def navigate_page(
    browser_id: str,
    page_id: str,
    request: Request,
    url: str | None = None,
) -> JSONResponse:
    target_url = url if url is not None else request.url.query
    if not target_url:
        raise HTTPException(status_code=400, detail="Missing 'url' query parameter")

    page_id = strip_browser_id_from_target_id(page_id)
    try:
        client = await open_cdp(browser_id)
    except BrowserNotFoundError:
        raise HTTPException(status_code=404, detail=f"Browser {browser_id} not found!")
    except CDPError as e:
        raise HTTPException(
            status_code=503, detail=f"Browser {browser_id} temporarily unavailable: {e}"
        )

    try:
        try:
            page = await client.attach_to_page(page_id)
        except PageNotFoundError:
            raise HTTPException(status_code=404, detail=f"Page {page_id} not found in browser")
        except Exception as e:
            logger.error(f"Failed to attach to {browser_id}/{page_id}: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to navigate page: {e}")

        try:
            await page.navigate(target_url)
        except Exception as e:
            logger.error(f"Error navigating page for {browser_id}/{page_id}: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to navigate page: {e}")

        return JSONResponse({"status": "success"})
    finally:
        await client.aclose()
