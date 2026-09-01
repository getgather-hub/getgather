import asyncio
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from pytest import MonkeyPatch

from getgather import recordings as recordings_module
from getgather.recordings import (
    CONTENT_TYPE,
    _s3_client,  # pyright: ignore[reportPrivateUsage]
    _wait_for_recording,  # pyright: ignore[reportPrivateUsage]
    list_recordings,
    presign_put,
    schedule_upload_for_closed_tab,
    split_namespaced_target_id,
    storage_key,
    upload_recording,
)


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeFleet:
    """Stands in for `call_chromefleet_api`, recording every call and replaying queued listings."""

    def __init__(self, listings: list[Any] | None = None) -> None:
        # One entry consumed per GET; the last one repeats once exhausted.
        empty: list[Any] = [{"recordings": []}]
        self.listings: list[Any] = listings if listings is not None else empty
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        method: str,
        browser_id: str | None = None,
        **kwargs: Any,
    ) -> _FakeResponse | None:
        self.calls.append({"method": method, "browser_id": browser_id, **kwargs})
        if method == "GET":
            payload = self.listings[0] if len(self.listings) == 1 else self.listings.pop(0)
            return _FakeResponse(payload)
        return None

    @property
    def uploads(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] == "POST"]


@pytest.fixture(autouse=True)
def tigris_settings(monkeypatch: MonkeyPatch):
    """Credentials good enough to sign with, and a fleet URL so uploads count as enabled."""
    settings = recordings_module.settings
    monkeypatch.setattr(settings, "CHROMEFLEET_URL", "http://flyfleet-dev.flycast", raising=False)
    monkeypatch.setattr(settings, "TIGRIS_BUCKET", "remotebrowser", raising=False)
    monkeypatch.setattr(settings, "TIGRIS_ENDPOINT_URL", "https://t3.storage.dev", raising=False)
    monkeypatch.setattr(settings, "TIGRIS_ACCESS_KEY_ID", "tid_test", raising=False)
    monkeypatch.setattr(settings, "TIGRIS_SECRET_ACCESS_KEY", "tsec_test", raising=False)
    # The client is memoized, so a previous test's credentials would otherwise leak into this one.
    _s3_client.cache_clear()
    yield
    _s3_client.cache_clear()


@pytest.fixture
def fast_poll(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(recordings_module, "_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(recordings_module, "_POLL_ATTEMPTS", 3)


def _fleet(monkeypatch: MonkeyPatch, fleet: _FakeFleet) -> _FakeFleet:
    monkeypatch.setattr(recordings_module, "call_chromefleet_api", fleet)
    return fleet


def test_storage_key_matches_the_layout_flyfleet_verified():
    assert storage_key("Fnx2t9kv3", "20260819T105633_ca138505_twns2u") == (
        "Fnx2t9kv3/20260819T105633_ca138505_twns2u.mp4"
    )


@pytest.mark.parametrize(
    ("namespaced", "expected"),
    [
        (
            "Fnx2t9kv3@CA13850558B83626DA99FA1A37568CDD",
            ("Fnx2t9kv3", "CA13850558B83626DA99FA1A37568CDD"),
        ),
        ("CA13850558B83626DA99FA1A37568CDD", None),
        ("@CA138505", None),
        ("Fnx2t9kv3@", None),
    ],
)
def test_split_namespaced_target_id(namespaced: str, expected: tuple[str, str] | None):
    assert split_namespaced_target_id(namespaced) == expected


@pytest.mark.asyncio
async def test_presigned_url_signs_the_content_type_the_container_sends():
    """chrome-live's upload.py PUTs `Content-Type: video/mp4`; if that is not in the signature
    Tigris answers SignatureDoesNotMatch, so the header must be signed, not merely allowed."""
    url = await presign_put("Fnx2t9kv3/20260819T105633_ca138505_twns2u.mp4")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    # Virtual addressing: bucket in the host, as in the URL flyfleet verified against Tigris.
    assert parsed.netloc == "remotebrowser.t3.storage.dev"
    assert parsed.path == "/Fnx2t9kv3/20260819T105633_ca138505_twns2u.mp4"
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert "content-type" in query["X-Amz-SignedHeaders"][0]
    assert "/auto/s3/aws4_request" in query["X-Amz-Credential"][0]
    assert CONTENT_TYPE == "video/mp4"


@pytest.mark.asyncio
async def test_list_recordings_drops_non_dict_entries(monkeypatch: MonkeyPatch):
    fleet = _fleet(monkeypatch, _FakeFleet([{"recordings": [{"recording_id": "a"}, "junk", None]}]))
    assert await list_recordings("Fnx2t9kv3") == [{"recording_id": "a"}]
    assert fleet.calls[0]["path_suffix"] == "/trace/recordings"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"recordings": None}, {"recordings": "nope"}])
async def test_list_recordings_tolerates_a_malformed_body(monkeypatch: MonkeyPatch, payload: Any):
    _fleet(monkeypatch, _FakeFleet([payload]))
    assert await list_recordings("Fnx2t9kv3") == []


