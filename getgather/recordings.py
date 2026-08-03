"""Pull browser session recordings from Chrome Fleet and store them in Tigris.

chrome-live records every tab and finalizes the MP4 only after the tab closes and ffmpeg has
encoded it, so a sweep keeps polling the fleet for a while after a tool closes its page. The
container's disk is ephemeral, so anything not pulled before the browser is deleted is lost.
"""

import asyncio
import json
from functools import lru_cache
from typing import Any

import boto3
import httpx
from botocore.client import Config
from botocore.exceptions import ClientError
from loguru import logger

from getgather.config import settings

LIST_TIMEOUT = 15.0
DOWNLOAD_TIMEOUT = 120.0
S3_TIMEOUT = 120.0

_sweeps: dict[str, asyncio.Task[None]] = {}

# boto3/botocore ship no type information, so everything crossing that boundary is Any.
_boto3: Any = boto3
_config: Any = Config


def schedule_sweep(browser_id: str) -> None:
    """Start a background upload sweep for a browser, unless one is already running."""
    if not settings.RECORDING_UPLOAD_ENABLED:
        return

    running = _sweeps.get(browser_id)
    if running is not None and not running.done():
        return

    task = asyncio.create_task(_sweep(browser_id))
    _sweeps[browser_id] = task

    def _forget(finished: asyncio.Task[None]) -> None:
        if _sweeps.get(browser_id) is finished:
            del _sweeps[browser_id]

    task.add_done_callback(_forget)


async def _sweep(browser_id: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.RECORDING_POLL_TIMEOUT_SECONDS
    uploaded: set[str] = set()

    while True:
        try:
            recordings = await _list_recordings(browser_id)
        except Exception as e:
            logger.warning(f"Could not list recordings for browser {browser_id}: {e}")
            recordings = []

        fresh = [r for r in recordings if r["recording_id"] not in uploaded]
        for meta in fresh:
            recording_id: str = meta["recording_id"]
            try:
                await _store_recording(browser_id, recording_id, meta)
                uploaded.add(recording_id)
            except Exception as e:
                logger.warning(f"Failed to store recording {recording_id}: {e}")

        if loop.time() >= deadline:
            break
        # Once a pass adds nothing new, every tab that was open has flushed.
        if uploaded and not fresh:
            break
        await asyncio.sleep(settings.RECORDING_POLL_INTERVAL_SECONDS)

    logger.info(f"Stored {len(uploaded)} recording(s) for browser {browser_id}")


async def _list_recordings(browser_id: str) -> list[dict[str, Any]]:
    base_url = settings.effective_chromefleet_url.rstrip("/")
    url = f"{base_url}/api/v1/browsers/{browser_id}/recordings"
    async with httpx.AsyncClient(timeout=LIST_TIMEOUT) as client:
        response = await client.get(url)
        # The browser is gone, or its image has no recordings server.
        if response.status_code in (404, 501):
            return []
        response.raise_for_status()
        payload: dict[str, Any] = response.json()

    recordings: list[dict[str, Any]] = payload.get("recordings", [])
    return [r for r in recordings if r.get("recording_id")]


async def _store_recording(browser_id: str, recording_id: str, meta: dict[str, Any]) -> None:
    prefix = f"recordings/{browser_id}/{recording_id}"
    if await _object_exists(f"{prefix}.mp4"):
        return

    base_url = settings.effective_chromefleet_url.rstrip("/")
    url = f"{base_url}/api/v1/browsers/{browser_id}/recordings/{recording_id}/video"
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT) as client:
        response = await client.get(url)
        response.raise_for_status()
        video = response.content

    await _put_object(f"{prefix}.mp4", video, "video/mp4")
    await _put_object(f"{prefix}.json", json.dumps(meta).encode(), "application/json")
    logger.info(f"Stored recording {recording_id} ({len(video)} bytes) at {prefix}.mp4")


@lru_cache(maxsize=1)
def _client() -> Any:
    """Shared S3 client. Call only from the event loop thread: botocore client construction is
    not thread-safe, while calls on a built client are."""
    return _boto3.client(
        "s3",
        endpoint_url=settings.TIGRIS_ENDPOINT_URL,
        region_name=settings.TIGRIS_REGION,
        aws_access_key_id=settings.TIGRIS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.TIGRIS_SECRET_ACCESS_KEY,
        # Tigris only serves virtual-hosted-style addressing (bucket.t3.storage.dev).
        config=_config(
            s3={"addressing_style": "virtual"},
            connect_timeout=S3_TIMEOUT,
            read_timeout=S3_TIMEOUT,
        ),
    )


async def _object_exists(key: str) -> bool:
    client = _client()

    def head() -> bool:
        try:
            client.head_object(Bucket=settings.TIGRIS_BUCKET, Key=key)
        except ClientError as e:
            response: Any = e.response
            if response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise
        return True

    return await asyncio.to_thread(head)


async def _put_object(key: str, body: bytes, content_type: str) -> None:
    client = _client()

    def put() -> None:
        client.put_object(
            Bucket=settings.TIGRIS_BUCKET, Key=key, Body=body, ContentType=content_type
        )

    await asyncio.to_thread(put)
