"""Pre-sign a PUT for one session recording and have the fleet push it to object storage."""

import asyncio
from functools import lru_cache
from typing import Any, cast

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger
from mypy_boto3_s3.client import S3Client

from getgather.browsers.fleet_browsers import call_chromefleet_api
from getgather.config import settings

CONTENT_TYPE = "video/mp4"

# The uploader retries 3x with a 600s per-attempt timeout, so the URL has to outlive roughly
# 30 minutes of a pathological transfer.
_URL_EXPIRY_SECONDS = 3600

# ffmpeg encodes after the tab is gone, and a recording can run to the container's 10-minute
# cap, so the entry appears some seconds late.
_POLL_INTERVAL_SECONDS = 2.0
_POLL_ATTEMPTS = 30

_pending: set[asyncio.Task[None]] = set()


@lru_cache(maxsize=1)
def _s3_client() -> S3Client:
    return boto3.client(  # pyright: ignore[reportUnknownMemberType]
        "s3",
        endpoint_url=settings.TIGRIS_ENDPOINT_URL,
        aws_access_key_id=settings.TIGRIS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.TIGRIS_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def storage_key(browser_id: str, recording_id: str) -> str:
    return f"{browser_id}/{recording_id}.mp4"


def _presign_put(key: str) -> str:
    return _s3_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.TIGRIS_BUCKET, "Key": key, "ContentType": CONTENT_TYPE},
        ExpiresIn=_URL_EXPIRY_SECONDS,
    )


async def presign_put(key: str) -> str:
    """Sign a PUT for `key`. Off the event loop: botocore is sync, though signing is local HMAC."""
    return await asyncio.to_thread(_presign_put, key)


def split_namespaced_target_id(namespaced: str) -> tuple[str, str] | None:
    """Split a `{browser_id}@{target_id}` CDP id, or None if it carries no browser namespace."""
    browser_id, _, target_id = namespaced.partition("@")
    if not target_id or not browser_id:
        return None
    return browser_id, target_id


async def list_recordings(browser_id: str) -> list[dict[str, Any]]:
    """Recordings finalized on this browser, newest first, via flyfleet's browser-trace proxy."""
    response = await call_chromefleet_api(
        "GET",
        browser_id,
        path_suffix="/trace/recordings",
        timeout=30.0,
        # Explicit so the fleet client does not try to read the inbound request's headers: this
        # runs in a background task, with no request in scope.
        headers={},
    )
    if response is None:
        return []
    recordings = cast(dict[str, Any], response.json()).get("recordings")
    if not isinstance(recordings, list):
        return []
    return [item for item in cast(list[Any], recordings) if isinstance(item, dict)]


async def _wait_for_recording(browser_id: str, target_id: str) -> dict[str, Any] | None:
    """Poll until this tab's recording is listed, or give up. Returns its metadata entry."""
    for _ in range(_POLL_ATTEMPTS):
        for recording in await list_recordings(browser_id):
            if recording.get("target_id") == target_id:
                return recording
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return None


async def upload_recording(browser_id: str, recording_id: str) -> str:
    """Sign a PUT and have the fleet stream that recording at it. Returns the storage key."""
    key = storage_key(browser_id, recording_id)
    upload_url = await presign_put(key)
    await call_chromefleet_api(
        "POST",
        browser_id,
        path_suffix=f"/recordings/{recording_id}/upload",
        json={"upload_url": upload_url},
        # Bounded by the container's own upload retries (3 attempts, 600s each).
        timeout=1900.0,
        # The exec is not idempotent — a retry would re-PUT the whole file.
        retries=0,
        headers={},
    )
    return key


async def _upload_closed_tab_recording(browser_id: str, target_id: str) -> None:
    recording = await _wait_for_recording(browser_id, target_id)
    if recording is None:
        logger.warning(
            "Recording never finalized for closed tab",
            browser_id=browser_id,
            target_id=target_id,
            waited_seconds=_POLL_ATTEMPTS * _POLL_INTERVAL_SECONDS,
        )
        return

    recording_id = recording.get("recording_id")
    if not isinstance(recording_id, str) or not recording_id:
        logger.warning(
            "Recording listed without an id",
            browser_id=browser_id,
            target_id=target_id,
            recording=recording,
        )
        return

    recordings = await list_recordings(browser_id)
    newest_recording_id = recordings[0].get("recording_id") if recordings else None

    key = await upload_recording(browser_id, recording_id)
    logger.info(
        "Recording uploaded",
        browser_id=browser_id,
        target_id=target_id,
        recording_id=recording_id,
        newest_recording_id=newest_recording_id,
        key=key,
        size_bytes=recording.get("size_bytes"),
        video_seconds=recording.get("video_seconds"),
    )


async def _upload_or_log(browser_id: str, target_id: str) -> None:
    try:
        await _upload_closed_tab_recording(browser_id, target_id)
    except (BotoCoreError, ClientError) as e:
        logger.warning(f"Failed to sign recording upload for {browser_id}: {e}")
    except Exception as e:
        logger.warning(f"Failed to upload recording for {browser_id}: {e}")


def schedule_upload_for_closed_tab(namespaced_target_id: str) -> None:
    """Kick off the upload for a tab that just closed."""
    if not settings.recording_upload_enabled:
        return

    split = split_namespaced_target_id(namespaced_target_id)
    if split is None:
        logger.debug(f"No browser namespace on target id {namespaced_target_id!r}, skipping upload")
        return
    browser_id, target_id = split

    try:
        task = asyncio.create_task(_upload_or_log(browser_id, target_id))
    except RuntimeError:
        # No running loop (sync caller / interpreter shutdown). Nothing to schedule onto.
        logger.debug("No running event loop, skipping recording upload")
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)
