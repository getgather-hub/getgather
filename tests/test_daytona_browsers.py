from typing import Any, cast

import pytest
from pytest import MonkeyPatch

from getgather.browsers import daytona_browsers
from getgather.browsers.daytona_browsers import DaytonaBackend, ProxyVerificationError


def _backend() -> DaytonaBackend:
    # The AsyncDaytona client is constructed but never touched: every test patches the methods
    # that would reach it (_create_candidate, _cleanup_losers, _get).
    return DaytonaBackend(api_key="test-key", api_url="", snapshot="test-snapshot")


def _patch_proxy(monkeypatch: MonkeyPatch, *, ips: list[str | None], proxy_ok: bool = True) -> None:
    """Force a configured proxy and drive _get_sandbox_public_ip's return sequence."""

    class _Cfg:
        def get_proxy_url(self, browser_id: str) -> str:
            return "http://proxy.example:9999"

    async def fake_get_proxy_config(*args: Any, **kwargs: Any):
        return _Cfg()

    async def fake_configure_sandbox_proxy(*args: Any, **kwargs: Any) -> bool:
        return proxy_ok

    it = iter(ips)

    async def fake_public_ip(*args: Any, **kwargs: Any):
        return next(it)

    monkeypatch.setattr(daytona_browsers, "get_proxy_config", fake_get_proxy_config)
    monkeypatch.setattr(daytona_browsers, "_configure_sandbox_proxy", fake_configure_sandbox_proxy)
    monkeypatch.setattr(daytona_browsers, "_get_sandbox_public_ip", fake_public_ip)


class _Sandbox:
    name = "chromium-test"
    id = "8addf98c-1ba7-4553-bb99-22d5fd73add1"


def _fake_sandbox() -> "daytona_browsers.AsyncSandbox":
    return cast("daytona_browsers.AsyncSandbox", _Sandbox())


@pytest.mark.asyncio
async def test_configure_remote_sandbox_ok_when_ip_before_missing(monkeypatch: MonkeyPatch) -> None:
    # Regression: a failed ip_before measurement (None) must NOT fail a working proxy.
    _patch_proxy(monkeypatch, ips=[None, "9.9.9.9"])
    await daytona_browsers._configure_remote_sandbox(_fake_sandbox(), "b0", None, None)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_configure_remote_sandbox_raises_on_ip_check_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    # ip_after is None (curl/exec timeout): distinct, accurate error, not "IP unchanged".
    _patch_proxy(monkeypatch, ips=["1.1.1.1", None])
    with pytest.raises(ProxyVerificationError, match="IP check failed"):
        await daytona_browsers._configure_remote_sandbox(_fake_sandbox(), "b0", None, None)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_configure_remote_sandbox_raises_when_ip_unchanged(monkeypatch: MonkeyPatch) -> None:
    _patch_proxy(monkeypatch, ips=["1.1.1.1", "1.1.1.1"])
    with pytest.raises(ProxyVerificationError, match="IP unchanged"):
        await daytona_browsers._configure_remote_sandbox(_fake_sandbox(), "b0", None, None)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_get_browser_never_reconfigures_proxy(monkeypatch: MonkeyPatch) -> None:
    # GET is a cheap read: proxy is configured+verified once on create, never on get, even when
    # x-origin-ip is present. Otherwise every GET restarts tinyproxy and can 500 on an IP-check flake.
    configured = False

    async def fake_configure(*args: Any, **kwargs: Any) -> None:
        nonlocal configured
        configured = True

    async def fake_get(self: Any, name: str):
        return _Sandbox()

    async def fake_info(self: Any, sandbox: Any):
        return {"id": "b0"}

    monkeypatch.setattr(daytona_browsers, "_configure_remote_sandbox", fake_configure)
    monkeypatch.setattr(DaytonaBackend, "_get", fake_get)
    monkeypatch.setattr(DaytonaBackend, "_get_info", fake_info)

    info = await _backend().get_browser("b0", origin_ip="1.2.3.4", target_domain="amazon.com")
    assert info == {"id": "b0"}
    assert configured is False


