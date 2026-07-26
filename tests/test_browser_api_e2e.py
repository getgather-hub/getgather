import asyncio
import os
from collections.abc import Generator

import httpx
import pytest
import websockets

REMOTEBROWSER_URL = os.getenv("REMOTEBROWSER_URL", "http://localhost:23456")


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=REMOTEBROWSER_URL, timeout=30.0) as c:
        yield c


@pytest.fixture(scope="module")
async def async_client():
    async with httpx.AsyncClient(base_url=REMOTEBROWSER_URL, timeout=30.0) as c:
        yield c


@pytest.mark.api
class TestHealthEndpoint:
    def test_health_returns_ok(self, client: httpx.Client) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert "OK" in response.text


@pytest.mark.api
class TestBrowserLifecycle:
    browser_ids: list[str] = []

    @pytest.fixture(autouse=True)
    def cleanup(self, client: httpx.Client) -> Generator[None, None, None]:
        self.browser_ids = []
        yield
        for browser_id in self.browser_ids:
            try:
                client.delete(f"/api/v1/browsers/{browser_id}")
            except Exception:
                pass

    def test_create_browser(self, client: httpx.Client) -> None:
        response = client.post("/api/v1/browsers/test01")
        assert response.status_code == 200
        self.browser_ids.append("test01")
        data = response.json()
        assert data["status"] == "created"

    def test_get_browser(self, client: httpx.Client) -> None:
        client.post("/api/v1/browsers/test02")
        self.browser_ids.append("test02")
        response = client.get("/api/v1/browsers/test02")
        assert response.status_code == 200
        data = response.json()
        assert "last_activity_timestamp" in data

    def test_get_nonexistent_browser(self, client: httpx.Client) -> None:
        response = client.get("/api/v1/browsers/nonexistent-browser")
        assert response.status_code == 404

    def test_delete_browser(self, client: httpx.Client) -> None:
        client.post("/api/v1/browsers/test03")
        self.browser_ids.append("test03")
        response = client.delete("/api/v1/browsers/test03")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deleted"
        self.browser_ids.remove("test03")

    def test_delete_nonexistent_browser(self, client: httpx.Client) -> None:
        response = client.delete("/api/v1/browsers/nonexistent-browser")
        assert response.status_code == 404


@pytest.mark.api
class TestBrowserListing:
    def test_list_browsers(self, client: httpx.Client) -> None:
        response = client.get("/api/v1/browsers")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


@pytest.mark.api
class TestAutoStart:
    browser_ids: list[str] = []

    @pytest.fixture(autouse=True)
    def cleanup(self, client: httpx.Client) -> Generator[None, None, None]:
        self.browser_ids = []
        yield
        for browser_id in self.browser_ids:
            try:
                client.delete(f"/api/v1/browsers/{browser_id}")
            except Exception:
                pass

    def test_cdp_websocket_autostart(self, client: httpx.Client) -> None:
        browser_id = "test-autostart"
        self.browser_ids.append(browser_id)

        # Ensure the container does not already exist
        client.delete(f"/api/v1/browsers/{browser_id}")

        ws_base = REMOTEBROWSER_URL.replace("http://", "ws://").replace("https://", "wss://")

        async def connect_and_verify():
            async with websockets.connect(f"{ws_base}/cdp/{browser_id}", open_timeout=60):
                pass  # successful connection confirms the container was auto-started

        asyncio.run(connect_and_verify())

        response = client.get(f"/api/v1/browsers/{browser_id}")
        assert response.status_code == 200


@pytest.mark.api
class TestListPages:
    browser_ids: list[str] = []

    @pytest.fixture(autouse=True)
    def cleanup(self, client: httpx.Client) -> Generator[None, None, None]:
        self.browser_ids = []
        yield
        for browser_id in self.browser_ids:
            try:
                client.delete(f"/api/v1/browsers/{browser_id}")
            except Exception:
                pass

    def test_list_pages_is_stable(self, client: httpx.Client) -> None:
        browser_id = "test-list-pages"
        self.browser_ids.append(browser_id)

        # Ensure clean state
        client.delete(f"/api/v1/browsers/{browser_id}")
        assert client.post(f"/api/v1/browsers/{browser_id}").status_code == 200

        first: list[object] = client.get(f"/api/v1/browsers/{browser_id}/pages").json()
        second: list[object] = client.get(f"/api/v1/browsers/{browser_id}/pages").json()

        assert isinstance(first, list)
        assert len(first) >= 1
        assert first == second


@pytest.mark.api
class TestPageContent:
    browser_ids: list[str] = []

    @pytest.fixture(autouse=True)
    def cleanup(self, client: httpx.Client) -> Generator[None, None, None]:
        self.browser_ids = []
        yield
        for browser_id in self.browser_ids:
            try:
                client.delete(f"/api/v1/browsers/{browser_id}")
            except Exception:
                pass

    def test_page_html_and_distilled(self, client: httpx.Client) -> None:
        browser_id = "test-page-content"
        self.browser_ids.append(browser_id)

        # Ensure clean state
        client.delete(f"/api/v1/browsers/{browser_id}")
        assert client.post(f"/api/v1/browsers/{browser_id}").status_code == 200

        page_ids: list[object] = client.get(f"/api/v1/browsers/{browser_id}/pages").json()
        assert len(page_ids) >= 1
        page_id = str(page_ids[0])

        html = client.get(f"/api/v1/browsers/{browser_id}/pages/{page_id}/html")
        assert html.status_code == 200
        assert "<html" in html.text.lower()
