import asyncio

import pytest
from pytest import MonkeyPatch

from getgather.browsers import backend as backend_mod
from getgather.browsers.backend import mark_browser_deleting, was_browser_recently_deleted


@pytest.mark.asyncio
async def test_unknown_browser_is_not_tombstoned() -> None:
    assert not was_browser_recently_deleted("Bneverseen")


@pytest.mark.asyncio
async def test_delete_in_flight_blocks_relaunch() -> None:
    """The observed leak: a CDP connect arriving while a delete was still in flight saw
    `browser_exists() == False` and re-created the sandbox under the same id."""
    exists = True

    async def delete_browser(browser_id: str) -> None:
        nonlocal exists
        mark_browser_deleting(browser_id)
        exists = False  # the backend drops it from `browser_exists` here...
        await asyncio.sleep(0.05)  # ...but the delete call has not returned yet

    task = asyncio.create_task(delete_browser("Binflight"))
    await asyncio.sleep(0.01)

    assert exists is False, "precondition: browser is already invisible mid-delete"
    assert was_browser_recently_deleted("Binflight"), "auto-launch would resurrect it"
    await task
    assert was_browser_recently_deleted("Binflight")


@pytest.mark.asyncio
async def test_tombstone_expires(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(backend_mod, "DELETE_TOMBSTONE_SECONDS", 0.01)
    mark_browser_deleting("Bexpire")
    assert was_browser_recently_deleted("Bexpire")
    await asyncio.sleep(0.02)
    assert not was_browser_recently_deleted("Bexpire")
