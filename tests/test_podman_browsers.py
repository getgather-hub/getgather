from typing import Any

import pytest
from pytest import MonkeyPatch

from getgather.browsers import podman_browsers
from getgather.browsers.backend import ProxyVerificationError
from getgather.browsers.podman_browsers import PodmanBackend


def _patch_proxy(monkeypatch: MonkeyPatch, *, ips: list[str | None], proxy_ok: bool = True) -> None:
    """Force a configured proxy and drive get_container_public_ip's return sequence."""

    class _Cfg:
        def get_proxy_url(self, browser_id: str) -> str:
            return "http://proxy.example:9999"

    async def fake_get_proxy_config(*args: Any, **kwargs: Any) -> Any:
        return _Cfg()

    async def fake_configure_container(*args: Any, **kwargs: Any) -> bool:
        return proxy_ok

    it = iter(ips)

    async def fake_public_ip(*args: Any, **kwargs: Any) -> str | None:
        return next(it)

    monkeypatch.setattr(podman_browsers, "get_proxy_config", fake_get_proxy_config)
    monkeypatch.setattr(podman_browsers, "configure_container", fake_configure_container)
    monkeypatch.setattr(podman_browsers, "get_container_public_ip", fake_public_ip)


@pytest.mark.asyncio
async def test_configure_remote_browser_ok_when_ip_before_missing(monkeypatch: MonkeyPatch) -> None:
    # Regression: a failed ip_before measurement (None) must NOT fail a working proxy.
    _patch_proxy(monkeypatch, ips=[None, "9.9.9.9"])
    ip = await podman_browsers.configure_remote_browser("b0", "chromium-b0", None, None, None)
    assert ip == "9.9.9.9"


@pytest.mark.asyncio
async def test_configure_remote_browser_raises_on_apply_failure(monkeypatch: MonkeyPatch) -> None:
    _patch_proxy(monkeypatch, ips=["1.1.1.1"], proxy_ok=False)
    with pytest.raises(ProxyVerificationError, match="Proxy failed to apply"):
        await podman_browsers.configure_remote_browser("b0", "chromium-b0", None, None, None)


@pytest.mark.asyncio
async def test_configure_remote_browser_raises_on_ip_check_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    # ip_after is None (curl/exec timeout): distinct, accurate error, not "IP unchanged".
    _patch_proxy(monkeypatch, ips=["1.1.1.1", None])
    with pytest.raises(ProxyVerificationError, match="IP check failed"):
        await podman_browsers.configure_remote_browser("b0", "chromium-b0", None, None, None)


@pytest.mark.asyncio
async def test_configure_remote_browser_raises_when_ip_unchanged(monkeypatch: MonkeyPatch) -> None:
    _patch_proxy(monkeypatch, ips=["1.1.1.1", "1.1.1.1"])
    with pytest.raises(ProxyVerificationError, match="IP unchanged"):
        await podman_browsers.configure_remote_browser("b0", "chromium-b0", None, None, None)


