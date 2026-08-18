import asyncio
import json
from typing import Any

import pytest
import websockets
from pytest import MonkeyPatch

from getgather.browser import _setup_cdp_url  # pyright: ignore[reportPrivateUsage]
from getgather.browsers import router as browsers_router
from getgather.browsers.backend import create_backend
from getgather.browsers.fleet_browsers import FleetBackend
from getgather.browsers.podman_browsers import PodmanBackend
from getgather.cdp_client import CDPClient, PageNotFoundError
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

    def cdp_targets_need_namespacing(self) -> bool:
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


def test_get_targets_response_is_namespaced() -> None:
    """`Target.getTargets` answers with `result.targetInfos[]`, a list the old shape-matching
    patch skipped entirely. zendriver's `update_targets` builds each tab's websocket URL as
    `/devtools/page/{target_id}` straight from that list, so an unpatched id produces a raw
    devtools URL and forces `find_browser_id` to scan the whole fleet."""
    message = json.dumps({
        "id": 3,
        "result": {
            "targetInfos": [
                {"targetId": "PAGE1", "type": "page"},
                {"targetId": "PAGE2", "type": "page"},
            ]
        },
    })
    patched = json.loads(browsers_router.patch_cdp_target_inbound(message, "Byr3kieca"))
    assert [t["targetId"] for t in patched["result"]["targetInfos"]] == [
        "Byr3kieca@PAGE1",
        "Byr3kieca@PAGE2",
    ]


def test_attached_to_target_event_is_namespaced() -> None:
    message = json.dumps({
        "method": "Target.attachedToTarget",
        "params": {"sessionId": "S1", "targetInfo": {"targetId": "PAGE1", "type": "page"}},
    })
    patched = json.loads(browsers_router.patch_cdp_target_inbound(message, "Byr3kieca"))
    assert patched["params"]["targetInfo"]["targetId"] == "Byr3kieca@PAGE1"


def test_target_created_event_is_namespaced() -> None:
    message = json.dumps({
        "method": "Target.targetCreated",
        "params": {"targetInfo": {"targetId": "PAGE1", "type": "page"}},
    })
    patched = json.loads(browsers_router.patch_cdp_target_inbound(message, "Byr3kieca"))
    assert patched["params"]["targetInfo"]["targetId"] == "Byr3kieca@PAGE1"


def test_namespacing_is_idempotent() -> None:
    message = json.dumps({"id": 1, "result": {"targetId": "Byr3kieca@PAGE1"}})
    patched = json.loads(browsers_router.patch_cdp_target_inbound(message, "Byr3kieca"))
    assert patched["result"]["targetId"] == "Byr3kieca@PAGE1"


def test_outbound_strips_namespace_for_any_method() -> None:
    """The browser only knows its own raw ids, so every id the client sends back must be
    stripped — including on methods no allowlist happened to enumerate."""
    for method in (
        "Target.attachToTarget",
        "Target.closeTarget",
        "Target.getTargetInfo",
        "Target.activateTarget",
        "Browser.getWindowForTarget",
    ):
        message = json.dumps({"id": 1, "method": method, "params": {"targetId": "Byr3kieca@PAGE1"}})
        patched = json.loads(browsers_router.patch_cdp_target_outbound(message, "Byr3kieca"))
        assert patched["params"]["targetId"] == "PAGE1", method


def test_patch_passes_through_messages_without_target_ids() -> None:
    message = json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": "https://a.test"}})
    assert browsers_router.patch_cdp_target_inbound(message, "Byr3kieca") == message
    assert browsers_router.patch_cdp_target_outbound(message, "Byr3kieca") == message


def test_signin_id_strips_namespaced_target_id() -> None:
    """zendriver tabs report namespaced ids because the browser is attached over the
    `/cdp/{browser_id}` proxy, but the sign-in id already carries `browser_id` and lands in a
    user-visible /dpage/ URL, so the prefix must not survive."""
    from getgather.mcp.dpage import SignInId

    signin_id = SignInId("Byr3kieca", "Byr3kieca@PAGE1", "sess1")
    assert signin_id.target_id == "PAGE1"
    assert str(signin_id) == "Byr3kieca--PAGE1--sess1"
    # Round trip: parsing our own string must be stable.
    assert SignInId.from_str(str(signin_id)) == signin_id


