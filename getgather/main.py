import ast
import asyncio
import os
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    Response,
)
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from loguru import logger

from getgather.browser import (
    create_remote_browser,
    reap_idle_browsers_if_enabled,
    terminate_remote_browser,
)
from getgather.browsers.router import backend, router as browsers_router
from getgather.config import PROJECT_DIR, settings
from getgather.logs import MCPLoggingContextMiddleware
from getgather.mcp.dpage import router as dpage_router
from getgather.mcp.main import MCPDoc, create_mcp_apps, mcp_app_docs
from getgather.pages_api_router import router as pages_router
from getgather.patterns_api_router import router as patterns_router
from getgather.tracing import MCPSessionTraceMiddleware, instrument_fastapi
from getgather.zen_distill import load_distillation_patterns, run_distillation_loop

# Create MCP apps once and reuse for lifespan and mounting
mcp_apps = create_mcp_apps()

BACKGROUND_TASK_INTERVAL = 5 * 60  # seconds


def custom_generate_unique_id(route: APIRoute) -> str:
    tag = route.tags[0] if route.tags else "no-tag"
    return f"{tag}-{route.name}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.CHROMEFLEET_URL:
        logger.warning("CHROMEFLEET_URL is not set, falling back to local browsers")

    stop_event = asyncio.Event()

    async def timer_loop():
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=BACKGROUND_TASK_INTERVAL)
            except asyncio.TimeoutError:
                try:
                    await backend.cleanup_idle()
                except Exception as e:
                    logger.error(f"Idle cleanup failed: {e}")
                try:
                    # Backstop for locally-attached browsers whose owner never called
                    # DELETE (a stranded sync). Separate try: a backend cleanup failure
                    # must not skip the reaper, since the reaper is what prevents the
                    # socket leak from wedging the machine.
                    await reap_idle_browsers_if_enabled()
                except Exception as e:
                    logger.error(f"Browser reaper failed: {e}")

    background_task = asyncio.create_task(timer_loop())

    try:
        async with AsyncExitStack() as stack:
            for mcp_app in mcp_apps:
                await stack.enter_async_context(mcp_app.app.lifespan(app))
            yield

            stop_event.set()
            await background_task
    finally:
        await backend.shutdown()


app = FastAPI(
    title="Get Gather",
    description="GetGather mcp, frontend, and api",
    version="0.1.1",
    generate_unique_id_function=custom_generate_unique_id,
    lifespan=lifespan,
)
app.add_middleware(MCPLoggingContextMiddleware)
instrument_fastapi(app)


@app.get("/health")
def health():
    return PlainTextResponse(
        content=f"OK {int(datetime.now().timestamp())} GIT_REV: {settings.GIT_REV}"
    )


@app.get("/extended-health")
async def extended_health():
    # A fresh ephemeral browser per probe, terminated when done
    browser = await create_remote_browser()
    try:
        patterns = load_distillation_patterns(
            os.path.join(os.path.dirname(__file__), "mcp", "patterns", "ip-fly.html")
        )
        terminated, distilled, converted = await run_distillation_loop(
            location="https://ip.fly.dev/ip",
            patterns=patterns,
            browser=browser,
            timeout=3,
        )
        if not terminated:
            raise ValueError("Distillation did not terminate for ip.fly.dev")
        payload = converted if converted is not None else distilled
        ip_text = str(payload)[:100]
        ip_list = ast.literal_eval(ip_text)
        ip_address = ip_list[0]["ip_address"]
        logger.debug(f"IP address: {ip_address}")
        return PlainTextResponse(content=f"OK IP: {ip_address}")
    except Exception as e:
        return PlainTextResponse(content=f"Error: {e}")
    finally:
        await terminate_remote_browser(browser)


@app.middleware("http")
async def mcp_slash_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
):
    # Make /mcp* and /mcp*/ behave the same without an actual redirect.
    path = request.url.path
    if path.startswith("/mcp") and not path.endswith("/"):
        request.scope["path"] = f"{path}/"
        if request.scope.get("raw_path"):
            request.scope["raw_path"] = f"{path}/".encode()
    return await call_next(request)


# Mount routers and apps AFTER middleware
app.include_router(browsers_router)
app.include_router(pages_router)
app.include_router(patterns_router)
app.include_router(dpage_router)

for mcp_app in mcp_apps:
    app.mount(mcp_app.route, mcp_app.app)


@app.get("/docs-mcp")
async def mcp_docs() -> list[MCPDoc]:
    return await asyncio.gather(*[mcp_app_docs(mcp_app) for mcp_app in create_mcp_apps()])


if settings.CHROMEFLEET_URL:

    @app.get("/")
    def homepage():
        html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GetGather</title>
<style>html,body{{margin:0;padding:0;height:100%;overflow:hidden}}iframe{{border:none;width:100%;height:100%}}</style>
</head>
<body><iframe src="{settings.CHROMEFLEET_URL}"></iframe></body>
</html>"""
        return HTMLResponse(content=html)

else:
    app.mount(
        "/",
        StaticFiles(directory=str(PROJECT_DIR / "getgather" / "webui"), html=True),
        name="webui",
    )


# Wrap the entire instrumented app so mcp-session-id handling runs BEFORE
# OTel's FastAPI instrumentation. When the caller has its own trace context,
# we leave it alone; otherwise we parent the request under a session trace.
app = MCPSessionTraceMiddleware(app)  # type: ignore[assignment]
