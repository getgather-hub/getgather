"""Route-level tests for the JSON sign-in endpoints.

The remote browser and the distillation loop are stubbed out, so these run without Chrome Fleet.
What they pin down is the wire contract and the error paths — neither of which had any coverage.
"""

from pathlib import Path
from typing import Any

import pytest
import zendriver as zd
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from getgather.mcp import dpage
from getgather.mcp.dpage import (
    DEFAULT_DPAGE_POST_POLL_TIMEOUT,
    DEFAULT_DPAGE_READ_POLL_TIMEOUT,
    LoopOutcome,
)

PATTERNS = Path(__file__).parent.parent / "getgather" / "mcp" / "patterns"
SIGNIN_ID = "browser1--target1--session1"
TIMEOUT_NOT_PASSED = -1


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(dpage.router)
    return TestClient(app)


@pytest.fixture
def stub_page(monkeypatch: MonkeyPatch) -> None:
    """Make sign-in id resolution succeed without a real browser."""

    async def fake_get_remote_browser(browser_id: str) -> object:
        return object()

    def fake_find_browser_tab(browser: object, target_id: str) -> object:
        return object()

    monkeypatch.setattr(dpage, "get_remote_browser", fake_get_remote_browser)
    monkeypatch.setattr(dpage, "find_browser_tab", fake_find_browser_tab)


def stub_loop(monkeypatch: MonkeyPatch, outcome: LoopOutcome) -> list[dict[str, Any]]:
    """Replace the distillation loop, recording how it was called."""
    calls: list[dict[str, Any]] = []

    async def fake_loop(
        page: zd.Tab,
        id: str,
        fields: dict[str, str],
        button: str | None = None,
        patterns: Any = None,
        # Sentinel, so a recorded timeout means "the route passed one explicitly" rather than
        # echoing a default this stub invented.
        timeout: int = TIMEOUT_NOT_PASSED,
    ) -> LoopOutcome:
        calls.append({"id": id, "fields": fields, "button": button, "timeout": timeout})
        return outcome

    monkeypatch.setattr(dpage, "distill_post_loop", fake_loop)
    return calls


def amazon_outcome() -> LoopOutcome:
    document = BeautifulSoup((PATTERNS / "amazon-signin.html").read_text(), "html.parser")
    return LoopOutcome(kind="need_input", title="Amazon Sign In", document=document)


def test_get_json_returns_the_current_step(
    client: TestClient, stub_page: None, monkeypatch: MonkeyPatch
) -> None:
    calls = stub_loop(monkeypatch, amazon_outcome())

    response = client.get(f"/dpage/{SIGNIN_ID}/json")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "NEED_SIGNIN"
    assert body["signin_id"] == SIGNIN_ID
    assert body["title"] == "Amazon Sign In"
    assert [b["type"] for b in body["blocks"]] == ["input", "input", "button"]
    assert body["blocks"][0]["name"] == "email"
    # A GET submits nothing — unlike GET /dpage/{id}, it does real work rather than trampolining.
    assert calls[0]["fields"] == {}
    assert calls[0]["button"] is None


def test_read_polls_briefly_but_submit_long_polls(
    client: TestClient, stub_page: None, monkeypatch: MonkeyPatch
) -> None:
    # A read is idempotent, so it must not occupy a request slot for the full submit window;
    # each in-flight request costs one of the machine's connection slots.
    calls = stub_loop(monkeypatch, amazon_outcome())

    client.get(f"/dpage/{SIGNIN_ID}/json")
    client.post(f"/dpage/{SIGNIN_ID}/json", json={"values": {"email": "a@b.c"}})

    # The read asks for the short window explicitly...
    assert calls[0]["timeout"] == DEFAULT_DPAGE_READ_POLL_TIMEOUT
    # ...while the submit passes nothing and so inherits the loop's own long default.
    assert calls[1]["timeout"] == TIMEOUT_NOT_PASSED
    assert DEFAULT_DPAGE_READ_POLL_TIMEOUT < DEFAULT_DPAGE_POST_POLL_TIMEOUT


def test_post_json_forwards_values_and_button_separately(
    client: TestClient, stub_page: None, monkeypatch: MonkeyPatch
) -> None:
    calls = stub_loop(monkeypatch, LoopOutcome(kind="finished", title="Done"))

    response = client.post(
        f"/dpage/{SIGNIN_ID}/json",
        json={"values": {"email": "a@b.c", "password": "hunter2"}, "button": "sms"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "SUCCESS"
    assert calls[0]["fields"] == {"email": "a@b.c", "password": "hunter2"}
    assert calls[0]["button"] == "sms"


def test_a_site_field_named_button_is_not_treated_as_a_choice(
    client: TestClient, stub_page: None, monkeypatch: MonkeyPatch
) -> None:
    calls = stub_loop(monkeypatch, LoopOutcome(kind="finished", title="Done"))

    client.post(f"/dpage/{SIGNIN_ID}/json", json={"values": {"button": "literal-field"}})

    # This is the collision the namespaced body exists to prevent.
    assert calls[0]["fields"] == {"button": "literal-field"}
    assert calls[0]["button"] is None


def test_post_json_accepts_an_empty_body(
    client: TestClient, stub_page: None, monkeypatch: MonkeyPatch
) -> None:
    calls = stub_loop(monkeypatch, amazon_outcome())

    response = client.post(f"/dpage/{SIGNIN_ID}/json")

    assert response.status_code == 200
    assert calls[0]["fields"] == {}


def test_error_pattern_reports_error_status_and_code(
    client: TestClient, stub_page: None, monkeypatch: MonkeyPatch
) -> None:
    stub_loop(
        monkeypatch,
        LoopOutcome(kind="finished", title="Closed", error_code="reset_password"),
    )

    body = client.get(f"/dpage/{SIGNIN_ID}/json").json()

    assert body["status"] == "ERROR"
    assert body["error_code"] == "reset_password"


def test_timeout_is_200_with_a_status_not_503(
    client: TestClient, stub_page: None, monkeypatch: MonkeyPatch
) -> None:
    stub_loop(monkeypatch, LoopOutcome(kind="timeout"))

    response = client.get(f"/dpage/{SIGNIN_ID}/json")

    # A JSON client cannot tell our 503 apart from a proxy killing a long-poll.
    assert response.status_code == 200
    assert response.json()["status"] == "TIMEOUT"


def test_invalid_signin_id_is_400(client: TestClient) -> None:
    response = client.get("/dpage/nodelimiter/json")
    assert response.status_code == 400


def test_missing_browser_is_404(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    async def no_browser(browser_id: str) -> None:
        return None

    monkeypatch.setattr(dpage, "get_remote_browser", no_browser)

    response = client.get(f"/dpage/{SIGNIN_ID}/json")
    assert response.status_code == 404
    assert response.json()["detail"] == "Remote browser not found"


def test_missing_tab_is_404(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    async def a_browser(browser_id: str) -> object:
        return object()

    def no_tab(browser: object, target_id: str) -> None:
        return None

    monkeypatch.setattr(dpage, "get_remote_browser", a_browser)
    monkeypatch.setattr(dpage, "find_browser_tab", no_tab)

    response = client.get(f"/dpage/{SIGNIN_ID}/json")
    assert response.status_code == 404
    assert response.json()["detail"] == "Page not found"


def test_json_route_does_not_shadow_the_html_route(client: TestClient) -> None:
    # GET /dpage/{id} still serves the auto-submit trampoline, untouched.
    response = client.get(f"/dpage/{SIGNIN_ID}")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert f'action="/dpage/{SIGNIN_ID}" method="post"' in response.text
