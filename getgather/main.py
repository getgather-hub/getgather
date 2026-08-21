import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
)
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from loguru import logger

from getgather.browser import create_remote_browser, terminate_remote_browser
from getgather.browsers.router import backend, router as browsers_router
from getgather.config import PROJECT_DIR, settings
from getgather.logs import LoggingContextMiddleware
from getgather.pages_api_router import router as pages_router
from getgather.tracing import SessionTraceMiddleware, instrument_fastapi

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

    background_task = asyncio.create_task(timer_loop())

    try:
        yield

        stop_event.set()
        await background_task
    finally:
        await backend.shutdown()


app = FastAPI(
    title="Get Gather",
    description="GetGather frontend and api",
    version="0.1.1",
    generate_unique_id_function=custom_generate_unique_id,
    lifespan=lifespan,
)
app.add_middleware(LoggingContextMiddleware)
instrument_fastapi(app)


@app.get("/health")
def health():
    return PlainTextResponse(
        content=f"OK {int(datetime.now().timestamp())} GIT_REV: {settings.GIT_REV}"
    )


@app.get("/extended-health")
async def extended_health():
    # A fresh ephemeral browser per probe, terminated when done
    try:
        browser = await create_remote_browser()
    except Exception as e:
        return PlainTextResponse(content=f"Error: {e}")
    try:
        return PlainTextResponse(content="OK")
    finally:
        await terminate_remote_browser(browser)


app.include_router(browsers_router)
app.include_router(pages_router)


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


# Wrap the entire instrumented app so session-id handling runs BEFORE
# OTel's FastAPI instrumentation. When the caller has its own trace context,
# we leave it alone; otherwise we parent the request under a session trace.
app = SessionTraceMiddleware(app)  # type: ignore[assignment]
