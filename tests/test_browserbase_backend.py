import asyncio
import json
import os
from typing import Any

import httpx
import pytest
from pytest import MonkeyPatch

from getgather.browsers.backend import create_backend
from getgather.browsers.browserbase_browsers import BrowserbaseBackend
from getgather.browsers.podman_browsers import PodmanBackend
from getgather.config import settings

pytestmark = pytest.mark.skipif(
    not os.environ.get("BROWSERBASE_API_KEY"),
    reason="BROWSERBASE_API_KEY env var not set",
)

SESSION_ID = "bc584de8-fd61-458f-aaef-4053b03e96bc"
CONNECT_URL = (
    "wss://connect.usw2.browserbase.com/?signingKey=eyJhbGciOiJBMjU2S1ciLCJlbmMiOiJBMjU2R0NNIn0"
)
BROWSERBASE_SESSIONS_URL = "https://api.browserbase.com/v1/sessions"
SAMPLE_RESPONSE: dict[str, Any] = {
    "id": SESSION_ID,
    "connectUrl": CONNECT_URL,
    "status": "RUNNING",
}


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self._body = body if body is not None else SAMPLE_RESPONSE

    def raise_for_status(self) -> None:
        if self.status_code >= 400 and self.status_code != 404:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._body


class FakeAsyncClient:
    """A fake httpx.AsyncClient that records every POST call.

    URLs routed:
      - POST {BROWSERBASE_SESSIONS_URL}        -> create session, echoes SAMPLE_RESPONSE
      - POST {BROWSERBASE_SESSIONS_URL}/{id}    -> release, echoes SAMPLE_RESPONSE at 200
    Tests can override the release status via `release_status`.
    """

    def __init__(self, *, release_status: int = 200) -> None:
        self.post_calls: list[dict[str, Any]] = []
        self.release_status = release_status

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, *, headers: dict[str, str], json: Any) -> FakeResponse:
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        if url == BROWSERBASE_SESSIONS_URL:
            return FakeResponse(status_code=200, body=SAMPLE_RESPONSE)
        # Per-session update / release — return the configured release status.
        return FakeResponse(status_code=self.release_status, body=SAMPLE_RESPONSE)

    @property
    def create_call(self) -> dict[str, Any]:
        return self.post_calls[0]

    @property
    def release_call(self) -> dict[str, Any]:
        return self.post_calls[-1]


def _make_fake_client_factory(*, release_status: int = 200) -> tuple[FakeAsyncClient, Any]:
    fake_client = FakeAsyncClient(release_status=release_status)

    def factory(**kwargs: Any) -> FakeAsyncClient:
        return fake_client

    return fake_client, factory


async def _noop_wait(self: Any, connect_url: str, browser_id: str) -> None:
    # Skip the live CDP readiness probe — exercised separately below.
    return None