def test_create_browser_n1_short_circuits_best_of_n(monkeypatch: MonkeyPatch) -> None:
    # The POST /api/v1/browsers endpoint short-circuits the race when N=1: it assigns an id and
    # calls `backend.create_browser` directly. The race helper must not run.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from getgather.browsers import router as router_module

    monkeypatch.setattr(router_module.settings, "BROWSER_BEST_OF_N", 1)
    monkeypatch.setattr(router_module, "backend", _backend())

    ids = iter(["solo"])

    def fake_new_id() -> str:
        return next(ids)

    called: dict[str, Any] = {}

    async def fake_create_browser(
        self: Any,
        browser_id: str,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> dict[str, str]:
        called["browser_id"] = browser_id
        called["browser_type"] = browser_type
        return {"id": browser_id}

    async def fail_best_of_n(*args: Any, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        raise AssertionError("best_of_n should not run when N=1")

    monkeypatch.setattr(router_module, "new_browser_id", fake_new_id)
    monkeypatch.setattr(DaytonaBackend, "create_browser", fake_create_browser)
    monkeypatch.setattr(router_module, "best_of_n", fail_best_of_n)

    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    response = client.post("/api/v1/browsers", headers={"x-browser-type": "cloak"})
    assert response.status_code == 200
    data = response.json()
    assert data == {"browser_id": "solo", "id": "solo"}
    assert called["browser_id"] == "solo"
    assert called["browser_type"] == "cloak"  # x-browser-type header threaded through


def test_create_browser_auto_n_gt1_invokes_best_of_n(monkeypatch: MonkeyPatch) -> None:
    # N>1 delegates to the shared best_of_n helper, passing the backend through.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from getgather.browsers import router as router_module

    monkeypatch.setattr(router_module.settings, "BROWSER_BEST_OF_N", 3)
    monkeypatch.setattr(router_module, "backend", _backend())

    invoked: dict[str, Any] = {}

    async def fake_best_of_n(
        backend: Any,
        n: int,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> tuple[str, dict[str, str]]:
        invoked["n"] = n
        invoked["origin_ip"] = origin_ip
        invoked["target_domain"] = target_domain
        invoked["browser_type"] = browser_type
        return "winner", {"id": "winner"}

    monkeypatch.setattr(router_module, "best_of_n", fake_best_of_n)

    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    response = client.post(
        "/api/v1/browsers",
        headers={
            "x-origin-ip": "1.2.3.4",
            "x-target-domains": "amazon.com",
            "x-browser-type": "cloak",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"browser_id": "winner", "id": "winner"}
    assert invoked == {
        "n": 3,
        "origin_ip": "1.2.3.4",
        "target_domain": "amazon.com",
        "browser_type": "cloak",
    }


async def _capture_create_params(monkeypatch: MonkeyPatch, backend: DaytonaBackend) -> list[Any]:
    """Patch the Daytona client's create() to record the params it was called with."""
    captured: list[Any] = []

    async def fake_create(params: Any, timeout: float = 0) -> Any:
        captured.append(params)
        return cast(Any, _Sandbox())

    monkeypatch.setattr(backend.client, "create", fake_create)
    return captured


@pytest.mark.asyncio
async def test_create_sets_active_browser_env_for_cloak(monkeypatch: MonkeyPatch) -> None:
    # browser_type="cloak" (x-browser-type header) selects CloakBrowser via the ACTIVE_BROWSER env.
    backend = _backend()
    monkeypatch.setattr(daytona_browsers.settings, "LOGFIRE_TOKEN", "")
    captured = await _capture_create_params(monkeypatch, backend)
    await backend._create("chromium-test", "cloak")  # pyright: ignore[reportPrivateUsage]
    assert captured[0].env_vars == {"ACTIVE_BROWSER": "cloak"}


@pytest.mark.asyncio
async def test_create_omits_env_for_chrome(monkeypatch: MonkeyPatch) -> None:
    # Chrome is the default: send no env_vars so the create call is identical to a Chrome-only
    # snapshot (older Daytona backends reject unexpected env_vars), when Logfire is unset.
    backend = _backend()
    monkeypatch.setattr(daytona_browsers.settings, "LOGFIRE_TOKEN", "")
    captured = await _capture_create_params(monkeypatch, backend)
    await backend._create("chromium-test", "chrome")  # pyright: ignore[reportPrivateUsage]
    assert captured[0].env_vars is None


@pytest.mark.asyncio
async def test_create_omits_env_when_browser_type_none(monkeypatch: MonkeyPatch) -> None:
    # No x-browser-type header -> browser_type None -> default Chrome, no env_vars (no Logfire).
    backend = _backend()
    monkeypatch.setattr(daytona_browsers.settings, "LOGFIRE_TOKEN", "")
    captured = await _capture_create_params(monkeypatch, backend)
    await backend._create("chromium-test", None)  # pyright: ignore[reportPrivateUsage]
    assert captured[0].env_vars is None


@pytest.mark.asyncio
async def test_create_passes_logfire_env_for_chrome(monkeypatch: MonkeyPatch) -> None:
    # chrome-live browser-trace needs LOGFIRE_TOKEN even for the default Chrome browser.
    backend = _backend()
    monkeypatch.setattr(daytona_browsers.settings, "LOGFIRE_TOKEN", "lf_test_token")
    monkeypatch.setattr(daytona_browsers.settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(daytona_browsers.settings, "LOG_LEVEL", "DEBUG")
    captured = await _capture_create_params(monkeypatch, backend)
    await backend._create("chromium-test", "chrome")  # pyright: ignore[reportPrivateUsage]
    env = captured[0].env_vars
    assert env["LOGFIRE_TOKEN"] == "lf_test_token"
    assert env["ENVIRONMENT"] == "test"
    assert env["LOG_LEVEL"] == "DEBUG"
    assert "ACTIVE_BROWSER" not in env


@pytest.mark.asyncio
async def test_create_passes_logfire_env_with_cloak(monkeypatch: MonkeyPatch) -> None:
    backend = _backend()
    monkeypatch.setattr(daytona_browsers.settings, "LOGFIRE_TOKEN", "lf_test_token")
    monkeypatch.setattr(daytona_browsers.settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(daytona_browsers.settings, "LOG_LEVEL", "INFO")
    captured = await _capture_create_params(monkeypatch, backend)
    await backend._create("chromium-test", "cloak")  # pyright: ignore[reportPrivateUsage]
    env = captured[0].env_vars
    assert env["ACTIVE_BROWSER"] == "cloak"
    assert env["LOGFIRE_TOKEN"] == "lf_test_token"
    assert env["ENVIRONMENT"] == "prod"
    assert env["LOG_LEVEL"] == "INFO"


def test_create_browser_auto_uses_backend_default_when_env_unset(monkeypatch: MonkeyPatch) -> None:
    # When BROWSER_BEST_OF_N is unset (None), the router falls back to the backend's own default
    # (DaytonaBackend.default_best_of_n == 3) instead of hard-coding 1.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from getgather.browsers import router as router_module

    monkeypatch.setattr(router_module.settings, "BROWSER_BEST_OF_N", None)
    monkeypatch.setattr(router_module, "backend", _backend())

    invoked: dict[str, Any] = {}

    async def fake_best_of_n(
        backend: Any,
        n: int,
        origin_ip: str | None,
        target_domain: str | None,
        browser_type: str | None,
    ) -> tuple[str, dict[str, str]]:
        invoked["n"] = n
        return "winner", {"id": "winner"}

    monkeypatch.setattr(router_module, "best_of_n", fake_best_of_n)

    app = FastAPI()
    app.include_router(router_module.router)
    client = TestClient(app)

    response = client.post("/api/v1/browsers")
    assert response.status_code == 200
    assert invoked["n"] == 3


@pytest.mark.parametrize(
    ("module_name", "expected"),
    [
        ("getgather.browsers.podman_browsers", 5),
        ("getgather.browsers.daytona_browsers", 3),
        ("getgather.browsers.fleet_browsers", 1),
    ],
)
def test_backend_default_best_of_n_consts(module_name: str, expected: int) -> None:
    import importlib

    module = importlib.import_module(module_name)
    assert module.DEFAULT_BEST_OF_N == expected