def test_find_page_target_matches_namespaced_ids() -> None:
    """`open_cdp` goes through the namespacing proxy, so `Target.getTargets` answers with
    prefixed ids while callers hold a raw page id. Both directions must match, and the returned
    TargetInfo must keep its id verbatim so `attach_to_page` echoes back what the proxy sent."""

    class FakeCDPClient(CDPClient):
        def __init__(self, target_infos: list[dict[str, Any]]) -> None:
            self._target_infos = target_infos

        async def send(  # pyright: ignore[reportIncompatibleMethodOverride]
            self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
        ) -> dict[str, Any]:
            assert method == "Target.getTargets"
            return {"targetInfos": self._target_infos}

    namespaced = FakeCDPClient([{"targetId": "Byr3kieca@PAGE1", "type": "page"}])
    raw = FakeCDPClient([{"targetId": "PAGE1", "type": "page"}])

    for client in (namespaced, raw):
        for query in ("PAGE1", "Byr3kieca@PAGE1"):
            found = asyncio.run(client.find_page_target(query))
            assert found is client._target_infos[0]  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(PageNotFoundError):
        asyncio.run(namespaced.find_page_target("OTHER"))


def test_runtime_evaluate_result_target_id_passes_through() -> None:
    """A `targetId` key inside a `Runtime.evaluate` result is application data, not a CDP target
    id — `window.ytInitialData` and friends come back as whole nested blobs. Rewriting it
    corrupts scraped output silently, with no error to notice."""
    message = json.dumps({
        "id": 7,
        "result": {"result": {"type": "object", "value": {"targetId": "order-123", "price": 9.99}}},
    })
    assert browsers_router.patch_cdp_target_inbound(message, "BROWSER") == message
    assert browsers_router.patch_cdp_target_outbound(message, "BROWSER") == message


def test_runtime_call_function_on_argument_target_id_passes_through() -> None:
    """Outbound is the more damaging direction: the strip truncates at the first `@`, so an
    `@`-bearing application value under a `targetId` key would lose everything before it."""
    message = json.dumps({
        "id": 8,
        "method": "Runtime.callFunctionOn",
        "params": {
            "functionDeclaration": "x=>x",
            "arguments": [{"value": {"targetId": "user@domain"}}],
        },
    })
    assert browsers_router.patch_cdp_target_outbound(message, "BROWSER") == message
    assert browsers_router.patch_cdp_target_inbound(message, "BROWSER") == message


def test_get_target_info_result_is_namespaced() -> None:
    """`result.targetInfo.targetId` — the shape `Target.getTargetInfo` answers with."""
    message = json.dumps({"id": 4, "result": {"targetInfo": {"targetId": "PAGE1", "type": "page"}}})
    patched = json.loads(browsers_router.patch_cdp_target_inbound(message, "Byr3kieca"))
    assert patched["result"]["targetInfo"]["targetId"] == "Byr3kieca@PAGE1"


def test_create_target_result_is_namespaced() -> None:
    """`result.targetId` — the shape `Target.createTarget` answers with."""
    message = json.dumps({"id": 5, "result": {"targetId": "PAGE1"}})
    patched = json.loads(browsers_router.patch_cdp_target_inbound(message, "Byr3kieca"))
    assert patched["result"]["targetId"] == "Byr3kieca@PAGE1"


def test_target_destroyed_event_params_target_id_is_namespaced() -> None:
    """`params.targetId` on an event, not just on a request."""
    message = json.dumps({"method": "Target.targetDestroyed", "params": {"targetId": "PAGE1"}})
    patched = json.loads(browsers_router.patch_cdp_target_inbound(message, "Byr3kieca"))
    assert patched["params"]["targetId"] == "Byr3kieca@PAGE1"