@pytest.mark.asyncio
async def test_wait_for_recording_polls_until_the_tab_appears(
    monkeypatch: MonkeyPatch, fast_poll: None
):
    """The .mp4 only exists after ffmpeg encodes, so the first listings are legitimately empty."""
    entry = {"recording_id": "20260819T105633_ca138505_twns2u", "target_id": "CA138505"}
    _fleet(monkeypatch, _FakeFleet([{"recordings": []}, {"recordings": [entry]}]))

    assert await _wait_for_recording("Fnx2t9kv3", "CA138505") == entry


@pytest.mark.asyncio
async def test_wait_for_recording_ignores_another_tabs_recording(
    monkeypatch: MonkeyPatch, fast_poll: None
):
    other = {"recording_id": "20260819T110000_ffffffff_zzzzzz", "target_id": "OTHERTAB"}
    _fleet(monkeypatch, _FakeFleet([{"recordings": [other]}]))

    assert await _wait_for_recording("Fnx2t9kv3", "CA138505") is None


@pytest.mark.asyncio
async def test_upload_recording_hands_the_fleet_a_signed_url_and_no_credentials(
    monkeypatch: MonkeyPatch,
):
    fleet = _fleet(monkeypatch, _FakeFleet())
    key = await upload_recording("Fnx2t9kv3", "20260819T105633_ca138505_twns2u")

    assert key == "Fnx2t9kv3/20260819T105633_ca138505_twns2u.mp4"
    (upload,) = fleet.uploads
    assert upload["path_suffix"] == "/recordings/20260819T105633_ca138505_twns2u/upload"
    # The exec re-PUTs the whole file, so the fleet client must not retry it.
    assert upload["retries"] == 0

    body = upload["json"]
    assert set(body) == {"upload_url"}
    assert body["upload_url"].startswith("https://remotebrowser.t3.storage.dev/")
    assert "X-Amz-Signature=" in body["upload_url"]
    assert "tsec_test" not in body["upload_url"]


@pytest.mark.asyncio
async def test_closed_tab_uploads_that_tabs_recording(monkeypatch: MonkeyPatch, fast_poll: None):
    entry = {
        "recording_id": "20260819T105633_ca138505_twns2u",
        "target_id": "CA13850558B83626DA99FA1A37568CDD",
        "size_bytes": 874240,
    }
    fleet = _fleet(monkeypatch, _FakeFleet([{"recordings": [entry]}]))

    schedule_upload_for_closed_tab("Fnx2t9kv3@CA13850558B83626DA99FA1A37568CDD")
    await asyncio.sleep(0)
    await asyncio.gather(*recordings_module._pending)  # pyright: ignore[reportPrivateUsage]

    (upload,) = fleet.uploads
    assert upload["browser_id"] == "Fnx2t9kv3"
    assert upload["path_suffix"] == "/recordings/20260819T105633_ca138505_twns2u/upload"


@pytest.mark.asyncio
async def test_closed_tab_uploads_nothing_when_the_recording_never_finalizes(
    monkeypatch: MonkeyPatch, fast_poll: None
):
    fleet = _fleet(monkeypatch, _FakeFleet([{"recordings": []}]))

    schedule_upload_for_closed_tab("Fnx2t9kv3@CA138505")
    await asyncio.sleep(0)
    await asyncio.gather(*recordings_module._pending)  # pyright: ignore[reportPrivateUsage]

    assert fleet.uploads == []


@pytest.mark.asyncio
async def test_a_failing_upload_never_escapes_the_background_task(
    monkeypatch: MonkeyPatch, fast_poll: None
):
    """The hook runs on the tab-close path, so a storage outage must not surface there."""
    entry = {"recording_id": "20260819T105633_ca138505_twns2u", "target_id": "CA138505"}

    async def exploding(method: str, browser_id: str | None = None, **kwargs: Any) -> Any:
        if method == "GET":
            return _FakeResponse({"recordings": [entry]})
        raise RuntimeError("fleet is down")

    monkeypatch.setattr(recordings_module, "call_chromefleet_api", exploding)

    schedule_upload_for_closed_tab("Fnx2t9kv3@CA138505")
    await asyncio.sleep(0)
    tasks = list(recordings_module._pending)  # pyright: ignore[reportPrivateUsage]
    await asyncio.gather(*tasks)

    assert all(task.exception() is None for task in tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unset",
    ["CHROMEFLEET_URL", "TIGRIS_BUCKET", "TIGRIS_ACCESS_KEY_ID", "TIGRIS_SECRET_ACCESS_KEY"],
)
async def test_missing_configuration_disables_uploads_entirely(
    monkeypatch: MonkeyPatch, unset: str
):
    """Gating is implicit: any missing piece means no upload attempt at all. CHROMEFLEET_URL is
    checked rather than effective_chromefleet_url, which falls back to this server's own port and
    so would report enabled on the local podman/daytona backends."""
    fleet = _fleet(monkeypatch, _FakeFleet())
    monkeypatch.setattr(recordings_module.settings, unset, "", raising=False)

    assert recordings_module.settings.recording_upload_enabled is False
    schedule_upload_for_closed_tab("Fnx2t9kv3@CA138505")
    await asyncio.sleep(0)

    assert fleet.calls == []


@pytest.mark.asyncio
async def test_a_target_id_without_a_browser_namespace_is_skipped(monkeypatch: MonkeyPatch):
    fleet = _fleet(monkeypatch, _FakeFleet())

    schedule_upload_for_closed_tab("CA13850558B83626DA99FA1A37568CDD")
    await asyncio.sleep(0)

    assert fleet.calls == []
