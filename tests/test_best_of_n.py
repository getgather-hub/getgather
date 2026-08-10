import asyncio
from typing import Any

import pytest
from pytest import MonkeyPatch

from getgather.browsers import backend as backend_module
from getgather.browsers.backend import (
    ProxyVerificationError,
    _cleanup_losers,  # pyright: ignore[reportPrivateUsage]
    best_of_n,
)


class _FakeBackend:
    """A minimal in-memory backend for exercising the generic best-of-N helper.

    `create_browser` resolves with the configured delay/exception per id; `browser_exists` /
    `delete_browser` track which losers get torn down so the cleanup path can be asserted."""

    def __init__(self) -> None:
        self.create_outcomes: dict[str, Exception | dict[str, Any]] = {}
        self.delays: dict[str, float] = {}
        self.deleted: list[str] = []
        self.existing: set[str] = set()

    async def create_browser(
        self,
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> dict[str, Any]:
        delay = self.delays.get(browser_id, 0.0)
        if delay:
            await asyncio.sleep(delay)
        outcome = self.create_outcomes.get(browser_id)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            outcome = {"id": browser_id}
        self.existing.add(browser_id)
        return outcome

    async def browser_exists(self, browser_id: str) -> bool:
        return browser_id in self.existing

    async def delete_browser(self, browser_id: str) -> dict[str, Any]:
        self.existing.discard(browser_id)
        self.deleted.append(browser_id)
        return {"status": "deleted"}


def _make_id_factory(ids: list[str]) -> Any:
    """Return a () -> str closure yielding the given ids in order (for monkeypatching new_browser_id)."""
    it = iter(ids)

    def _factory() -> str:
        return next(it)

    return _factory


def _patch_ids(monkeypatch: MonkeyPatch, ids: list[str]) -> None:
    monkeypatch.setattr(backend_module, "new_browser_id", _make_id_factory(ids))


@pytest.mark.asyncio
async def test_best_of_n_picks_first_to_complete(monkeypatch: MonkeyPatch) -> None:
    _patch_ids(monkeypatch, ["b0", "b1", "b2"])
    backend = _FakeBackend()
    # b1 finishes fastest but its create raises (proxy/launch failure); b0 completes next -> b0 wins.
    backend.delays = {"b0": 0.02, "b1": 0.01, "b2": 0.05}
    backend.create_outcomes = {"b1": ProxyVerificationError("proxy unchanged")}

    winner_id, info = await best_of_n(backend, 3, None, None, None)
    await asyncio.sleep(0)  # let the fire-and-forget cleanup task schedule

    assert winner_id == "b0"
    assert info == {"id": "b0"}


@pytest.mark.asyncio
async def test_best_of_n_raises_when_all_candidates_fail(monkeypatch: MonkeyPatch) -> None:
    _patch_ids(monkeypatch, ["b0", "b1"])
    backend = _FakeBackend()
    backend.create_outcomes = {
        "b0": ProxyVerificationError("IP unchanged after proxy"),
        "b1": RuntimeError("boom"),
    }

    with pytest.raises(ProxyVerificationError, match="no browser candidate started"):
        await best_of_n(backend, 2, None, None, None)


@pytest.mark.asyncio
async def test_best_of_n_winner_is_not_deleted(monkeypatch: MonkeyPatch) -> None:
    # The winner is authoritative and must never be torn down by the background loser cleanup.
    _patch_ids(monkeypatch, ["w", "l1", "l2"])
    backend = _FakeBackend()
    backend.delays = {"w": 0.01, "l1": 0.05, "l2": 0.05}

    winner_id, _ = await best_of_n(backend, 3, None, None, None)
    assert winner_id == "w"
    assert "w" not in backend.deleted


@pytest.mark.asyncio
async def test_cleanup_losers_deletes_existing_losers() -> None:
    # Losers that DID materialize (create completed but lost the race) are present and must be
    # deleted; the winner is left alone.
    backend = _FakeBackend()
    backend.existing = {"w", "l1", "l2"}

    await _cleanup_losers(backend, ["w", "l1", "l2"], winner_id="w")

    assert backend.deleted == ["l1", "l2"]
    assert "w" in backend.existing


class _TransientBackend:
    """Mimics Daytona's transient-delete behavior: a loser may not be visible yet (still
    materializing) on the first check, then appears and deletes cleanly. Verifies the retry loop
    waits for materialization instead of giving up immediately."""

    def __init__(self, *, appears_after: dict[str, int]) -> None:
        self.deleted: list[str] = []
        self._appears_after = appears_after
        self._checks: dict[str, int] = {}

    async def browser_exists(self, browser_id: str) -> bool:
        n = self._checks.get(browser_id, 0) + 1
        self._checks[browser_id] = n
        return n >= self._appears_after.get(browser_id, 1)

    async def delete_browser(self, browser_id: str) -> dict[str, Any]:
        self.deleted.append(browser_id)
        return {"status": "deleted"}


async def _noop_sleep(_delay: float) -> None:
    return None


@pytest.mark.asyncio
async def test_cleanup_losers_waits_for_materialization(monkeypatch: MonkeyPatch) -> None:
    # l1 appears on the 3rd exists check; cleanup must keep retrying (not give up) until then.
    monkeypatch.setattr(backend_module.asyncio, "sleep", _noop_sleep)
    backend = _TransientBackend(appears_after={"l1": 3})

    await _cleanup_losers(backend, ["w", "l1"], winner_id="w")

    assert backend.deleted == ["l1"]


@pytest.mark.asyncio
async def test_best_of_n_passes_origin_ip_target_domain_and_browser_type(
    monkeypatch: MonkeyPatch,
) -> None:
    _patch_ids(monkeypatch, ["b0", "b1"])
    seen: list[tuple[str, str | None, str | None, str | None]] = []

    class _Tracking(_FakeBackend):
        async def create_browser(
            self,
            browser_id: str,
            origin_ip: str | None,
            target_domain: str | None,
            browser_type: str | None,
        ) -> dict[str, Any]:
            seen.append((browser_id, origin_ip, target_domain, browser_type))
            self.existing.add(browser_id)
            return {"id": browser_id}

    await best_of_n(_Tracking(), 2, "1.2.3.4", "amazon.com", "cloak")
    assert set(seen) == {
        ("b0", "1.2.3.4", "amazon.com", "cloak"),
        ("b1", "1.2.3.4", "amazon.com", "cloak"),
    }