@pytest.mark.asyncio
async def test_configure_remote_browser_noop_without_proxy(monkeypatch: MonkeyPatch) -> None:
    # No proxy configured: it's a no-op that returns the current egress IP (no verification needed).
    async def fake_get_proxy_config(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_public_ip(*args: Any, **kwargs: Any) -> str | None:
        return "1.1.1.1"

    monkeypatch.setattr(podman_browsers, "get_proxy_config", fake_get_proxy_config)
    monkeypatch.setattr(podman_browsers, "get_container_public_ip", fake_public_ip)

    ip = await podman_browsers.configure_remote_browser("b0", "chromium-b0", None, None, None)
    assert ip == "1.1.1.1"


@pytest.mark.asyncio
async def test_get_browser_never_reconfigures_proxy(monkeypatch: MonkeyPatch) -> None:
    # GET is a cheap read: proxy is configured+verified once on create, never on get, even when
    # x-origin-ip is present. Otherwise every GET restarts tinyproxy and can 500 on an IP-check flake.
    configured = False

    async def fake_configure(*args: Any, **kwargs: Any) -> str | None:
        nonlocal configured
        configured = True
        return "9.9.9.9"

    async def fake_running(name: str) -> bool:
        return True

    async def fake_last_activity(name: str) -> float | None:
        return 1.0

    async def fake_public_ip(name: str, *args: Any, **kwargs: Any) -> str | None:
        return "9.9.9.9"

    monkeypatch.setattr(podman_browsers, "configure_remote_browser", fake_configure)
    monkeypatch.setattr(podman_browsers, "container_is_running", fake_running)
    monkeypatch.setattr(podman_browsers, "get_container_last_activity", fake_last_activity)
    monkeypatch.setattr(podman_browsers, "get_container_public_ip", fake_public_ip)

    info = await PodmanBackend().get_browser(
        "b0", origin_ip="1.2.3.4", target_domain="amazon.com", country=None
    )
    assert info == {"last_activity_timestamp": 1.0, "ip": "9.9.9.9"}
    assert configured is False


@pytest.mark.asyncio
async def test_create_browser_propagates_proxy_verification_error(monkeypatch: MonkeyPatch) -> None:
    # create_browser must let ProxyVerificationError propagate (the endpoint maps it to 500) so the
    # client can retry rather than get an unproxied browser; best-of-N relies on this to fail a loser.
    # It must also kill the now-unusable container rather than leaking it.
    async def fake_launch(image: str, name: str) -> str:
        return "container-id"

    async def fake_configure(*args: Any, **kwargs: Any) -> str | None:
        raise ProxyVerificationError("IP unchanged after proxy")

    killed: list[str] = []

    async def fake_kill(container_name: str) -> None:
        killed.append(container_name)

    monkeypatch.setattr(podman_browsers, "launch_container", fake_launch)
    monkeypatch.setattr(podman_browsers, "configure_remote_browser", fake_configure)
    monkeypatch.setattr(podman_browsers, "kill_container", fake_kill)

    with pytest.raises(ProxyVerificationError, match="IP unchanged"):
        await PodmanBackend().create_browser("b0", "1.2.3.4", "amazon.com", None, None)

    assert killed == ["chromium-b0"]


@pytest.mark.asyncio
async def test_configure_container_returns_true_without_proxy() -> None:
    # No proxy_url: a no-op success (proxy is not required for this browser).
    assert await podman_browsers.configure_container("chromium-b0", None) is True


@pytest.mark.asyncio
async def test_best_of_n_treats_proxy_failure_as_loser(monkeypatch: MonkeyPatch) -> None:
    # End-to-end-ish: best-of-N races create_browser; a candidate whose proxy fails to verify raises
    # ProxyVerificationError and so loses, while another candidate succeeds and wins. (Generic race
    # mechanics are covered in tests/test_best_of_n.py; this asserts podman's create_browser raises
    # verifiably, which is what makes the race meaningful for podman.)
    from getgather.browsers import backend as backend_module
    from getgather.browsers.backend import best_of_n

    class _Backend:
        def __init__(self) -> None:
            self.existing: set[str] = set()

        async def create_browser(
            self,
            browser_id: str,
            origin_ip: str | None,
            target_domain: str | None,
            browser_type: str | None,
            country: str | None,
        ) -> dict[str, Any]:
            if browser_id == "b0":
                raise ProxyVerificationError("IP unchanged after proxy")
            self.existing.add(browser_id)
            return {"id": browser_id}

        async def browser_exists(self, browser_id: str) -> bool:
            return browser_id in self.existing

        async def delete_browser(self, browser_id: str) -> dict[str, Any]:
            self.existing.discard(browser_id)
            return {"status": "deleted"}

    ids = iter(["b0", "b1"])
    monkeypatch.setattr(backend_module, "new_browser_id", lambda: next(ids))

    winner_id, info = await best_of_n(_Backend(), 2, "1.2.3.4", "amazon.com", None, None)
    assert winner_id == "b1"
    assert info == {"id": "b1"}
