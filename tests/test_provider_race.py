import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from getgather.browsers.backend import BROWSER_SCOPE, BrowserNotFound
from getgather.browsers.provider_race import ProviderRaceBackend


class _FakeBackend:
    def __init__(
        self,
        *,
        create_delay: float = 0,
        provider_browser_id: str | None = None,
        remote_url: str = "wss://provider.invalid/cdp",
        namespacing: bool = True,
        create_error: Exception | None = None,
        readiness_error: Exception | None = None,
    ) -> None:
        self.create_delay = create_delay
        self.provider_browser_id = provider_browser_id
        self.remote_url = remote_url
        self.namespacing = namespacing
        self.create_error = create_error
        self.readiness_error = readiness_error
        self.created: set[str] = set()
        self.deleted: list[str] = []
        self.readiness_checks: list[str] = []

    @property
    def default_best_of_n(self) -> int:
        return 1

    async def shutdown(self) -> None:
        return None

    async def create_browser(
        self,
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> dict[str, Any]:
        del origin_ip, target_domain, browser_type
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        if self.create_error:
            raise self.create_error
        actual_id = self.provider_browser_id or browser_id
        self.created.add(actual_id)
        return {"browser_id": actual_id, "provider_secret": self.remote_url}

    async def get_browser(
        self, browser_id: str, origin_ip: str | None, target_domain: str | None
    ) -> dict[str, Any]:
        del origin_ip, target_domain
        if browser_id not in self.created:
            raise BrowserNotFound(browser_id)
        return {"browser_id": browser_id}

    async def delete_browser(self, browser_id: str) -> dict[str, Any]:
        self.created.discard(browser_id)
        self.deleted.append(browser_id)
        return {"status": "deleted"}

    async def list_browser_ids(self, scope: BROWSER_SCOPE = "all") -> list[str]:
        del scope
        return list(self.created)

    async def browser_exists(self, browser_id: str) -> bool:
        return browser_id in self.created

    async def cleanup_idle(self) -> list[str]:
        return []

    async def get_cdp_base_url(self, browser_id: str) -> str:
        return f"https://provider.invalid/{browser_id}"

    def cdp_websocket_base(self) -> None:
        return None

    async def get_cdp_websocket_remote_url(self, browser_id: str) -> str | None:
        if browser_id not in self.created:
            return None
        return f"{self.remote_url}/{browser_id}"

    async def wait_until_cdp_ready(self, browser_id: str) -> None:
        self.readiness_checks.append(browser_id)
        if self.readiness_error is not None:
            raise self.readiness_error
        assert await self.get_cdp_websocket_remote_url(browser_id)

    def cdp_targets_need_namespacing(self, browser_id: str | None = None) -> bool:
        del browser_id
        return self.namespacing

    async def get_devtools_websocket_remote_url(
        self, client_ws: WebSocket, browser_id: str, page_id: str
    ) -> str | None:
        del client_ws
        return f"{self.remote_url}/{browser_id}/page/{page_id}"

    async def get_vnc_endpoint(self, browser_id: str) -> tuple[str, int] | None:
        del browser_id
        return None

    async def get_live_view_url(self, browser_id: str) -> str | None:
        del browser_id
        return None


@pytest.mark.asyncio
async def test_provider_race_routes_public_id_to_fastest_ready_provider() -> None:
    slow = _FakeBackend(create_delay=0.02, remote_url="wss://slow.invalid", namespacing=False)
    fast = _FakeBackend(remote_url="wss://fast.invalid")
    race = ProviderRaceBackend(slow, {"fly": slow, "daytona": fast})

    public_id = await race.create_raced_browser("Bpuxvstp6", "1.2.3.4", "example.com", "chrome")

    assert public_id == "Bduxvstp6"
    assert await race.get_cdp_websocket_remote_url(public_id) == ("wss://fast.invalid/Bduxvstp6")
    assert race.cdp_targets_need_namespacing(public_id) is True
    assert fast.readiness_checks == ["Bduxvstp6"]
    assert await race.get_browser(public_id, None, None) == {
        "browser_id": public_id,
        "status": "created",
    }

    await race.shutdown()
    assert slow.deleted == ["Bfuxvstp6"]
    assert fast.deleted == []


@pytest.mark.asyncio
async def test_provider_race_cleans_failed_candidates() -> None:
    first = _FakeBackend(create_error=RuntimeError("create failed"))
    second = _FakeBackend(readiness_error=RuntimeError("CDP failed"))
    race = ProviderRaceBackend(first, {"fly": first, "daytona": second})

    with pytest.raises(RuntimeError, match="No browser provider became CDP-ready"):
        await race.create_raced_browser("Bpuxvstp6", None, None, None)

    assert first.deleted == ["Bfuxvstp6"]
    assert second.deleted == ["Bduxvstp6"]


@pytest.mark.asyncio
async def test_deleted_winner_does_not_fall_through_to_fallback() -> None:
    fallback = _FakeBackend(
        remote_url="wss://fallback.invalid", create_error=RuntimeError("fallback used")
    )
    winner = _FakeBackend(remote_url="wss://winner.invalid")
    race = ProviderRaceBackend(fallback, {"fly": fallback, "daytona": winner})

    public_id = await race.create_raced_browser("Bpuxvstp6", None, None, None)

    assert await race.delete_browser(public_id) == {
        "browser_id": public_id,
        "status": "deleted",
    }
    assert await race.browser_exists(public_id) is False
    with pytest.raises(BrowserNotFound):
        await race.get_browser(public_id, None, None)
    assert public_id not in await race.list_browser_ids()
    assert fallback.created == set()

    # Existing CDP auto-start semantics are retained, but the browser stays on its winning provider.
    result = await race.create_browser(public_id, None, None, None)
    assert result["browser_id"] == "Bduxvstp6"
    assert await race.browser_exists(public_id) is True
    assert fallback.created == set()

    await race.shutdown()


def test_provider_race_create_response_preserves_contract(monkeypatch: MonkeyPatch) -> None:
    from getgather.browsers import router as router_module

    fallback = _FakeBackend()
    race = ProviderRaceBackend(fallback, {"fly": fallback, "daytona": _FakeBackend()})

    async def fake_create_raced_browser(
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> str:
        del browser_id, origin_ip, target_domain, browser_type
        return "Bduxvstp6"

    monkeypatch.setattr(race, "create_raced_browser", fake_create_raced_browser)
    monkeypatch.setattr(router_module, "backend", race)
    monkeypatch.setattr(router_module, "new_browser_id", lambda: "public-id")

    app = FastAPI()
    app.include_router(router_module.router)
    response = TestClient(app).post("/api/v1/browsers")

    assert response.status_code == 200
    assert response.json() == {
        "browser_id": "Bduxvstp6",
        "status": "created",
    }
    assert "provider" not in response.text


@pytest.mark.asyncio
async def test_provider_route_survives_process_boundary() -> None:
    first_backend = _FakeBackend()
    first = ProviderRaceBackend(
        first_backend,
        {"daytona": first_backend, "fly": _FakeBackend(create_delay=0.02)},
    )

    public_id = await first.create_raced_browser("Bpuxvstp6", None, None, None)

    second_backend = _FakeBackend()
    second_backend.created.add("Bduxvstp6")
    second_fallback = _FakeBackend()
    second = ProviderRaceBackend(
        second_fallback,
        {"daytona": second_backend, "fly": second_fallback},
    )

    assert await second.get_browser(public_id, None, None) == {
        "browser_id": public_id,
        "status": "created",
    }
    assert await second.get_cdp_websocket_remote_url(public_id) == (
        "wss://provider.invalid/cdp/Bduxvstp6"
    )
    assert public_id in await second.list_browser_ids()
    assert await second.delete_browser(public_id) == {
        "browser_id": public_id,
        "status": "deleted",
    }
    first_backend.created.clear()
    assert await first.browser_exists(public_id) is False

    await first.shutdown()
    await second.shutdown()
