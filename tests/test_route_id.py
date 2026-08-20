import pytest

from getgather.browsers.route_id import (
    BrowserRoute,
    make_routed_browser_id,
    parse_routed_browser_id,
)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("fly", "Bfuxvstp6"), ("daytona", "Bduxvstp6"), ("browserbase", "Bbuxvstp6")],
)
def test_routed_browser_id_keeps_short_public_contract(provider: str, expected: str) -> None:
    browser_id = make_routed_browser_id(provider, "Bpuxvstp6")

    assert browser_id == expected
    assert parse_routed_browser_id(browser_id) == BrowserRoute(provider, expected)


def test_non_routed_browser_id_is_left_for_the_fallback_backend() -> None:
    assert parse_routed_browser_id("B9uxvstp6") is None
    assert parse_routed_browser_id("named-browser") is None