def _patch_cdp_wait(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(BrowserbaseBackend, "_wait_until_cdp_ready", _noop_wait)


def test_create_backend_defaults_to_podman(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CHROMEFLEET_URL", "")
    monkeypatch.setattr(settings, "BROWSER_BACKEND", "podman")
    assert isinstance(create_backend(), PodmanBackend)


def test_create_backend_selects_browserbase(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CHROMEFLEET_URL", "")
    monkeypatch.setattr(settings, "BROWSER_BACKEND", "browserbase")
    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    backend = create_backend()
    assert isinstance(backend, BrowserbaseBackend)


@pytest.mark.asyncio
async def test_browserbase_create_browser_stores_connect_url_mapping(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    _patch_cdp_wait(monkeypatch)

    fake_client, factory = _make_fake_client_factory()
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    backend = BrowserbaseBackend()
    # The caller-supplied id is ignored; Browserbase mints its own session id.
    result = await backend.create_browser("ignored-id", None, None, None)

    assert result == {"browser_id": SESSION_ID, "status": "created", "ip": None}

    # The session API was called with the x-bb-api-key header from settings and keepAlive:true.
    assert fake_client.create_call["url"] == BROWSERBASE_SESSIONS_URL
    assert fake_client.create_call["headers"]["x-bb-api-key"] == "test-key"
    assert fake_client.create_call["headers"]["Content-Type"] == "application/json"
    assert fake_client.create_call["json"] == {
        "keepAlive": True,
        "userMetadata": {"getgatherBrowserId": "ignored-id"},
    }

    # The id -> connectUrl mapping is stored for the CDP proxy to look up.
    assert await backend.get_cdp_websocket_remote_url(SESSION_ID) == CONNECT_URL
    assert await backend.get_cdp_websocket_remote_url("unknown") is None
    assert await backend.browser_exists(SESSION_ID) is True
    assert SESSION_ID in await backend.list_browser_ids()


@pytest.mark.asyncio
async def test_browserbase_recovers_session_from_provider_metadata(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    get_calls: list[dict[str, Any]] = []

    class RecoveryResponse:
        def __init__(self, body: Any) -> None:
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return self._body

    class RecoveryClient:
        async def __aenter__(self) -> "RecoveryClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            params: dict[str, str] | None = None,
        ) -> RecoveryResponse:
            get_calls.append({"url": url, "headers": headers, "params": params})
            if url == BROWSERBASE_SESSIONS_URL:
                return RecoveryResponse([
                    {
                        "id": SESSION_ID,
                        "status": "RUNNING",
                        "updatedAt": "2026-08-19",
                        "userMetadata": {"getgatherBrowserId": "Bbuxvstp6"},
                    }
                ])
            return RecoveryResponse(SAMPLE_RESPONSE)

    def recovery_client_factory(**kwargs: Any) -> RecoveryClient:
        return RecoveryClient()

    monkeypatch.setattr(httpx, "AsyncClient", recovery_client_factory)

    backend = BrowserbaseBackend()
    assert await backend.resolve_session("logical-id") == SESSION_ID
    assert await backend.get_cdp_websocket_remote_url(SESSION_ID) == CONNECT_URL
    assert get_calls[0]["params"] == {"q": "user_metadata['getgatherBrowserId']:'logical-id'"}
    assert await backend.list_routed_sessions() == {"Bbuxvstp6": SESSION_ID}


@pytest.mark.asyncio
async def test_browserbase_create_browser_attaches_residential_proxy(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    _patch_cdp_wait(monkeypatch)

    fake_client, factory = _make_fake_client_factory()
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    proxy_url = "http://customer-u-cc-US-sessid-bid-sesstime-1440:p@pr.oxylabs.io:7777"

    class _Cfg:
        type_ = "oxylabs"

        class location:
            country = "us"

        def get_proxy_url(self, browser_id: str) -> str:
            return proxy_url

    async def fake_get_proxy_config(*args: Any, **kwargs: Any) -> Any:
        return _Cfg()

    from getgather.browsers import browserbase_browsers

    monkeypatch.setattr(browserbase_browsers, "get_proxy_config", fake_get_proxy_config)

    backend = BrowserbaseBackend()
    result = await backend.create_browser("bid", "1.2.3.4", "amazon.com", None)

    assert result == {"browser_id": SESSION_ID, "status": "created", "ip": None}
    assert fake_client.create_call["json"] == {
        "keepAlive": True,
        "userMetadata": {"getgatherBrowserId": "bid"},
        "proxies": [{"type": "external", "server": proxy_url}],
    }


@pytest.mark.asyncio
async def test_browserbase_create_browser_omits_proxies_without_proxy(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    _patch_cdp_wait(monkeypatch)
    fake_client, factory = _make_fake_client_factory()
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    async def fake_get_proxy_config(*args: Any, **kwargs: Any) -> None:
        return None

    from getgather.browsers import browserbase_browsers

    monkeypatch.setattr(browserbase_browsers, "get_proxy_config", fake_get_proxy_config)

    backend = BrowserbaseBackend()
    await backend.create_browser("bid", "1.2.3.4", "amazon.com", None)
    assert fake_client.create_call["json"] == {
        "keepAlive": True,
        "userMetadata": {"getgatherBrowserId": "bid"},
    }


@pytest.mark.asyncio
async def test_browserbase_cdp_proxy_single_attempt_on_410(
    monkeypatch: MonkeyPatch,
) -> None:
    # By the time /cdp is hit, create_browser has already confirmed chrome is CDP-ready, so the
    # router's websocket_proxy no longer retries the wss upgrade. A 410 from the connectUrl is a
    # real failure (e.g. session terminated), not a boot-race — single attempt, close 4502.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    from getgather.browsers import router as browsers_router

    # The API key is only used on the HTTP /v1/sessions call, not on the WS upgrade.
    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    _patch_cdp_wait(monkeypatch)
    _fake_client, factory = _make_fake_client_factory()
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    backend = BrowserbaseBackend()
    await backend.create_browser("ignored", None, None, None)
    monkeypatch.setattr(browsers_router, "backend", backend)

    connect_calls: list[int] = []
    fake_resp = Response(status_code=410, reason_phrase="Gone", headers=Headers())

    def fake_connect(url: str, **kwargs: Any) -> Any:
        connect_calls.append(1)
        raise InvalidStatus(fake_resp)

    import websockets

    monkeypatch.setattr(websockets, "connect", fake_connect)

    app = FastAPI()
    app.include_router(browsers_router.router)
    client = TestClient(app)

    with pytest.raises(Exception):  # the wss upgrade closes the socket with code 4502
        with client.websocket_connect(f"/cdp/{SESSION_ID}") as ws:
            ws.receive_text()

    # Single attempt — no boot-race retry.
    assert len(connect_calls) == 1


@pytest.mark.asyncio
async def test_browserbase_delete_browser_releases_session_via_post(
    monkeypatch: MonkeyPatch,
) -> None:
    # Browserbase has no DELETE endpoint: a session is closed by POSTing
    # `{"status": "REQUEST_RELEASE"}` to /v1/sessions/{id}.
    # See https://docs.browserbase.com/reference/api/update-a-session.
    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    _patch_cdp_wait(monkeypatch)
    fake_client, factory = _make_fake_client_factory(release_status=200)
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    backend = BrowserbaseBackend()
    await backend.create_browser("ignored", None, None, None)
    assert await backend.get_cdp_websocket_remote_url(SESSION_ID) == CONNECT_URL

    result = await backend.delete_browser(SESSION_ID)

    assert result == {"browser_id": SESSION_ID, "status": "deleted"}
    # The release call posted REQUEST_RELEASE to the per-session endpoint.
    assert fake_client.release_call["url"] == f"{BROWSERBASE_SESSIONS_URL}/{SESSION_ID}"
    assert fake_client.release_call["json"] == {"status": "REQUEST_RELEASE"}
    assert fake_client.release_call["headers"]["x-bb-api-key"] == "test-key"
    # The local id -> connectUrl mapping is cleared regardless of upstream outcome.
    assert await backend.get_cdp_websocket_remote_url(SESSION_ID) is None
    assert await backend.browser_exists(SESSION_ID) is False


@pytest.mark.asyncio
async def test_browserbase_delete_browser_swallows_404(monkeypatch: MonkeyPatch) -> None:
    # 404 means the session is already gone upstream — desired state, not an error.
    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    _patch_cdp_wait(monkeypatch)
    _fake_client, factory = _make_fake_client_factory(release_status=404)
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    backend = BrowserbaseBackend()
    await backend.create_browser("ignored", None, None, None)

    result = await backend.delete_browser(SESSION_ID)
    assert result == {"browser_id": SESSION_ID, "status": "deleted"}
    assert await backend.get_cdp_websocket_remote_url(SESSION_ID) is None


@pytest.mark.asyncio
async def test_browserbase_delete_browser_redacts_release_error(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    _patch_cdp_wait(monkeypatch)
    _fake_client, factory = _make_fake_client_factory(release_status=401)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    warnings: list[str] = []
    monkeypatch.setattr("getgather.browsers.browserbase_browsers.logger.warning", warnings.append)

    backend = BrowserbaseBackend()
    await backend.create_browser("ignored", None, None, None)
    result = await backend.delete_browser(SESSION_ID)

    assert result == {"browser_id": SESSION_ID, "status": "deleted"}
    log_text = "\n".join(warnings)
    assert "error=RuntimeError" in log_text
    assert "HTTP 401" not in log_text
    assert BROWSERBASE_SESSIONS_URL not in log_text


@pytest.mark.asyncio
async def test_browserbase_delete_browser_for_unknown_id_clears_silently(
    monkeypatch: MonkeyPatch,
) -> None:
    # Deleting an id we never created must still succeed (router's DELETE endpoint relies on
    # delete_browser being idempotent once browser_exists has confirmed existence — but the
    # backend must also tolerate clearing a never-tracked id).
    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    _fake_client, factory = _make_fake_client_factory(release_status=404)
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    backend = BrowserbaseBackend()
    result = await backend.delete_browser("never-existed")
    assert result == {"browser_id": "never-existed", "status": "deleted"}


@pytest.mark.asyncio
async def test_browserbase_cdp_websocket_base_is_none() -> None:
    # None signals the router to use the direct connectUrl flow rather than a shared relay.
    assert BrowserbaseBackend().cdp_websocket_base() is None


@pytest.mark.asyncio
async def test_browserbase_create_browser_waits_until_cdp_ready(
    monkeypatch: MonkeyPatch,
) -> None:
    # After POST /v1/sessions returns, chrome inside the session is still booting. create_browser
    # must wait: it opens the multiplexed CDP socket, sends `Target.getTargets` and waits for the
    # matching `id`-tagged response before handing the id back.
    import websockets

    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    _fake_client, factory = _make_fake_client_factory()
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    sent: list[str] = []
    response_json = json.dumps({"id": -2, "result": {"targetInfos": []}})

    class FakeRemoteWS:
        async def __aenter__(self) -> "FakeRemoteWS":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def send(self, message: str) -> None:
            sent.append(message)

        async def recv(self) -> str:
            return response_json

    def fake_connect(url: str, **kwargs: Any) -> Any:
        return FakeRemoteWS()

    monkeypatch.setattr(websockets, "connect", fake_connect)

    backend = BrowserbaseBackend()
    result = await backend.create_browser("ignored", None, None, None)

    assert result == {"browser_id": SESSION_ID, "status": "created", "ip": None}
    # Exactly one probe was sent (Target.getTargets with id=-2) and it succeeded first try.
    assert sent == [json.dumps({"id": -2, "method": "Target.getTargets"})]
    assert await backend.get_cdp_websocket_remote_url(SESSION_ID) == CONNECT_URL


@pytest.mark.asyncio
async def test_browserbase_create_browser_never_ready_releases_and_raises(
    monkeypatch: MonkeyPatch,
) -> None:
    # If every readiness probe fails (HTTP 410 while chrome stays PENDING), the budget is
    # exhausted: the never-ready session is released upstream (no leak) and create_browser
    # surfaces a failure rather than handing back a dead id.
    import websockets
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    monkeypatch.setattr(settings, "BROWSERBASE_API_KEY", "test-key")
    fake_client, factory = _make_fake_client_factory()
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    fake_resp = Response(status_code=410, reason_phrase="Gone", headers=Headers())

    def fake_connect(url: str, **kwargs: Any) -> Any:
        raise InvalidStatus(fake_resp)

    monkeypatch.setattr(websockets, "connect", fake_connect)

    backend = BrowserbaseBackend()

    async def _no_sleep(_duration: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="never became CDP-ready"):
        await backend.create_browser("ignored", None, None, None)

    # Cleanup released the upstream session and dropped the dead id from the local mapping.
    assert fake_client.release_call["url"] == f"{BROWSERBASE_SESSIONS_URL}/{SESSION_ID}"
    assert fake_client.release_call["json"] == {"status": "REQUEST_RELEASE"}
    assert await backend.get_cdp_websocket_remote_url(SESSION_ID) is None
    assert await backend.browser_exists(SESSION_ID) is False
