import asyncio
import json
from typing import Any

import pytest
import websockets
from pytest import MonkeyPatch

from getgather.browser import _setup_cdp_url  # pyright: ignore[reportPrivateUsage]
from getgather.browsers import router as browsers_router
from getgather.browsers.backend import create_backend, wait_for_cdp_ready
from getgather.browsers.fleet_browsers import FleetBackend
from getgather.browsers.podman_browsers import PodmanBackend
from getgather.config import settings


def test_create_backend_selects_fleet_when_url_set(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CHROMEFLEET_URL", "http://localhost:8300")
    assert isinstance(create_backend(), FleetBackend)


def test_create_backend_defaults_to_podman_without_url(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CHROMEFLEET_URL", "")
    monkeypatch.setattr(settings, "BROWSER_BACKEND", "podman")
    assert isinstance(create_backend(), PodmanBackend)


def test_fleet_cdp_websocket_base_rewrites_scheme(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CHROMEFLEET_URL", "http://localhost:8300/")
    assert FleetBackend().cdp_websocket_base() == "ws://localhost:8300"

    monkeypatch.setattr(settings, "CHROMEFLEET_URL", "https://fleet.example.com")
    assert FleetBackend().cdp_websocket_base() == "wss://fleet.example.com"


def test_fleet_relay_url_matches_internal_cdp_url(monkeypatch: MonkeyPatch) -> None:
    # The /cdp relay target the router builds (cdp_websocket_base + "/cdp/{id}") must match the
    # URL getgather's own zendriver client connects to, so external and internal CDP agree.
    monkeypatch.setattr(settings, "CHROMEFLEET_URL", "http://localhost:8300")
    base = FleetBackend().cdp_websocket_base()
    assert f"{base}/cdp/abc123" == _setup_cdp_url("abc123")


def test_local_backend_opts_out_of_relay() -> None:
    # None signals the router to use the per-browser /json/version flow instead of a relay.
    assert PodmanBackend().cdp_websocket_base() is None


@pytest.mark.asyncio
async def test_wait_for_cdp_ready_sends_real_cdp_command(monkeypatch: MonkeyPatch) -> None:
    from getgather import cdp_client

    commands: list[str] = []
    closed = False

    class _ReadyClient:
        async def send(self, method: str) -> dict[str, Any]:
            commands.append(method)
            return {"targetInfos": []}

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    async def open_ready_client(url: str, timeout: float | None = None) -> _ReadyClient:
        assert url == "wss://provider.invalid/cdp"
        assert timeout == 10.0
        return _ReadyClient()

    async def resolve_url() -> str:
        return "wss://provider.invalid/cdp"

    monkeypatch.setattr(cdp_client, "open_cdp_url", open_ready_client)

    await wait_for_cdp_ready("browser-id", resolve_url)

    assert commands == ["Target.getTargets"]
    assert closed is True


def test_create_browser_auto_name_starts_with_b(monkeypatch: MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def fake_create_browser(
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ):
        return {
            "container_name": f"chromium-{browser_id}",
            "status": "created",
            "ip": "1.2.3.4",
        }

    monkeypatch.setattr(settings, "BROWSER_BEST_OF_N", 1)
    monkeypatch.setattr(browsers_router.backend, "create_browser", fake_create_browser)

    app = FastAPI()
    app.include_router(browsers_router.router)
    client = TestClient(app)

    response = client.post("/api/v1/browsers")
    assert response.status_code == 200
    data = response.json()
    assert data["browser_id"].startswith("B")


class _FakeRemote:
    """A stand-in for the browser's CDP socket: records what the relay forwards, and answers
    every command with a canned result carrying the browser's own (raw) target id."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._replies: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, message: str) -> None:
        self.sent.append(message)
        await self._replies.put(json.dumps({"id": 1, "result": {"targetId": "abc1234567"}}))

    def __aiter__(self) -> "_FakeRemote":
        return self

    async def __anext__(self) -> str:
        return await self._replies.get()


class _FakeConnect:
    def __init__(self, remote: _FakeRemote) -> None:
        self._remote = remote

    async def __aenter__(self) -> _FakeRemote:
        return self._remote

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeCDPBackend:
    """The slice of Backend that the browser-level CDP relay touches."""

    async def browser_exists(self, browser_id: str) -> bool:
        return True

    async def get_cdp_websocket_remote_url(self, browser_id: str) -> str:
        return "ws://remote/devtools/browser/xyz"

    def cdp_targets_need_namespacing(self, browser_id: str | None = None) -> bool:
        del browser_id
        return True


def _relay_roundtrip(monkeypatch: MonkeyPatch, path: str, sent_target_id: str) -> tuple[str, str]:
    """Drive one command through `path` and return (what the browser saw, what the client saw)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    remote = _FakeRemote()
    monkeypatch.setattr(browsers_router, "backend", _FakeCDPBackend())
    monkeypatch.setattr(websockets, "connect", lambda url, **kwargs: _FakeConnect(remote))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]

    app = FastAPI()
    app.include_router(browsers_router.router)
    with TestClient(app).websocket_connect(path) as ws:
        ws.send_text(
            json.dumps({
                "id": 1,
                "method": "Target.attachToTarget",
                "params": {"targetId": sent_target_id},
            })
        )
        received: str = ws.receive_text()
    return remote.sent[0], received


def test_cdp_route_namespaces_target_ids(monkeypatch: MonkeyPatch) -> None:
    # /cdp strips the browser_id off ids the client sends and prepends it to ids coming back.
    to_browser, to_client = _relay_roundtrip(monkeypatch, "/cdp/BID", "BID@abc1234567")
    assert json.loads(to_browser)["params"]["targetId"] == "abc1234567"
    assert json.loads(to_client)["result"]["targetId"] == "BID@abc1234567"


def test_cdp_raw_route_relays_verbatim(monkeypatch: MonkeyPatch) -> None:
    # /api/v1/browsers/{browser_id}/cdp does no patching in either direction: the client speaks the browser's
    # own raw target ids, and both frames cross the relay byte-for-byte.
    to_browser, to_client = _relay_roundtrip(monkeypatch, "/api/v1/browsers/BID/cdp", "abc1234567")
    assert json.loads(to_browser)["params"]["targetId"] == "abc1234567"
    assert json.loads(to_client)["result"]["targetId"] == "abc1234567"
