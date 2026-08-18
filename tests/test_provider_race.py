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
    ) -> None:
        self.create_delay = create_delay
        self.provider_browser_id = provider_browser_id
        self.remote_url = remote_url
        self.namespacing = namespacing
        self.create_error = create_error
        self.created: set[str] = set()
        self.deleted: list[str] = []

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
async def test_provider_race_routes_public_id_to_fastest_ready_provider(
    monkeypatch: MonkeyPatch,
) -> None:
    slow = _FakeBackend(create_delay=0.02, remote_url="wss://slow.invalid", namespacing=False)
    fast = _FakeBackend(
        provider_browser_id="provider-assigned-secret-id",
        remote_url="wss://fast.invalid",
    )
    race = ProviderRaceBackend(slow, {"slow": slow, "fast": fast})

    async def ready(self: Any, provider_backend: _FakeBackend, browser_id: str) -> None:
        assert await provider_backend.get_cdp_websocket_remote_url(browser_id)

    monkeypatch.setattr(ProviderRaceBackend, "_wait_until_cdp_ready", ready)

    await race.create_raced_browser("public-id", "1.2.3.4", "example.com", "chrome")

    assert await race.get_cdp_websocket_remote_url("public-id") == (
        "wss://fast.invalid/provider-assigned-secret-id"
    )
    assert race.cdp_targets_need_namespacing("public-id") is True
    assert await race.get_browser("public-id", None, None) == {
        "browser_id": "public-id",
        "status": "created",
    }

    await race.shutdown()
    assert slow.deleted == ["public-id"]
    assert fast.deleted == []


@pytest.mark.asyncio
async def test_provider_race_cleans_failed_candidates(monkeypatch: MonkeyPatch) -> None:
    first = _FakeBackend(create_error=RuntimeError("create failed"))
    second = _FakeBackend()
    race = ProviderRaceBackend(first, {"first": first, "second": second})

    async def never_ready(self: Any, provider_backend: _FakeBackend, browser_id: str) -> None:
        del provider_backend, browser_id
        raise RuntimeError("CDP failed")

    monkeypatch.setattr(ProviderRaceBackend, "_wait_until_cdp_ready", never_ready)

    with pytest.raises(RuntimeError, match="No browser provider became CDP-ready"):
        await race.create_raced_browser("public-id", None, None, None)

    assert first.deleted == ["public-id"]
    assert second.deleted == ["public-id"]


def test_provider_race_create_response_contains_only_proxy_url(monkeypatch: MonkeyPatch) -> None:
    from getgather.browsers import router as router_module

    fallback = _FakeBackend()
    race = ProviderRaceBackend(fallback, {"one": fallback, "two": _FakeBackend()})

    async def fake_create_raced_browser(
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> None:
        del browser_id, origin_ip, target_domain, browser_type

    monkeypatch.setattr(race, "create_raced_browser", fake_create_raced_browser)
    monkeypatch.setattr(router_module, "backend", race)
    monkeypatch.setattr(router_module, "new_browser_id", lambda: "public-id")

    app = FastAPI()
    app.include_router(router_module.router)
    response = TestClient(app).post("/api/v1/browsers")

    assert response.status_code == 200
    assert response.json() == {
        "browser_id": "public-id",
        "cdp_url": "ws://testserver/cdp/public-id",
        "status": "created",
    }
    assert "provider" not in response.text
